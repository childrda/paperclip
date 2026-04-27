"""Case management.

A *case* is the top-level grouping for one FOIA production: one or more
mailbox uploads, the resulting redactions, and ultimately the produced
PDF. The Bates prefix lives on the case (so productions for separate
matters get separate numbering) and the status drives the UI.

Pipeline jobs (the import workflow) live in this module too because
they're keyed to a case.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


VALID_STATUSES = ("processing", "ready", "failed", "exported", "archived")


@dataclass(frozen=True)
class Case:
    id: int
    name: str
    bates_prefix: str
    status: str
    created_by: int | None
    created_at: str
    updated_at: str
    error_message: str | None
    failed_stage: str | None


class CaseError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_case(row: sqlite3.Row) -> Case:
    return Case(
        id=int(row["id"]),
        name=row["name"],
        bates_prefix=row["bates_prefix"],
        status=row["status"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        error_message=row["error_message"],
        failed_stage=row["failed_stage"],
    )


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def create_case(
    conn: sqlite3.Connection,
    *,
    name: str,
    bates_prefix: str,
    created_by_user_id: int | None,
    status: str = "processing",
) -> Case:
    name = (name or "").strip()
    if not name:
        raise CaseError("name is required")
    bates_prefix = (bates_prefix or "").strip()
    if not bates_prefix:
        raise CaseError("bates_prefix is required")
    if status not in VALID_STATUSES:
        raise CaseError(f"invalid status {status!r}")
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO cases (
            name, bates_prefix, status, created_by, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (name, bates_prefix, status, created_by_user_id, now, now),
    )
    conn.commit()
    return get_case(conn, int(cur.lastrowid))  # type: ignore[arg-type]


def get_case(conn: sqlite3.Connection, case_id: int) -> Case:
    row = conn.execute(
        "SELECT * FROM cases WHERE id = ?", (case_id,)
    ).fetchone()
    if row is None:
        raise CaseError(f"case {case_id} not found")
    return _row_to_case(row)


def list_cases(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[Case], int]:
    where = ""
    params: list[Any] = []
    if status:
        where = " WHERE status = ?"
        params.append(status)
    total = int(conn.execute(
        f"SELECT COUNT(*) FROM cases{where}", params,
    ).fetchone()[0])
    rows = conn.execute(
        f"SELECT * FROM cases{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return [_row_to_case(r) for r in rows], total


def update_case_status(
    conn: sqlite3.Connection,
    case_id: int,
    *,
    status: str,
    error_message: str | None = None,
    failed_stage: str | None = None,
) -> Case:
    if status not in VALID_STATUSES:
        raise CaseError(f"invalid status {status!r}")
    conn.execute(
        """
        UPDATE cases
        SET status = ?, error_message = ?, failed_stage = ?, updated_at = ?
        WHERE id = ?
        """,
        (status, error_message, failed_stage, _now(), case_id),
    )
    conn.commit()
    return get_case(conn, case_id)


def case_stats(conn: sqlite3.Connection, case_id: int) -> dict[str, int]:
    """Counts to render on the case detail page."""
    emails = int(conn.execute(
        "SELECT COUNT(*) FROM emails WHERE case_id = ?", (case_id,)
    ).fetchone()[0])
    attachments = int(conn.execute(
        "SELECT COUNT(*) FROM attachments a "
        "JOIN emails e ON e.id = a.email_id "
        "WHERE e.case_id = ?",
        (case_id,),
    ).fetchone()[0])
    pii = int(conn.execute(
        "SELECT COUNT(*) FROM pii_detections p "
        "JOIN emails e ON ("
        "    p.source_type LIKE 'email_%' AND p.source_id = e.id"
        ") "
        "WHERE e.case_id = ?",
        (case_id,),
    ).fetchone()[0])
    redactions = int(conn.execute(
        "SELECT COUNT(*) FROM redactions r "
        "JOIN emails e ON ("
        "    r.source_type LIKE 'email_%' AND r.source_id = e.id"
        ") "
        "WHERE e.case_id = ?",
        (case_id,),
    ).fetchone()[0])
    accepted = int(conn.execute(
        "SELECT COUNT(*) FROM redactions r "
        "JOIN emails e ON ("
        "    r.source_type LIKE 'email_%' AND r.source_id = e.id"
        ") "
        "WHERE e.case_id = ? AND r.status = 'accepted'",
        (case_id,),
    ).fetchone()[0])
    return {
        "emails": emails,
        "attachments": attachments,
        "pii_detections": pii,
        "redactions": redactions,
        "redactions_accepted": accepted,
    }


# ---------------------------------------------------------------------------
# Pipeline jobs
# ---------------------------------------------------------------------------


def create_job(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    started_by_user_id: int | None,
    upload_path: str | None,
    label: str | None,
    propose_redactions: bool,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO pipeline_jobs (
            case_id, started_by, upload_path, label,
            propose_redactions, status, created_at
        ) VALUES (?, ?, ?, ?, ?, 'queued', ?)
        """,
        (
            case_id, started_by_user_id, upload_path, label,
            1 if propose_redactions else 0, _now(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_job(conn: sqlite3.Connection, job_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM pipeline_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    return {k: row[k] for k in row.keys()} if row else None


def list_jobs(
    conn: sqlite3.Connection, *, case_id: int | None = None, limit: int = 50,
) -> list[dict]:
    sql = "SELECT * FROM pipeline_jobs"
    params: list = []
    if case_id is not None:
        sql += " WHERE case_id = ?"
        params.append(case_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [
        {k: r[k] for k in r.keys()}
        for r in conn.execute(sql, params).fetchall()
    ]


def update_job_status(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    status: str,
    current_stage: str | None = None,
    error_message: str | None = None,
    failed_stage: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> None:
    sets: list[str] = ["status = ?"]
    params: list[Any] = [status]
    for col, val in (
        ("current_stage", current_stage),
        ("error_message", error_message),
        ("failed_stage", failed_stage),
        ("started_at", started_at),
        ("finished_at", finished_at),
    ):
        if val is not None:
            sets.append(f"{col} = ?")
            params.append(val)
    params.append(job_id)
    conn.execute(
        f"UPDATE pipeline_jobs SET {', '.join(sets)} WHERE id = ?",
        params,
    )
    conn.commit()


def emit_event(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    stage: str,
    kind: str,
    message: str | None = None,
    payload: dict | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO pipeline_events (
            job_id, stage, kind, message, payload_json, event_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            job_id, stage, kind, message,
            json.dumps(payload, default=str) if payload is not None else None,
            _now(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_events(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    since_id: int = 0,
) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM pipeline_events "
        "WHERE job_id = ? AND id > ? ORDER BY id",
        (job_id, since_id),
    ).fetchall()
    out: list[dict] = []
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
    return out


__all__ = [
    "Case",
    "CaseError",
    "case_stats",
    "create_case",
    "create_job",
    "emit_event",
    "get_case",
    "get_job",
    "list_cases",
    "list_events",
    "list_jobs",
    "update_case_status",
    "update_job_status",
]
