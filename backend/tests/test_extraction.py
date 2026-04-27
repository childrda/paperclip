"""Unit tests for the per-type extraction handlers."""

from __future__ import annotations

from pathlib import Path

import pytest

from foia import extraction as E
from foia.extraction import ExtractionOptions, extract


# ---------------------------------------------------------------------------
# PDF handlers
# ---------------------------------------------------------------------------


def test_pdf_text_layer(text_pdf_factory):
    pdf = text_pdf_factory(["Hello Phase Two", "Second line of text"])
    res = extract(pdf, "application/pdf", ExtractionOptions(ocr_enabled=False))
    assert res.status == "ok"
    assert res.method == "pypdf"
    assert "Hello Phase Two" in res.text
    assert "Second line of text" in res.text
    assert res.page_count == 1
    assert res.ocr_applied is False


def test_pdf_empty_without_ocr(blank_pdf_factory):
    pdf = blank_pdf_factory(page_count=1)
    res = extract(pdf, "application/pdf", ExtractionOptions(ocr_enabled=False))
    assert res.method == "pypdf"
    assert res.status == "empty"
    assert res.page_count == 1


def test_pdf_corrupt_fails_cleanly(tmp_path: Path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf at all")
    res = extract(bad, "application/pdf", ExtractionOptions(ocr_enabled=False))
    assert res.status == "failed"
    assert res.error is not None
    assert res.method in {"pypdf", "dispatch"}


def test_scanned_pdf_falls_back_to_ocr(monkeypatch, blank_pdf_factory):
    pdf = blank_pdf_factory(page_count=2)

    monkeypatch.setattr(
        E, "_ocr_available", lambda opts: (True, None)
    )

    def fake_ocr(path, opts):
        return E.ExtractionResult(
            status="ok", method="pdf_ocr",
            text="OCR PAGE 1\nOCR PAGE 2",
            page_count=2, ocr_applied=True,
        )

    monkeypatch.setattr(E, "_ocr_pdf_rasterize", fake_ocr)

    res = extract(pdf, "application/pdf", ExtractionOptions(ocr_enabled=True))
    assert res.status == "ok"
    assert res.ocr_applied is True
    assert res.method == "pdf_ocr"
    assert "OCR PAGE 1" in res.text


def test_pdf_text_layer_wins_when_ocr_empty(monkeypatch, text_pdf_factory):
    pdf = text_pdf_factory(["Real text"])

    monkeypatch.setattr(E, "_ocr_available", lambda opts: (True, None))
    monkeypatch.setattr(
        E, "_ocr_pdf_rasterize",
        lambda p, o: E.ExtractionResult(
            status="empty", method="pdf_ocr", ocr_applied=True,
        ),
    )
    res = extract(pdf, "application/pdf", ExtractionOptions(ocr_enabled=True))
    assert res.status == "ok"
    assert res.method == "pypdf"
    assert "Real text" in res.text


def test_pdf_ocr_disabled_does_not_call_tesseract(monkeypatch, blank_pdf_factory):
    calls = []
    monkeypatch.setattr(
        E, "_ocr_pdf_rasterize",
        lambda p, o: calls.append(p) or E.ExtractionResult(
            status="ok", method="pdf_ocr", text="shouldnt get here", ocr_applied=True,
        ),
    )
    pdf = blank_pdf_factory(page_count=1)
    res = extract(pdf, "application/pdf", ExtractionOptions(ocr_enabled=False))
    assert calls == []
    assert res.method == "pypdf"
    assert res.status in {"empty", "ok"}


# ---------------------------------------------------------------------------
# Image OCR
# ---------------------------------------------------------------------------


def _write_png(path: Path) -> Path:
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff"
        b"\xff?\x00\x05\xfe\x02\xfe\xdc\xccY\xe7\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    path.write_bytes(png)
    return path


def test_image_ocr_happy_path(monkeypatch, tmp_path: Path):
    img = _write_png(tmp_path / "scan.png")

    monkeypatch.setattr(E, "_ocr_available", lambda opts: (True, None))

    import pytesseract
    monkeypatch.setattr(
        pytesseract, "image_to_string", lambda *a, **k: "  OCR TEXT  "
    )
    res = extract(img, "image/png", ExtractionOptions(ocr_enabled=True))
    assert res.status == "ok"
    assert res.method == "ocr_tesseract"
    assert res.text == "OCR TEXT"
    assert res.ocr_applied is True


def test_image_ocr_unavailable(monkeypatch, tmp_path: Path):
    img = _write_png(tmp_path / "scan.png")
    monkeypatch.setattr(
        E, "_ocr_available", lambda opts: (False, "tesseract binary not found on PATH")
    )
    res = extract(img, "image/png", ExtractionOptions(ocr_enabled=True))
    assert res.status == "failed"
    assert res.method == "ocr_tesseract"
    assert "tesseract" in (res.error or "").lower()


def test_image_ocr_empty(monkeypatch, tmp_path: Path):
    img = _write_png(tmp_path / "scan.png")
    monkeypatch.setattr(E, "_ocr_available", lambda opts: (True, None))
    import pytesseract
    monkeypatch.setattr(pytesseract, "image_to_string", lambda *a, **k: "   ")
    res = extract(img, "image/png", ExtractionOptions(ocr_enabled=True))
    assert res.status == "empty"
    assert res.method == "ocr_tesseract"


# ---------------------------------------------------------------------------
# Office documents
# ---------------------------------------------------------------------------


def test_office_disabled(tmp_path: Path):
    doc = tmp_path / "doc.docx"
    doc.write_bytes(b"PK\x03\x04 fake zip")  # any bytes
    res = extract(
        doc,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ExtractionOptions(office_enabled=False),
    )
    assert res.status == "failed"
    assert res.method.startswith("libreoffice")
    assert "disabled" in (res.error or "").lower()


def test_office_converts_via_libreoffice(monkeypatch, tmp_path: Path, text_pdf_factory):
    doc = tmp_path / "memo.docx"
    doc.write_bytes(b"PK\x03\x04 fake zip")

    # Pretend soffice exists on PATH.
    monkeypatch.setattr(E.shutil, "which", lambda name: "/fake/soffice")

    def fake_run(cmd, *args, **kwargs):
        # Cmd: [soffice, --headless, --norestore, --nologo, --convert-to, pdf, --outdir, TMPDIR, SRC]
        outdir = Path(cmd[cmd.index("--outdir") + 1])
        # Build a real PDF with a known text string into outdir.
        from reportlab.pdfgen.canvas import Canvas
        out = outdir / (Path(cmd[-1]).stem + ".pdf")
        c = Canvas(str(out))
        c.drawString(72, 750, "Converted memo content")
        c.showPage()
        c.save()
        class R:
            returncode = 0
            stderr = b""
        return R()

    monkeypatch.setattr(E.subprocess, "run", fake_run)

    res = extract(
        doc,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ExtractionOptions(office_enabled=True, ocr_enabled=False),
    )
    assert res.status == "ok"
    assert res.method == "libreoffice+pypdf"
    assert "Converted memo content" in res.text


def test_office_libreoffice_missing(monkeypatch, tmp_path: Path):
    doc = tmp_path / "ppt.pptx"
    doc.write_bytes(b"PK")
    monkeypatch.setattr(E.shutil, "which", lambda name: None)
    res = extract(
        doc,
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ExtractionOptions(office_enabled=True),
    )
    assert res.status == "failed"
    assert "libreoffice" in (res.error or "").lower()


def test_office_nonzero_exit_reports_error(monkeypatch, tmp_path: Path):
    doc = tmp_path / "x.odt"
    doc.write_bytes(b"garbage")
    monkeypatch.setattr(E.shutil, "which", lambda n: "/fake/soffice")

    class R:
        returncode = 77
        stderr = b"conversion failure"
    monkeypatch.setattr(E.subprocess, "run", lambda *a, **k: R())

    res = extract(
        doc,
        "application/vnd.oasis.opendocument.text",
        ExtractionOptions(office_enabled=True),
    )
    assert res.status == "failed"
    assert "exit=77" in (res.error or "")


# ---------------------------------------------------------------------------
# Nested .eml and text types
# ---------------------------------------------------------------------------


def test_eml_extraction(tmp_path: Path):
    from email.message import EmailMessage
    m = EmailMessage()
    m["From"] = "Parent <p@example.com>"
    m["To"] = "Principal <pr@example.org>"
    m["Subject"] = "Concern"
    m.set_content("The body of the original complaint.")
    path = tmp_path / "nested.eml"
    path.write_bytes(bytes(m))

    res = extract(path, "message/rfc822")
    assert res.status == "ok"
    assert res.method == "eml_body"
    assert "From: Parent <p@example.com>" in res.text
    assert "Subject: Concern" in res.text
    assert "body of the original complaint" in res.text


def test_text_plain_extraction(tmp_path: Path):
    p = tmp_path / "note.txt"
    p.write_text("just a note")
    res = extract(p, "text/plain")
    assert res.status == "ok"
    assert res.method == "text"
    assert res.text == "just a note"


def test_text_plain_cp1252(tmp_path: Path):
    p = tmp_path / "win.txt"
    p.write_bytes("café naïve".encode("cp1252"))
    res = extract(p, "text/plain")
    assert res.status == "ok"
    assert "café" in res.text


def test_html_extraction(tmp_path: Path):
    p = tmp_path / "p.html"
    p.write_text("<html><body><p>hi <b>there</b></p><script>x</script></body></html>")
    res = extract(p, "text/html")
    assert res.status == "ok"
    assert res.method == "html"
    assert "hi there" in res.text
    assert "x" not in res.text  # script content stripped


def test_unsupported_type(tmp_path: Path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"\x00\x01\x02")
    res = extract(p, "application/octet-stream")
    assert res.status == "unsupported"
    assert res.method == "skipped"


def test_missing_file(tmp_path: Path):
    res = extract(tmp_path / "nope.pdf", "application/pdf")
    assert res.status == "failed"
    assert res.method == "dispatch"
    assert "not found" in (res.error or "").lower()
