"""Phase 10 — AI driver and CLI/API integration tests."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from foia.ai import AiFlag, AiProvider
from foia.ai_driver import (
    AiFlagError,
    dismiss_flag,
    get_flag,
    list_flags,
    promote_flag,
    run_ai_qa,
)
from foia.api.app import create_app
from foia.config import Config
from foia.db import connect, init_schema
from foia.district import (
    AiConfig,
    BatesConfig,
    DistrictConfig,
    ExemptionCode,
    PiiDetectionConfig,
    RedactionConfig,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeProvider(AiProvider):
    """Returns a scripted set of flags per text. Deterministic, no network."""
    name = "fake"
    model = "fake-model"

    def __init__(self, script: dict[str, list[AiFlag]]):
        self.script = script
        self.calls = 0

    def flag_risks(self, text: str) -> list[AiFlag]:
        self.calls += 1
        return list(self.script.get(text, []))


def _district() -> DistrictConfig:
    return DistrictConfig(
        name="Test",
        email_domains=("district.test",),
        pii=PiiDetectionConfig(builtins=()),
        exemptions=(ExemptionCode(code="FERPA"), ExemptionCode(code="PII")),
        redaction=RedactionConfig(
            default_exemption="FERPA",
            entity_exemptions={"STUDENT_NAME": "FERPA", "MEDICAL": "PII"},
        ),
        bates=BatesConfig(),
        ai=AiConfig(enabled=True, provider="null", model=None),
    )


def _ins_email(conn, *, idx: int = 0, subject: str = "Hi", body: str = "Hello") -> int:
    cur = conn.execute(
        """
        INSERT INTO emails (mbox_source, mbox_index, subject, body_text,
                            body_html_sanitized, ingested_at)
        VALUES ('t.mbox', ?, ?, ?, '', ?)
        """,
        (idx, subject, body, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Driver: run_ai_qa
# ---------------------------------------------------------------------------


def test_driver_persists_flags(db_conn):
    body = "Met with Sarah today about her IEP."
    eid = _ins_email(db_conn, body=body, subject="IEP follow-up")
    provider = FakeProvider({
        body: [
            AiFlag(
                entity_type="STUDENT_NAME", start=body.index("Sarah"),
                end=body.index("Sarah") + 5, matched_text="Sarah",
                confidence=0.9, rationale="minor",
                suggested_exemption="FERPA",
            ),
            AiFlag(
                entity_type="MEDICAL", start=body.index("IEP"),
                end=body.index("IEP") + 3, matched_text="IEP",
                confidence=0.7, rationale="education record",
            ),
        ],
    })
    stats = run_ai_qa(db_conn, provider)
    assert stats.flags_written == 2
    assert stats.sources_scanned >= 2
    assert stats.qa_run_id

    rows, total = list_flags(db_conn)
    assert total == 2
    kinds = {r["entity_type"] for r in rows}
    assert kinds == {"STUDENT_NAME", "MEDICAL"}
    # provider name + model captured.
    assert all(r["provider"] == "fake" for r in rows)
    assert all(r["model"] == "fake-model" for r in rows)
    assert all(r["review_status"] == "open" for r in rows)


def test_driver_idempotent_unique_index(db_conn):
    body = "Sarah is fine."
    _ins_email(db_conn, body=body)
    flag = AiFlag(
        entity_type="STUDENT_NAME", start=0, end=5,
        matched_text="Sarah", confidence=0.9,
    )
    p = FakeProvider({body: [flag]})
    s1 = run_ai_qa(db_conn, p)
    s2 = run_ai_qa(db_conn, p)
    assert s1.flags_written == 1
    assert s2.flags_written == 0
    assert s2.flags_skipped_existing == 1


def test_driver_handles_provider_failure(db_conn):
    body = "anything"
    _ins_email(db_conn, body=body)

    class Boom(AiProvider):
        name = "boom"; model = None
        def flag_risks(self, text):
            raise RuntimeError("provider exploded")

    stats = run_ai_qa(db_conn, Boom())
    assert stats.sources_failed >= 1
    assert stats.flags_written == 0


def test_driver_skips_empty_sources(db_conn):
    _ins_email(db_conn, subject="", body="")
    stats = run_ai_qa(db_conn, FakeProvider({}))
    assert stats.sources_scanned == 0


def test_driver_only_email_id_filter(db_conn):
    a = _ins_email(db_conn, idx=0, body="aaa")
    b = _ins_email(db_conn, idx=1, body="bbb")
    p = FakeProvider({
        "aaa": [AiFlag(entity_type="X", start=0, end=1,
                       matched_text="a", confidence=0.5)],
        "bbb": [AiFlag(entity_type="Y", start=0, end=1,
                       matched_text="b", confidence=0.5)],
    })
    run_ai_qa(db_conn, p, only_email_id=a)
    rows, total = list_flags(db_conn)
    assert total == 1
    assert rows[0]["source_id"] == a
    assert rows[0]["entity_type"] == "X"


# ---------------------------------------------------------------------------
# Dismiss / promote
# ---------------------------------------------------------------------------


def _flag_one(db_conn) -> int:
    body = "Sarah here."
    _ins_email(db_conn, body=body)
    p = FakeProvider({
        body: [AiFlag(entity_type="STUDENT_NAME", start=0, end=5,
                      matched_text="Sarah", confidence=0.9,
                      suggested_exemption="FERPA")],
    })
    run_ai_qa(db_conn, p)
    rows, _ = list_flags(db_conn)
    return int(rows[0]["id"])


def test_dismiss_marks_status(db_conn):
    fid = _flag_one(db_conn)
    row = dismiss_flag(db_conn, fid, actor="alice", note="nope")
    assert row["review_status"] == "dismissed"
    assert row["review_actor"] == "alice"
    assert row["review_note"] == "nope"
    assert row["reviewed_at"]


def test_dismiss_already_promoted_raises(db_conn):
    fid = _flag_one(db_conn)
    promote_flag(db_conn, _district(), fid, actor="bob")
    with pytest.raises(AiFlagError):
        dismiss_flag(db_conn, fid, actor="bob")


def test_promote_creates_proposed_redaction(db_conn):
    fid = _flag_one(db_conn)
    result = promote_flag(db_conn, _district(), fid, actor="bob")
    flag = get_flag(db_conn, fid)
    red = result["redaction"]
    assert flag["review_status"] == "promoted"
    assert flag["promoted_redaction_id"] == red["id"]
    # Hard rule: proposed, never auto-accepted.
    assert red["status"] == "proposed"
    assert red["origin"] == "manual"   # human action even if AI suggested
    assert red["exemption_code"] == "FERPA"


def test_promote_uses_default_when_no_suggestion(db_conn):
    body = "Other text."
    _ins_email(db_conn, body=body)
    p = FakeProvider({
        body: [AiFlag(entity_type="STUDENT_NAME", start=0, end=5,
                      matched_text="Other", confidence=0.5,
                      suggested_exemption=None)],
    })
    run_ai_qa(db_conn, p)
    fid = list_flags(db_conn)[0][0]["id"]
    result = promote_flag(db_conn, _district(), fid, actor="x")
    # district maps STUDENT_NAME → FERPA via redaction.entity_exemptions.
    assert result["redaction"]["exemption_code"] == "FERPA"


def test_promote_accepts_explicit_exemption(db_conn):
    fid = _flag_one(db_conn)
    result = promote_flag(
        db_conn, _district(), fid, actor="x", exemption_code="PII",
    )
    assert result["redaction"]["exemption_code"] == "PII"


def test_promote_rejects_unknown_exemption(db_conn):
    fid = _flag_one(db_conn)
    with pytest.raises(AiFlagError):
        promote_flag(
            db_conn, _district(), fid, actor="x", exemption_code="MADE_UP",
        )


def test_promote_already_promoted_raises(db_conn):
    fid = _flag_one(db_conn)
    promote_flag(db_conn, _district(), fid, actor="x")
    with pytest.raises(AiFlagError):
        promote_flag(db_conn, _district(), fid, actor="x")


def test_promote_dismissed_raises(db_conn):
    fid = _flag_one(db_conn)
    dismiss_flag(db_conn, fid, actor="x")
    with pytest.raises(AiFlagError):
        promote_flag(db_conn, _district(), fid, actor="x")


def test_promote_when_no_default_and_unmappable(db_conn):
    body = "ZZ here."
    _ins_email(db_conn, body=body)
    p = FakeProvider({
        body: [AiFlag(entity_type="UNKNOWN_KIND", start=0, end=2,
                      matched_text="ZZ", confidence=0.5,
                      suggested_exemption=None)],
    })
    run_ai_qa(db_conn, p)
    fid = list_flags(db_conn)[0][0]["id"]

    bare_district = DistrictConfig(
        name="x", email_domains=(),
        pii=PiiDetectionConfig(builtins=()),
        exemptions=(ExemptionCode(code="FERPA"),),
        redaction=RedactionConfig(default_exemption=None),
    )
    with pytest.raises(AiFlagError):
        promote_flag(bare_district_conn := db_conn, bare_district, fid, actor="x")
    _ = bare_district_conn


def test_promoted_redaction_delete_does_not_break_flag(db_conn):
    """If the redaction is later deleted, the flag's link goes NULL but
    the flag itself stays — the audit trail is preserved by Phase 9."""
    fid = _flag_one(db_conn)
    result = promote_flag(db_conn, _district(), fid, actor="x")
    rid = int(result["redaction"]["id"])
    db_conn.execute("DELETE FROM redactions WHERE id = ?", (rid,))
    db_conn.commit()
    row = get_flag(db_conn, fid)
    assert row["review_status"] == "promoted"
    assert row["promoted_redaction_id"] is None


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded_cli(tmp_path: Path):
    """Fresh DB with one email + one open flag, ready for the qa CLI."""
    db_path = tmp_path / "qa.db"
    cfg_path = tmp_path / "d.yaml"
    cfg_path.write_text(
        """
district:
  name: Test
pii_detection:
  builtins: []
exemption_codes: [{code: FERPA}, {code: PII}]
redaction:
  default_exemption: FERPA
ai:
  enabled: true
  provider: null
""",
        encoding="utf-8",
    )
    conn = connect(db_path)
    init_schema(conn)
    cur = conn.execute(
        """
        INSERT INTO emails (mbox_source, mbox_index, subject, body_text,
                            body_html_sanitized, ingested_at)
        VALUES ('t.mbox', 0, 'Subj', 'Body about Sarah today.', '', ?)
        """,
        (datetime.now(timezone.utc).isoformat(),),
    )
    eid = int(cur.lastrowid)
    conn.execute(
        """
        INSERT INTO ai_flags (
            source_type, source_id, entity_type, start_offset, end_offset,
            matched_text, confidence, rationale, suggested_exemption,
            provider, model, qa_run_id, flagged_at
        ) VALUES (
            'email_body_text', ?, 'STUDENT_NAME', 11, 16, 'Sarah',
            0.9, 'minor', 'FERPA', 'fake', 'fake', 'run-1', ?
        )
        """,
        (eid, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    return db_path, cfg_path


def test_cli_run_with_null_provider(tmp_path: Path, capsys):
    db_path, cfg_path = (tmp_path / "x.db", tmp_path / "d.yaml")
    cfg_path.write_text(
        "district: {name: T}\n"
        "pii_detection: {builtins: []}\n"
        "exemption_codes: [{code: FERPA}]\n"
        "ai: {enabled: false}\n",
        encoding="utf-8",
    )
    conn = connect(db_path)
    init_schema(conn)
    conn.execute(
        """INSERT INTO emails (mbox_source, mbox_index, subject, body_text,
                              body_html_sanitized, ingested_at)
           VALUES ('t.mbox', 0, 's', 'b', '', ?)""",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit(); conn.close()

    import qa as cli
    rc = cli.main(
        ["--db", str(db_path), "--config", str(cfg_path),
         "--actor", "tester", "run", "--provider", "null"]
    )
    assert rc == 0
    payload = capsys.readouterr().out
    assert "flags_written" in payload


def test_cli_list_dismiss_promote(tmp_path: Path, capsys):
    db_path, cfg_path = seeded_cli_files(tmp_path)
    import qa as cli

    # list
    rc = cli.main(
        ["--db", str(db_path), "--config", str(cfg_path), "list"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    import json as _json
    listing = _json.loads(out)
    assert listing["total"] == 1
    fid = listing["items"][0]["id"]

    # show
    rc = cli.main(
        ["--db", str(db_path), "--config", str(cfg_path),
         "show", str(fid)]
    )
    assert rc == 0
    capsys.readouterr()

    # promote
    rc = cli.main(
        ["--db", str(db_path), "--config", str(cfg_path),
         "--actor", "tester", "promote", str(fid)]
    )
    assert rc == 0
    promoted = _json.loads(capsys.readouterr().out)
    assert promoted["review_status"] == "promoted"
    rid = promoted["redaction"]["id"]
    assert promoted["redaction"]["status"] == "proposed"
    _ = rid

    # cannot promote again
    rc = cli.main(
        ["--db", str(db_path), "--config", str(cfg_path),
         "--actor", "tester", "promote", str(fid)]
    )
    assert rc == 1


def test_cli_dismiss_path(tmp_path: Path, capsys):
    db_path, cfg_path = seeded_cli_files(tmp_path)
    import qa as cli
    cli.main([
        "--db", str(db_path), "--config", str(cfg_path), "list",
    ])
    listing = __import__("json").loads(capsys.readouterr().out)
    fid = listing["items"][0]["id"]
    rc = cli.main([
        "--db", str(db_path), "--config", str(cfg_path),
        "--actor", "tester", "dismiss", str(fid),
        "--note", "false positive",
    ])
    assert rc == 0
    out = __import__("json").loads(capsys.readouterr().out)
    assert out["review_status"] == "dismissed"
    assert out["review_note"] == "false positive"


def test_cli_show_missing_returns_1(tmp_path: Path):
    db_path, cfg_path = seeded_cli_files(tmp_path)
    import qa as cli
    rc = cli.main(["--db", str(db_path), "--config", str(cfg_path),
                   "show", "9999"])
    assert rc == 1


def seeded_cli_files(tmp_path: Path):
    """Helper duplicating the seeded_cli fixture inline (so non-fixture tests can use it)."""
    db_path = tmp_path / "q.db"
    cfg_path = tmp_path / "d.yaml"
    cfg_path.write_text(
        """
district:
  name: Test
pii_detection:
  builtins: []
exemption_codes: [{code: FERPA}, {code: PII}]
redaction:
  default_exemption: FERPA
ai:
  enabled: true
  provider: null
""",
        encoding="utf-8",
    )
    conn = connect(db_path)
    init_schema(conn)
    cur = conn.execute(
        """INSERT INTO emails (mbox_source, mbox_index, subject, body_text,
                              body_html_sanitized, ingested_at)
           VALUES ('t.mbox', 0, 'Subj', 'Body about Sarah today.', '', ?)""",
        (datetime.now(timezone.utc).isoformat(),),
    )
    eid = int(cur.lastrowid)
    conn.execute(
        """
        INSERT INTO ai_flags (
            source_type, source_id, entity_type, start_offset, end_offset,
            matched_text, confidence, rationale, suggested_exemption,
            provider, model, qa_run_id, flagged_at
        ) VALUES ('email_body_text', ?, 'STUDENT_NAME', 11, 16, 'Sarah',
                  0.9, 'minor', 'FERPA', 'fake', 'fake', 'run-1', ?)
        """,
        (eid, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit(); conn.close()
    return db_path, cfg_path


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def api_client(tmp_path: Path, monkeypatch):
    db_path, cfg_path = seeded_cli_files(tmp_path)
    monkeypatch.setenv("FOIA_CONFIG_FILE", str(cfg_path))
    cfg = Config(
        db_path=db_path, attachment_dir=tmp_path / "att",
        log_level="WARNING", ocr_enabled=False, ocr_language="eng",
        ocr_dpi=200, tesseract_cmd=None, office_enabled=False,
        libreoffice_cmd="soffice", extraction_timeout_s=60,
        cors_origins=("http://localhost:5173",),
        export_dir=tmp_path / "exports",
    )
    client = TestClient(create_app(cfg))
    fid = client.get("/api/v1/ai-flags").json()["items"][0]["id"]
    return client, fid


def test_api_list_filters(api_client):
    client, _ = api_client
    r = client.get("/api/v1/ai-flags").json()
    assert r["total"] == 1
    only_dismissed = client.get("/api/v1/ai-flags?status=dismissed").json()
    assert only_dismissed["total"] == 0


def test_api_get_flag(api_client):
    client, fid = api_client
    r = client.get(f"/api/v1/ai-flags/{fid}")
    assert r.status_code == 200
    assert r.json()["matched_text"] == "Sarah"


def test_api_get_flag_404(api_client):
    client, _ = api_client
    assert client.get("/api/v1/ai-flags/9999").status_code == 404


def test_api_run_with_null_provider(api_client):
    client, _ = api_client
    r = client.post(
        "/api/v1/ai-flags/run",
        headers={"X-FOIA-Reviewer": "analyst"},
        json={"provider": "null"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "qa_run_id" in body
    # Audit row recorded.
    audit = client.get("/api/v1/audit?event_type=ai_qa.run").json()
    assert audit["total"] == 1
    assert audit["items"][0]["actor"] == "analyst"


def test_api_dismiss_records_actor(api_client):
    client, fid = api_client
    r = client.patch(
        f"/api/v1/ai-flags/{fid}/dismiss",
        headers={"X-FOIA-Reviewer": "Janet"},
        json={"note": "false positive"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["review_status"] == "dismissed"
    assert body["review_actor"] == "Janet"


def test_api_promote_creates_proposed_redaction(api_client):
    client, fid = api_client
    r = client.post(
        f"/api/v1/ai-flags/{fid}/promote",
        headers={"X-FOIA-Reviewer": "Janet"},
        json={},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["flag"]["review_status"] == "promoted"
    assert body["redaction"]["status"] == "proposed"     # AI never auto-redacts
    assert body["redaction"]["origin"] == "manual"       # human-attributed
    # Listing redactions should now include the new proposed row.
    reds = client.get("/api/v1/redactions").json()
    assert reds["total"] == 1


def test_api_promote_unknown_exemption_400(api_client):
    client, fid = api_client
    r = client.post(
        f"/api/v1/ai-flags/{fid}/promote",
        headers={"X-FOIA-Reviewer": "Janet"},
        json={"exemption_code": "MADE_UP"},
    )
    assert r.status_code == 400


def test_api_promote_extra_fields_rejected(api_client):
    client, fid = api_client
    r = client.post(
        f"/api/v1/ai-flags/{fid}/promote",
        json={"evil": "x"},
    )
    assert r.status_code == 422


def test_api_run_extra_fields_rejected(api_client):
    client, _ = api_client
    r = client.post("/api/v1/ai-flags/run", json={"evil": "x"})
    assert r.status_code == 422


def test_openapi_includes_ai_routes(api_client):
    client, _ = api_client
    paths = set(client.get("/openapi.json").json()["paths"].keys())
    for p in (
        "/api/v1/ai-flags",
        "/api/v1/ai-flags/{flag_id}",
        "/api/v1/ai-flags/run",
        "/api/v1/ai-flags/{flag_id}/dismiss",
        "/api/v1/ai-flags/{flag_id}/promote",
    ):
        assert p in paths, f"missing {p}"


def test_promote_then_redaction_fully_reviewable(api_client):
    """The whole spec rule expressed as a test: AI flag → proposed redaction
    → human accept → only then does it become 'real'."""
    client, fid = api_client
    promote = client.post(
        f"/api/v1/ai-flags/{fid}/promote",
        headers={"X-FOIA-Reviewer": "alice"},
        json={},
    ).json()
    rid = promote["redaction"]["id"]

    # Cannot accept without a reviewer (Phase 6 enforcement still applies).
    bad = client.patch(
        f"/api/v1/redactions/{rid}",
        json={"status": "accepted"},
    )
    assert bad.status_code == 400
    good = client.patch(
        f"/api/v1/redactions/{rid}",
        headers={"X-FOIA-Reviewer": "alice"},
        json={"status": "accepted", "reviewer_id": "alice"},
    )
    assert good.status_code == 200
    assert good.json()["status"] == "accepted"


# ---------------------------------------------------------------------------
# Schema constraint
# ---------------------------------------------------------------------------


def test_schema_blocks_invalid_review_status(db_conn):
    import sqlite3
    eid = _ins_email(db_conn, body="x")
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            """
            INSERT INTO ai_flags (
                source_type, source_id, entity_type,
                start_offset, end_offset, matched_text, confidence,
                provider, qa_run_id, flagged_at, review_status
            ) VALUES ('email_body_text', ?, 'X',
                      0, 1, 'a', 0.5, 'fake', 'r', 'now', 'bogus')
            """,
            (eid,),
        )
        db_conn.commit()


def test_schema_blocks_invalid_offset(db_conn):
    import sqlite3
    eid = _ins_email(db_conn, body="x")
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            """
            INSERT INTO ai_flags (
                source_type, source_id, entity_type,
                start_offset, end_offset, matched_text, confidence,
                provider, qa_run_id, flagged_at
            ) VALUES ('email_body_text', ?, 'X', 5, 2, 'a', 0.5,
                      'fake', 'r', 'now')
            """,
            (eid,),
        )
        db_conn.commit()
