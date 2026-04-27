"""/persons — read-only views of the Phase 4 entity-resolution output."""

from __future__ import annotations

import json
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import Pagination, get_db, pagination
from ..schemas import Page, PersonDetail, PersonEmailOut, PersonSummary

router = APIRouter(prefix="/persons", tags=["persons"])


@router.get("", response_model=Page[PersonSummary], summary="List unified persons")
def list_persons(
    conn: sqlite3.Connection = Depends(get_db),
    page: Pagination = Depends(pagination),
    is_internal: Optional[bool] = Query(None, description="Filter by district-internal flag."),
    email_domain: Optional[str] = Query(
        None, description="Exact-match filter on the primary email domain."
    ),
    name_contains: Optional[str] = Query(
        None, description="Case-insensitive substring match on display_name."
    ),
):
    where: list[str] = ["1=1"]
    params: list = []
    if is_internal is True:
        where.append("p.is_internal = 1")
    elif is_internal is False:
        where.append("p.is_internal = 0")
    if name_contains:
        where.append("LOWER(p.display_name) LIKE ?")
        params.append(f"%{name_contains.lower()}%")
    if email_domain:
        where.append(
            "EXISTS (SELECT 1 FROM person_emails pe "
            "         WHERE pe.person_id = p.id AND pe.email LIKE ?)"
        )
        params.append(f"%@{email_domain.lower()}")

    clause = " WHERE " + " AND ".join(where)

    total = conn.execute(
        f"SELECT COUNT(*) FROM persons p{clause}", params,
    ).fetchone()[0]
    rows = conn.execute(
        f"""
        SELECT p.id, p.display_name, p.is_internal,
               (SELECT email FROM person_emails pe
                 WHERE pe.person_id = p.id AND pe.is_primary = 1 LIMIT 1) AS primary_email,
               (SELECT COUNT(*) FROM person_occurrences po WHERE po.person_id = p.id)
                 AS occurrences
        FROM persons p
        {clause}
        ORDER BY occurrences DESC, p.display_name
        LIMIT ? OFFSET ?
        """,
        [*params, page.limit, page.offset],
    ).fetchall()
    return Page[PersonSummary](
        items=[
            PersonSummary(
                id=r["id"], display_name=r["display_name"],
                primary_email=r["primary_email"],
                is_internal=bool(r["is_internal"]),
                occurrences=int(r["occurrences"] or 0),
            )
            for r in rows
        ],
        total=int(total), limit=page.limit, offset=page.offset,
    )


@router.get("/{person_id}", response_model=PersonDetail, summary="Person detail")
def get_person(
    person_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> PersonDetail:
    p = conn.execute("SELECT * FROM persons WHERE id = ?", (person_id,)).fetchone()
    if p is None:
        raise HTTPException(404, f"person {person_id} not found")

    emails = [
        PersonEmailOut(
            email=r["email"], is_primary=bool(r["is_primary"]),
            first_seen=r["first_seen"],
        )
        for r in conn.execute(
            "SELECT email, is_primary, first_seen FROM person_emails "
            "WHERE person_id = ? ORDER BY is_primary DESC, email",
            (person_id,),
        )
    ]
    occ = dict(
        conn.execute(
            "SELECT source_type, COUNT(*) FROM person_occurrences "
            "WHERE person_id = ? GROUP BY source_type",
            (person_id,),
        ).fetchall()
    )

    return PersonDetail(
        id=p["id"],
        display_name=p["display_name"],
        names=json.loads(p["names_json"] or "[]"),
        is_internal=bool(p["is_internal"]),
        notes=p["notes"],
        emails=emails,
        occurrences_by_type={str(k): int(v) for k, v in occ.items()},
        created_at=p["created_at"],
        updated_at=p["updated_at"],
    )
