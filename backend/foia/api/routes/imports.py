"""/imports — UI-driven full-pipeline ingestion.

A reviewer drops a `.mbox` file into the browser; the server creates a
new ``cases`` row, queues a background job that runs ingest → extract
→ detect → resolve → propose, and returns the job ID immediately. The
UI subscribes to /imports/{job_id}/events (SSE) to watch progress in
real time.

Authentication is required. Imports cannot be initiated by anonymous
or X-FOIA-Reviewer-only callers.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ... import audit, cases as cases_mod
from ...config import Config
from ...db import connect, init_schema
from ...detection import PiiDetector
from ...detection_driver import run_detection
from ...district import DistrictConfig, load_district_config
from ...er_driver import run_resolution
from ...extraction import ExtractionOptions
from ...ingestion import ingest_mbox
from ...processing import process_attachments
from ...redaction import propose_from_detections
from ..deps import CallerIdentity, get_caller, get_db, require_user
from fastapi import UploadFile

log = logging.getLogger(__name__)

router = APIRouter(prefix="/imports", tags=["imports"])


class ImportSubmitted(BaseModel):
    job_id: int
    case_id: int
    case_name: str
    bates_prefix: str
    filename: str
    saved_path: str
    label: str | None
    status: str
    submitted_at: str


def _district(request: Request) -> DistrictConfig:
    cached = getattr(request.app.state, "district_config", None)
    if cached is not None:
        return cached
    cfg = load_district_config()
    request.app.state.district_config = cfg
    return cfg


def _inbox_dir(cfg: Config) -> Path:
    inbox: Path = cfg.inbox_dir or Path("./data/inbox").resolve()
    inbox.mkdir(parents=True, exist_ok=True)
    return inbox


# ---------------------------------------------------------------------------
# Background pipeline runner
# ---------------------------------------------------------------------------


_STAGES = ("ingest", "extract", "detect", "resolve", "propose")


def _run_pipeline_job(
    cfg: Config,
    district: DistrictConfig,
    *,
    job_id: int,
    case_id: int,
    saved_path: Path,
    label: str | None,
    propose_redactions: bool,
    actor: str,
    user_id: int | None,
) -> None:
    """Run the whole pipeline against a fresh DB connection (own thread).

    Each stage emits a 'started' event, then 'finished' (or 'failed')
    with the stats payload. The case status flips to 'ready' on success
    or 'failed' on the first stage that raises.
    """
    conn = connect(cfg.db_path)
    try:
        init_schema(conn)
        _emit = lambda **kw: cases_mod.emit_event(conn, job_id, **kw)
        cases_mod.update_job_status(
            conn, job_id, status="running",
            current_stage="ingest",
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        # ---- Stage 1: ingest
        try:
            _emit(stage="ingest", kind="started", message="Reading mailbox…")
            stats = ingest_mbox(
                saved_path, conn, cfg.attachment_dir,
                source_label=label or saved_path.name,
            )
            # Tag the freshly-inserted emails to this case.
            conn.execute(
                "UPDATE emails SET case_id = ? "
                "WHERE case_id IS NULL AND mbox_source = ?",
                (case_id, label or saved_path.name),
            )
            conn.commit()
            _emit(
                stage="ingest", kind="finished",
                message=(
                    f"{stats.emails_ingested} email(s), "
                    f"{stats.attachments_saved} attachment(s)"
                ),
                payload=stats.as_dict(),
            )
        except Exception as e:
            log.exception("ingest stage failed for job %s", job_id)
            _fail_job(conn, job_id, case_id, "ingest", str(e))
            return

        # ---- Stage 2: extract
        try:
            cases_mod.update_job_status(conn, job_id, status="running",
                                        current_stage="extract")
            _emit(stage="extract", kind="started",
                  message="Extracting attachment text…")
            extract_opts = ExtractionOptions(
                ocr_enabled=cfg.ocr_enabled,
                ocr_language=cfg.ocr_language,
                ocr_dpi=cfg.ocr_dpi,
                tesseract_cmd=cfg.tesseract_cmd,
                office_enabled=cfg.office_enabled,
                libreoffice_cmd=cfg.libreoffice_cmd,
                timeout_s=cfg.extraction_timeout_s,
            )
            stats = process_attachments(conn, options=extract_opts)
            _emit(
                stage="extract", kind="finished",
                message=(
                    f"{stats.extracted_ok} ok, "
                    f"{stats.failed} failed, "
                    f"{stats.unsupported} skipped"
                ),
                payload=stats.as_dict(),
            )
        except Exception as e:
            log.exception("extract stage failed for job %s", job_id)
            _fail_job(conn, job_id, case_id, "extract", str(e))
            return

        # ---- Stage 3: detect
        try:
            cases_mod.update_job_status(conn, job_id, status="running",
                                        current_stage="detect")
            _emit(stage="detect", kind="started",
                  message="Scanning for PII…")
            detector = PiiDetector(district.pii)
            stats = run_detection(conn, detector)
            _emit(
                stage="detect", kind="finished",
                message=f"{stats.detections_written} PII span(s)",
                payload=stats.as_dict(),
            )
        except Exception as e:
            log.exception("detect stage failed for job %s", job_id)
            _fail_job(conn, job_id, case_id, "detect", str(e))
            return

        # ---- Stage 4: resolve
        try:
            cases_mod.update_job_status(conn, job_id, status="running",
                                        current_stage="resolve")
            _emit(stage="resolve", kind="started",
                  message="Building person index…")
            stats = run_resolution(
                conn, internal_domains=district.email_domains,
            )
            _emit(
                stage="resolve", kind="finished",
                message=f"{stats.persons_created} person(s)",
                payload=stats.as_dict(),
            )
        except Exception as e:
            log.exception("resolve stage failed for job %s", job_id)
            _fail_job(conn, job_id, case_id, "resolve", str(e))
            return

        # ---- Stage 5: propose redactions
        try:
            cases_mod.update_job_status(conn, job_id, status="running",
                                        current_stage="propose")
            if propose_redactions:
                _emit(stage="propose", kind="started",
                      message="Auto-proposing redactions…")
                stats = propose_from_detections(conn, district)
                _emit(
                    stage="propose", kind="finished",
                    message=f"{stats.proposed} redaction(s) proposed",
                    payload=stats.as_dict(),
                )
            else:
                _emit(stage="propose", kind="finished",
                      message="Skipped per request",
                      payload={"skipped": True})
        except Exception as e:
            log.exception("propose stage failed for job %s", job_id)
            _fail_job(conn, job_id, case_id, "propose", str(e))
            return

        # Success.
        cases_mod.update_job_status(
            conn, job_id, status="succeeded",
            current_stage=None,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        cases_mod.update_case_status(conn, case_id, status="ready")
        audit.log_event(
            conn,
            event_type="import.run",
            actor=actor, user_id=user_id, origin="api",
            source_type="case", source_id=case_id,
            payload={"job_id": job_id, "case_id": case_id},
        )
        _emit(
            stage="done", kind="finished",
            message="All stages complete.",
            payload={"case_id": case_id},
        )
    finally:
        conn.close()


def _fail_job(
    conn: sqlite3.Connection,
    job_id: int,
    case_id: int,
    stage: str,
    error: str,
) -> None:
    cases_mod.emit_event(
        conn, job_id, stage=stage, kind="failed",
        message=error[:400], payload={"error": error},
    )
    cases_mod.update_job_status(
        conn, job_id, status="failed",
        failed_stage=stage, error_message=error,
        finished_at=datetime.now(timezone.utc).isoformat(),
    )
    cases_mod.update_case_status(
        conn, case_id, status="failed",
        failed_stage=stage, error_message=error,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=ImportSubmitted,
    summary="Upload a .mbox; create a case + queue the pipeline as a job.",
)
def create_import(
    request: Request,
    file: UploadFile = File(...),
    name: str | None = Form(default=None),
    bates_prefix: str | None = Form(default=None),
    label: str | None = Form(default=None),
    propose_redactions: bool = Form(default=True),
    conn: sqlite3.Connection = Depends(get_db),
    caller: CallerIdentity = Depends(require_user),
) -> ImportSubmitted:
    cfg: Config = request.app.state.config
    district = _district(request)

    if not file.filename:
        raise HTTPException(400, "missing filename")
    safe_name = Path(file.filename).name
    if not safe_name:
        raise HTTPException(400, "invalid filename")

    case_name = (name or "").strip() or f"Case from {safe_name}"
    prefix = (bates_prefix or "").strip() or district.bates.prefix

    case = cases_mod.create_case(
        conn,
        name=case_name,
        bates_prefix=prefix,
        created_by_user_id=caller.user_id,
        status="processing",
    )

    inbox = _inbox_dir(cfg)
    saved_path = inbox / f"case-{case.id}__{safe_name}"
    try:
        with saved_path.open("wb") as fh:
            shutil.copyfileobj(file.file, fh)
    finally:
        file.file.close()

    if saved_path.stat().st_size == 0:
        saved_path.unlink(missing_ok=True)
        cases_mod.update_case_status(
            conn, case.id, status="failed",
            failed_stage="upload", error_message="empty file",
        )
        raise HTTPException(400, "uploaded file is empty")

    job_id = cases_mod.create_job(
        conn,
        case_id=case.id,
        started_by_user_id=caller.user_id,
        upload_path=str(saved_path),
        label=label or case_name,
        propose_redactions=propose_redactions,
    )

    audit.log_event(
        conn,
        event_type="import.submitted",
        actor=caller.actor, user_id=caller.user_id, origin="api",
        source_type="case", source_id=case.id,
        payload={
            "job_id": job_id,
            "case_id": case.id,
            "filename": safe_name,
            "case_name": case_name,
            "bates_prefix": prefix,
        },
    )

    # Spawn the pipeline thread. We use a plain thread (not BackgroundTasks)
    # because BackgroundTasks would block the response until the job ends.
    thread = threading.Thread(
        target=_run_pipeline_job,
        kwargs=dict(
            cfg=cfg,
            district=district,
            job_id=job_id,
            case_id=case.id,
            saved_path=saved_path,
            label=label or case_name,
            propose_redactions=propose_redactions,
            actor=caller.actor,
            user_id=caller.user_id,
        ),
        daemon=True,
        name=f"pipeline-job-{job_id}",
    )
    thread.start()

    return ImportSubmitted(
        job_id=job_id,
        case_id=case.id,
        case_name=case_name,
        bates_prefix=prefix,
        filename=safe_name,
        saved_path=str(saved_path),
        label=label,
        status="queued",
        submitted_at=datetime.now(timezone.utc).isoformat(),
    )


@router.get(
    "/{job_id}",
    summary="Snapshot of a pipeline job (status + all events to date).",
)
def get_import(
    job_id: int, conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    job = cases_mod.get_job(conn, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    events = cases_mod.list_events(conn, job_id)
    return {"job": job, "events": events}


@router.get(
    "/{job_id}/events",
    summary="Server-Sent Events stream of pipeline progress for this job.",
)
def stream_events(
    job_id: int,
    request: Request,
):
    """Stream pipeline events via SSE.

    Each row in ``pipeline_events`` becomes one ``data:`` line. Closes
    when the job reaches a terminal state (``succeeded`` / ``failed``).
    """
    cfg: Config = request.app.state.config

    def event_stream():
        # Use a fresh connection — Starlette streams responses on the
        # event loop thread, but our writer is on a worker thread. The
        # SQLite connection from the get_db dep can't be shared across
        # threads. ``check_same_thread=False`` is fine: we're read-only
        # here.
        conn = connect(cfg.db_path)
        try:
            init_schema(conn)
            last_id = 0
            terminal = {"succeeded", "failed", "cancelled"}
            sleep_s = 0.5
            # Up to 10 minutes of idle wait — generous; cancel by
            # disconnecting the client.
            deadline = time.time() + 600
            yield "retry: 2000\n\n"
            while True:
                events = cases_mod.list_events(conn, job_id, since_id=last_id)
                for e in events:
                    last_id = max(last_id, e["id"])
                    payload = {
                        "id": e["id"],
                        "stage": e["stage"],
                        "kind": e["kind"],
                        "message": e["message"],
                        "payload": e.get("payload"),
                        "event_at": e["event_at"],
                    }
                    yield f"event: {e['kind']}\n"
                    yield f"data: {json.dumps(payload)}\n\n"
                job = cases_mod.get_job(conn, job_id)
                if job is None:
                    yield "event: error\ndata: {\"error\": \"job not found\"}\n\n"
                    return
                if job["status"] in terminal:
                    yield (
                        "event: done\n"
                        f"data: {json.dumps({'status': job['status']})}\n\n"
                    )
                    return
                if time.time() > deadline:
                    yield (
                        "event: timeout\n"
                        "data: {\"reason\": \"client idle deadline\"}\n\n"
                    )
                    return
                time.sleep(sleep_s)
        finally:
            conn.close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/{job_id}/retry",
    summary="Retry a failed job from the failing stage onwards.",
)
def retry_import(
    job_id: int,
    request: Request,
    background: BackgroundTasks,
    conn: sqlite3.Connection = Depends(get_db),
    caller: CallerIdentity = Depends(require_user),
):
    job = cases_mod.get_job(conn, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if job["status"] != "failed":
        raise HTTPException(400, f"job is not in failed state: {job['status']}")

    cfg: Config = request.app.state.config
    district = _district(request)
    saved_path = Path(job["upload_path"])
    if not saved_path.exists():
        raise HTTPException(
            400,
            "original upload is no longer on disk; please re-upload the mailbox",
        )

    # Reset the job state so the SSE stream picks up new events.
    cases_mod.update_job_status(
        conn, job_id, status="queued",
        current_stage=None, error_message=None, failed_stage=None,
        finished_at=None, started_at=None,
    )
    cases_mod.update_case_status(
        conn, int(job["case_id"]), status="processing",
    )
    cases_mod.emit_event(
        conn, job_id, stage="job", kind="started",
        message="Retry requested by reviewer.",
    )
    audit.log_event(
        conn,
        event_type="import.retried",
        actor=caller.actor, user_id=caller.user_id, origin="api",
        source_type="case", source_id=int(job["case_id"]),
        payload={"job_id": job_id},
    )

    threading.Thread(
        target=_run_pipeline_job,
        kwargs=dict(
            cfg=cfg, district=district,
            job_id=job_id,
            case_id=int(job["case_id"]),
            saved_path=saved_path,
            label=job["label"],
            propose_redactions=bool(job["propose_redactions"]),
            actor=caller.actor,
            user_id=caller.user_id,
        ),
        daemon=True,
        name=f"pipeline-job-retry-{job_id}",
    ).start()
    _ = background  # parameter retained for FastAPI signature stability
    return {"job_id": job_id, "status": "queued"}


@router.get(
    "",
    summary="List recent imports (newest first).",
)
def list_imports(
    conn: sqlite3.Connection = Depends(get_db),
    limit: int = 50,
):
    return cases_mod.list_jobs(conn, limit=limit)
