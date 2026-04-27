"""Phase 9 — audit log unit + integration tests.

Covers:
  * append-only triggers (UPDATE/DELETE blocked)
  * log_event payload shape and JSON round-trip
  * query_events filters
  * actor resolution from --actor / FOIA_ACTOR / username
  * every CLI hook writes its expected event
  * every API write hook records actor from X-FOIA-Reviewer
  * /api/v1/audit endpoint pagination + filtering
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from foia import audit
from foia.api.app import create_app
from foia.config import Config


# ---------------------------------------------------------------------------
# DB-level immutability
# ---------------------------------------------------------------------------


def test_log_event_inserts_row(db_conn):
    rid = audit.log_event(
        db_conn,
        event_type=audit.EVT_INGEST_RUN,
        actor="alice",
        source_type="mbox",
        payload={"file": "x.mbox", "count": 5},
    )
    row = db_conn.execute(
        "SELECT * FROM audit_log WHERE id = ?", (rid,),
    ).fetchone()
    assert row["event_type"] == audit.EVT_INGEST_RUN
    assert row["actor"] == "alice"
    assert row["request_origin"] == "cli"
    assert json.loads(row["payload_json"]) == {"file": "x.mbox", "count": 5}


def test_log_event_rejects_invalid_origin(db_conn):
    with pytest.raises(ValueError):
        audit.log_event(
            db_conn, event_type="x", actor="a", origin="bogus",
        )


def test_audit_log_blocks_update(db_conn):
    audit.log_event(db_conn, event_type="x.test", actor="a")
    with pytest.raises(sqlite3.IntegrityError) as exc:
        db_conn.execute("UPDATE audit_log SET actor = 'mallory'")
        db_conn.commit()
    assert "append-only" in str(exc.value).lower()


def test_audit_log_blocks_delete(db_conn):
    audit.log_event(db_conn, event_type="x.test", actor="a")
    with pytest.raises(sqlite3.IntegrityError) as exc:
        db_conn.execute("DELETE FROM audit_log")
        db_conn.commit()
    assert "append-only" in str(exc.value).lower()


def test_query_events_filters_and_order(db_conn):
    audit.log_event(db_conn, event_type="x.a", actor="alice")
    audit.log_event(db_conn, event_type="x.b", actor="bob")
    audit.log_event(db_conn, event_type="x.a", actor="alice")
    items, total = audit.query_events(db_conn)
    assert total == 3
    # Newest first.
    assert items[0]["event_type"] == "x.a"
    items_alice, total_alice = audit.query_events(db_conn, actor="alice")
    assert total_alice == 2
    items_b, total_b = audit.query_events(db_conn, event_type="x.b")
    assert total_b == 1 and items_b[0]["actor"] == "bob"


def test_query_events_payload_round_trip(db_conn):
    audit.log_event(
        db_conn, event_type="t", actor="a",
        payload={"nested": {"k": [1, 2]}, "ts": datetime(2026, 4, 1)},
    )
    items, _ = audit.query_events(db_conn)
    assert items[0]["payload"]["nested"] == {"k": [1, 2]}


# ---------------------------------------------------------------------------
# Actor resolution
# ---------------------------------------------------------------------------


def _ns(actor=None) -> argparse.Namespace:
    return argparse.Namespace(actor=actor)


def test_resolve_actor_arg_wins(monkeypatch):
    monkeypatch.setenv("FOIA_ACTOR", "from-env")
    assert audit.resolve_actor(_ns(actor="cli-arg")) == "cli-arg"


def test_resolve_actor_env_fallback(monkeypatch):
    monkeypatch.setenv("FOIA_ACTOR", "from-env")
    assert audit.resolve_actor(_ns(actor=None)) == "from-env"


def test_resolve_actor_username_fallback(monkeypatch):
    monkeypatch.delenv("FOIA_ACTOR", raising=False)
    actor = audit.resolve_actor(_ns(actor=None))
    assert actor.startswith("cli:")


# ---------------------------------------------------------------------------
# CLI hooks (one per phase)
# ---------------------------------------------------------------------------


def _ingest_minimal_mbox(tmp_path: Path) -> Path:
    import mailbox
    m = EmailMessage()
    m["From"] = "a@x.org"; m["To"] = "b@x.org"
    m["Subject"] = "Test"; m["Date"] = "Mon, 1 Jan 2024 00:00:00 +0000"
    m.set_content("Body 572-68-1439 trailing.")
    p = tmp_path / "a.mbox"
    if p.exists():
        p.unlink()
    box = mailbox.mbox(str(p))
    box.lock()
    try:
        box.add(m); box.flush()
    finally:
        box.unlock(); box.close()
    return p


def _yaml(tmp_path: Path) -> Path:
    p = tmp_path / "d.yaml"
    p.write_text(
        "district:\n  name: Test\n"
        "pii_detection:\n  builtins: [US_SSN]\n"
        "exemption_codes: [{code: FERPA}, {code: PII}]\n"
        "redaction:\n  default_exemption: FERPA\n"
        "  entity_exemptions: {US_SSN: PII}\n",
        encoding="utf-8",
    )
    return p


def test_ingest_cli_writes_audit_row(tmp_path: Path):
    mbox = _ingest_minimal_mbox(tmp_path)
    db_path = tmp_path / "a.db"
    import ingest as cli
    rc = cli.main([
        "--file", str(mbox),
        "--db", str(db_path),
        "--attachments", str(tmp_path / "att"),
        "--actor", "alice",
    ])
    assert rc == 0
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE event_type = ?",
            (audit.EVT_INGEST_RUN,),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["actor"] == "alice"
        assert rows[0]["request_origin"] == "cli"
        payload = json.loads(rows[0]["payload_json"])
        assert payload["emails_ingested"] == 1
    finally:
        conn.close()


def test_extract_detect_redact_export_cli_emit_audit(tmp_path: Path):
    mbox = _ingest_minimal_mbox(tmp_path)
    db = tmp_path / "p.db"
    cfg = _yaml(tmp_path)

    import ingest, extract, detect, redact, export
    assert ingest.main([
        "--file", str(mbox), "--db", str(db),
        "--attachments", str(tmp_path / "att"),
        "--actor", "alice",
    ]) == 0
    assert extract.main([
        "--db", str(db), "--no-ocr", "--no-office", "--actor", "bob",
    ]) == 0
    assert detect.main([
        "--db", str(db), "--config", str(cfg), "--actor", "carol",
    ]) == 0
    assert redact.main([
        "--db", str(db), "--config", str(cfg), "--actor", "dan", "propose",
    ]) == 0
    # Pull a redaction id back out so we can accept it via CLI.
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rid = conn.execute("SELECT id FROM redactions LIMIT 1").fetchone()["id"]
    conn.close()
    assert redact.main([
        "--db", str(db), "--config", str(cfg), "--actor", "eve",
        "accept", str(rid), "--reviewer", "eve",
    ]) == 0
    assert export.main([
        "--db", str(db), "--config", str(cfg),
        "--out", str(tmp_path / "out"), "--actor", "frank",
    ]) == 0

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        actors_by_event = {
            r["event_type"]: r["actor"]
            for r in conn.execute(
                "SELECT event_type, actor FROM audit_log "
                "ORDER BY id DESC"
            )
        }
    finally:
        conn.close()
    expected = {
        audit.EVT_INGEST_RUN: "alice",
        audit.EVT_EXTRACT_RUN: "bob",
        audit.EVT_DETECTION_RUN: "carol",
        audit.EVT_REDACTION_PROPOSE: "dan",
        audit.EVT_REDACTION_UPDATE: "eve",
        audit.EVT_EXPORT_RUN: "frank",
    }
    for evt, who in expected.items():
        assert actors_by_event.get(evt) == who, f"missing or wrong actor for {evt}"


def test_resolve_cli_audit_subcommands(tmp_path: Path):
    mbox = _ingest_minimal_mbox(tmp_path)
    db = tmp_path / "r.db"
    cfg = _yaml(tmp_path)
    import ingest, resolve
    ingest.main([
        "--file", str(mbox), "--db", str(db),
        "--attachments", str(tmp_path / "att"),
    ])
    assert resolve.main([
        "--db", str(db), "--config", str(cfg), "--actor", "amy", "run",
    ]) == 0
    # Pull a person to rename and note.
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    pid = conn.execute("SELECT id FROM persons LIMIT 1").fetchone()["id"]
    conn.close()
    assert resolve.main([
        "--db", str(db), "--actor", "amy",
        "rename", str(pid), "Renamed Person",
    ]) == 0
    assert resolve.main([
        "--db", str(db), "--actor", "amy",
        "note", str(pid), "test note",
    ]) == 0
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        rows = {
            r["event_type"]: r for r in conn.execute(
                "SELECT * FROM audit_log WHERE event_type LIKE 'resolve.%'"
            )
        }
    finally:
        conn.close()
    assert audit.EVT_RESOLVE_RUN in rows
    assert audit.EVT_RESOLVE_RENAME in rows
    assert audit.EVT_RESOLVE_NOTE in rows
    rename_payload = json.loads(rows[audit.EVT_RESOLVE_RENAME]["payload_json"])
    assert rename_payload["new_display_name"] == "Renamed Person"


# ---------------------------------------------------------------------------
# API hooks
# ---------------------------------------------------------------------------


@pytest.fixture()
def api_client(tmp_path: Path, monkeypatch):
    cfg_yaml = _yaml(tmp_path)
    monkeypatch.setenv("FOIA_CONFIG_FILE", str(cfg_yaml))
    db_path = tmp_path / "api.db"
    cfg = Config(
        db_path=db_path, attachment_dir=tmp_path / "att",
        log_level="WARNING", ocr_enabled=False, ocr_language="eng",
        ocr_dpi=200, tesseract_cmd=None, office_enabled=False,
        libreoffice_cmd="soffice", extraction_timeout_s=60,
        cors_origins=("http://localhost:5173",),
        export_dir=tmp_path / "exports",
    )

    # Seed: one email + one PII detection so propose has something to do.
    from foia.db import connect, init_schema
    conn = connect(db_path)
    init_schema(conn)
    cur = conn.execute(
        """
        INSERT INTO emails (mbox_source, mbox_index, subject, body_text,
                            body_html_sanitized, ingested_at)
        VALUES ('t.mbox', 0, 'Subj', 'Body 572-68-1439 trailing.', '', ?)
        """,
        (datetime.now(timezone.utc).isoformat(),),
    )
    eid = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return TestClient(create_app(cfg)), eid


def test_api_create_redaction_records_actor_from_header(api_client):
    client, eid = api_client
    r = client.post(
        "/api/v1/redactions",
        headers={"X-FOIA-Reviewer": "Inspector Janet"},
        json={
            "source_type": "email_body_text", "source_id": eid,
            "start_offset": 0, "end_offset": 4, "exemption_code": "FERPA",
        },
    )
    assert r.status_code == 201, r.text

    audit_resp = client.get(
        f"/api/v1/audit?event_type={audit.EVT_REDACTION_CREATE}"
    ).json()
    assert audit_resp["total"] == 1
    row = audit_resp["items"][0]
    assert row["actor"] == "Inspector Janet"
    assert row["request_origin"] == "api"
    assert row["source_type"] == "redaction"
    assert row["payload"]["exemption_code"] == "FERPA"


def test_api_anonymous_actor_recorded(api_client):
    client, eid = api_client
    # No X-FOIA-Reviewer header → "api:anonymous" actor in the log.
    r = client.post(
        "/api/v1/redactions",
        json={
            "source_type": "email_body_text", "source_id": eid,
            "start_offset": 0, "end_offset": 4, "exemption_code": "FERPA",
        },
    )
    assert r.status_code == 201
    rows = client.get(
        f"/api/v1/audit?event_type={audit.EVT_REDACTION_CREATE}"
    ).json()
    assert rows["items"][0]["actor"] == "api:anonymous"


def test_api_patch_and_delete_logged(api_client):
    client, eid = api_client
    rid = client.post(
        "/api/v1/redactions",
        headers={"X-FOIA-Reviewer": "creator"},
        json={
            "source_type": "email_body_text", "source_id": eid,
            "start_offset": 0, "end_offset": 4, "exemption_code": "FERPA",
        },
    ).json()["id"]
    client.patch(
        f"/api/v1/redactions/{rid}",
        headers={"X-FOIA-Reviewer": "patcher"},
        json={"status": "accepted", "reviewer_id": "reviewer-y"},
    )
    client.delete(
        f"/api/v1/redactions/{rid}",
        headers={"X-FOIA-Reviewer": "deleter"},
    )
    rows = client.get("/api/v1/audit?limit=20").json()["items"]
    by_evt = {r["event_type"]: r for r in rows}
    assert by_evt[audit.EVT_REDACTION_CREATE]["actor"] == "creator"
    assert by_evt[audit.EVT_REDACTION_UPDATE]["actor"] == "patcher"
    assert by_evt[audit.EVT_REDACTION_DELETE]["actor"] == "deleter"
    # Deleted-redaction audit row survives the cascade — that's the whole point.


def test_api_export_logged(api_client):
    client, _ = api_client
    r = client.post(
        "/api/v1/exports",
        headers={"X-FOIA-Reviewer": "exporter"},
        json={},
    )
    assert r.status_code == 200
    rows = client.get(
        f"/api/v1/audit?event_type={audit.EVT_EXPORT_RUN}"
    ).json()["items"]
    assert rows[0]["actor"] == "exporter"
    assert rows[0]["payload"]["export_id"]


def test_audit_endpoint_filters_pagination_origin(api_client):
    client, eid = api_client
    # 3 different actors so pagination + filter combos are exercised. Distinct
    # (start, end) per call so the UNIQUE index doesn't collapse them.
    for i, actor in enumerate(("a", "b", "c")):
        r = client.post(
            "/api/v1/redactions",
            headers={"X-FOIA-Reviewer": actor},
            json={
                "source_type": "email_body_text", "source_id": eid,
                "start_offset": i * 3,
                "end_offset": i * 3 + 2,
                "exemption_code": "FERPA",
            },
        )
        assert r.status_code == 201, r.text
    page1 = client.get("/api/v1/audit?limit=2").json()
    assert page1["total"] == 3
    assert len(page1["items"]) == 2
    # Newest first.
    assert page1["items"][0]["actor"] == "c"
    page2 = client.get("/api/v1/audit?limit=2&offset=2").json()
    assert len(page2["items"]) == 1
    assert page2["items"][0]["actor"] == "a"

    # Origin filter
    api_only = client.get("/api/v1/audit?origin=api").json()
    assert api_only["total"] == 3

    # Actor filter
    a_only = client.get("/api/v1/audit?actor=b").json()
    assert a_only["total"] == 1
    assert a_only["items"][0]["actor"] == "b"


def test_openapi_includes_audit_route(api_client):
    client, _ = api_client
    paths = set(client.get("/openapi.json").json()["paths"].keys())
    assert "/api/v1/audit" in paths


def test_audit_endpoint_after_before_filters(api_client):
    client, eid = api_client
    before_create = datetime.now(timezone.utc).isoformat()
    client.post(
        "/api/v1/redactions",
        headers={"X-FOIA-Reviewer": "x"},
        json={
            "source_type": "email_body_text", "source_id": eid,
            "start_offset": 0, "end_offset": 2, "exemption_code": "FERPA",
        },
    )
    after_query = client.get(
        f"/api/v1/audit?after={before_create}"
    ).json()
    assert after_query["total"] == 1


# ---------------------------------------------------------------------------
# Backwards-compatibility: writes via the underlying create() functions
# without an actor still log "system" rather than crashing.
# ---------------------------------------------------------------------------


def test_log_event_default_actor_when_none(db_conn):
    rid = audit.log_event(db_conn, event_type="t", actor="")
    actor = db_conn.execute(
        "SELECT actor FROM audit_log WHERE id = ?", (rid,)
    ).fetchone()[0]
    assert actor == "system"
