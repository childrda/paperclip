"""/emails — list, view, raw RFC822."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from ... import audit
from ...district import load_district_config
from ...redaction import propose_from_detections
from ..deps import CallerIdentity, Pagination, get_db, pagination, require_user
from ..schemas import (
    AttachmentSummary,
    EmailDetail,
    EmailSummary,
    Page,
    PiiDetectionOut,
)


class ExcludePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = None

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
        is_excluded=bool(row["is_excluded"]),
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
    case_id: Optional[int] = Query(
        None,
        description=(
            "Restrict to emails belonging to a specific case. "
            "Reviewers click into a case from /cases and expect to see "
            "only that case's emails — without this filter the list is "
            "global across the database."
        ),
    ),
):
    where: list[str] = []
    params: list = []
    if case_id is not None:
        where.append("case_id = ?")
        params.append(case_id)
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
              WHERE p.source_type LIKE 'email_%' AND p.source_id = emails.id) AS pii_count,
            (emails.excluded_at IS NOT NULL) AS is_excluded
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
        excluded_at=row["excluded_at"],
        excluded_by_user_id=row["excluded_by_user_id"],
        exclusion_reason=row["exclusion_reason"],
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


@router.post(
    "/{email_id}/exclude",
    response_model=EmailDetail,
    summary=(
        "Withhold this email from the production. The row stays in the "
        "database (audit trail), but the export pipeline skips it."
    ),
)
def exclude_email(
    email_id: int,
    payload: ExcludePayload,
    conn: sqlite3.Connection = Depends(get_db),
    caller: CallerIdentity = Depends(require_user),
):
    if conn.execute(
        "SELECT 1 FROM emails WHERE id = ?", (email_id,)
    ).fetchone() is None:
        raise HTTPException(404, f"email {email_id} not found")

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE emails SET excluded_at = ?, excluded_by_user_id = ?, "
        "       exclusion_reason = ? WHERE id = ?",
        (now, caller.user_id, payload.reason, email_id),
    )
    audit.log_event(
        conn,
        event_type=audit.EVT_EMAIL_EXCLUDED,
        actor=caller.actor, user_id=caller.user_id, origin="api",
        source_type="email", source_id=email_id,
        payload={"reason": payload.reason, "excluded_at": now},
    )
    conn.commit()
    return get_email(email_id, conn)


@router.post(
    "/{email_id}/include",
    response_model=EmailDetail,
    summary=(
        "Reverse a prior exclusion — bring this email back into the "
        "production."
    ),
)
def include_email(
    email_id: int,
    conn: sqlite3.Connection = Depends(get_db),
    caller: CallerIdentity = Depends(require_user),
):
    row = conn.execute(
        "SELECT excluded_at FROM emails WHERE id = ?", (email_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"email {email_id} not found")
    if row["excluded_at"] is None:
        # Idempotent — re-including a non-excluded email is a no-op
        # rather than an error.
        return get_email(email_id, conn)

    conn.execute(
        "UPDATE emails SET excluded_at = NULL, excluded_by_user_id = NULL, "
        "       exclusion_reason = NULL WHERE id = ?",
        (email_id,),
    )
    audit.log_event(
        conn,
        event_type=audit.EVT_EMAIL_INCLUDED,
        actor=caller.actor, user_id=caller.user_id, origin="api",
        source_type="email", source_id=email_id,
        payload={},
    )
    conn.commit()
    return get_email(email_id, conn)


@router.post(
    "/{email_id}/propose-redactions",
    summary=(
        "Run the auto-proposer over this email's PII detections. "
        "Recovery path for emails imported with proposing skipped."
    ),
)
def propose_email_redactions(
    email_id: int,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    caller: CallerIdentity = Depends(require_user),
):
    if conn.execute(
        "SELECT 1 FROM emails WHERE id = ?", (email_id,)
    ).fetchone() is None:
        raise HTTPException(404, f"email {email_id} not found")

    cached = getattr(request.app.state, "district_config", None)
    if cached is None:
        cached = load_district_config()
        request.app.state.district_config = cached

    stats = propose_from_detections(conn, cached, only_email_id=email_id)
    conn.commit()
    audit.log_event(
        conn,
        event_type="email.redactions_proposed",
        actor=caller.actor, user_id=caller.user_id, origin="api",
        source_type="email", source_id=email_id,
        payload=stats.as_dict(),
    )
    conn.commit()
    return stats.as_dict()


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
