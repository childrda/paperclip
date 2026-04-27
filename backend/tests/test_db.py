from __future__ import annotations


def test_schema_creates_expected_tables(db_conn):
    rows = db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {r["name"] for r in rows}
    assert {"emails", "attachments", "raw_content"}.issubset(names)


def test_foreign_keys_enabled(db_conn):
    fk = db_conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1


def test_emails_unique_source_index(db_conn):
    db_conn.execute(
        "INSERT INTO emails (mbox_source, mbox_index, ingested_at) VALUES (?, ?, ?)",
        ("a.mbox", 0, "2026-01-01T00:00:00+00:00"),
    )
    import sqlite3
    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "INSERT INTO emails (mbox_source, mbox_index, ingested_at) VALUES (?, ?, ?)",
            ("a.mbox", 0, "2026-01-01T00:00:00+00:00"),
        )


def test_cascade_delete_removes_attachments_and_raw(db_conn):
    cur = db_conn.execute(
        "INSERT INTO emails (mbox_source, mbox_index, ingested_at) VALUES (?, ?, ?)",
        ("a.mbox", 0, "2026-01-01T00:00:00+00:00"),
    )
    eid = cur.lastrowid
    db_conn.execute(
        "INSERT INTO attachments (email_id, size_bytes, sha256, storage_path) "
        "VALUES (?, 0, 'x', 'p')",
        (eid,),
    )
    db_conn.execute(
        "INSERT INTO raw_content (email_id, raw_rfc822, raw_sha256) VALUES (?, ?, ?)",
        (eid, b"abc", "y"),
    )
    db_conn.execute("DELETE FROM emails WHERE id = ?", (eid,))
    db_conn.commit()
    assert db_conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
    assert db_conn.execute("SELECT COUNT(*) FROM raw_content").fetchone()[0] == 0
