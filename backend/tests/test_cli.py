from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def test_cli_main_ingests(tmp_path: Path, capsys, sample_mbox: Path):
    db_path = tmp_path / "cli.db"
    att_dir = tmp_path / "att"

    import ingest as cli
    rc = cli.main(
        [
            "--file",
            str(sample_mbox),
            "--db",
            str(db_path),
            "--attachments",
            str(att_dir),
            "--label",
            "cli-test",
        ]
    )
    assert rc == 0

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["emails_ingested"] == 5
    assert payload["errors"] == 0

    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
        assert count == 5
    finally:
        conn.close()


def test_cli_missing_file_returns_2(tmp_path: Path):
    import ingest as cli
    rc = cli.main(
        [
            "--file",
            str(tmp_path / "nope.mbox"),
            "--db",
            str(tmp_path / "x.db"),
            "--attachments",
            str(tmp_path / "att"),
        ]
    )
    assert rc == 2
