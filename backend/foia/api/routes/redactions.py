"""/redactions — CRUD over the Phase 6 redactions table.

Phase 5's API was read-only. This is the first router that mutates state,
matching the spec's "CRUD API" deliverable. Validation lives in
:mod:`foia.redaction` so the CLI and HTTP layer share one source of
truth for the rules.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from ... import audit
from ...district import DistrictConfig, load_district_config
from ...redaction import (
    RedactionError,
    create_redaction,
    delete_redaction,
    get_redaction,
    list_redactions,
    update_redaction,
)
from ..deps import CallerIdentity, Pagination, get_caller, get_db, pagination
from ..schemas import (
    ExemptionCodeOut,
    Page,
    RedactionCreate,
    RedactionOut,
    RedactionPatch,
)

router = APIRouter(tags=["redactions"])


def _district(request: Request) -> DistrictConfig:
    cached = getattr(request.app.state, "district_config", None)
    if cached is not None:
        return cached
    cfg = load_district_config()
    request.app.state.district_config = cfg
    return cfg


def _row_to_out(row: dict) -> RedactionOut:
    return RedactionOut(
        id=row["id"],
        source_type=row["source_type"],
        source_id=row["source_id"],
        start_offset=row["start_offset"],
        end_offset=row["end_offset"],
        exemption_code=row["exemption_code"],
        reviewer_id=row["reviewer_id"],
        status=row["status"],
        origin=row["origin"],
        source_detection_id=row["source_detection_id"],
        notes=row["notes"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.get(
    "/redactions",
    response_model=Page[RedactionOut],
    summary="List redactions",
)
def list_redactions_endpoint(
    conn: sqlite3.Connection = Depends(get_db),
    page: Pagination = Depends(pagination),
    source_type: Optional[str] = Query(None),
    source_id: Optional[int] = Query(None),
    redaction_status: Optional[str] = Query(
        None, alias="status",
        description="Filter by 'proposed' | 'accepted' | 'rejected'.",
    ),
    origin: Optional[str] = Query(None, description="'auto' or 'manual'."),
    exemption_code: Optional[str] = Query(None),
):
    items, total = list_redactions(
        conn,
        source_type=source_type,
        source_id=source_id,
        status=redaction_status,
        origin=origin,
        exemption_code=exemption_code,
        limit=page.limit,
        offset=page.offset,
    )
    return Page[RedactionOut](
        items=[_row_to_out(r) for r in items],
        total=total, limit=page.limit, offset=page.offset,
    )


@router.get(
    "/redactions/{redaction_id}",
    response_model=RedactionOut,
    summary="Get a single redaction",
)
def get_redaction_endpoint(
    redaction_id: int,
    conn: sqlite3.Connection = Depends(get_db),
):
    try:
        return _row_to_out(get_redaction(conn, redaction_id))
    except RedactionError as e:
        raise HTTPException(404, str(e))


@router.post(
    "/redactions",
    response_model=RedactionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new redaction (proposed | accepted | rejected)",
)
def create_redaction_endpoint(
    payload: RedactionCreate,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    caller: CallerIdentity = Depends(get_caller),
):
    try:
        row = create_redaction(
            conn,
            _district(request),
            source_type=payload.source_type,
            source_id=payload.source_id,
            start_offset=payload.start_offset,
            end_offset=payload.end_offset,
            exemption_code=payload.exemption_code,
            status=payload.status,
            origin="manual",
            reviewer_id=payload.reviewer_id,
            notes=payload.notes,
        )
    except RedactionError as e:
        raise HTTPException(400, str(e))
    audit.log_event(
        conn,
        event_type=audit.EVT_REDACTION_CREATE,
        actor=caller.actor,
        user_id=caller.user_id,
        origin="api",
        source_type="redaction",
        source_id=int(row["id"]),
        payload={
            "source_type": payload.source_type,
            "source_id": payload.source_id,
            "start_offset": payload.start_offset,
            "end_offset": payload.end_offset,
            "exemption_code": payload.exemption_code,
            "status": payload.status,
        },
    )
    return _row_to_out(row)


@router.patch(
    "/redactions/{redaction_id}",
    response_model=RedactionOut,
    summary="Update a redaction (status, exemption_code, reviewer_id, notes)",
)
def patch_redaction_endpoint(
    redaction_id: int,
    payload: RedactionPatch,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    caller: CallerIdentity = Depends(get_caller),
):
    try:
        row = update_redaction(
            conn,
            _district(request),
            redaction_id,
            status=payload.status,
            exemption_code=payload.exemption_code,
            reviewer_id=payload.reviewer_id,
            notes=payload.notes,
        )
    except RedactionError as e:
        msg = str(e).lower()
        if "not found" in msg:
            raise HTTPException(404, str(e))
        raise HTTPException(400, str(e))
    audit.log_event(
        conn,
        event_type=audit.EVT_REDACTION_UPDATE,
        actor=caller.actor,
        user_id=caller.user_id,
        origin="api",
        source_type="redaction",
        source_id=int(redaction_id),
        payload={
            "new_status": payload.status,
            "new_exemption_code": payload.exemption_code,
            "reviewer_id": payload.reviewer_id,
            "note_set": payload.notes is not None,
        },
    )
    return _row_to_out(row)


@router.delete(
    "/redactions/{redaction_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a redaction",
)
def delete_redaction_endpoint(
    redaction_id: int,
    conn: sqlite3.Connection = Depends(get_db),
    caller: CallerIdentity = Depends(get_caller),
):
    try:
        delete_redaction(conn, redaction_id)
    except RedactionError as e:
        raise HTTPException(404, str(e))
    audit.log_event(
        conn,
        event_type=audit.EVT_REDACTION_DELETE,
        actor=caller.actor,
        user_id=caller.user_id,
        origin="api",
        source_type="redaction",
        source_id=int(redaction_id),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/exemption-codes",
    response_model=list[ExemptionCodeOut],
    summary="List exemption codes configured for this district",
)
def list_exemption_codes(request: Request) -> list[ExemptionCodeOut]:
    district = _district(request)
    return [
        ExemptionCodeOut(code=e.code, description=e.description)
        for e in district.exemptions
    ]
