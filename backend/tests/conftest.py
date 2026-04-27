from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from foia.db import connect, init_schema  # noqa: E402


@pytest.fixture()
def db_conn(tmp_path: Path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def attachment_dir(tmp_path: Path) -> Path:
    d = tmp_path / "attachments"
    d.mkdir()
    return d


@pytest.fixture()
def sample_mbox(tmp_path: Path) -> Path:
    from scripts.generate_sample_mbox import build
    out = tmp_path / "sample.mbox"
    build(out)
    return out


def make_text_pdf(path: Path, lines: list[str]) -> Path:
    """Write a one-page PDF containing the given text lines."""
    from reportlab.pdfgen.canvas import Canvas
    c = Canvas(str(path))
    y = 750
    for line in lines:
        c.drawString(72, y, line)
        y -= 18
    c.showPage()
    c.save()
    return path


def make_blank_pdf(path: Path, page_count: int = 1) -> Path:
    """Write a PDF with N empty pages (simulates a scanned PDF)."""
    from reportlab.pdfgen.canvas import Canvas
    c = Canvas(str(path))
    for _ in range(page_count):
        c.showPage()
    c.save()
    return path


@pytest.fixture()
def text_pdf_factory(tmp_path: Path):
    def _mk(lines: list[str], name: str = "doc.pdf") -> Path:
        return make_text_pdf(tmp_path / name, lines)
    return _mk


@pytest.fixture()
def blank_pdf_factory(tmp_path: Path):
    def _mk(page_count: int = 1, name: str = "scan.pdf") -> Path:
        return make_blank_pdf(tmp_path / name, page_count)
    return _mk
