"""/search — full-text search across emails and extracted attachment text."""

from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import Pagination, get_db, pagination
from ..schemas import Page, SearchHit

router = APIRouter(prefix="/search", tags=["search"])


def _escape_fts(query: str) -> str:
    """Wrap individual terms in quotes so FTS5 treats them as literals.

    Users of this API aren't expected to know FTS5 syntax. We split on
    whitespace, strip obvious control characters, and re-quote. An empty
    query raises upstream.
    """
    clean = query.replace('"', "").strip()
    if not clean:
        return ""
    parts = [p for p in clean.split() if p]
    return " ".join(f'"{p}"' for p in parts)


@router.get(
    "",
    response_model=Page[SearchHit],
    summary="Full-text search across emails + attachment text (FTS5).",
)
def search(
    conn: sqlite3.Connection = Depends(get_db),
    page: Pagination = Depends(pagination),
    q: str = Query(..., min_length=1, description="Query string."),
    scope: Optional[str] = Query(
        None,
        pattern="^(emails|attachments)$",
        description="Restrict to only emails or only attachments.",
    ),
):
    expr = _escape_fts(q)
    if not expr:
        raise HTTPException(400, "query must contain at least one non-empty term")

    unions: list[str] = []
    params: list = []

    if scope in (None, "emails"):
        unions.append(
            """
            SELECT
                'email'    AS source_type,
                e.id       AS source_id,
                COALESCE(e.subject, '(no subject)') AS title,
                snippet(emails_fts, -1, '<mark>', '</mark>', '…', 12) AS snippet,
                bm25(emails_fts)                     AS rank,
                e.id AS email_id
            FROM emails_fts
            JOIN emails e ON e.id = emails_fts.rowid
            WHERE emails_fts MATCH ?
            """
        )
        params.append(expr)

    if scope in (None, "attachments"):
        unions.append(
            """
            SELECT
                'attachment' AS source_type,
                a.id         AS source_id,
                COALESCE(a.filename, '(unnamed)') AS title,
                snippet(attachments_fts, -1, '<mark>', '</mark>', '…', 12) AS snippet,
                bm25(attachments_fts)             AS rank,
                a.email_id AS email_id
            FROM attachments_fts
            JOIN attachments a ON a.id = attachments_fts.rowid
            WHERE attachments_fts MATCH ?
            """
        )
        params.append(expr)

    if not unions:
        raise HTTPException(400, "no sources selected")

    union_sql = " UNION ALL ".join(unions)
    total_sql = f"SELECT COUNT(*) FROM ({union_sql})"
    total = conn.execute(total_sql, params).fetchone()[0]

    rows_sql = (
        f"SELECT * FROM ({union_sql}) ORDER BY rank LIMIT ? OFFSET ?"
    )
    rows = conn.execute(rows_sql, [*params, page.limit, page.offset]).fetchall()
    return Page[SearchHit](
        items=[
            SearchHit(
                source_type=r["source_type"],
                source_id=int(r["source_id"]),
                title=r["title"],
                snippet=r["snippet"] or "",
                rank=float(r["rank"]),
                email_id=(int(r["email_id"]) if r["email_id"] is not None else None),
            )
            for r in rows
        ],
        total=int(total), limit=page.limit, offset=page.offset,
    )
