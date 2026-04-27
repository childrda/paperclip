"""Phase 6 — non-destructive redaction primitives.

Source content is *never* modified. A redaction is a span (`source_type`,
`source_id`, `[start, end)`) plus an exemption code, a status, and a
reviewer. Phase 8's PDF export will read only ``status='accepted'``
rows; everything else is a proposal awaiting human review.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .district import DistrictConfig

log = logging.getLogger(__name__)


SOURCE_EMAIL_SUBJECT = "email_subject"
SOURCE_EMAIL_BODY_TEXT = "email_body_text"
SOURCE_EMAIL_BODY_HTML = "email_body_html"
SOURCE_ATTACHMENT_TEXT = "attachment_text"

VALID_SOURCE_TYPES: frozenset[str] = frozenset({
    SOURCE_EMAIL_SUBJECT,
    SOURCE_EMAIL_BODY_TEXT,
    SOURCE_EMAIL_BODY_HTML,
    SOURCE_ATTACHMENT_TEXT,
})

VALID_STATUSES: frozenset[str] = frozenset({"proposed", "accepted", "rejected"})


class RedactionError(ValueError):
    """Raised when a redaction would violate validation rules."""


@dataclass(frozen=True)
class SpanLookup:
    """Result of resolving a (source_type, source_id) to its underlying text."""

    text: str
    exists: bool


def get_source_text(
    conn: sqlite3.Connection, source_type: str, source_id: int
) -> SpanLookup:
    """Fetch the canonical text for a redaction target.

    Returns ``exists=False`` when the row is missing; ``text`` is empty
    when the target row has no text (e.g. an email with no subject).
    Both are valid distinctions for validation.
    """
    if source_type == SOURCE_EMAIL_SUBJECT:
        row = conn.execute(
            "SELECT subject FROM emails WHERE id = ?", (source_id,)
        ).fetchone()
        if row is None:
            return SpanLookup(text="", exists=False)
        return SpanLookup(text=row["subject"] or "", exists=True)

    if source_type == SOURCE_EMAIL_BODY_TEXT:
        row = conn.execute(
            "SELECT body_text FROM emails WHERE id = ?", (source_id,)
        ).fetchone()
        if row is None:
            return SpanLookup(text="", exists=False)
        return SpanLookup(text=row["body_text"] or "", exists=True)

    if source_type == SOURCE_EMAIL_BODY_HTML:
        row = conn.execute(
            "SELECT body_html_sanitized FROM emails WHERE id = ?", (source_id,)
        ).fetchone()
        if row is None:
            return SpanLookup(text="", exists=False)
        return SpanLookup(text=row["body_html_sanitized"] or "", exists=True)

    if source_type == SOURCE_ATTACHMENT_TEXT:
        row = conn.execute(
            "SELECT extracted_text FROM attachments_text "
            "WHERE attachment_id = ?",
            (source_id,),
        ).fetchone()
        if row is None:
            return SpanLookup(text="", exists=False)
        return SpanLookup(text=row["extracted_text"] or "", exists=True)

    raise RedactionError(f"unknown source_type: {source_type!r}")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_new_redaction(
    conn: sqlite3.Connection,
    district: DistrictConfig,
    *,
    source_type: str,
    source_id: int,
    start_offset: int,
    end_offset: int,
    exemption_code: str,
    status: str = "proposed",
    origin: str = "manual",
    reviewer_id: str | None = None,
) -> None:
    if source_type not in VALID_SOURCE_TYPES:
        raise RedactionError(
            f"source_type must be one of {sorted(VALID_SOURCE_TYPES)}; got {source_type!r}"
        )
    if status not in VALID_STATUSES:
        raise RedactionError(
            f"status must be one of {sorted(VALID_STATUSES)}; got {status!r}"
        )
    if origin not in ("auto", "manual"):
        raise RedactionError(f"origin must be 'auto' or 'manual'; got {origin!r}")
    if not isinstance(start_offset, int) or not isinstance(end_offset, int):
        raise RedactionError("start_offset and end_offset must be integers")
    if start_offset < 0:
        raise RedactionError("start_offset must be >= 0")
    if end_offset <= start_offset:
        raise RedactionError("end_offset must be greater than start_offset")
    if not exemption_code:
        raise RedactionError("exemption_code is required")
    if not district.is_known_exemption(exemption_code):
        raise RedactionError(
            f"exemption_code {exemption_code!r} is not in the configured list"
        )
    if status in ("accepted", "rejected") and not reviewer_id:
        raise RedactionError(
            f"reviewer_id is required to set status={status!r}"
        )

    span = get_source_text(conn, source_type, source_id)
    if not span.exists:
        raise RedactionError(
            f"source row not found: {source_type}#{source_id}"
        )
    if end_offset > len(span.text):
        raise RedactionError(
            f"end_offset {end_offset} exceeds source length {len(span.text)}"
        )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def create_redaction(
    conn: sqlite3.Connection,
    district: DistrictConfig,
    *,
    source_type: str,
    source_id: int,
    start_offset: int,
    end_offset: int,
    exemption_code: str,
    status: str = "proposed",
    origin: str = "manual",
    reviewer_id: str | None = None,
    source_detection_id: int | None = None,
    notes: str | None = None,
) -> dict:
    validate_new_redaction(
        conn, district,
        source_type=source_type, source_id=source_id,
        start_offset=start_offset, end_offset=end_offset,
        exemption_code=exemption_code, status=status,
        origin=origin, reviewer_id=reviewer_id,
    )
    now = _now()
    try:
        cur = conn.execute(
            """
            INSERT INTO redactions (
                source_type, source_id, start_offset, end_offset,
                exemption_code, reviewer_id, status, origin,
                source_detection_id, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_type, source_id, start_offset, end_offset,
                exemption_code, reviewer_id, status, origin,
                source_detection_id, notes, now, now,
            ),
        )
    except sqlite3.IntegrityError as e:
        # Duplicate (source_type, source_id, start, end, exemption_code).
        raise RedactionError(f"duplicate redaction: {e}") from e
    conn.commit()
    return get_redaction(conn, int(cur.lastrowid))  # type: ignore[arg-type]


def get_redaction(conn: sqlite3.Connection, redaction_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM redactions WHERE id = ?", (redaction_id,)
    ).fetchone()
    if row is None:
        raise RedactionError(f"redaction {redaction_id} not found")
    return _row_to_dict(row)


def update_redaction(
    conn: sqlite3.Connection,
    district: DistrictConfig,
    redaction_id: int,
    *,
    status: str | None = None,
    exemption_code: str | None = None,
    reviewer_id: str | None = None,
    notes: str | None = None,
) -> dict:
    cur = get_redaction(conn, redaction_id)
    new_status = status if status is not None else cur["status"]
    new_exempt = exemption_code if exemption_code is not None else cur["exemption_code"]
    new_reviewer = reviewer_id if reviewer_id is not None else cur["reviewer_id"]

    if new_status not in VALID_STATUSES:
        raise RedactionError(f"invalid status {new_status!r}")
    if not district.is_known_exemption(new_exempt):
        raise RedactionError(
            f"exemption_code {new_exempt!r} is not in the configured list"
        )
    if new_status in ("accepted", "rejected") and not new_reviewer:
        raise RedactionError(
            f"reviewer_id is required to set status={new_status!r}"
        )

    sets: list[str] = []
    params: list = []
    if status is not None:
        sets.append("status = ?"); params.append(new_status)
    if exemption_code is not None:
        sets.append("exemption_code = ?"); params.append(new_exempt)
    if reviewer_id is not None:
        sets.append("reviewer_id = ?"); params.append(new_reviewer)
    if notes is not None:
        sets.append("notes = ?"); params.append(notes)

    if not sets:
        return cur

    sets.append("updated_at = ?")
    params.append(_now())
    params.append(redaction_id)

    conn.execute(
        f"UPDATE redactions SET {', '.join(sets)} WHERE id = ?", params
    )
    conn.commit()
    return get_redaction(conn, redaction_id)


def delete_redaction(conn: sqlite3.Connection, redaction_id: int) -> None:
    cur = conn.execute(
        "DELETE FROM redactions WHERE id = ?", (redaction_id,)
    )
    if cur.rowcount == 0:
        raise RedactionError(f"redaction {redaction_id} not found")
    conn.commit()


def list_redactions(
    conn: sqlite3.Connection,
    *,
    source_type: str | None = None,
    source_id: int | None = None,
    status: str | None = None,
    origin: str | None = None,
    exemption_code: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict], int]:
    where: list[str] = []
    params: list = []
    if source_type:
        where.append("source_type = ?"); params.append(source_type)
    if source_id is not None:
        where.append("source_id = ?"); params.append(source_id)
    if status:
        where.append("status = ?"); params.append(status)
    if origin:
        where.append("origin = ?"); params.append(origin)
    if exemption_code:
        where.append("exemption_code = ?"); params.append(exemption_code)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute(
        f"SELECT COUNT(*) FROM redactions{clause}", params
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM redactions{clause} "
        "ORDER BY source_type, source_id, start_offset, id "
        "LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return [_row_to_dict(r) for r in rows], int(total)


# ---------------------------------------------------------------------------
# Auto-propose from PII detections
# ---------------------------------------------------------------------------


@dataclass
class ProposeStats:
    detections_seen: int = 0
    proposed: int = 0
    skipped_existing: int = 0
    skipped_no_exemption: int = 0
    skipped_invalid: int = 0
    by_entity: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.by_entity is None:
            self.by_entity = {}

    def as_dict(self) -> dict:
        return {
            "detections_seen": self.detections_seen,
            "proposed": self.proposed,
            "skipped_existing": self.skipped_existing,
            "skipped_no_exemption": self.skipped_no_exemption,
            "skipped_invalid": self.skipped_invalid,
            "by_entity": dict(sorted(self.by_entity.items())),
        }


def propose_from_detections(
    conn: sqlite3.Connection,
    district: DistrictConfig,
    *,
    only_email_id: int | None = None,
    only_attachment_id: int | None = None,
    min_score: float | None = None,
) -> ProposeStats:
    """Auto-create ``status='proposed'`` redactions for every PII detection.

    Idempotent — the unique index on
    ``(source_type, source_id, start_offset, end_offset, exemption_code)``
    suppresses duplicates so re-running adds only the delta.
    """
    stats = ProposeStats()

    sql = "SELECT * FROM pii_detections"
    where: list[str] = []
    params: list = []
    if only_email_id is not None:
        where.append("source_type LIKE 'email_%' AND source_id = ?")
        params.append(only_email_id)
    if only_attachment_id is not None:
        where.append("source_type = 'attachment_text' AND source_id = ?")
        params.append(only_attachment_id)
    if min_score is not None:
        where.append("score >= ?")
        params.append(min_score)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id"

    for det in conn.execute(sql, params):
        stats.detections_seen += 1
        ent = det["entity_type"]
        exemption = district.exemption_for_entity(ent)
        if not exemption:
            stats.skipped_no_exemption += 1
            continue
        if not district.is_known_exemption(exemption):
            stats.skipped_no_exemption += 1
            continue

        try:
            create_redaction(
                conn, district,
                source_type=det["source_type"],
                source_id=int(det["source_id"]),
                start_offset=int(det["start_offset"]),
                end_offset=int(det["end_offset"]),
                exemption_code=exemption,
                status="proposed",
                origin="auto",
                source_detection_id=int(det["id"]),
            )
        except RedactionError as e:
            msg = str(e).lower()
            if "duplicate" in msg:
                stats.skipped_existing += 1
            else:
                log.warning(
                    "skip detection id=%s: %s", det["id"], e
                )
                stats.skipped_invalid += 1
            continue
        stats.proposed += 1
        stats.by_entity[ent] = stats.by_entity.get(ent, 0) + 1

    return stats


__all__ = [
    "ProposeStats",
    "RedactionError",
    "SOURCE_ATTACHMENT_TEXT",
    "SOURCE_EMAIL_BODY_HTML",
    "SOURCE_EMAIL_BODY_TEXT",
    "SOURCE_EMAIL_SUBJECT",
    "SpanLookup",
    "VALID_SOURCE_TYPES",
    "VALID_STATUSES",
    "create_redaction",
    "delete_redaction",
    "get_redaction",
    "get_source_text",
    "list_redactions",
    "propose_from_detections",
    "update_redaction",
    "validate_new_redaction",
]
