"""/audit — Phase 9 read-only window into the immutable audit log.

There are no write endpoints here by design. Audit rows can only enter
the system through the central :func:`foia.audit.log_event` calls
that every CLI / API write already wires up. Direct ``UPDATE`` /
``DELETE`` are blocked at the DB-trigger level.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, Query

from ... import audit as audit_mod
from ..deps import Pagination, get_db, pagination
from ..schemas import AuditEventOut, Page

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get(
    "",
    response_model=Page[AuditEventOut],
    summary="List audit events, newest first.",
)
def list_audit(
    conn: sqlite3.Connection = Depends(get_db),
    page: Pagination = Depends(pagination),
    event_type: Optional[str] = Query(
        None, description="Exact-match filter on event_type "
                          "(e.g. redaction.update, export.run)."
    ),
    actor: Optional[str] = Query(None, description="Exact-match filter on actor."),
    source_type: Optional[str] = Query(None),
    source_id: Optional[int] = Query(None),
    after: Optional[str] = Query(
        None, description="ISO 8601 inclusive lower bound for event_at."
    ),
    before: Optional[str] = Query(
        None, description="ISO 8601 exclusive upper bound for event_at."
    ),
    origin: Optional[str] = Query(
        None, description="'cli' | 'api' | 'system'"
    ),
):
    items, total = audit_mod.query_events(
        conn,
        event_type=event_type,
        actor=actor,
        source_type=source_type,
        source_id=source_id,
        after=after,
        before=before,
        origin=origin,
        limit=page.limit,
        offset=page.offset,
    )
    return Page[AuditEventOut](
        items=[
            AuditEventOut(
                id=int(r["id"]),
                event_at=r["event_at"],
                event_type=r["event_type"],
                actor=r["actor"],
                source_type=r["source_type"],
                source_id=r["source_id"],
                payload=r["payload"],
                request_origin=r["request_origin"],
            )
            for r in items
        ],
        total=total, limit=page.limit, offset=page.offset,
    )
