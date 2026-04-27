"""/ai-flags — Phase 10 endpoints.

The single deliberate constraint: AI never auto-redacts. The
``promote`` endpoint creates a *proposed* redaction (Phase 6) and
links it back to the flag, but the redaction still requires a human
to Accept it through the normal flow.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ... import audit
from ...ai import AiProviderError, build_provider
from ...ai_driver import (
    AiFlagError,
    dismiss_flag,
    get_flag,
    list_flags,
    promote_flag,
    run_ai_qa,
)
from ...district import DistrictConfig, load_district_config
from ..deps import Pagination, get_actor, get_db, pagination
from ..schemas import (
    AiDismissRequest,
    AiFlagOut,
    AiPromoteRequest,
    AiQaRunRequest,
    Page,
)

router = APIRouter(prefix="/ai-flags", tags=["ai"])


def _district(request: Request) -> DistrictConfig:
    cached = getattr(request.app.state, "district_config", None)
    if cached is not None:
        return cached
    cfg = load_district_config()
    request.app.state.district_config = cfg
    return cfg


def _row_to_out(row: dict) -> AiFlagOut:
    return AiFlagOut(**{k: row[k] for k in AiFlagOut.model_fields.keys()})


@router.get(
    "",
    response_model=Page[AiFlagOut],
    summary="List AI QA flags (advisory; never automatically applied).",
)
def list_ai_flags(
    conn: sqlite3.Connection = Depends(get_db),
    page: Pagination = Depends(pagination),
    review_status: Optional[str] = Query(
        None, alias="status",
        description="open | dismissed | promoted",
    ),
    source_type: Optional[str] = Query(None),
    source_id: Optional[int] = Query(None),
    entity_type: Optional[str] = Query(None),
    provider: Optional[str] = Query(None),
    qa_run_id: Optional[str] = Query(None),
):
    items, total = list_flags(
        conn,
        review_status=review_status,
        source_type=source_type,
        source_id=source_id,
        entity_type=entity_type,
        provider=provider,
        qa_run_id=qa_run_id,
        limit=page.limit,
        offset=page.offset,
    )
    return Page[AiFlagOut](
        items=[_row_to_out(r) for r in items],
        total=total, limit=page.limit, offset=page.offset,
    )


@router.get(
    "/{flag_id}",
    response_model=AiFlagOut,
    summary="View one AI flag.",
)
def get_ai_flag(
    flag_id: int,
    conn: sqlite3.Connection = Depends(get_db),
):
    try:
        return _row_to_out(get_flag(conn, flag_id))
    except AiFlagError as e:
        raise HTTPException(404, str(e))


@router.post(
    "/run",
    summary="Trigger an AI QA scan and persist the resulting flags.",
)
def run_ai_qa_endpoint(
    payload: AiQaRunRequest,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    actor: str = Depends(get_actor),
):
    district = _district(request)
    try:
        provider = build_provider(
            district.ai,
            override_provider=payload.provider,
            override_model=payload.model,
        )
    except AiProviderError as e:
        raise HTTPException(400, str(e))

    stats = run_ai_qa(
        conn, provider,
        only_email_id=payload.email_id,
        only_attachment_id=payload.attachment_id,
    )
    audit.log_event(
        conn,
        event_type="ai_qa.run",
        actor=actor,
        origin="api",
        payload={
            "provider": provider.name,
            "model": provider.model,
            "email_id": payload.email_id,
            "attachment_id": payload.attachment_id,
            **stats.as_dict(),
        },
    )
    return stats.as_dict()


@router.patch(
    "/{flag_id}/dismiss",
    response_model=AiFlagOut,
    summary="Mark an AI flag as not actionable.",
)
def dismiss_ai_flag(
    flag_id: int,
    payload: AiDismissRequest,
    conn: sqlite3.Connection = Depends(get_db),
    actor: str = Depends(get_actor),
):
    try:
        row = dismiss_flag(conn, flag_id, actor=actor, note=payload.note)
    except AiFlagError as e:
        msg = str(e).lower()
        raise HTTPException(404 if "not found" in msg else 400, str(e))
    audit.log_event(
        conn,
        event_type="ai_qa.dismiss",
        actor=actor,
        origin="api",
        source_type="ai_flag",
        source_id=int(flag_id),
        payload={"note": payload.note},
    )
    return _row_to_out(row)


@router.post(
    "/{flag_id}/promote",
    status_code=status.HTTP_201_CREATED,
    summary="Create a *proposed* redaction from this AI flag (human action).",
)
def promote_ai_flag(
    flag_id: int,
    payload: AiPromoteRequest,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    actor: str = Depends(get_actor),
):
    district = _district(request)
    try:
        result = promote_flag(
            conn, district, flag_id,
            actor=actor,
            exemption_code=payload.exemption_code,
            note=payload.note,
        )
    except AiFlagError as e:
        msg = str(e).lower()
        raise HTTPException(404 if "not found" in msg else 400, str(e))
    audit.log_event(
        conn,
        event_type="ai_qa.promote",
        actor=actor,
        origin="api",
        source_type="ai_flag",
        source_id=int(flag_id),
        payload={
            "redaction_id": int(result["redaction"]["id"]),
            "exemption_code": result["redaction"]["exemption_code"],
            "note": payload.note,
        },
    )
    return {
        "flag": _row_to_out({k: result[k] for k in result if k != "redaction"}),
        "redaction": result["redaction"],
    }
