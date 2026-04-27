"""End-to-end CLI tests for detect.py and evaluate.py."""

from __future__ import annotations

import json
import sqlite3
import sys
from email.message import EmailMessage
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "d.yaml"
    path.write_text(
        "district:\n"
        "  name: Test\n"
        "pii_detection:\n"
        "  builtins: [US_SSN, EMAIL_ADDRESS]\n"
        "  min_score: 0.3\n"
        "  custom_recognizers:\n"
        "    - name: SID\n"
        "      entity_type: STUDENT_ID\n"
        "      patterns: [{regex: '\\b\\d{8}\\b', score: 0.7}]\n",
        encoding="utf-8",
    )
    return path


def _ingest_message(tmp_path: Path, subject: str, body: str) -> Path:
    import mailbox
    m = EmailMessage()
    m["From"] = "a@x.org"; m["To"] = "b@x.org"
    m["Subject"] = subject
    m["Date"] = "Mon, 1 Jan 2024 00:00:00 +0000"
    m.set_content(body)

    mbox_path = tmp_path / "d.mbox"
    if mbox_path.exists():
        mbox_path.unlink()
    box = mailbox.mbox(str(mbox_path))
    box.lock()
    try:
        box.add(m); box.flush()
    finally:
        box.unlock(); box.close()

    db_path = tmp_path / "d.db"
    att_dir = tmp_path / "att"
    import ingest as ingest_cli
    rc = ingest_cli.main(
        ["--file", str(mbox_path), "--db", str(db_path), "--attachments", str(att_dir)]
    )
    assert rc == 0
    return db_path


def test_detect_cli_populates_pii_detections(tmp_path: Path, capsys):
    cfg_path = _write_config(tmp_path)
    db_path = _ingest_message(
        tmp_path,
        subject="Student ID 82746153",
        body="Parent email jane@example.com. SSN 572-68-1439.",
    )
    capsys.readouterr()

    import detect as detect_cli
    rc = detect_cli.main(["--db", str(db_path), "--config", str(cfg_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sources_scanned"] >= 1
    assert payload["detections_written"] >= 3
    assert payload["by_entity"].get("US_SSN", 0) >= 1
    assert payload["by_entity"].get("EMAIL_ADDRESS", 0) >= 1
    assert payload["by_entity"].get("STUDENT_ID", 0) >= 1

    conn = sqlite3.connect(db_path)
    try:
        kinds = [
            r[0] for r in conn.execute("SELECT entity_type FROM pii_detections")
        ]
        assert "US_SSN" in kinds
        assert "EMAIL_ADDRESS" in kinds
        assert "STUDENT_ID" in kinds
    finally:
        conn.close()


def test_detect_cli_missing_db(tmp_path: Path):
    import detect as detect_cli
    rc = detect_cli.main(["--db", str(tmp_path / "nope.db")])
    assert rc == 2


def test_evaluate_cli_prints_report(tmp_path: Path, capsys):
    cfg_path = _write_config(tmp_path)

    import evaluate as evaluate_cli
    rc = evaluate_cli.main(["--config", str(cfg_path), "--n", "20", "--seed", "0"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["docs_evaluated"] == 20
    assert "per_entity" in payload
    assert "US_SSN" in payload["per_entity"]
