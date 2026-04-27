"""/imports — UI-driven full-pipeline ingestion.

A reviewer drops a `.mbox` file into the browser; the server runs every
stage of the pipeline (ingest → extract → detect → resolve → propose)
synchronously and returns a single summary. The CLI tools still exist
for power users; this endpoint is what the UI uses.

Synchronous on purpose: most district FOIA exports are small enough
that a 5–60 second request feels acceptable, and the UX is far simpler
than juggling job IDs / polling. If a district needs to ingest GB-sized
mailboxes, this endpoint would be replaced by a background-task setup.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from ... import audit
from ...detection import PiiDetector
from ...detection_driver import run_detection
from ...district import DistrictConfig, load_district_config
from ...er_driver import run_resolution
from ...extraction import ExtractionOptions
from ...ingestion import ingest_mbox
from ...processing import process_attachments
from ...redaction import propose_from_detections
from ..deps import get_actor, get_db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/imports", tags=["imports"])


class ImportSummary(BaseModel):
    import_id: str
    filename: str
    saved_path: str
    label: str | None
    started_at: str
    finished_at: str
    stages: dict


def _district(request: Request) -> DistrictConfig:
    cached = getattr(request.app.state, "district_config", None)
    if cached is not None:
        return cached
    cfg = load_district_config()
    request.app.state.district_config = cfg
    return cfg


def _inbox_dir(request: Request) -> Path:
    cfg = request.app.state.config
    inbox: Path = cfg.inbox_dir or Path("./data/inbox").resolve()
    inbox.mkdir(parents=True, exist_ok=True)
    return inbox


def _attachment_dir(request: Request) -> Path:
    cfg = request.app.state.config
    return cfg.attachment_dir


@router.post(
    "",
    response_model=ImportSummary,
    summary="Upload a .mbox and run the full ingest pipeline",
)
def create_import(
    request: Request,
    file: UploadFile = File(..., description="The .mbox file to ingest."),
    label: str | None = Form(default=None),
    propose_redactions: bool = Form(default=True),
    conn: sqlite3.Connection = Depends(get_db),
    actor: str = Depends(get_actor),
) -> ImportSummary:
    if not file.filename:
        raise HTTPException(400, "missing filename")
    # Lenient extension check; accept any non-empty mbox-shaped upload.
    safe_name = Path(file.filename).name  # strip any path components
    if not safe_name:
        raise HTTPException(400, "invalid filename")

    cfg = request.app.state.config
    inbox = _inbox_dir(request)
    import_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-")
        + uuid.uuid4().hex[:8]
    )
    saved_path = inbox / f"{import_id}__{safe_name}"

    # Persist the upload before doing anything else, so a crash mid-stage
    # leaves a recoverable file on disk.
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        with saved_path.open("wb") as fh:
            shutil.copyfileobj(file.file, fh)
    finally:
        file.file.close()

    if saved_path.stat().st_size == 0:
        saved_path.unlink(missing_ok=True)
        raise HTTPException(400, "uploaded file is empty")

    district = _district(request)
    stages: dict = {}

    # ---- 1. Ingest
    try:
        ingest_stats = ingest_mbox(
            saved_path, conn, _attachment_dir(request),
            source_label=label or saved_path.name,
        )
    except Exception as e:
        log.exception("ingest stage failed")
        raise HTTPException(500, f"ingest failed: {e}")
    stages["ingest"] = ingest_stats.as_dict()

    # ---- 2. Extract
    extract_opts = ExtractionOptions(
        ocr_enabled=cfg.ocr_enabled,
        ocr_language=cfg.ocr_language,
        ocr_dpi=cfg.ocr_dpi,
        tesseract_cmd=cfg.tesseract_cmd,
        office_enabled=cfg.office_enabled,
        libreoffice_cmd=cfg.libreoffice_cmd,
        timeout_s=cfg.extraction_timeout_s,
    )
    try:
        extract_stats = process_attachments(conn, options=extract_opts)
    except Exception as e:
        log.exception("extract stage failed")
        raise HTTPException(500, f"extract failed: {e}")
    stages["extract"] = extract_stats.as_dict()

    # ---- 3. Detect
    try:
        detector = PiiDetector(district.pii)
        detect_stats = run_detection(conn, detector)
    except Exception as e:
        log.exception("detection stage failed")
        raise HTTPException(500, f"detection failed: {e}")
    stages["detect"] = detect_stats.as_dict()

    # ---- 4. Resolve
    try:
        resolve_stats = run_resolution(
            conn, internal_domains=district.email_domains,
        )
    except Exception as e:
        log.exception("resolve stage failed")
        raise HTTPException(500, f"resolve failed: {e}")
    stages["resolve"] = resolve_stats.as_dict()

    # ---- 5. Propose redactions (optional, on by default)
    if propose_redactions:
        try:
            propose_stats = propose_from_detections(conn, district)
        except Exception as e:
            log.exception("propose stage failed")
            raise HTTPException(500, f"propose failed: {e}")
        stages["propose"] = propose_stats.as_dict()
    else:
        stages["propose"] = {"skipped": True}

    finished_at = datetime.now(timezone.utc).isoformat()

    audit.log_event(
        conn,
        event_type="import.run",
        actor=actor,
        origin="api",
        source_type="mbox",
        payload={
            "import_id": import_id,
            "filename": safe_name,
            "saved_path": str(saved_path),
            "label": label,
            "propose_redactions": propose_redactions,
            "stages": stages,
        },
    )

    return ImportSummary(
        import_id=import_id,
        filename=safe_name,
        saved_path=str(saved_path),
        label=label,
        started_at=started_at,
        finished_at=finished_at,
        stages=stages,
    )


@router.get(
    "",
    summary="List previous imports (derived from the audit log).",
)
def list_imports(
    conn: sqlite3.Connection = Depends(get_db),
    limit: int = 50,
):
    rows, _ = audit.query_events(
        conn, event_type="import.run", limit=limit,
    )
    return [
        {
            "import_id": (r.get("payload") or {}).get("import_id"),
            "filename": (r.get("payload") or {}).get("filename"),
            "label": (r.get("payload") or {}).get("label"),
            "actor": r["actor"],
            "event_at": r["event_at"],
            "stages": (r.get("payload") or {}).get("stages", {}),
        }
        for r in rows
    ]
