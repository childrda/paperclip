"""/cases — case management API.

A case is the top-level grouping the UI revolves around. Read endpoints
are open to any authenticated caller; case-creation arrives via the
import upload (see :mod:`foia.api.routes.imports`); status updates
(archive, mark exported) are explicit verbs here.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ... import audit, cases as cases_mod
from ...district import load_district_config
from ...redaction import propose_from_detections
from ..deps import CallerIdentity, get_caller, get_db, require_user

router = APIRouter(prefix="/cases", tags=["cases"])


class CaseOut(BaseModel):
    id: int
    name: str
    bates_prefix: str
    status: str
    created_by: int | None
    created_at: str
    updated_at: str
    error_message: str | None
    failed_stage: str | None


class CaseDetail(BaseModel):
    case: CaseOut
    stats: dict
    latest_job: dict | None = None


class CaseStatusPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(..., description="processing | ready | failed | exported | archived")


def _to_out(c: cases_mod.Case) -> CaseOut:
    return CaseOut(
        id=c.id, name=c.name, bates_prefix=c.bates_prefix, status=c.status,
        created_by=c.created_by, created_at=c.created_at,
        updated_at=c.updated_at, error_message=c.error_message,
        failed_stage=c.failed_stage,
    )


@router.get("", summary="List cases (newest first).")
def list_cases_endpoint(
    conn: sqlite3.Connection = Depends(get_db),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    items, total = cases_mod.list_cases(
        conn, status=status, limit=limit, offset=offset,
    )
    return {
        "items": [_to_out(c).model_dump() for c in items],
        "total": total, "limit": limit, "offset": offset,
    }


@router.get("/{case_id}", response_model=CaseDetail, summary="Detail view.")
def get_case_endpoint(
    case_id: int, conn: sqlite3.Connection = Depends(get_db),
):
    try:
        c = cases_mod.get_case(conn, case_id)
    except cases_mod.CaseError:
        raise HTTPException(404, f"case {case_id} not found")
    stats = cases_mod.case_stats(conn, case_id)
    jobs = cases_mod.list_jobs(conn, case_id=case_id, limit=1)
    return CaseDetail(
        case=_to_out(c), stats=stats,
        latest_job=jobs[0] if jobs else None,
    )


@router.post(
    "/{case_id}/propose-redactions",
    summary=(
        "Run the auto-proposer over every PII detection in this case "
        "and emit ``status='proposed'`` redactions."
    ),
)
def propose_case_redactions(
    case_id: int,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    caller: CallerIdentity = Depends(require_user),
):
    """Recovery / on-demand path for the propose stage.

    The import pipeline already runs this when ``propose_redactions`` is
    true at upload time, but operators who imported with the flag off
    (or whose first run skipped everything because their district YAML
    didn't yet map the detected entity types to exemption codes) need
    a way to trigger it later. This endpoint is also idempotent on the
    underlying unique index, so running it twice does no harm.
    """
    try:
        cases_mod.get_case(conn, case_id)
    except cases_mod.CaseError:
        raise HTTPException(404, f"case {case_id} not found")

    cached = getattr(request.app.state, "district_config", None)
    if cached is None:
        cached = load_district_config()
        request.app.state.district_config = cached

    stats = propose_from_detections(conn, cached, only_case_id=case_id)
    conn.commit()
    audit.log_event(
        conn,
        event_type="case.redactions_proposed",
        actor=caller.actor, user_id=caller.user_id, origin="api",
        source_type="case", source_id=case_id,
        payload=stats.as_dict(),
    )
    conn.commit()
    return stats.as_dict()


@router.patch(
    "/{case_id}/status",
    response_model=CaseOut,
    summary="Move a case to a different status (archive, mark exported, ...).",
)
def patch_case_status(
    case_id: int,
    payload: CaseStatusPatch,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    caller: CallerIdentity = Depends(require_user),
):
    try:
        c = cases_mod.update_case_status(conn, case_id, status=payload.status)
    except cases_mod.CaseError as e:
        raise HTTPException(400, str(e))
    audit.log_event(
        conn,
        event_type="case.status_changed",
        actor=caller.actor, user_id=caller.user_id, origin="api",
        source_type="case", source_id=case_id,
        payload={"new_status": payload.status},
    )
    _ = request
    return _to_out(c)
