from __future__ import annotations

import json
from pathlib import Path

from foia.ingestion import ingest_mbox


def _fetch_all(conn, sql: str, *params):
    return conn.execute(sql, params).fetchall()


def test_ingest_sample_roundtrip(db_conn, attachment_dir: Path, sample_mbox: Path):
    stats = ingest_mbox(sample_mbox, db_conn, attachment_dir, source_label="sample")
    assert stats.errors == 0
    assert stats.emails_ingested == 5
    assert stats.emails_skipped_duplicate == 0
    assert stats.attachments_saved >= 3  # pdf, png, nested eml

    emails = _fetch_all(db_conn, "SELECT * FROM emails ORDER BY id")
    assert len(emails) == 5
    subjects = [e["subject"] for e in emails]
    assert "Bus route change" in subjects
    assert "Weekly newsletter" in subjects

    # Every email has raw_content preserved exactly once.
    raw_count = db_conn.execute("SELECT COUNT(*) FROM raw_content").fetchone()[0]
    assert raw_count == 5


def test_duplicate_ingest_is_idempotent(db_conn, attachment_dir, sample_mbox):
    first = ingest_mbox(sample_mbox, db_conn, attachment_dir, source_label="sample")
    second = ingest_mbox(sample_mbox, db_conn, attachment_dir, source_label="sample")
    assert first.emails_ingested == 5
    assert second.emails_ingested == 0
    assert second.emails_skipped_duplicate == 5


def test_attachments_metadata(db_conn, attachment_dir, sample_mbox):
    ingest_mbox(sample_mbox, db_conn, attachment_dir, source_label="sample")
    pdfs = _fetch_all(
        db_conn, "SELECT * FROM attachments WHERE content_type = 'application/pdf'"
    )
    assert len(pdfs) == 1
    pdf_row = pdfs[0]
    assert pdf_row["filename"].endswith(".pdf")
    assert pdf_row["size_bytes"] > 0
    stored = Path(pdf_row["storage_path"])
    assert stored.exists()
    assert stored.read_bytes().startswith(b"%PDF")

    nested = _fetch_all(db_conn, "SELECT * FROM attachments WHERE is_nested_eml = 1")
    assert len(nested) == 1


def test_html_body_is_sanitized(db_conn, attachment_dir, sample_mbox):
    ingest_mbox(sample_mbox, db_conn, attachment_dir, source_label="sample")
    row = db_conn.execute(
        "SELECT body_html_sanitized FROM emails WHERE subject = 'Weekly newsletter'"
    ).fetchone()
    html = row["body_html_sanitized"] or ""
    for forbidden in ("script", "iframe", "<img", "track.example", "alert("):
        assert forbidden not in html.lower()
    assert "https://district.example.org/news" in html


def test_headers_and_addresses_parsed(db_conn, attachment_dir, sample_mbox):
    ingest_mbox(sample_mbox, db_conn, attachment_dir, source_label="sample")
    row = db_conn.execute(
        "SELECT from_addr, to_addrs, headers_json FROM emails "
        "WHERE subject = 'Bus route change'"
    ).fetchone()
    assert "alice@district.example.org" in row["from_addr"]
    to_list = json.loads(row["to_addrs"])
    assert any("bob@example.com" in addr for addr in to_list)
    headers = json.loads(row["headers_json"])
    assert "Subject" in headers


def test_raw_content_bytes_preserved(db_conn, attachment_dir, sample_mbox):
    ingest_mbox(sample_mbox, db_conn, attachment_dir, source_label="sample")
    row = db_conn.execute(
        "SELECT raw_rfc822, raw_sha256 FROM raw_content LIMIT 1"
    ).fetchone()
    import hashlib
    assert hashlib.sha256(row["raw_rfc822"]).hexdigest() == row["raw_sha256"]
    # Raw content should be parseable as an email message.
    from email import message_from_bytes
    msg = message_from_bytes(row["raw_rfc822"])
    assert msg["Subject"] is not None


def test_missing_file_raises(db_conn, attachment_dir, tmp_path: Path):
    import pytest
    with pytest.raises(FileNotFoundError):
        ingest_mbox(tmp_path / "nope.mbox", db_conn, attachment_dir)
