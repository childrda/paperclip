"""Integration tests for the batch extraction driver."""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

import pytest

from foia import extraction as E
from foia.extraction import ExtractionOptions
from foia.ingestion import ingest_mbox
from foia.processing import process_attachments


def _mk_mbox_with_text_pdf(path: Path, pdf_path: Path) -> Path:
    import mailbox
    m = EmailMessage()
    m["From"] = "a@x.org"
    m["To"] = "b@x.org"
    m["Subject"] = "with pdf"
    m["Date"] = "Mon, 1 Jan 2024 00:00:00 +0000"
    m.set_content("see attached")
    m.add_attachment(
        pdf_path.read_bytes(),
        maintype="application", subtype="pdf", filename=pdf_path.name,
    )
    if path.exists():
        path.unlink()
    box = mailbox.mbox(str(path))
    box.lock()
    try:
        box.add(m)
        box.flush()
    finally:
        box.unlock()
        box.close()
    return path


def test_processing_populates_attachments_text(
    db_conn, attachment_dir, tmp_path: Path, text_pdf_factory
):
    pdf = text_pdf_factory(["Extracted content for Phase 2"])
    mbox = _mk_mbox_with_text_pdf(tmp_path / "p.mbox", pdf)
    ingest_mbox(mbox, db_conn, attachment_dir)

    stats = process_attachments(
        db_conn, options=ExtractionOptions(ocr_enabled=False)
    )
    assert stats.total == 1
    assert stats.extracted_ok == 1
    assert stats.failed == 0

    row = db_conn.execute(
        "SELECT extracted_text, extraction_method, extraction_status, "
        "       character_count, ocr_applied, page_count "
        "FROM attachments_text"
    ).fetchone()
    assert row["extraction_status"] == "ok"
    assert row["extraction_method"] == "pypdf"
    assert row["ocr_applied"] == 0
    assert row["page_count"] == 1
    assert row["character_count"] > 0
    assert "Extracted content for Phase 2" in row["extracted_text"]


def test_processing_is_idempotent(
    db_conn, attachment_dir, tmp_path: Path, text_pdf_factory
):
    pdf = text_pdf_factory(["Idempotent"])
    mbox = _mk_mbox_with_text_pdf(tmp_path / "i.mbox", pdf)
    ingest_mbox(mbox, db_conn, attachment_dir)

    s1 = process_attachments(db_conn, options=ExtractionOptions(ocr_enabled=False))
    s2 = process_attachments(db_conn, options=ExtractionOptions(ocr_enabled=False))
    assert s1.total == 1 and s1.extracted_ok == 1
    assert s2.total == 0  # nothing new to do
    assert db_conn.execute("SELECT COUNT(*) FROM attachments_text").fetchone()[0] == 1


def test_force_reprocesses(db_conn, attachment_dir, tmp_path: Path, text_pdf_factory):
    pdf = text_pdf_factory(["First run"])
    mbox = _mk_mbox_with_text_pdf(tmp_path / "f.mbox", pdf)
    ingest_mbox(mbox, db_conn, attachment_dir)

    process_attachments(db_conn, options=ExtractionOptions(ocr_enabled=False))
    # Force re-run with OCR disabled again — must produce one fresh row.
    s = process_attachments(
        db_conn, options=ExtractionOptions(ocr_enabled=False), force=True
    )
    assert s.total == 1
    assert s.extracted_ok == 1
    assert db_conn.execute("SELECT COUNT(*) FROM attachments_text").fetchone()[0] == 1


def test_processing_handles_failure_without_raising(
    monkeypatch, db_conn, attachment_dir, tmp_path: Path
):
    """A handler crash should store a 'failed' row, not kill the run."""
    import mailbox
    m = EmailMessage()
    m["From"] = "a@x.org"; m["To"] = "b@x.org"; m["Subject"] = "bad"
    m["Date"] = "Mon, 1 Jan 2024 00:00:00 +0000"
    m.set_content("see attached")
    m.add_attachment(
        b"garbage garbage garbage",
        maintype="application", subtype="pdf", filename="bad.pdf",
    )
    mbox_path = tmp_path / "bad.mbox"
    if mbox_path.exists():
        mbox_path.unlink()
    box = mailbox.mbox(str(mbox_path))
    box.lock()
    try:
        box.add(m); box.flush()
    finally:
        box.unlock(); box.close()
    ingest_mbox(mbox_path, db_conn, attachment_dir)

    stats = process_attachments(
        db_conn, options=ExtractionOptions(ocr_enabled=False)
    )
    assert stats.total == 1
    assert stats.failed == 1
    row = db_conn.execute(
        "SELECT extraction_status, error_message FROM attachments_text"
    ).fetchone()
    assert row["extraction_status"] == "failed"
    assert row["error_message"]


def test_full_sample_mbox_processing(
    monkeypatch, db_conn, attachment_dir, sample_mbox: Path
):
    """End-to-end: ingest the canonical fixture, extract, every attachment
    ends up with a text row."""
    # Mock OCR so we don't depend on tesseract at test time.
    monkeypatch.setattr(E, "_ocr_available", lambda opts: (True, None))
    import pytesseract
    monkeypatch.setattr(
        pytesseract, "image_to_string", lambda *a, **k: "FAKE OCR"
    )

    ingest_mbox(sample_mbox, db_conn, attachment_dir)
    stats = process_attachments(
        db_conn, options=ExtractionOptions(ocr_enabled=True, office_enabled=False)
    )

    # 3 attachments: pdf (fake, will fail parsing), png (ocr), nested eml (eml_body).
    assert stats.total == 3

    methods = [
        r["extraction_method"] for r in db_conn.execute(
            "SELECT extraction_method FROM attachments_text"
        )
    ]
    assert "ocr_tesseract" in methods  # the png
    assert "eml_body" in methods        # the nested .eml
    # The fake PDF from the fixture isn't a valid PDF body; expect failed.
    assert any(m == "pypdf" or m == "pdf_ocr" or m == "dispatch" for m in methods)


def test_only_attachment_id(db_conn, attachment_dir, tmp_path: Path, text_pdf_factory):
    pdf = text_pdf_factory(["one"])
    mbox = _mk_mbox_with_text_pdf(tmp_path / "s.mbox", pdf)
    ingest_mbox(mbox, db_conn, attachment_dir)

    aid = db_conn.execute("SELECT id FROM attachments").fetchone()["id"]
    stats = process_attachments(
        db_conn, options=ExtractionOptions(ocr_enabled=False),
        only_attachment_id=aid,
    )
    assert stats.total == 1
    assert stats.extracted_ok == 1
