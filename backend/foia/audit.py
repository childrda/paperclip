"""Phase 9 — append-only audit log.

Centralised logging API for every write across the system. All hooks
flow through :func:`log_event`, which simply inserts into
``audit_log``. The DB-level triggers in :mod:`foia.schema` reject any
``UPDATE`` or ``DELETE`` on that table, so a tampered row can be
proven against the rest of the trail.

Actor resolution
----------------
* CLIs read from ``--actor`` first, then ``FOIA_ACTOR`` env var, then
  ``cli:{username}``.
* API requests read the ``X-FOIA-Reviewer`` header (the same value the
  Phase 7 UI already keeps in ``localStorage``); a missing header
  becomes ``api:anonymous``.
* Internal callers (background jobs, future schedulers) should pass
  an explicit ``actor`` string and ``origin='system'``.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event-type taxonomy. Keep in sync when new hooks are added.
# ---------------------------------------------------------------------------

EVT_INGEST_RUN = "ingest.run"
EVT_EXTRACT_RUN = "extract.run"
EVT_DETECTION_RUN = "detection.run"
EVT_RESOLVE_RUN = "resolve.run"
EVT_RESOLVE_MERGE = "resolve.merge"
EVT_RESOLVE_RENAME = "resolve.rename"
EVT_RESOLVE_NOTE = "resolve.note"
EVT_REDACTION_PROPOSE = "redaction.propose"
EVT_REDACTION_CREATE = "redaction.create"
EVT_REDACTION_UPDATE = "redaction.update"
EVT_REDACTION_DELETE = "redaction.delete"
EVT_EXPORT_RUN = "export.run"


# ---------------------------------------------------------------------------
# Actor helpers
# ---------------------------------------------------------------------------


def resolve_actor(args: argparse.Namespace | None, *, prefix: str = "cli") -> str:
    """Resolve an actor string for a CLI invocation."""
    arg = getattr(args, "actor", None) if args else None
    if arg:
        return str(arg)
    env = os.environ.get("FOIA_ACTOR")
    if env:
        return env
    try:
        return f"{prefix}:{getpass.getuser()}"
    except Exception:
        return prefix


def add_actor_arg(parser: argparse.ArgumentParser) -> None:
    """Attach a ``--actor`` flag to a CLI parser."""
    parser.add_argument(
        "--actor",
        default=None,
        help=(
            "Audit-log actor for this run. Defaults to FOIA_ACTOR env var, "
            "then 'cli:{username}'."
        ),
    )


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def log_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    actor: str,
    source_type: str | None = None,
    source_id: int | None = None,
    payload: dict[str, Any] | None = None,
    origin: str = "cli",
    user_id: int | None = None,
) -> int:
    """Append one row to ``audit_log``. Returns the new row id.

    ``user_id`` ties the event to the local mirror of the directory user
    (Phase auth). Legacy callers without an authenticated session
    leave it None and the row falls back to the free-text ``actor``
    column for attribution.
    """
    if origin not in ("cli", "api", "system"):
        raise ValueError(f"origin must be cli|api|system; got {origin!r}")
    payload_json = (
        json.dumps(payload, default=str, ensure_ascii=False)
        if payload is not None
        else None
    )
    cur = conn.execute(
        """
        INSERT INTO audit_log (
            event_at, event_type, actor, user_id,
            source_type, source_id,
            payload_json, request_origin
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            event_type, actor or "system", user_id,
            source_type, source_id,
            payload_json, origin,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def query_events(
    conn: sqlite3.Connection,
    *,
    event_type: str | None = None,
    actor: str | None = None,
    source_type: str | None = None,
    source_id: int | None = None,
    after: str | None = None,
    before: str | None = None,
    origin: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Query the audit log with optional filters; newest first."""
    where: list[str] = []
    params: list[Any] = []
    if event_type:
        where.append("event_type = ?")
        params.append(event_type)
    if actor:
        where.append("actor = ?")
        params.append(actor)
    if source_type:
        where.append("source_type = ?")
        params.append(source_type)
    if source_id is not None:
        where.append("source_id = ?")
        params.append(source_id)
    if after:
        where.append("event_at > ?")
        params.append(after)
    if before:
        where.append("event_at < ?")
        params.append(before)
    if origin:
        where.append("request_origin = ?")
        params.append(origin)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute(
        f"SELECT COUNT(*) FROM audit_log{clause}", params,
    ).fetchone()[0]

    rows = conn.execute(
        f"""
        SELECT * FROM audit_log
        {clause}
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        d = {k: r[k] for k in r.keys()}
        if d.get("payload_json"):
            try:
                d["payload"] = json.loads(d["payload_json"])
            except json.JSONDecodeError:
                d["payload"] = None
        else:
            d["payload"] = None
        out.append(d)
    return out, int(total)


__all__ = [
    "EVT_DETECTION_RUN",
    "EVT_EXPORT_RUN",
    "EVT_EXTRACT_RUN",
    "EVT_INGEST_RUN",
    "EVT_REDACTION_CREATE",
    "EVT_REDACTION_DELETE",
    "EVT_REDACTION_PROPOSE",
    "EVT_REDACTION_UPDATE",
    "EVT_RESOLVE_MERGE",
    "EVT_RESOLVE_NOTE",
    "EVT_RESOLVE_RENAME",
    "EVT_RESOLVE_RUN",
    "add_actor_arg",
    "log_event",
    "query_events",
    "resolve_actor",
]
