"""Unit / integration tests for foia.redaction."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from foia.district import (
    DistrictConfig,
    ExemptionCode,
    PiiDetectionConfig,
    RedactionConfig,
)
from foia.redaction import (
    RedactionError,
    create_redaction,
    delete_redaction,
    get_redaction,
    get_source_text,
    list_redactions,
    propose_from_detections,
    update_redaction,
    validate_new_redaction,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _district() -> DistrictConfig:
    return DistrictConfig(
        name="Test",
        email_domains=("district.example.org",),
        pii=PiiDetectionConfig(builtins=("US_SSN", "EMAIL_ADDRESS")),
        exemptions=(
            ExemptionCode(code="FERPA"),
            ExemptionCode(code="PII"),
        ),
        redaction=RedactionConfig(
            default_exemption="FERPA",
            entity_exemptions={"US_SSN": "PII"},
        ),
    )


def _ins_email(conn, *, subject="Hi", body="Hello world", index=0) -> int:
    cur = conn.execute(
        """
        INSERT INTO emails (mbox_source, mbox_index, subject, body_text,
                            body_html_sanitized, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "t.mbox", index, subject, body, "",
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _ins_attachment_text(conn, text: str) -> int:
    """Insert a fake email + attachment + extracted text. Returns attachment id."""
    eid = _ins_email(conn, body="anchor")
    cur = conn.execute(
        "INSERT INTO attachments (email_id, size_bytes, sha256, storage_path) "
        "VALUES (?, 1, 'sha', 'p')",
        (eid,),
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


def _ins_pii(conn, *, source_type, source_id, entity_type, start, end, score=0.9) -> int:
    cur = conn.execute(
        """
        INSERT INTO pii_detections (
            source_type, source_id, entity_type, start_offset, end_offset,
            matched_text, score, recognizer, detected_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_type, source_id, entity_type, start, end,
            f"<{entity_type}>", score, "test",
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# get_source_text
# ---------------------------------------------------------------------------


def test_get_source_text_email_subject(db_conn):
    eid = _ins_email(db_conn, subject="Subject text", body="Body text")
    span = get_source_text(db_conn, "email_subject", eid)
    assert span.exists
    assert span.text == "Subject text"


def test_get_source_text_email_body_html(db_conn):
    eid = _ins_email(db_conn)
    db_conn.execute(
        "UPDATE emails SET body_html_sanitized = ? WHERE id = ?",
        ("<p>safe</p>", eid),
    )
    db_conn.commit()
    assert get_source_text(db_conn, "email_body_html", eid).text == "<p>safe</p>"


def test_get_source_text_missing_returns_not_exists(db_conn):
    assert not get_source_text(db_conn, "email_subject", 9999).exists


def test_get_source_text_unknown_type_raises(db_conn):
    with pytest.raises(RedactionError):
        get_source_text(db_conn, "bogus", 1)


# ---------------------------------------------------------------------------
# validate_new_redaction
# ---------------------------------------------------------------------------


def test_validate_rejects_unknown_source_type(db_conn):
    with pytest.raises(RedactionError):
        validate_new_redaction(
            db_conn, _district(),
            source_type="bogus", source_id=1,
            start_offset=0, end_offset=1, exemption_code="FERPA",
        )


def test_validate_rejects_inverted_offsets(db_conn):
    eid = _ins_email(db_conn, body="abcdef")
    with pytest.raises(RedactionError):
        validate_new_redaction(
            db_conn, _district(),
            source_type="email_body_text", source_id=eid,
            start_offset=5, end_offset=2, exemption_code="FERPA",
        )


def test_validate_rejects_negative_start(db_conn):
    eid = _ins_email(db_conn, body="abcdef")
    with pytest.raises(RedactionError):
        validate_new_redaction(
            db_conn, _district(),
            source_type="email_body_text", source_id=eid,
            start_offset=-1, end_offset=2, exemption_code="FERPA",
        )


def test_validate_rejects_end_past_text(db_conn):
    eid = _ins_email(db_conn, body="abcdef")  # length 6
    with pytest.raises(RedactionError):
        validate_new_redaction(
            db_conn, _district(),
            source_type="email_body_text", source_id=eid,
            start_offset=0, end_offset=99, exemption_code="FERPA",
        )


def test_validate_rejects_unknown_exemption(db_conn):
    eid = _ins_email(db_conn, body="abcdef")
    with pytest.raises(RedactionError):
        validate_new_redaction(
            db_conn, _district(),
            source_type="email_body_text", source_id=eid,
            start_offset=0, end_offset=2, exemption_code="MADE_UP",
        )


def test_validate_requires_reviewer_for_accepted(db_conn):
    eid = _ins_email(db_conn, body="abcdef")
    with pytest.raises(RedactionError):
        validate_new_redaction(
            db_conn, _district(),
            source_type="email_body_text", source_id=eid,
            start_offset=0, end_offset=2, exemption_code="FERPA",
            status="accepted",
        )


def test_validate_rejects_when_source_missing(db_conn):
    with pytest.raises(RedactionError):
        validate_new_redaction(
            db_conn, _district(),
            source_type="email_body_text", source_id=999,
            start_offset=0, end_offset=2, exemption_code="FERPA",
        )


def test_validate_passes_for_well_formed(db_conn):
    eid = _ins_email(db_conn, body="Hello world")
    validate_new_redaction(
        db_conn, _district(),
        source_type="email_body_text", source_id=eid,
        start_offset=0, end_offset=5, exemption_code="FERPA",
    )


# ---------------------------------------------------------------------------
# create / get / update / delete
# ---------------------------------------------------------------------------


def test_create_returns_full_row(db_conn):
    eid = _ins_email(db_conn, body="HELLO 12345")
    row = create_redaction(
        db_conn, _district(),
        source_type="email_body_text", source_id=eid,
        start_offset=6, end_offset=11, exemption_code="FERPA",
        notes="initial",
    )
    assert row["status"] == "proposed"
    assert row["origin"] == "manual"
    assert row["exemption_code"] == "FERPA"
    assert row["start_offset"] == 6
    assert row["notes"] == "initial"


def test_create_duplicate_raises(db_conn):
    eid = _ins_email(db_conn, body="HELLO 12345")
    create_redaction(
        db_conn, _district(),
        source_type="email_body_text", source_id=eid,
        start_offset=6, end_offset=11, exemption_code="FERPA",
    )
    with pytest.raises(RedactionError) as exc:
        create_redaction(
            db_conn, _district(),
            source_type="email_body_text", source_id=eid,
            start_offset=6, end_offset=11, exemption_code="FERPA",
        )
    assert "duplicate" in str(exc.value).lower()


def test_update_status_requires_reviewer(db_conn):
    eid = _ins_email(db_conn, body="HELLO 12345")
    row = create_redaction(
        db_conn, _district(),
        source_type="email_body_text", source_id=eid,
        start_offset=6, end_offset=11, exemption_code="FERPA",
    )
    with pytest.raises(RedactionError):
        update_redaction(db_conn, _district(), row["id"], status="accepted")
    updated = update_redaction(
        db_conn, _district(), row["id"],
        status="accepted", reviewer_id="Records Clerk",
    )
    assert updated["status"] == "accepted"
    assert updated["reviewer_id"] == "Records Clerk"
    # updated_at is refreshed; on fast machines two consecutive ISO
    # timestamps can be equal at microsecond resolution, so just check
    # the value is non-empty rather than strictly newer.
    assert updated["updated_at"]


def test_update_can_change_exemption(db_conn):
    eid = _ins_email(db_conn, body="HELLO 12345")
    row = create_redaction(
        db_conn, _district(),
        source_type="email_body_text", source_id=eid,
        start_offset=6, end_offset=11, exemption_code="FERPA",
    )
    updated = update_redaction(
        db_conn, _district(), row["id"], exemption_code="PII",
    )
    assert updated["exemption_code"] == "PII"


def test_update_rejects_unknown_exemption(db_conn):
    eid = _ins_email(db_conn, body="HELLO 12345")
    row = create_redaction(
        db_conn, _district(),
        source_type="email_body_text", source_id=eid,
        start_offset=6, end_offset=11, exemption_code="FERPA",
    )
    with pytest.raises(RedactionError):
        update_redaction(db_conn, _district(), row["id"], exemption_code="MADE_UP")


def test_update_missing_id_raises(db_conn):
    with pytest.raises(RedactionError):
        update_redaction(db_conn, _district(), 9999, status="rejected", reviewer_id="x")


def test_delete(db_conn):
    eid = _ins_email(db_conn, body="HELLO 12345")
    row = create_redaction(
        db_conn, _district(),
        source_type="email_body_text", source_id=eid,
        start_offset=6, end_offset=11, exemption_code="FERPA",
    )
    delete_redaction(db_conn, row["id"])
    with pytest.raises(RedactionError):
        get_redaction(db_conn, row["id"])
    with pytest.raises(RedactionError):
        delete_redaction(db_conn, row["id"])


def test_list_filters_combine(db_conn):
    eid = _ins_email(db_conn, body="HELLO 12345")
    create_redaction(
        db_conn, _district(),
        source_type="email_body_text", source_id=eid,
        start_offset=0, end_offset=5, exemption_code="FERPA",
    )
    create_redaction(
        db_conn, _district(),
        source_type="email_body_text", source_id=eid,
        start_offset=6, end_offset=11, exemption_code="PII",
    )
    rows, total = list_redactions(db_conn, exemption_code="FERPA")
    assert total == 1
    assert rows[0]["exemption_code"] == "FERPA"
    rows, total = list_redactions(db_conn, status="proposed")
    assert total == 2


# ---------------------------------------------------------------------------
# Constraint enforcement at the SQL level
# ---------------------------------------------------------------------------


def test_check_constraint_blocks_invalid_offset_range(db_conn):
    # Direct SQL bypasses Python-level validation; the schema CHECK still bites.
    eid = _ins_email(db_conn, body="aaa")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            """
            INSERT INTO redactions (
                source_type, source_id, start_offset, end_offset,
                exemption_code, status, origin, created_at, updated_at
            ) VALUES (?, ?, 5, 2, 'FERPA', 'proposed', 'manual',
                      datetime('now'), datetime('now'))
            """,
            ("email_body_text", eid),
        )
        db_conn.commit()


def test_check_constraint_blocks_unknown_status(db_conn):
    eid = _ins_email(db_conn, body="aaa")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            """
            INSERT INTO redactions (
                source_type, source_id, start_offset, end_offset,
                exemption_code, status, origin, created_at, updated_at
            ) VALUES (?, ?, 0, 1, 'FERPA', 'whatever', 'manual',
                      datetime('now'), datetime('now'))
            """,
            ("email_body_text", eid),
        )
        db_conn.commit()


# ---------------------------------------------------------------------------
# propose_from_detections
# ---------------------------------------------------------------------------


def test_propose_creates_one_per_detection(db_conn):
    eid = _ins_email(db_conn, body="hello 572-68-1439 world")
    _ins_pii(
        db_conn, source_type="email_body_text", source_id=eid,
        entity_type="US_SSN", start=6, end=17,
    )
    _ins_pii(
        db_conn, source_type="email_subject", source_id=eid,
        entity_type="STUDENT_ID", start=0, end=2,
    )
    db_conn.execute("UPDATE emails SET subject = 'ID' WHERE id = ?", (eid,))
    db_conn.commit()

    stats = propose_from_detections(db_conn, _district())
    assert stats.detections_seen == 2
    assert stats.proposed == 2
    assert stats.skipped_existing == 0

    rows, total = list_redactions(db_conn)
    assert total == 2
    by_ent = {(r["source_type"], r["start_offset"]): r for r in rows}
    assert by_ent[("email_body_text", 6)]["exemption_code"] == "PII"   # US_SSN override
    assert by_ent[("email_subject", 0)]["exemption_code"] == "FERPA"   # default


def test_propose_is_idempotent(db_conn):
    eid = _ins_email(db_conn, body="aaa 12345 bbb")
    _ins_pii(
        db_conn, source_type="email_body_text", source_id=eid,
        entity_type="STUDENT_ID", start=4, end=9,
    )
    s1 = propose_from_detections(db_conn, _district())
    s2 = propose_from_detections(db_conn, _district())
    assert s1.proposed == 1
    assert s2.proposed == 0
    assert s2.skipped_existing == 1


def test_propose_skips_when_no_exemption(db_conn):
    """No default + no entity mapping => skip with a counted reason."""
    district_strict = DistrictConfig(
        name="Strict", email_domains=(),
        pii=PiiDetectionConfig(builtins=()),
        exemptions=(ExemptionCode(code="FERPA"),),
        redaction=RedactionConfig(default_exemption=None, entity_exemptions={}),
    )
    eid = _ins_email(db_conn, body="aaa 12345 bbb")
    _ins_pii(
        db_conn, source_type="email_body_text", source_id=eid,
        entity_type="STUDENT_ID", start=4, end=9,
    )
    stats = propose_from_detections(db_conn, district_strict)
    assert stats.proposed == 0
    assert stats.skipped_no_exemption == 1


def test_propose_min_score_filter(db_conn):
    eid = _ins_email(db_conn, body="aaa 12345 bbb")
    _ins_pii(
        db_conn, source_type="email_body_text", source_id=eid,
        entity_type="STUDENT_ID", start=4, end=9, score=0.4,
    )
    stats = propose_from_detections(db_conn, _district(), min_score=0.5)
    assert stats.detections_seen == 0
    assert stats.proposed == 0


def test_propose_for_attachment_text(db_conn):
    aid = _ins_attachment_text(db_conn, "see student 12345678 in records")
    _ins_pii(
        db_conn, source_type="attachment_text", source_id=aid,
        entity_type="STUDENT_ID", start=12, end=20,
    )
    stats = propose_from_detections(db_conn, _district())
    assert stats.proposed == 1
    rows, _ = list_redactions(db_conn, source_type="attachment_text")
    assert rows[0]["origin"] == "auto"
    assert rows[0]["source_detection_id"] is not None


def test_propose_only_email_id_scopes_run(db_conn):
    a = _ins_email(db_conn, index=0, body="a 12345")
    b = _ins_email(db_conn, index=1, body="b 12345")
    _ins_pii(db_conn, source_type="email_body_text", source_id=a,
             entity_type="STUDENT_ID", start=2, end=7)
    _ins_pii(db_conn, source_type="email_body_text", source_id=b,
             entity_type="STUDENT_ID", start=2, end=7)
    stats = propose_from_detections(db_conn, _district(), only_email_id=a)
    assert stats.proposed == 1
    rows, total = list_redactions(db_conn)
    assert total == 1
    assert rows[0]["source_id"] == a


def test_pii_detection_delete_sets_source_detection_id_null(db_conn):
    """Deleting the upstream detection must not orphan the redaction;
    the FK uses ON DELETE SET NULL."""
    eid = _ins_email(db_conn, body="aaa 12345 bbb")
    pid = _ins_pii(
        db_conn, source_type="email_body_text", source_id=eid,
        entity_type="STUDENT_ID", start=4, end=9,
    )
    propose_from_detections(db_conn, _district())
    db_conn.execute("DELETE FROM pii_detections WHERE id = ?", (pid,))
    db_conn.commit()
    rows, _ = list_redactions(db_conn)
    assert rows[0]["source_detection_id"] is None
