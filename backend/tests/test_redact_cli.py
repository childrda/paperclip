"""End-to-end CLI tests for redact.py."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _write_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "d.yaml"
    p.write_text(
        """
district:
  name: Test
pii_detection:
  builtins: []
exemption_codes:
  - code: FERPA
  - code: PII
redaction:
  default_exemption: FERPA
  entity_exemptions:
    US_SSN: PII
""",
        encoding="utf-8",
    )
    return p


@pytest.fixture()
def seeded(tmp_path: Path) -> tuple[Path, Path, int]:
    """Build a tiny DB with one email and one PII detection. Returns (db, cfg, email_id)."""
    db_path = tmp_path / "phase6.db"
    cfg_path = _write_yaml(tmp_path)
    from foia.db import connect, init_schema
    conn = connect(db_path)
    init_schema(conn)
    cur = conn.execute(
        """
        INSERT INTO emails (mbox_source, mbox_index, subject, body_text,
                            body_html_sanitized, ingested_at)
        VALUES ('t.mbox', 0, 'Subject', 'Body 572-68-1439 trailing.', '', ?)
        """,
        (datetime.now(timezone.utc).isoformat(),),
    )
    eid = int(cur.lastrowid)
    conn.execute(
        """
        INSERT INTO pii_detections (
            source_type, source_id, entity_type, start_offset, end_offset,
            matched_text, score, recognizer, detected_at
        ) VALUES ('email_body_text', ?, 'US_SSN', 5, 16,
                  '572-68-1439', 0.9, 'test', ?)
        """,
        (eid, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    return db_path, cfg_path, eid


def test_propose_seeds_redactions(seeded, capsys):
    db, cfg, _ = seeded
    import redact as redact_cli
    rc = redact_cli.main(
        ["--db", str(db), "--config", str(cfg), "propose"]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["proposed"] == 1
    assert payload["by_entity"]["US_SSN"] == 1


def test_propose_then_list_then_accept_then_reject(seeded, capsys):
    db, cfg, _ = seeded
    import redact as redact_cli
    redact_cli.main(["--db", str(db), "--config", str(cfg), "propose"])
    capsys.readouterr()

    rc = redact_cli.main(
        ["--db", str(db), "--config", str(cfg), "list", "--status", "proposed"]
    )
    assert rc == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["total"] == 1
    rid = listing["items"][0]["id"]

    rc = redact_cli.main(
        ["--db", str(db), "--config", str(cfg),
         "accept", str(rid), "--reviewer", "Records Clerk"]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "accepted"
    assert payload["reviewer_id"] == "Records Clerk"

    rc = redact_cli.main(
        ["--db", str(db), "--config", str(cfg),
         "reject", str(rid), "--reviewer", "Records Clerk", "--note", "actually keep"]
    )
    assert rc == 0
    flipped = json.loads(capsys.readouterr().out)
    assert flipped["status"] == "rejected"
    assert flipped["notes"] == "actually keep"


def test_show_missing_returns_1(seeded):
    db, cfg, _ = seeded
    import redact as redact_cli
    rc = redact_cli.main(
        ["--db", str(db), "--config", str(cfg), "show", "9999"]
    )
    assert rc == 1


def test_exemptions_subcommand(seeded, capsys):
    db, cfg, _ = seeded
    import redact as redact_cli
    rc = redact_cli.main(
        ["--db", str(db), "--config", str(cfg), "exemptions"]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    codes = {x["code"] for x in payload}
    assert codes == {"FERPA", "PII"}


def test_delete_subcommand(seeded, capsys):
    db, cfg, _ = seeded
    import redact as redact_cli
    redact_cli.main(["--db", str(db), "--config", str(cfg), "propose"])
    capsys.readouterr()
    redact_cli.main(["--db", str(db), "--config", str(cfg), "list"])
    listing = json.loads(capsys.readouterr().out)
    rid = listing["items"][0]["id"]

    rc = redact_cli.main(
        ["--db", str(db), "--config", str(cfg), "delete", str(rid)]
    )
    assert rc == 0
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM redactions").fetchone()[0] == 0
    finally:
        conn.close()


def test_accept_unknown_id_returns_1(seeded):
    db, cfg, _ = seeded
    import redact as redact_cli
    rc = redact_cli.main(
        ["--db", str(db), "--config", str(cfg),
         "accept", "9999", "--reviewer", "x"]
    )
    assert rc == 1
