"""Phase 8 — PDF export unit tests.

We don't try to inspect the PDF visually; we use pypdf to read back the
text layer and assert that:
  * redacted spans never appear (burned-in, not overlay-ed)
  * surrounding text remains intact
  * Bates labels appear on every page
  * the CSV log has one row per accepted redaction
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pypdf import PdfReader

from foia.district import (
    BatesConfig,
    DistrictConfig,
    ExemptionCode,
    PiiDetectionConfig,
    RedactionConfig,
)
from foia.export import (
    EMAIL_SOURCES,
    ExportConfig,
    _line_intersections,
    _wrap_lines,
    run_export,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _district(prefix: str = "TEST", start: int = 1) -> DistrictConfig:
    return DistrictConfig(
        name="Test District",
        email_domains=(),
        pii=PiiDetectionConfig(builtins=()),
        exemptions=(ExemptionCode(code="FERPA"), ExemptionCode(code="PII")),
        redaction=RedactionConfig(default_exemption="FERPA"),
        bates=BatesConfig(prefix=prefix, start=start, width=4),
    )


def _ins_email(conn, *, idx: int, subject: str = "Hi", body: str = "Hello") -> int:
    cur = conn.execute(
        """
        INSERT INTO emails (mbox_source, mbox_index, subject, body_text,
                            body_html_sanitized, ingested_at)
        VALUES ('t.mbox', ?, ?, ?, '', ?)
        """,
        (idx, subject, body, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return int(cur.lastrowid)


def _ins_redaction(
    conn, *,
    source_type: str,
    source_id: int,
    start: int,
    end: int,
    exemption_code: str = "FERPA",
    status: str = "accepted",
    reviewer: str | None = "Records Clerk",
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """
        INSERT INTO redactions (
            source_type, source_id, start_offset, end_offset,
            exemption_code, status, origin, reviewer_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'manual', ?, ?, ?)
        """,
        (source_type, source_id, start, end, exemption_code,
         status, reviewer, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def _ins_attachment_with_text(conn, email_id: int, text: str, filename: str = "memo.txt") -> int:
    cur = conn.execute(
        "INSERT INTO attachments (email_id, filename, content_type, "
        "size_bytes, sha256, storage_path) "
        "VALUES (?, ?, 'text/plain', 1, 'sha', 'p')",
        (email_id, filename),
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


def _pdf_text(path: Path) -> str:
    reader = PdfReader(str(path))
    return "\n".join(p.extract_text() or "" for p in reader.pages)


def _pdf_pages(path: Path) -> int:
    return len(PdfReader(str(path)).pages)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_email_sources_constant_is_complete():
    types = {st for st, _ in EMAIL_SOURCES}
    assert types == {"email_subject", "email_body_text", "email_body_html"}


def test_wrap_lines_preserves_offsets():
    text = "abcdefghij\nshort\nlonger line that wraps somewhere"
    lines = _wrap_lines(text, width_chars=10)
    # Reconstruct and check offsets are monotonic and slices match.
    cursor = 0
    for offset, line in lines:
        assert offset >= cursor
        cursor = offset
        if line:
            assert text[offset:offset + len(line)] == line


def test_wrap_lines_empty_text():
    assert _wrap_lines("", 80) == [(0, "")]


def test_line_intersections_disjoint():
    from foia.export import _Redaction
    r1 = _Redaction(1, "email_body_text", 1, 2, 5, "FERPA", None, "")
    r2 = _Redaction(2, "email_body_text", 1, 20, 25, "FERPA", None, "")
    inters = _line_intersections(0, "0123456789ABCDEFGHIJ", [r1, r2])
    # r1 fits in this 0..20 line; r2 partially intersects (start=20 → outside this line).
    assert len(inters) == 1
    s, e, r = inters[0]
    assert s == 2 and e == 5 and r.id == 1


def test_line_intersections_clip_to_line():
    from foia.export import _Redaction
    r = _Redaction(1, "email_body_text", 1, 5, 30, "FERPA", None, "")
    inters = _line_intersections(line_start=10, line_text="0123456789", redactions=[r])
    # Redaction spans 5..30; line covers 10..20 → clipped to 0..10 in line coords.
    assert inters == [(0, 10, r)]


# ---------------------------------------------------------------------------
# End-to-end PDF + CSV
# ---------------------------------------------------------------------------


def test_export_burns_in_redactions(db_conn, tmp_path: Path):
    eid = _ins_email(
        db_conn, idx=0,
        subject="Bus route update",
        body="Hello team,\n\nSSN on file: SUPER_SECRET. Trailing context.",
    )
    # Accepted redaction over "SUPER_SECRET" (12 chars).
    body = "Hello team,\n\nSSN on file: SUPER_SECRET. Trailing context."
    start = body.index("SUPER_SECRET")
    _ins_redaction(
        db_conn,
        source_type="email_body_text", source_id=eid,
        start=start, end=start + len("SUPER_SECRET"),
        exemption_code="PII",
    )

    out_dir = tmp_path / "exp"
    stats = run_export(db_conn, _district(), ExportConfig(output_dir=out_dir))

    assert stats.emails_exported == 1
    assert stats.redactions_burned == 1
    assert stats.output_pdf and stats.output_pdf.exists()
    assert stats.output_csv and stats.output_csv.exists()

    # The redacted token must NOT appear in the PDF text layer.
    text = _pdf_text(stats.output_pdf)
    assert "SUPER_SECRET" not in text
    # Surrounding context survives — the redaction was burned in, not the whole line.
    assert "Hello team" in text
    assert "Trailing context" in text


def test_export_only_accepted_status_is_burned(db_conn, tmp_path: Path):
    eid = _ins_email(
        db_conn, idx=0,
        body="proposed=PROPOSED rejected=REJECTED accepted=ACCEPTED",
    )
    body = "proposed=PROPOSED rejected=REJECTED accepted=ACCEPTED"
    for tok, status in (("PROPOSED", "proposed"), ("REJECTED", "rejected"),
                        ("ACCEPTED", "accepted")):
        i = body.index(tok)
        _ins_redaction(
            db_conn, source_type="email_body_text", source_id=eid,
            start=i, end=i + len(tok), exemption_code="FERPA",
            status=status,
            reviewer="x" if status != "proposed" else None,
        )

    out_dir = tmp_path / "exp"
    stats = run_export(db_conn, _district(), ExportConfig(output_dir=out_dir))
    text = _pdf_text(stats.output_pdf)
    assert "PROPOSED" in text
    assert "REJECTED" in text
    assert "ACCEPTED" not in text       # only this one is burned in


def test_csv_log_columns_and_rows(db_conn, tmp_path: Path):
    eid = _ins_email(db_conn, idx=0, body="A 12345678 B")
    rid = _ins_redaction(
        db_conn, source_type="email_body_text", source_id=eid,
        start=2, end=10, exemption_code="FERPA", reviewer="Reviewer A",
    )
    out_dir = tmp_path / "exp"
    stats = run_export(db_conn, _district(prefix="ECPS", start=42),
                       ExportConfig(output_dir=out_dir))
    assert stats.bates_first == "ECPS-0042"

    with stats.output_csv.open("r", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    r = rows[0]
    assert r["redaction_id"] == str(rid)
    assert r["source_type"] == "email_body_text"
    assert r["source_id"] == str(eid)
    assert r["start_offset"] == "2"
    assert r["end_offset"] == "10"
    assert r["length"] == "8"
    assert r["exemption_code"] == "FERPA"
    assert r["reviewer_id"] == "Reviewer A"
    assert r["bates_label"] == "ECPS-0042"


def test_bates_increments_across_pages(db_conn, tmp_path: Path):
    # Each email triggers a forced page break, so two emails produce
    # at least two pages and two distinct Bates labels.
    _ins_email(db_conn, idx=0, body="email one body text")
    _ins_email(db_conn, idx=1, body="email two body text")
    stats = run_export(
        db_conn, _district(prefix="DOC", start=1),
        ExportConfig(output_dir=tmp_path / "exp"),
    )
    assert stats.pages_written >= 2
    assert stats.bates_first == "DOC-0001"
    last = stats.bates_last
    assert last is not None
    assert last.startswith("DOC-")
    assert int(last.split("-")[1]) == stats.pages_written


def test_export_includes_attachment_text_by_default(db_conn, tmp_path: Path):
    eid = _ins_email(db_conn, idx=0, body="see attached")
    aid = _ins_attachment_with_text(
        db_conn, eid, "Attachment body — KEEP_ME and DROP_ME tokens."
    )
    txt = "Attachment body — KEEP_ME and DROP_ME tokens."
    drop_start = txt.index("DROP_ME")
    _ins_redaction(
        db_conn, source_type="attachment_text", source_id=aid,
        start=drop_start, end=drop_start + len("DROP_ME"),
        exemption_code="FERPA",
    )
    stats = run_export(db_conn, _district(),
                       ExportConfig(output_dir=tmp_path / "exp"))
    assert stats.attachments_exported == 1
    text = _pdf_text(stats.output_pdf)
    assert "KEEP_ME" in text
    assert "DROP_ME" not in text


def test_export_no_attachments_flag(db_conn, tmp_path: Path):
    eid = _ins_email(db_conn, idx=0, body="see attached")
    _ins_attachment_with_text(db_conn, eid, "attachment text")
    stats = run_export(
        db_conn, _district(),
        ExportConfig(output_dir=tmp_path / "exp"),
        include_attachments=False,
    )
    assert stats.attachments_exported == 0


def test_only_email_ids_filter(db_conn, tmp_path: Path):
    a = _ins_email(db_conn, idx=0, body="emailA body alpha")
    b = _ins_email(db_conn, idx=1, body="emailB body beta")
    _ins_redaction(
        db_conn, source_type="email_body_text", source_id=a,
        start=0, end=5, exemption_code="FERPA",
    )
    _ins_redaction(
        db_conn, source_type="email_body_text", source_id=b,
        start=0, end=5, exemption_code="FERPA",
    )
    stats = run_export(
        db_conn, _district(),
        ExportConfig(output_dir=tmp_path / "exp"),
        only_email_ids=[a],
    )
    assert stats.emails_exported == 1
    assert stats.redactions_burned == 1


def test_empty_scope_still_produces_pdf(db_conn, tmp_path: Path):
    stats = run_export(db_conn, _district(),
                       ExportConfig(output_dir=tmp_path / "exp"))
    assert stats.emails_exported == 0
    assert stats.attachments_exported == 0
    assert stats.output_pdf.exists()
    # CSV exists with just the header.
    with stats.output_csv.open("r", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert len(rows) == 1
    assert rows[0][0] == "bates_label"


def test_exemption_label_in_csv_for_each_redaction(db_conn, tmp_path: Path):
    eid = _ins_email(db_conn, idx=0, body="aaa BBB ccc DDD eee")
    body = "aaa BBB ccc DDD eee"
    _ins_redaction(
        db_conn, source_type="email_body_text", source_id=eid,
        start=body.index("BBB"), end=body.index("BBB") + 3,
        exemption_code="FERPA",
    )
    _ins_redaction(
        db_conn, source_type="email_body_text", source_id=eid,
        start=body.index("DDD"), end=body.index("DDD") + 3,
        exemption_code="PII",
    )
    stats = run_export(db_conn, _district(),
                       ExportConfig(output_dir=tmp_path / "exp"))
    with stats.output_csv.open("r", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    codes = sorted(r["exemption_code"] for r in rows)
    assert codes == ["FERPA", "PII"]
    # Both should reference the same email source label.
    assert {r["source_label"] for r in rows} == {f"email {eid}"}


def test_pdf_pages_match_stats(db_conn, tmp_path: Path):
    _ins_email(db_conn, idx=0, body="a")
    _ins_email(db_conn, idx=1, body="b")
    _ins_email(db_conn, idx=2, body="c")
    stats = run_export(db_conn, _district(),
                       ExportConfig(output_dir=tmp_path / "exp"))
    assert _pdf_pages(stats.output_pdf) == stats.pages_written
