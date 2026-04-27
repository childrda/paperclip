"""CLI-level tests for resolve.py."""

from __future__ import annotations

import json
import sqlite3
import sys
from email.message import EmailMessage
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _ingest_sample(tmp_path: Path) -> Path:
    import mailbox
    msgs = []
    m1 = EmailMessage()
    m1["From"] = "Jane Doe <jane@district.org>"
    m1["To"] = "Parent A <parent@example.com>"
    m1["Cc"] = "secretary@district.org"
    m1["Subject"] = "Meeting"
    m1["Date"] = "Mon, 1 Jan 2024 00:00:00 +0000"
    m1.set_content(
        "Hi,\n\nSee you then.\n\n"
        "Best,\nJane Doe\nPrincipal\njane.alt@district.org\n"
    )
    msgs.append(m1)

    m2 = EmailMessage()
    m2["From"] = "Parent A <parent@example.com>"
    m2["To"] = "Jane Doe <jane@district.org>"
    m2["Subject"] = "RE: Meeting"
    m2["Date"] = "Tue, 2 Jan 2024 00:00:00 +0000"
    m2.set_content("Thanks.\n")
    msgs.append(m2)

    mbox_path = tmp_path / "r.mbox"
    if mbox_path.exists():
        mbox_path.unlink()
    box = mailbox.mbox(str(mbox_path))
    box.lock()
    try:
        for m in msgs:
            box.add(m)
        box.flush()
    finally:
        box.unlock(); box.close()

    db_path = tmp_path / "r.db"
    import ingest as ingest_cli
    rc = ingest_cli.main(
        ["--file", str(mbox_path), "--db", str(db_path),
         "--attachments", str(tmp_path / "att")]
    )
    assert rc == 0
    return db_path


def _district_config(tmp_path: Path) -> Path:
    path = tmp_path / "d.yaml"
    path.write_text(
        "district:\n  name: Test\n  email_domains:\n    - district.org\n",
        encoding="utf-8",
    )
    return path


def test_resolve_run_populates_persons(tmp_path: Path, capsys):
    db_path = _ingest_sample(tmp_path)
    cfg = _district_config(tmp_path)
    capsys.readouterr()

    import resolve as resolve_cli
    rc = resolve_cli.main(
        ["--db", str(db_path), "--config", str(cfg), "run"]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["emails_scanned"] == 2
    assert payload["persons_created"] >= 3   # jane, parent, secretary (+maybe sig)
    assert payload["signatures_with_extra_emails"] == 1

    conn = sqlite3.connect(db_path)
    try:
        ppl = conn.execute(
            "SELECT display_name, is_internal FROM persons ORDER BY id"
        ).fetchall()
        internal_count = sum(1 for _, flag in ppl if flag == 1)
        assert internal_count >= 2  # jane + secretary + jane.alt
    finally:
        conn.close()


def test_resolve_list_and_show(tmp_path: Path, capsys):
    db_path = _ingest_sample(tmp_path)
    cfg = _district_config(tmp_path)
    capsys.readouterr()
    import resolve as resolve_cli
    resolve_cli.main(["--db", str(db_path), "--config", str(cfg), "run"])
    capsys.readouterr()

    rc = resolve_cli.main(["--db", str(db_path), "list"])
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows
    # Pick a person with 2+ occurrences for show.
    target = max(rows, key=lambda r: r["occurrences"])
    rc = resolve_cli.main(
        ["--db", str(db_path), "show", str(target["id"])]
    )
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["id"] == target["id"]
    assert "emails" in data
    assert "occurrences_by_type" in data


def test_resolve_merge_and_rename(tmp_path: Path, capsys):
    db_path = _ingest_sample(tmp_path)
    cfg = _district_config(tmp_path)
    capsys.readouterr()
    import resolve as resolve_cli
    resolve_cli.main(["--db", str(db_path), "--config", str(cfg), "run"])
    capsys.readouterr()

    # Pick Jane Doe and jane.alt — representing the same person — merge them.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        jane = conn.execute(
            "SELECT person_id FROM person_emails WHERE email = ?",
            ("jane@district.org",),
        ).fetchone()["person_id"]
        alt = conn.execute(
            "SELECT person_id FROM person_emails WHERE email = ?",
            ("jane.alt@district.org",),
        ).fetchone()["person_id"]
    finally:
        conn.close()

    rc = resolve_cli.main(
        ["--db", str(db_path), "merge", str(alt), str(jane)]
    )
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert result["winner_id"] == jane

    rc = resolve_cli.main(
        ["--db", str(db_path), "rename", str(jane), "Principal Jane Doe"]
    )
    assert rc == 0
    renamed = json.loads(capsys.readouterr().out)
    assert renamed["display_name"] == "Principal Jane Doe"


def test_resolve_show_missing_returns_1(tmp_path: Path):
    db_path = _ingest_sample(tmp_path)
    import resolve as resolve_cli
    rc = resolve_cli.main(["--db", str(db_path), "show", "9999"])
    assert rc == 1


def test_resolve_missing_db(tmp_path: Path):
    import resolve as resolve_cli
    rc = resolve_cli.main(["--db", str(tmp_path / "nope.db"), "list"])
    assert rc == 2
