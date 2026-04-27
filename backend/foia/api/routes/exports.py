"""/exports — Phase 8 redacted-PDF export endpoint.

Single endpoint by design: POST runs the export synchronously into a
unique subdirectory under ``FOIA_EXPORT_DIR`` and returns a small
manifest with paths the client can hit via GET. We don't stream the
PDF directly because the CSV ships alongside it and the UI needs both.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from ... import audit
from ...district import DistrictConfig, load_district_config
from ...export import ExportConfig, run_export
from ..deps import get_actor, get_db

router = APIRouter(prefix="/exports", tags=["exports"])


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email_ids: list[int] | None = Field(
        None, description="Restrict the export to these email ids."
    )
    include_attachments: bool = True


class ExportManifest(BaseModel):
    export_id: str
    pdf_url: str
    csv_url: str
    emails_exported: int
    attachments_exported: int
    pages_written: int
    redactions_burned: int
    bates_first: str | None
    bates_last: str | None
    created_at: str


def _district(request: Request) -> DistrictConfig:
    cached = getattr(request.app.state, "district_config", None)
    if cached is not None:
        return cached
    cfg = load_district_config()
    request.app.state.district_config = cfg
    return cfg


def _export_root(request: Request) -> Path:
    cfg = request.app.state.config
    root: Path = cfg.export_dir or Path("./data/exports").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_subpath(root: Path, candidate: Path) -> Path:
    """Resolve ``candidate`` and ensure it lives inside ``root``."""
    resolved = candidate.resolve()
    root_resolved = root.resolve()
    if root_resolved not in resolved.parents and resolved != root_resolved:
        raise HTTPException(404, "file not found")
    return resolved


@router.post(
    "",
    response_model=ExportManifest,
    summary="Generate a redacted PDF + CSV log for the configured scope",
)
def create_export(
    payload: ExportRequest,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    actor: str = Depends(get_actor),
) -> ExportManifest:
    root = _export_root(request)
    export_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    out_dir = root / export_id
    cfg = ExportConfig(output_dir=out_dir)

    stats = run_export(
        conn,
        _district(request),
        cfg,
        only_email_ids=payload.email_ids,
        include_attachments=payload.include_attachments,
    )
    audit.log_event(
        conn,
        event_type=audit.EVT_EXPORT_RUN,
        actor=actor,
        origin="api",
        source_type="export",
        payload={
            "export_id": export_id,
            "output_dir": str(out_dir),
            "email_ids": payload.email_ids,
            "include_attachments": payload.include_attachments,
            "emails_exported": stats.emails_exported,
            "attachments_exported": stats.attachments_exported,
            "pages_written": stats.pages_written,
            "redactions_burned": stats.redactions_burned,
            "bates_first": stats.bates_first,
            "bates_last": stats.bates_last,
        },
    )

    return ExportManifest(
        export_id=export_id,
        pdf_url=f"/api/v1/exports/{export_id}/production.pdf",
        csv_url=f"/api/v1/exports/{export_id}/redaction_log.csv",
        emails_exported=stats.emails_exported,
        attachments_exported=stats.attachments_exported,
        pages_written=stats.pages_written,
        redactions_burned=stats.redactions_burned,
        bates_first=stats.bates_first,
        bates_last=stats.bates_last,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


@router.get(
    "/{export_id}/{filename}",
    summary="Download a previously generated export file",
)
def download_export_file(
    export_id: str,
    filename: str,
    request: Request,
) -> FileResponse:
    if filename not in ("production.pdf", "redaction_log.csv"):
        raise HTTPException(404, "unknown file")
    root = _export_root(request)
    target = _safe_subpath(root, root / export_id / filename)
    if not target.exists():
        raise HTTPException(404, f"export {export_id}/{filename} not found")
    media_type = (
        "application/pdf" if filename.endswith(".pdf") else "text/csv"
    )
    return FileResponse(target, media_type=media_type, filename=filename)


@router.get(
    "",
    summary="List previously generated exports (newest first)",
)
def list_exports(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
) -> list[dict]:
    root = _export_root(request)
    if not root.exists():
        return []
    entries = []
    for child in sorted(root.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        pdf = child / "production.pdf"
        csv = child / "redaction_log.csv"
        if not pdf.exists():
            continue
        entries.append({
            "export_id": child.name,
            "pdf_url": f"/api/v1/exports/{child.name}/production.pdf",
            "csv_url": f"/api/v1/exports/{child.name}/redaction_log.csv",
            "pdf_bytes": pdf.stat().st_size,
            "csv_bytes": csv.stat().st_size if csv.exists() else 0,
            "created_at": datetime.fromtimestamp(
                child.stat().st_mtime, tz=timezone.utc,
            ).isoformat(),
        })
        if len(entries) >= limit:
            break
    return entries
