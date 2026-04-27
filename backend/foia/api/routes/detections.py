"""/detections — list PII detections + entity-count aggregate."""

from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, Query

from ..deps import Pagination, get_db, pagination
from ..schemas import EntityCount, Page, PiiDetectionOut

router = APIRouter(prefix="/detections", tags=["detections"])


@router.get("", response_model=Page[PiiDetectionOut], summary="List PII detections")
def list_detections(
    conn: sqlite3.Connection = Depends(get_db),
    page: Pagination = Depends(pagination),
    entity_type: Optional[str] = Query(
        None, description="Filter by entity type (e.g. US_SSN, EMAIL_ADDRESS)."
    ),
    source_type: Optional[str] = Query(
        None, description="Filter by source: email_subject / email_body_text / "
                          "email_body_html / attachment_text.",
    ),
    source_id: Optional[int] = Query(
        None, description="Filter to a specific source row (email.id or attachment.id)."
    ),
    min_score: Optional[float] = Query(
        None, ge=0.0, le=1.0,
        description="Drop detections below this score.",
    ),
):
    where: list[str] = []
    params: list = []
    if entity_type:
        where.append("entity_type = ?"); params.append(entity_type)
    if source_type:
        where.append("source_type = ?"); params.append(source_type)
    if source_id is not None:
        where.append("source_id = ?"); params.append(source_id)
    if min_score is not None:
        where.append("score >= ?"); params.append(min_score)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute(
        f"SELECT COUNT(*) FROM pii_detections{clause}", params,
    ).fetchone()[0]
    rows = conn.execute(
        f"""
        SELECT * FROM pii_detections
        {clause}
        ORDER BY id
        LIMIT ? OFFSET ?
        """,
        [*params, page.limit, page.offset],
    ).fetchall()
    return Page[PiiDetectionOut](
        items=[
            PiiDetectionOut(
                id=r["id"], source_type=r["source_type"],
                source_id=r["source_id"], entity_type=r["entity_type"],
                start_offset=r["start_offset"], end_offset=r["end_offset"],
                matched_text=r["matched_text"], score=r["score"],
                recognizer=r["recognizer"], detected_at=r["detected_at"],
            )
            for r in rows
        ],
        total=int(total), limit=page.limit, offset=page.offset,
    )


@router.get(
    "/entities",
    response_model=list[EntityCount],
    summary="Aggregate count per entity type.",
)
def entity_counts(
    conn: sqlite3.Connection = Depends(get_db),
    min_score: Optional[float] = Query(None, ge=0.0, le=1.0),
) -> list[EntityCount]:
    sql = "SELECT entity_type, COUNT(*) AS c FROM pii_detections"
    params: list = []
    if min_score is not None:
        sql += " WHERE score >= ?"; params.append(min_score)
    sql += " GROUP BY entity_type ORDER BY c DESC, entity_type"
    rows = conn.execute(sql, params).fetchall()
    return [EntityCount(entity_type=r["entity_type"], count=int(r["c"])) for r in rows]
