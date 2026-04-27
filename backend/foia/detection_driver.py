"""Batch PII detection driver.

Walks over every email and attachment_text in the DB, runs
:class:`PiiDetector` over each text source, and stores results in
``pii_detections``. Re-scans replace prior results for the same
``(source_type, source_id)`` to stay idempotent.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

from .detection import Detection, PiiDetector
from .sanitizer import html_to_text

log = logging.getLogger(__name__)


SOURCE_EMAIL_SUBJECT = "email_subject"
SOURCE_EMAIL_BODY_TEXT = "email_body_text"
SOURCE_EMAIL_BODY_HTML = "email_body_html"
SOURCE_ATTACHMENT_TEXT = "attachment_text"


@dataclass
class DetectStats:
    sources_scanned: int = 0
    sources_skipped: int = 0
    detections_written: int = 0
    by_entity: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.by_entity is None:
            self.by_entity = {}

    def as_dict(self) -> dict:
        return {
            "sources_scanned": self.sources_scanned,
            "sources_skipped": self.sources_skipped,
            "detections_written": self.detections_written,
            "by_entity": dict(sorted(self.by_entity.items())),
        }


def _replace_detections(
    conn: sqlite3.Connection,
    source_type: str,
    source_id: int,
    detections: Sequence[Detection],
) -> int:
    conn.execute(
        "DELETE FROM pii_detections WHERE source_type = ? AND source_id = ?",
        (source_type, source_id),
    )
    if not detections:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """
        INSERT INTO pii_detections (
            source_type, source_id, entity_type,
            start_offset, end_offset, matched_text, score,
            recognizer, detected_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                source_type, source_id, d.entity_type,
                d.start, d.end, d.matched_text, d.score,
                d.recognizer, now,
            )
            for d in detections
        ],
    )
    return len(detections)


def _iter_email_sources(
    conn: sqlite3.Connection, only_email_id: int | None
) -> Iterable[tuple[str, int, str]]:
    sql = "SELECT id, subject, body_text, body_html_sanitized FROM emails"
    params: list = []
    if only_email_id is not None:
        sql += " WHERE id = ?"
        params.append(only_email_id)
    for row in conn.execute(sql, params):
        eid = int(row["id"])
        if row["subject"]:
            yield SOURCE_EMAIL_SUBJECT, eid, row["subject"]
        if row["body_text"]:
            yield SOURCE_EMAIL_BODY_TEXT, eid, row["body_text"]
        if row["body_html_sanitized"]:
            # Scan the text projection of sanitized HTML so offsets refer
            # to human-readable content, not markup.
            yield SOURCE_EMAIL_BODY_HTML, eid, html_to_text(row["body_html_sanitized"])


def _iter_attachment_sources(
    conn: sqlite3.Connection, only_attachment_id: int | None
) -> Iterable[tuple[str, int, str]]:
    sql = (
        "SELECT attachment_id, extracted_text FROM attachments_text "
        "WHERE extraction_status = 'ok' AND extracted_text IS NOT NULL"
    )
    params: list = []
    if only_attachment_id is not None:
        sql += " AND attachment_id = ?"
        params.append(only_attachment_id)
    for row in conn.execute(sql, params):
        yield SOURCE_ATTACHMENT_TEXT, int(row["attachment_id"]), row["extracted_text"]


def run_detection(
    conn: sqlite3.Connection,
    detector: PiiDetector,
    *,
    only_email_id: int | None = None,
    only_attachment_id: int | None = None,
) -> DetectStats:
    stats = DetectStats()
    iterables: list[Iterable[tuple[str, int, str]]] = []
    if only_attachment_id is None:
        iterables.append(_iter_email_sources(conn, only_email_id))
    if only_email_id is None:
        iterables.append(_iter_attachment_sources(conn, only_attachment_id))

    for it in iterables:
        for source_type, source_id, text in it:
            if not text or not text.strip():
                stats.sources_skipped += 1
                continue
            try:
                detections = detector.detect(text)
            except Exception:
                log.exception(
                    "detection crashed for %s id=%s", source_type, source_id
                )
                stats.sources_skipped += 1
                continue
            written = _replace_detections(conn, source_type, source_id, detections)
            stats.sources_scanned += 1
            stats.detections_written += written
            for d in detections:
                stats.by_entity[d.entity_type] = (
                    stats.by_entity.get(d.entity_type, 0) + 1
                )
            conn.commit()

    return stats


__all__ = [
    "DetectStats",
    "run_detection",
    "SOURCE_EMAIL_SUBJECT",
    "SOURCE_EMAIL_BODY_TEXT",
    "SOURCE_EMAIL_BODY_HTML",
    "SOURCE_ATTACHMENT_TEXT",
]
