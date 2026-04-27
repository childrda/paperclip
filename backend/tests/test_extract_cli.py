"""CLI-level tests for the extract.py entry point."""

from __future__ import annotations

import json
import sqlite3
import sys
from email.message import EmailMessage
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _build_db_with_text_pdf(tmp_path: Path, pdf_path: Path) -> tuple[Path, Path]:
    """Ingest a 1-message mbox with the given PDF attachment into a fresh DB."""
    import mailbox
    m = EmailMessage()
    m["From"] = "a@x.org"; m["To"] = "b@x.org"; m["Subject"] = "for cli"
    m["Date"] = "Mon, 1 Jan 2024 00:00:00 +0000"
    m.set_content("see attached")
    m.add_attachment(
        pdf_path.read_bytes(),
        maintype="application", subtype="pdf", filename="cli.pdf",
    )
    mbox_path = tmp_path / "cli.mbox"
    if mbox_path.exists():
        mbox_path.unlink()
    box = mailbox.mbox(str(mbox_path))
    box.lock()
    try:
        box.add(m); box.flush()
    finally:
        box.unlock(); box.close()

    db_path = tmp_path / "cli.db"
    att_dir = tmp_path / "att"
    import ingest as ingest_cli
    rc = ingest_cli.main(
        ["--file", str(mbox_path), "--db", str(db_path), "--attachments", str(att_dir)]
    )
    assert rc == 0
    return db_path, att_dir


def test_extract_cli_happy_path(tmp_path: Path, capsys, text_pdf_factory):
    pdf = text_pdf_factory(["CLI-run content"])
    db_path, _ = _build_db_with_text_pdf(tmp_path, pdf)
    capsys.readouterr()  # drain ingestion output

    import extract as extract_cli
    rc = extract_cli.main(
        ["--db", str(db_path), "--no-ocr", "--no-office"]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 1
    assert payload["extracted_ok"] == 1

    conn = sqlite3.connect(db_path)
    try:
        text = conn.execute(
            "SELECT extracted_text FROM attachments_text"
        ).fetchone()[0]
        assert "CLI-run content" in text
    finally:
        conn.close()


def test_extract_cli_missing_db(tmp_path: Path):
    import extract as extract_cli
    rc = extract_cli.main(["--db", str(tmp_path / "nope.db")])
    assert rc == 2


def test_extract_cli_force(tmp_path: Path, capsys, text_pdf_factory):
    pdf = text_pdf_factory(["force me"])
    db_path, _ = _build_db_with_text_pdf(tmp_path, pdf)
    capsys.readouterr()

    import extract as extract_cli
    extract_cli.main(["--db", str(db_path), "--no-ocr", "--no-office"])
    capsys.readouterr()
    rc = extract_cli.main(
        ["--db", str(db_path), "--no-ocr", "--no-office", "--force"]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 1  # reprocessed
