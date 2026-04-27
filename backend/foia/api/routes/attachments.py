"""/attachments — list, view, download."""

from __future__ import annotations

import mimetypes
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from ..deps import Pagination, get_db, pagination
from ..schemas import AttachmentDetail, AttachmentSummary, Page

router = APIRouter(prefix="/attachments", tags=["attachments"])


@router.get("", response_model=Page[AttachmentSummary], summary="List attachments")
def list_attachments(
    conn: sqlite3.Connection = Depends(get_db),
    page: Pagination = Depends(pagination),
    email_id: Optional[int] = Query(None, description="Filter by email id."),
    content_type: Optional[str] = Query(
        None, description="Exact-match filter on MIME type (e.g. application/pdf)."
    ),
    content_type_prefix: Optional[str] = Query(
        None, description="Prefix filter (e.g. image/ matches image/png, image/jpeg)."
    ),
    extraction_status: Optional[str] = Query(
        None, description="Filter by attachments_text.extraction_status.",
    ),
    only_inline: Optional[bool] = Query(
        None, description="If true, return only Content-Disposition: inline parts.",
    ),
):
    where: list[str] = []
    params: list = []
    if email_id is not None:
        where.append("a.email_id = ?"); params.append(email_id)
    if content_type:
        where.append("a.content_type = ?"); params.append(content_type)
    if content_type_prefix:
        where.append("a.content_type LIKE ?"); params.append(content_type_prefix + "%")
    if extraction_status:
        where.append("t.extraction_status = ?"); params.append(extraction_status)
    if only_inline is True:
        where.append("a.is_inline = 1")
    elif only_inline is False:
        where.append("a.is_inline = 0")

    clause = (" WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute(
        f"""
        SELECT COUNT(*) FROM attachments a
        LEFT JOIN attachments_text t ON t.attachment_id = a.id
        {clause}
        """,
        params,
    ).fetchone()[0]
    rows = conn.execute(
        f"""
        SELECT
            a.id, a.email_id, a.filename, a.content_type, a.size_bytes,
            a.is_inline, a.is_nested_eml, t.extraction_status
        FROM attachments a
        LEFT JOIN attachments_text t ON t.attachment_id = a.id
        {clause}
        ORDER BY a.id
        LIMIT ? OFFSET ?
        """,
        [*params, page.limit, page.offset],
    ).fetchall()

    return Page[AttachmentSummary](
        items=[
            AttachmentSummary(
                id=r["id"], email_id=r["email_id"], filename=r["filename"],
                content_type=r["content_type"], size_bytes=r["size_bytes"],
                is_inline=bool(r["is_inline"]),
                is_nested_eml=bool(r["is_nested_eml"]),
                extraction_status=r["extraction_status"],
            )
            for r in rows
        ],
        total=int(total), limit=page.limit, offset=page.offset,
    )


@router.get(
    "/{attachment_id}",
    response_model=AttachmentDetail,
    summary="View a single attachment (metadata + extracted text).",
)
def get_attachment(
    attachment_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> AttachmentDetail:
    row = conn.execute(
        """
        SELECT a.*,
               t.extracted_text, t.extraction_method, t.extraction_status,
               t.ocr_applied, t.page_count, t.character_count, t.error_message
        FROM attachments a
        LEFT JOIN attachments_text t ON t.attachment_id = a.id
        WHERE a.id = ?
        """,
        (attachment_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"attachment {attachment_id} not found")

    return AttachmentDetail(
        id=row["id"], email_id=row["email_id"], filename=row["filename"],
        content_type=row["content_type"],
        content_disposition=row["content_disposition"],
        size_bytes=row["size_bytes"], sha256=row["sha256"],
        is_inline=bool(row["is_inline"]),
        is_nested_eml=bool(row["is_nested_eml"]),
        storage_path=row["storage_path"],
        extracted_text=row["extracted_text"],
        extraction_method=row["extraction_method"],
        extraction_status=row["extraction_status"],
        ocr_applied=bool(row["ocr_applied"]) if row["ocr_applied"] is not None else None,
        page_count=row["page_count"],
        character_count=row["character_count"],
        extraction_error=row["error_message"],
    )


@router.get(
    "/{attachment_id}/download",
    summary="Download the original attachment bytes.",
    responses={
        200: {"content": {"application/octet-stream": {}}},
        404: {"description": "attachment or stored file not found"},
    },
)
def download_attachment(
    attachment_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> FileResponse:
    row = conn.execute(
        "SELECT filename, content_type, storage_path FROM attachments WHERE id = ?",
        (attachment_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"attachment {attachment_id} not found")
    path = Path(row["storage_path"])
    if not path.exists():
        raise HTTPException(404, f"file missing on disk: {path}")
    media_type = row["content_type"] or (
        mimetypes.guess_type(row["filename"] or "")[0] or "application/octet-stream"
    )
    return FileResponse(
        path,
        media_type=media_type,
        filename=row["filename"] or f"attachment-{attachment_id}",
    )
