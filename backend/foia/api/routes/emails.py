"""/emails — list, view, raw RFC822."""

from __future__ import annotations

import json
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ..deps import Pagination, get_db, pagination
from ..schemas import (
    AttachmentSummary,
    EmailDetail,
    EmailSummary,
    Page,
    PiiDetectionOut,
)

router = APIRouter(prefix="/emails", tags=["emails"])


def _row_to_summary(row: sqlite3.Row) -> EmailSummary:
    return EmailSummary(
        id=row["id"],
        subject=row["subject"],
        from_addr=row["from_addr"],
        date_sent=row["date_sent"],
        mbox_source=row["mbox_source"],
        mbox_index=row["mbox_index"],
        has_attachments=bool(row["has_attachments"]),
        pii_count=int(row["pii_count"] or 0),
    )


@router.get("", response_model=Page[EmailSummary], summary="List emails")
def list_emails(
    conn: sqlite3.Connection = Depends(get_db),
    page: Pagination = Depends(pagination),
    from_contains: Optional[str] = Query(
        None, description="Case-insensitive substring match on the From header."
    ),
    subject_contains: Optional[str] = Query(
        None, description="Case-insensitive substring match on the Subject."
    ),
    date_from: Optional[str] = Query(
        None, description="ISO 8601 lower bound for date_sent (inclusive)."
    ),
    date_to: Optional[str] = Query(
        None, description="ISO 8601 upper bound for date_sent (exclusive)."
    ),
    has_attachments: Optional[bool] = Query(
        None, description="Filter to emails with / without attachments."
    ),
    has_pii: Optional[bool] = Query(
        None, description="Filter to emails with / without any PII detection."
    ),
    mbox_source: Optional[str] = Query(
        None, description="Exact-match filter on mbox_source."
    ),
):
    where: list[str] = []
    params: list = []
    if from_contains:
        where.append("LOWER(from_addr) LIKE ?")
        params.append(f"%{from_contains.lower()}%")
    if subject_contains:
        where.append("LOWER(subject) LIKE ?")
        params.append(f"%{subject_contains.lower()}%")
    if date_from:
        where.append("date_sent >= ?")
        params.append(date_from)
    if date_to:
        where.append("date_sent < ?")
        params.append(date_to)
    if mbox_source:
        where.append("mbox_source = ?")
        params.append(mbox_source)
    if has_attachments is True:
        where.append("EXISTS (SELECT 1 FROM attachments a WHERE a.email_id = emails.id)")
    elif has_attachments is False:
        where.append("NOT EXISTS (SELECT 1 FROM attachments a WHERE a.email_id = emails.id)")
    if has_pii is True:
        where.append(
            "EXISTS (SELECT 1 FROM pii_detections p "
            "WHERE (p.source_type LIKE 'email_%' AND p.source_id = emails.id))"
        )
    elif has_pii is False:
        where.append(
            "NOT EXISTS (SELECT 1 FROM pii_detections p "
            "WHERE (p.source_type LIKE 'email_%' AND p.source_id = emails.id))"
        )

    clause = (" WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute(
        f"SELECT COUNT(*) FROM emails{clause}", params
    ).fetchone()[0]

    rows = conn.execute(
        f"""
        SELECT
            emails.id, emails.subject, emails.from_addr, emails.date_sent,
            emails.mbox_source, emails.mbox_index,
            EXISTS(SELECT 1 FROM attachments a WHERE a.email_id = emails.id) AS has_attachments,
            (SELECT COUNT(*) FROM pii_detections p
              WHERE p.source_type LIKE 'email_%' AND p.source_id = emails.id) AS pii_count
        FROM emails
        {clause}
        ORDER BY (emails.date_sent IS NULL), emails.date_sent DESC, emails.id DESC
        LIMIT ? OFFSET ?
        """,
        [*params, page.limit, page.offset],
    ).fetchall()
    return Page[EmailSummary](
        items=[_row_to_summary(r) for r in rows],
        total=int(total), limit=page.limit, offset=page.offset,
    )


@router.get("/{email_id}", response_model=EmailDetail, summary="View a single email")
def get_email(
    email_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> EmailDetail:
    row = conn.execute(
        "SELECT * FROM emails WHERE id = ?", (email_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"email {email_id} not found")

    atts = conn.execute(
        """
        SELECT a.id, a.email_id, a.filename, a.content_type, a.size_bytes,
               a.is_inline, a.is_nested_eml, t.extraction_status
        FROM attachments a
        LEFT JOIN attachments_text t ON t.attachment_id = a.id
        WHERE a.email_id = ?
        ORDER BY a.id
        """,
        (email_id,),
    ).fetchall()

    pii = conn.execute(
        """
        SELECT * FROM pii_detections
        WHERE source_type LIKE 'email_%' AND source_id = ?
        ORDER BY source_type, start_offset
        """,
        (email_id,),
    ).fetchall()

    return EmailDetail(
        id=row["id"],
        message_id=row["message_id"],
        subject=row["subject"],
        from_addr=row["from_addr"],
        to_addrs=json.loads(row["to_addrs"] or "[]"),
        cc_addrs=json.loads(row["cc_addrs"] or "[]"),
        bcc_addrs=json.loads(row["bcc_addrs"] or "[]"),
        date_sent=row["date_sent"],
        date_raw=row["date_raw"],
        body_text=row["body_text"],
        body_html_sanitized=row["body_html_sanitized"],
        headers=json.loads(row["headers_json"] or "{}"),
        mbox_source=row["mbox_source"],
        mbox_index=row["mbox_index"],
        ingested_at=row["ingested_at"],
        attachments=[
            AttachmentSummary(
                id=a["id"], email_id=a["email_id"], filename=a["filename"],
                content_type=a["content_type"], size_bytes=a["size_bytes"],
                is_inline=bool(a["is_inline"]),
                is_nested_eml=bool(a["is_nested_eml"]),
                extraction_status=a["extraction_status"],
            )
            for a in atts
        ],
        pii_detections=[
            PiiDetectionOut(
                id=d["id"], source_type=d["source_type"],
                source_id=d["source_id"], entity_type=d["entity_type"],
                start_offset=d["start_offset"], end_offset=d["end_offset"],
                matched_text=d["matched_text"], score=d["score"],
                recognizer=d["recognizer"], detected_at=d["detected_at"],
            )
            for d in pii
        ],
    )


@router.get(
    "/{email_id}/redactions",
    summary="All redactions tied to this email (subject + bodies)",
)
def get_email_redactions(
    email_id: int,
    conn: sqlite3.Connection = Depends(get_db),
):
    # Verify the email exists; otherwise return 404 even if no redactions.
    if conn.execute(
        "SELECT 1 FROM emails WHERE id = ?", (email_id,)
    ).fetchone() is None:
        raise HTTPException(404, f"email {email_id} not found")

    rows = conn.execute(
        """
        SELECT * FROM redactions
        WHERE source_type LIKE 'email_%' AND source_id = ?
        ORDER BY source_type, start_offset, id
        """,
        (email_id,),
    ).fetchall()
    return [dict(r) for r in rows]


@router.get(
    "/{email_id}/raw",
    summary="Download the original RFC822 bytes",
    responses={
        200: {"content": {"message/rfc822": {}}},
        404: {"description": "email not found"},
    },
)
def get_email_raw(
    email_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    row = conn.execute(
        "SELECT raw_rfc822 FROM raw_content WHERE email_id = ?", (email_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"raw content for email {email_id} not found")
    return Response(
        content=bytes(row["raw_rfc822"]),
        media_type="message/rfc822",
        headers={
            "Content-Disposition": f'attachment; filename="email-{email_id}.eml"',
        },
    )
