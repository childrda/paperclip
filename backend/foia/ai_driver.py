"""Phase 10 — DB-backed AI QA driver.

Walks every text source, runs the configured :class:`AiProvider`, and
persists the resulting flags into ``ai_flags``. Promotion of a flag to
a real redaction is the *only* path from AI output into the redaction
table, and it's a manual action that must be initiated by a human via
:func:`promote_flag` (CLI or API).
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .ai import AiFlag, AiProvider, AiProviderError
from .district import DistrictConfig
from .redaction import (
    RedactionError,
    create_redaction,
)
from .sanitizer import html_to_text

log = logging.getLogger(__name__)


SOURCE_EMAIL_SUBJECT = "email_subject"
SOURCE_EMAIL_BODY_TEXT = "email_body_text"
SOURCE_EMAIL_BODY_HTML = "email_body_html"
SOURCE_ATTACHMENT_TEXT = "attachment_text"


@dataclass
class AiQaStats:
    qa_run_id: str
    sources_scanned: int = 0
    sources_skipped: int = 0
    sources_failed: int = 0
    flags_written: int = 0
    flags_skipped_existing: int = 0
    by_entity: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.by_entity is None:
            self.by_entity = {}

    def as_dict(self) -> dict:
        return {
            "qa_run_id": self.qa_run_id,
            "sources_scanned": self.sources_scanned,
            "sources_skipped": self.sources_skipped,
            "sources_failed": self.sources_failed,
            "flags_written": self.flags_written,
            "flags_skipped_existing": self.flags_skipped_existing,
            "by_entity": dict(sorted(self.by_entity.items())),
        }


class AiFlagError(Exception):
    """Raised when a flag operation cannot proceed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _store_flag(
    conn: sqlite3.Connection,
    flag: AiFlag,
    *,
    source_type: str,
    source_id: int,
    provider_name: str,
    provider_model: str | None,
    qa_run_id: str,
    now: str,
) -> bool:
    """Returns True if newly inserted, False on UNIQUE conflict."""
    try:
        conn.execute(
            """
            INSERT INTO ai_flags (
                source_type, source_id, entity_type,
                start_offset, end_offset, matched_text,
                confidence, rationale, suggested_exemption,
                provider, model, qa_run_id, flagged_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_type, source_id, flag.entity_type,
                flag.start, flag.end, flag.matched_text,
                flag.confidence, flag.rationale, flag.suggested_exemption,
                provider_name, provider_model, qa_run_id, now,
            ),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def _iter_email_sources(
    conn: sqlite3.Connection, only_email_id: int | None,
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
            yield SOURCE_EMAIL_BODY_HTML, eid, html_to_text(row["body_html_sanitized"])


def _iter_attachment_sources(
    conn: sqlite3.Connection, only_attachment_id: int | None,
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


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_ai_qa(
    conn: sqlite3.Connection,
    provider: AiProvider,
    *,
    only_email_id: int | None = None,
    only_attachment_id: int | None = None,
) -> AiQaStats:
    """Scan in-scope text with the provider; persist new flags.

    Idempotent: repeats of the same (source, span, entity, provider)
    are reported as ``flags_skipped_existing`` thanks to the UNIQUE
    index.
    """
    stats = AiQaStats(qa_run_id=uuid.uuid4().hex)
    now = _now()

    iters: list[Iterable[tuple[str, int, str]]] = []
    if only_attachment_id is None:
        iters.append(_iter_email_sources(conn, only_email_id))
    if only_email_id is None:
        iters.append(_iter_attachment_sources(conn, only_attachment_id))

    for it in iters:
        for source_type, source_id, text in it:
            if not text or not text.strip():
                stats.sources_skipped += 1
                continue
            try:
                flags = provider.flag_risks(text)
            except AiProviderError:
                log.exception(
                    "AI provider failed on %s id=%s", source_type, source_id,
                )
                stats.sources_failed += 1
                continue
            except Exception:  # defence in depth
                log.exception(
                    "unexpected provider error on %s id=%s", source_type, source_id,
                )
                stats.sources_failed += 1
                continue

            stats.sources_scanned += 1
            for flag in flags:
                inserted = _store_flag(
                    conn, flag,
                    source_type=source_type,
                    source_id=source_id,
                    provider_name=provider.name,
                    provider_model=provider.model,
                    qa_run_id=stats.qa_run_id,
                    now=now,
                )
                if inserted:
                    stats.flags_written += 1
                    stats.by_entity[flag.entity_type] = (
                        stats.by_entity.get(flag.entity_type, 0) + 1
                    )
                else:
                    stats.flags_skipped_existing += 1
            conn.commit()

    return stats


# ---------------------------------------------------------------------------
# Read / list / show
# ---------------------------------------------------------------------------


def list_flags(
    conn: sqlite3.Connection,
    *,
    review_status: str | None = None,
    source_type: str | None = None,
    source_id: int | None = None,
    entity_type: str | None = None,
    provider: str | None = None,
    qa_run_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict], int]:
    where: list[str] = []
    params: list = []
    if review_status:
        where.append("review_status = ?"); params.append(review_status)
    if source_type:
        where.append("source_type = ?"); params.append(source_type)
    if source_id is not None:
        where.append("source_id = ?"); params.append(source_id)
    if entity_type:
        where.append("entity_type = ?"); params.append(entity_type)
    if provider:
        where.append("provider = ?"); params.append(provider)
    if qa_run_id:
        where.append("qa_run_id = ?"); params.append(qa_run_id)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute(
        f"SELECT COUNT(*) FROM ai_flags{clause}", params,
    ).fetchone()[0]
    rows = conn.execute(
        f"""
        SELECT * FROM ai_flags
        {clause}
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    return [_row_to_dict(r) for r in rows], int(total)


def get_flag(conn: sqlite3.Connection, flag_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM ai_flags WHERE id = ?", (flag_id,),
    ).fetchone()
    if row is None:
        raise AiFlagError(f"ai_flag {flag_id} not found")
    return _row_to_dict(row)


# ---------------------------------------------------------------------------
# Review actions: dismiss / promote
# ---------------------------------------------------------------------------


def dismiss_flag(
    conn: sqlite3.Connection,
    flag_id: int,
    *,
    actor: str,
    note: str | None = None,
) -> dict:
    flag = get_flag(conn, flag_id)
    if flag["review_status"] == "promoted":
        raise AiFlagError(
            f"ai_flag {flag_id} has already been promoted; cannot dismiss"
        )
    now = _now()
    conn.execute(
        """
        UPDATE ai_flags
        SET review_status = 'dismissed',
            review_actor = ?, reviewed_at = ?, review_note = ?
        WHERE id = ?
        """,
        (actor, now, note, flag_id),
    )
    conn.commit()
    return get_flag(conn, flag_id)


def promote_flag(
    conn: sqlite3.Connection,
    district: DistrictConfig,
    flag_id: int,
    *,
    actor: str,
    exemption_code: str | None = None,
    note: str | None = None,
) -> dict:
    """Create a *proposed* redaction from this AI flag.

    The created redaction is intentionally ``status='proposed'`` — the
    spec's "AI never auto-redacts" rule means a human must still
    Accept (or Reject) it through the normal Phase 6 flow. Promoting
    an AI flag is itself a human action; auto-acceptance is not.
    """
    flag = get_flag(conn, flag_id)
    if flag["review_status"] == "promoted":
        raise AiFlagError(f"ai_flag {flag_id} has already been promoted")
    if flag["review_status"] == "dismissed":
        raise AiFlagError(
            f"ai_flag {flag_id} was dismissed; un-dismiss before promoting"
        )

    chosen = exemption_code or flag["suggested_exemption"]
    if not chosen:
        # Fall back to district default mapping by entity type, then to
        # district.redaction.default_exemption.
        chosen = district.exemption_for_entity(flag["entity_type"])
    if not chosen:
        raise AiFlagError(
            "no exemption_code provided, no suggestion on the flag, "
            "and no district default for this entity_type"
        )
    if not district.is_known_exemption(chosen):
        raise AiFlagError(
            f"exemption_code {chosen!r} is not configured for this district"
        )

    try:
        red = create_redaction(
            conn, district,
            source_type=flag["source_type"],
            source_id=int(flag["source_id"]),
            start_offset=int(flag["start_offset"]),
            end_offset=int(flag["end_offset"]),
            exemption_code=chosen,
            status="proposed",
            origin="manual",        # AI never auto-redacts; human did this.
            notes=note,
        )
    except RedactionError as e:
        # Surface as AiFlagError so the CLI/API can return a single error type.
        raise AiFlagError(f"could not create redaction: {e}") from e

    now = _now()
    conn.execute(
        """
        UPDATE ai_flags
        SET review_status = 'promoted',
            review_actor = ?, reviewed_at = ?,
            review_note = COALESCE(?, review_note),
            promoted_redaction_id = ?
        WHERE id = ?
        """,
        (actor, now, note, int(red["id"]), flag_id),
    )
    conn.commit()
    return {**get_flag(conn, flag_id), "redaction": red}


__all__ = [
    "AiFlagError",
    "AiQaStats",
    "SOURCE_ATTACHMENT_TEXT",
    "SOURCE_EMAIL_BODY_HTML",
    "SOURCE_EMAIL_BODY_TEXT",
    "SOURCE_EMAIL_SUBJECT",
    "dismiss_flag",
    "get_flag",
    "list_flags",
    "promote_flag",
    "run_ai_qa",
]
