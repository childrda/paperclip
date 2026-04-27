"""Attachment → text extraction.

Each handler returns an :class:`ExtractionResult`. The dispatcher picks a
handler from the attachment's content type, tries the text-layer path
first, and falls back to OCR for scans when enabled.

Binary dependencies (tesseract, LibreOffice) are optional at runtime.
Missing binaries produce a clean ``failed`` or ``empty`` status with an
error message — the pipeline never raises out to the caller.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from pathlib import Path

log = logging.getLogger(__name__)


# Content-type buckets.
_IMAGE_TYPES = frozenset({
    "image/png", "image/jpeg", "image/jpg", "image/gif", "image/tiff",
    "image/bmp", "image/webp",
})

_OFFICE_TYPES = frozenset({
    # Microsoft
    "application/msword",
    "application/vnd.ms-word",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/rtf",
    "text/rtf",
    # OpenDocument
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.oasis.opendocument.presentation",
})


# A PDF with fewer than this many characters per page is treated as a
# scanned document and OCRd instead.
_SCANNED_PDF_CHARS_PER_PAGE = 20


@dataclass
class ExtractionOptions:
    ocr_enabled: bool = True
    ocr_language: str = "eng"
    ocr_dpi: int = 200
    tesseract_cmd: str | None = None
    office_enabled: bool = True
    libreoffice_cmd: str = "soffice"
    timeout_s: int = 180


@dataclass
class ExtractionResult:
    status: str                     # 'ok' | 'empty' | 'unsupported' | 'failed'
    method: str                     # handler label (see schema)
    text: str = ""
    page_count: int | None = None
    ocr_applied: bool = False
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def character_count(self) -> int:
        return len(self.text or "")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _extract_pdf_text_layer(path: Path) -> tuple[str, int | None, str | None]:
    """Return (text, page_count, error) using pypdf's text layer only."""
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as e:
        return "", None, f"pypdf unavailable: {e}"

    try:
        reader = PdfReader(str(path))
    except PdfReadError as e:
        return "", None, f"PdfReadError: {e}"
    except Exception as e:  # corrupt / not-a-pdf
        return "", None, f"pypdf open failed: {e}"

    pages = reader.pages
    parts: list[str] = []
    for page in pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception as e:
            log.warning("pypdf page extract failed: %s", e)
            parts.append("")
    return "\n".join(parts).strip(), len(pages), None


def _ocr_available(opts: ExtractionOptions) -> tuple[bool, str | None]:
    if not opts.ocr_enabled:
        return False, "ocr disabled"
    try:
        import pytesseract  # noqa: F401
    except ImportError as e:
        return False, f"pytesseract unavailable: {e}"
    cmd = opts.tesseract_cmd or shutil.which("tesseract")
    if not cmd:
        return False, "tesseract binary not found on PATH"
    return True, None


def _ocr_image(path: Path, opts: ExtractionOptions) -> ExtractionResult:
    ok, why = _ocr_available(opts)
    if not ok:
        return ExtractionResult(
            status="failed", method="ocr_tesseract",
            error=f"OCR unavailable: {why}",
        )
    try:
        import pytesseract
        from PIL import Image
    except ImportError as e:
        return ExtractionResult(
            status="failed", method="ocr_tesseract",
            error=f"OCR deps missing: {e}",
        )

    if opts.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = opts.tesseract_cmd

    try:
        with Image.open(path) as img:
            text = pytesseract.image_to_string(img, lang=opts.ocr_language)
    except subprocess.TimeoutExpired as e:
        return ExtractionResult(
            status="failed", method="ocr_tesseract",
            error=f"tesseract timed out: {e}",
        )
    except Exception as e:
        return ExtractionResult(
            status="failed", method="ocr_tesseract",
            error=f"tesseract error: {e}",
        )

    text = text.strip()
    return ExtractionResult(
        status="ok" if text else "empty",
        method="ocr_tesseract",
        text=text,
        ocr_applied=True,
    )


def _ocr_pdf_rasterize(path: Path, opts: ExtractionOptions) -> ExtractionResult:
    ok, why = _ocr_available(opts)
    if not ok:
        return ExtractionResult(
            status="failed", method="pdf_ocr",
            error=f"OCR unavailable: {why}",
        )
    try:
        import pypdfium2 as pdfium
        import pytesseract
    except ImportError as e:
        return ExtractionResult(
            status="failed", method="pdf_ocr",
            error=f"OCR deps missing: {e}",
        )

    if opts.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = opts.tesseract_cmd

    try:
        pdf = pdfium.PdfDocument(str(path))
    except Exception as e:
        return ExtractionResult(
            status="failed", method="pdf_ocr",
            error=f"pdfium open failed: {e}",
        )

    try:
        scale = max(opts.ocr_dpi, 72) / 72.0
        parts: list[str] = []
        for i in range(len(pdf)):
            try:
                page = pdf[i]
                pil = page.render(scale=scale).to_pil()
                parts.append(
                    pytesseract.image_to_string(pil, lang=opts.ocr_language)
                )
            except Exception as e:
                log.warning("pdf ocr page %d failed: %s", i, e)
        text = "\n".join(parts).strip()
        return ExtractionResult(
            status="ok" if text else "empty",
            method="pdf_ocr",
            text=text,
            page_count=len(pdf),
            ocr_applied=True,
        )
    finally:
        try:
            pdf.close()
        except Exception:
            pass


def _extract_pdf(path: Path, opts: ExtractionOptions) -> ExtractionResult:
    text, page_count, err = _extract_pdf_text_layer(path)
    if err:
        if opts.ocr_enabled:
            res = _ocr_pdf_rasterize(path, opts)
            if res.status == "ok":
                res.notes.append(f"text-layer failed: {err}")
            return res
        return ExtractionResult(
            status="failed", method="pypdf",
            page_count=page_count, error=err,
        )

    threshold = max(1, page_count or 1) * _SCANNED_PDF_CHARS_PER_PAGE
    is_sparse = len(text) < threshold
    if is_sparse and opts.ocr_enabled:
        ocr_res = _ocr_pdf_rasterize(path, opts)
        if ocr_res.status == "ok" and len(ocr_res.text) > len(text):
            ocr_res.notes.append(
                f"text layer too sparse ({len(text)} chars, {page_count} pages)"
            )
            return ocr_res
        # Fall through to the text-layer result below.

    return ExtractionResult(
        status="ok" if text else "empty",
        method="pypdf",
        text=text,
        page_count=page_count,
    )


def _extract_office(path: Path, opts: ExtractionOptions) -> ExtractionResult:
    if not opts.office_enabled:
        return ExtractionResult(
            status="failed", method="libreoffice+pypdf",
            error="office conversion disabled",
        )
    soffice = shutil.which(opts.libreoffice_cmd) or opts.libreoffice_cmd
    if not Path(soffice).exists() and not shutil.which(opts.libreoffice_cmd):
        return ExtractionResult(
            status="failed", method="libreoffice+pypdf",
            error=f"libreoffice binary not found: {opts.libreoffice_cmd}",
        )

    with tempfile.TemporaryDirectory(prefix="foia_office_") as tmpdir:
        try:
            proc = subprocess.run(
                [
                    soffice, "--headless", "--norestore", "--nologo",
                    "--convert-to", "pdf", "--outdir", tmpdir, str(path),
                ],
                capture_output=True,
                timeout=opts.timeout_s,
            )
        except FileNotFoundError as e:
            return ExtractionResult(
                status="failed", method="libreoffice+pypdf",
                error=f"libreoffice not executable: {e}",
            )
        except subprocess.TimeoutExpired:
            return ExtractionResult(
                status="failed", method="libreoffice+pypdf",
                error=f"libreoffice timed out after {opts.timeout_s}s",
            )
        if proc.returncode != 0:
            return ExtractionResult(
                status="failed", method="libreoffice+pypdf",
                error=(
                    f"libreoffice exit={proc.returncode}: "
                    f"{proc.stderr.decode('utf-8', 'replace').strip()[:400]}"
                ),
            )

        pdfs = list(Path(tmpdir).glob("*.pdf"))
        if not pdfs:
            return ExtractionResult(
                status="failed", method="libreoffice+pypdf",
                error="libreoffice produced no PDF",
            )
        pdf_path = pdfs[0]
        inner = _extract_pdf(pdf_path, opts)
        inner.method = (
            "libreoffice+pdf_ocr" if inner.ocr_applied else "libreoffice+pypdf"
        )
        return inner


def _extract_eml(path: Path) -> ExtractionResult:
    try:
        data = path.read_bytes()
    except OSError as e:
        return ExtractionResult(
            status="failed", method="eml_body", error=f"read failed: {e}",
        )
    try:
        msg = BytesParser(policy=policy.default).parsebytes(data)
    except Exception as e:
        return ExtractionResult(
            status="failed", method="eml_body", error=f"parse failed: {e}",
        )

    lines: list[str] = []
    for h in ("From", "To", "Cc", "Subject", "Date"):
        v = msg.get(h)
        if v is not None:
            lines.append(f"{h}: {v}")

    body_text = ""
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disp = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disp:
            continue
        if ctype == "text/plain" and not body_text:
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            try:
                body_text = payload.decode(charset, errors="replace")
            except LookupError:
                body_text = payload.decode("utf-8", errors="replace")
    if body_text:
        lines.append("")
        lines.append(body_text.strip())

    text = "\n".join(lines).strip()
    return ExtractionResult(
        status="ok" if text else "empty",
        method="eml_body",
        text=text,
    )


def _extract_text_file(path: Path) -> ExtractionResult:
    try:
        data = path.read_bytes()
    except OSError as e:
        return ExtractionResult(
            status="failed", method="text", error=f"read failed: {e}",
        )

    text: str | None = None
    # BOM sniff — only use UTF-16 if we actually see its marker, otherwise
    # any even-length byte string decodes as garbage UTF-16 without error.
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            text = data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    if text is None:
        for charset in ("utf-8", "cp1252", "latin-1"):
            try:
                text = data.decode(charset)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = data.decode("utf-8", errors="replace")

    text = text.strip()
    return ExtractionResult(
        status="ok" if text else "empty",
        method="text",
        text=text,
    )


def _extract_html_file(path: Path) -> ExtractionResult:
    try:
        from .sanitizer import html_to_text
        data = path.read_bytes()
        text = html_to_text(data.decode("utf-8", errors="replace")).strip()
    except Exception as e:
        return ExtractionResult(
            status="failed", method="html", error=f"html parse failed: {e}",
        )
    return ExtractionResult(
        status="ok" if text else "empty", method="html", text=text,
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def extract(
    path: Path,
    content_type: str,
    opts: ExtractionOptions | None = None,
) -> ExtractionResult:
    """Route an attachment to the appropriate handler by content type."""
    opts = opts or ExtractionOptions()
    if not path.exists():
        return ExtractionResult(
            status="failed", method="dispatch",
            error=f"file not found: {path}",
        )

    ct = (content_type or "").lower().split(";", 1)[0].strip()

    try:
        if ct == "application/pdf":
            return _extract_pdf(path, opts)
        if ct in _IMAGE_TYPES or ct.startswith("image/"):
            return _ocr_image(path, opts)
        if ct in _OFFICE_TYPES:
            return _extract_office(path, opts)
        if ct == "message/rfc822":
            return _extract_eml(path)
        if ct == "text/plain":
            return _extract_text_file(path)
        if ct == "text/html":
            return _extract_html_file(path)
    except Exception as e:  # defence in depth
        log.exception("handler raised for %s (%s)", path, ct)
        return ExtractionResult(
            status="failed", method="dispatch", error=f"handler raised: {e}",
        )

    return ExtractionResult(
        status="unsupported", method="skipped",
        error=f"unsupported content-type: {ct or '(unknown)'}",
    )
