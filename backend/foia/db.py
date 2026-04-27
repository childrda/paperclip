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
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_load_schema())
    _backfill_fts(conn)
    conn.commit()


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
