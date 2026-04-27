"""SQLite connection helpers and schema initialization."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Iterator


def _load_schema() -> str:
    return resources.files("foia").joinpath("schema.sql").read_text(encoding="utf-8")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # ``check_same_thread=False`` lets the SSE streaming endpoint open
    # the same DB from a worker thread while a background pipeline
    # writer holds another connection on the worker pool. Each caller
    # still owns its own ``Connection`` object; we never share one
    # connection across threads.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_load_schema())
    _migrate_legacy_columns(conn)
    _backfill_fts(conn)
    conn.commit()


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_legacy_columns(conn: sqlite3.Connection) -> None:
    """Add columns that schema.sql can't add to pre-existing tables.

    SQLite's ``CREATE TABLE IF NOT EXISTS`` is a no-op when the table is
    already present, so a column added to schema.sql later won't appear
    on databases created before the change. This function reads
    ``PRAGMA table_info`` and applies the missing ALTERs imperatively.
    Each branch is idempotent.
    """
    if "case_id" not in _column_names(conn, "emails"):
        conn.execute(
            "ALTER TABLE emails ADD COLUMN case_id INTEGER"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_emails_case ON emails(case_id)"
        )
    if "user_id" not in _column_names(conn, "audit_log"):
        conn.execute(
            "ALTER TABLE audit_log ADD COLUMN user_id INTEGER"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id)"
        )


def _backfill_fts(conn: sqlite3.Connection) -> None:
    """Populate the FTS tables for rows inserted before Phase 5.

    Triggers keep new inserts in sync; this one-time backfill covers
    upgrades from earlier-phase databases. Idempotent: already-indexed
    rowids are skipped via a LEFT JOIN.
    """
    conn.execute(
        """
        INSERT INTO emails_fts (rowid, subject, body_text)
        SELECT e.id, COALESCE(e.subject, ''), COALESCE(e.body_text, '')
        FROM emails e
        LEFT JOIN emails_fts f ON f.rowid = e.id
        WHERE f.rowid IS NULL
        """
    )
    conn.execute(
        """
        INSERT INTO attachments_fts (rowid, filename, extracted_text)
        SELECT
            t.attachment_id,
            COALESCE(a.filename, ''),
            COALESCE(t.extracted_text, '')
        FROM attachments_text t
        JOIN attachments a ON a.id = t.attachment_id
        LEFT JOIN attachments_fts f ON f.rowid = t.attachment_id
        WHERE f.rowid IS NULL
        """
    )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
