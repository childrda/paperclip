"""Batch extraction driver.

Iterates over every attachment that does not yet have a row in
``attachments_text`` and runs the appropriate handler from
:mod:`foia.extraction`. Failures are logged and stored — never raised.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .extraction import ExtractionOptions, ExtractionResult, extract

log = logging.getLogger(__name__)


@dataclass
class ProcessStats:
    total: int = 0
    extracted_ok: int = 0
    extracted_empty: int = 0
    unsupported: int = 0
    failed: int = 0
    skipped_already_done: int = 0

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "extracted_ok": self.extracted_ok,
            "extracted_empty": self.extracted_empty,
            "unsupported": self.unsupported,
            "failed": self.failed,
            "skipped_already_done": self.skipped_already_done,
        }


def _store_result(
    conn: sqlite3.Connection,
    attachment_id: int,
    result: ExtractionResult,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    error = result.error
    if result.notes:
        joined = "; ".join(result.notes)
        error = f"{error}; notes: {joined}" if error else f"notes: {joined}"
    conn.execute(
        """
        INSERT INTO attachments_text (
            attachment_id, extracted_text, extraction_method,
            ocr_applied, page_count, character_count,
            extraction_status, error_message, extracted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            attachment_id,
            result.text or None,
            result.method,
            1 if result.ocr_applied else 0,
            result.page_count,
            result.character_count,
            result.status,
            error,
            now,
        ),
    )


def process_attachments(
    conn: sqlite3.Connection,
    *,
    options: ExtractionOptions | None = None,
    force: bool = False,
    only_attachment_id: int | None = None,
) -> ProcessStats:
    """Extract text for every attachment without an existing ``attachments_text`` row.

    Parameters
    ----------
    force : bool
        If True, also re-process attachments that already have a row
        (the existing row is deleted first).
    only_attachment_id : int | None
        If set, restrict processing to a single attachment id.
    """
    options = options or ExtractionOptions()
    stats = ProcessStats()

    query = """
        SELECT a.id, a.storage_path, a.content_type, a.filename, t.id AS text_id
        FROM attachments a
        LEFT JOIN attachments_text t ON t.attachment_id = a.id
    """
    params: list = []
    clauses: list[str] = []
    if only_attachment_id is not None:
        clauses.append("a.id = ?")
        params.append(only_attachment_id)
    if not force:
        clauses.append("t.id IS NULL")
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY a.id"

    rows = conn.execute(query, params).fetchall()
    stats.total = len(rows)

    for row in rows:
        attachment_id = int(row["id"])
        if force and row["text_id"] is not None:
            conn.execute(
                "DELETE FROM attachments_text WHERE attachment_id = ?",
                (attachment_id,),
            )

        storage_path = Path(row["storage_path"])
        content_type = row["content_type"] or ""
        try:
            result = extract(storage_path, content_type, options)
        except Exception as e:  # defence in depth; extract() also guards
            log.exception("extraction crashed for attachment id=%s", attachment_id)
            result = ExtractionResult(
                status="failed", method="dispatch",
                error=f"driver crash: {e}",
            )

        _store_result(conn, attachment_id, result)
        conn.commit()

        if result.status == "ok":
            stats.extracted_ok += 1
        elif result.status == "empty":
            stats.extracted_empty += 1
        elif result.status == "unsupported":
            stats.unsupported += 1
        else:
            stats.failed += 1
        log.info(
            "attachment id=%s status=%s method=%s chars=%d",
            attachment_id, result.status, result.method, result.character_count,
        )

    # Count attachments already-processed that we skipped (only meaningful when !force).
    if not force and only_attachment_id is None:
        stats.skipped_already_done = conn.execute(
            "SELECT COUNT(*) FROM attachments_text"
        ).fetchone()[0] - (stats.extracted_ok + stats.extracted_empty
                           + stats.unsupported + stats.failed)
        if stats.skipped_already_done < 0:
            stats.skipped_already_done = 0

    return stats


__all__ = ["ProcessStats", "process_attachments"]
