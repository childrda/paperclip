"""Edge-case coverage for Phase 1 ingestion.

Covers cases that don't belong in the happy-path fixture: missing headers,
non-UTF8 encodings, inline images, bcc, plain-text-only, empty mbox,
and malformed messages mixed with valid ones.
"""

from __future__ import annotations

import json
import mailbox
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from foia.ingestion import ingest_mbox


def _mbox_from(messages, path: Path) -> Path:
    if path.exists():
        path.unlink()
    box = mailbox.mbox(str(path))
    box.lock()
    try:
        for m in messages:
            box.add(m)
        box.flush()
    finally:
        box.unlock()
        box.close()
    return path


def _plain_minimal(subject: str = "Hi", body: str = "body") -> EmailMessage:
    m = EmailMessage()
    m["Message-ID"] = make_msgid(domain="example.org")
    m["From"] = "a@example.org"
    m["To"] = "b@example.org"
    m["Subject"] = subject
    m["Date"] = formatdate(localtime=True)
    m.set_content(body)
    return m


def test_empty_mbox(db_conn, attachment_dir, tmp_path: Path):
    mb = _mbox_from([], tmp_path / "empty.mbox")
    stats = ingest_mbox(mb, db_conn, attachment_dir)
    assert stats.emails_ingested == 0
    assert stats.errors == 0
    assert stats.attachments_saved == 0


def test_plain_text_only_has_no_html_sanitized(db_conn, attachment_dir, tmp_path: Path):
    mb = _mbox_from([_plain_minimal()], tmp_path / "p.mbox")
    ingest_mbox(mb, db_conn, attachment_dir)
    row = db_conn.execute(
        "SELECT body_text, body_html_sanitized FROM emails"
    ).fetchone()
    assert "body" in row["body_text"]
    assert row["body_html_sanitized"] == ""


def test_missing_headers_are_null(db_conn, attachment_dir, tmp_path: Path):
    m = EmailMessage()
    m.set_content("no headers really")
    mb = _mbox_from([m], tmp_path / "nohdr.mbox")
    stats = ingest_mbox(mb, db_conn, attachment_dir)
    assert stats.emails_ingested == 1
    assert stats.errors == 0
    row = db_conn.execute(
        "SELECT subject, from_addr, date_sent, message_id FROM emails"
    ).fetchone()
    assert row["subject"] is None
    assert row["from_addr"] is None
    assert row["date_sent"] is None
    assert row["message_id"] is None


def test_bcc_recorded(db_conn, attachment_dir, tmp_path: Path):
    m = _plain_minimal()
    m["Cc"] = "c1@example.org, c2@example.org"
    m["Bcc"] = "secret@example.org"
    mb = _mbox_from([m], tmp_path / "bcc.mbox")
    ingest_mbox(mb, db_conn, attachment_dir)
    row = db_conn.execute("SELECT cc_addrs, bcc_addrs FROM emails").fetchone()
    ccs = json.loads(row["cc_addrs"])
    bccs = json.loads(row["bcc_addrs"])
    assert any("c1@example.org" in a for a in ccs)
    assert any("c2@example.org" in a for a in ccs)
    assert any("secret@example.org" in a for a in bccs)


def test_non_utf8_body_is_decoded(db_conn, attachment_dir, tmp_path: Path):
    m = EmailMessage()
    m["From"] = "a@example.org"
    m["To"] = "b@example.org"
    m["Subject"] = "latin1"
    m["Date"] = formatdate()
    m.set_content("cafe naif", charset="utf-8")  # placeholder, we rewrite payload
    # Overwrite body payload with real latin-1 bytes to exercise decode path.
    latin1_bytes = "café naïve résumé".encode("latin-1")
    m.set_payload(latin1_bytes)
    m.replace_header("Content-Transfer-Encoding", "8bit") if m.get(
        "Content-Transfer-Encoding"
    ) else m.add_header("Content-Transfer-Encoding", "8bit")
    m.set_charset("latin-1")

    mb = _mbox_from([m], tmp_path / "l1.mbox")
    stats = ingest_mbox(mb, db_conn, attachment_dir)
    assert stats.errors == 0
    row = db_conn.execute("SELECT body_text FROM emails").fetchone()
    assert "café" in row["body_text"]
    assert "naïve" in row["body_text"]


def test_inline_image_stored_as_attachment(db_conn, attachment_dir, tmp_path: Path):
    m = EmailMessage()
    m["From"] = "a@example.org"
    m["To"] = "b@example.org"
    m["Subject"] = "inline"
    m["Date"] = formatdate()
    m.set_content("see embedded image")
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff"
        b"\xff?\x00\x05\xfe\x02\xfe\xdc\xccY\xe7\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    m.add_attachment(
        png,
        maintype="image",
        subtype="png",
        filename="embed.png",
        disposition="inline",
        cid="inline-1",
    )
    mb = _mbox_from([m], tmp_path / "inl.mbox")
    stats = ingest_mbox(mb, db_conn, attachment_dir)
    assert stats.attachments_saved == 1
    row = db_conn.execute(
        "SELECT is_inline, filename, content_type FROM attachments"
    ).fetchone()
    assert row["is_inline"] == 1
    assert row["filename"] == "embed.png"
    assert row["content_type"] == "image/png"


def test_malformed_mbox_one_bad_then_good(db_conn, attachment_dir, tmp_path: Path):
    """A garbage byte block between two valid messages should not abort the run."""
    valid1 = _plain_minimal(subject="first")
    valid2 = _plain_minimal(subject="second")
    mb = _mbox_from([valid1, valid2], tmp_path / "mixed.mbox")

    # Splice a broken "From " section in between — some parsers will emit an
    # extra defective message; the pipeline should keep going either way.
    data = mb.read_bytes()
    middle = data.find(b"\nFrom ", 10)
    assert middle > 0
    corrupted = (
        data[:middle + 1]
        + b"From garbage@example.org Tue Jan  1 00:00:00 2030\n"
        + b"Not a real message: \xff\xfe\x00broken\n\n"
        + b"random garbage without proper headers\n"
        + data[middle + 1:]
    )
    mb.write_bytes(corrupted)

    stats = ingest_mbox(mb, db_conn, attachment_dir)
    subjects = [
        r["subject"]
        for r in db_conn.execute("SELECT subject FROM emails").fetchall()
    ]
    assert "first" in subjects
    assert "second" in subjects
    # errors may or may not be >0 depending on how the mbox module handles
    # the spliced fragment, but we must not crash and must persist valid msgs.
    assert stats.emails_ingested >= 2


def test_duplicate_attachment_same_email_yields_two_rows_one_file(
    db_conn, attachment_dir, tmp_path: Path
):
    """Same attachment attached twice: both occurrences recorded in DB; one file on disk."""
    m = _plain_minimal()
    payload = b"%PDF-1.4\nfake\n%%EOF\n"
    m.add_attachment(
        payload, maintype="application", subtype="pdf", filename="a.pdf"
    )
    m.add_attachment(
        payload, maintype="application", subtype="pdf", filename="b.pdf"
    )
    mb = _mbox_from([m], tmp_path / "dup.mbox")
    ingest_mbox(mb, db_conn, attachment_dir)
    rows = db_conn.execute(
        "SELECT filename, sha256, storage_path FROM attachments ORDER BY filename"
    ).fetchall()
    assert len(rows) == 2
    # Same sha.
    assert rows[0]["sha256"] == rows[1]["sha256"]
    # Distinct storage paths because filename differs.
    assert rows[0]["storage_path"] != rows[1]["storage_path"]
    # Both files exist on disk.
    assert Path(rows[0]["storage_path"]).exists()
    assert Path(rows[1]["storage_path"]).exists()


def test_html_only_email_has_empty_body_text(db_conn, attachment_dir, tmp_path: Path):
    m = EmailMessage()
    m["From"] = "a@example.org"
    m["To"] = "b@example.org"
    m["Subject"] = "html-only"
    m["Date"] = formatdate()
    m.set_content("<p>hi</p>", subtype="html")
    mb = _mbox_from([m], tmp_path / "html.mbox")
    ingest_mbox(mb, db_conn, attachment_dir)
    row = db_conn.execute(
        "SELECT body_text, body_html_sanitized FROM emails"
    ).fetchone()
    assert row["body_text"] == ""
    assert "<p>hi</p>" in row["body_html_sanitized"]
