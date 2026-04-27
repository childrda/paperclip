"""Integration tests for the detection driver against a real SQLite DB."""

from __future__ import annotations

from datetime import datetime, timezone

from foia.detection import PiiDetector
from foia.detection_driver import (
    SOURCE_ATTACHMENT_TEXT,
    SOURCE_EMAIL_BODY_TEXT,
    SOURCE_EMAIL_SUBJECT,
    run_detection,
)
from foia.district import (
    CustomRecognizerSpec,
    PatternSpec,
    PiiDetectionConfig,
)


def _insert_email(conn, *, subject: str, body: str) -> int:
    # mbox_index is UNIQUE per mbox_source; use the next available integer
    # so multiple emails can share the fixture source.
    next_idx = (
        conn.execute(
            "SELECT COALESCE(MAX(mbox_index), -1) + 1 FROM emails "
            "WHERE mbox_source = ?",
            ("test.mbox",),
        ).fetchone()[0]
    )
    cur = conn.execute(
        """
        INSERT INTO emails (mbox_source, mbox_index, subject, body_text,
                            body_html_sanitized, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "test.mbox", int(next_idx), subject, body, "",
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return int(cur.lastrowid)


def _insert_attachment_with_text(conn, text: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO emails (mbox_source, mbox_index, subject, ingested_at)
        VALUES (?, ?, ?, ?)
        """,
        ("t.mbox", 100, None, datetime.now(timezone.utc).isoformat()),
    )
    email_id = int(cur.lastrowid)
    cur = conn.execute(
        """
        INSERT INTO attachments (email_id, size_bytes, sha256, storage_path)
        VALUES (?, 1, 'sha', 'p')
        """,
        (email_id,),
    )
    aid = int(cur.lastrowid)
    conn.execute(
        """
        INSERT INTO attachments_text (
            attachment_id, extracted_text, extraction_method,
            character_count, extraction_status, extracted_at
        ) VALUES (?, ?, 'text', ?, 'ok', ?)
        """,
        (aid, text, len(text), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return aid


def _detector() -> PiiDetector:
    cfg = PiiDetectionConfig(
        builtins=("US_SSN", "EMAIL_ADDRESS"),
        min_score=0.3,
        custom_recognizers=(
            CustomRecognizerSpec(
                name="Student ID",
                entity_type="STUDENT_ID",
                patterns=(PatternSpec(regex=r"\b\d{8}\b", score=0.7),),
            ),
        ),
    )
    return PiiDetector(cfg)


def test_scans_email_subject_and_body(db_conn):
    eid = _insert_email(
        db_conn,
        subject="RE: jane@example.com urgent",
        body="Student 12345678 — SSN 572-68-1439.",
    )
    db_conn.commit()
    stats = run_detection(db_conn, _detector())
    assert stats.sources_scanned >= 2
    rows = db_conn.execute(
        "SELECT source_type, entity_type, matched_text "
        "FROM pii_detections WHERE source_id = ? ORDER BY source_type, start_offset",
        (eid,),
    ).fetchall()
    pairs = [(r["source_type"], r["entity_type"]) for r in rows]
    assert (SOURCE_EMAIL_SUBJECT, "EMAIL_ADDRESS") in pairs
    assert (SOURCE_EMAIL_BODY_TEXT, "STUDENT_ID") in pairs
    assert (SOURCE_EMAIL_BODY_TEXT, "US_SSN") in pairs


def test_scans_attachment_text(db_conn):
    aid = _insert_attachment_with_text(
        db_conn,
        "Case notes: contact parent via teacher@school.k12.va.us. SID 87654321.",
    )
    run_detection(db_conn, _detector())
    rows = db_conn.execute(
        "SELECT entity_type FROM pii_detections "
        "WHERE source_type = ? AND source_id = ?",
        (SOURCE_ATTACHMENT_TEXT, aid),
    ).fetchall()
    kinds = {r["entity_type"] for r in rows}
    assert "EMAIL_ADDRESS" in kinds
    assert "STUDENT_ID" in kinds


def test_is_idempotent(db_conn):
    eid = _insert_email(db_conn, subject="", body="ping 572-68-1439")
    db_conn.commit()
    run_detection(db_conn, _detector())
    first = db_conn.execute(
        "SELECT COUNT(*) FROM pii_detections WHERE source_id = ?", (eid,)
    ).fetchone()[0]
    run_detection(db_conn, _detector())
    second = db_conn.execute(
        "SELECT COUNT(*) FROM pii_detections WHERE source_id = ?", (eid,)
    ).fetchone()[0]
    assert first == second
    assert first >= 1


def test_only_email_id_restricts_scope(db_conn):
    a = _insert_email(db_conn, subject="a@x.com", body="")
    b = _insert_email(db_conn, subject="b@y.com", body="")
    db_conn.commit()
    run_detection(db_conn, _detector(), only_email_id=a)
    rows_a = db_conn.execute(
        "SELECT COUNT(*) FROM pii_detections WHERE source_id = ?", (a,)
    ).fetchone()[0]
    rows_b = db_conn.execute(
        "SELECT COUNT(*) FROM pii_detections WHERE source_id = ?", (b,)
    ).fetchone()[0]
    assert rows_a >= 1
    assert rows_b == 0


def test_only_attachment_id_excludes_emails(db_conn):
    eid = _insert_email(db_conn, subject="ignore@me.com", body="")
    aid = _insert_attachment_with_text(db_conn, "touch.this@example.com")
    db_conn.commit()
    run_detection(db_conn, _detector(), only_attachment_id=aid)
    assert db_conn.execute(
        "SELECT COUNT(*) FROM pii_detections WHERE source_id = ? AND source_type = 'email_subject'",
        (eid,),
    ).fetchone()[0] == 0
    assert db_conn.execute(
        "SELECT COUNT(*) FROM pii_detections WHERE source_id = ? AND source_type = 'attachment_text'",
        (aid,),
    ).fetchone()[0] >= 1


def test_empty_sources_are_skipped(db_conn):
    eid = _insert_email(db_conn, subject="", body="")
    db_conn.commit()
    stats = run_detection(db_conn, _detector(), only_email_id=eid)
    assert stats.sources_scanned == 0
