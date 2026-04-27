"""Phase 6 API tests — CRUD over /api/v1/redactions and exemption-codes."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from foia.api.app import create_app
from foia.config import Config


def _write_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "d.yaml"
    path.write_text(
        """
district:
  name: Test
  email_domains: [district.example.org]
pii_detection:
  builtins: [US_SSN]
exemption_codes:
  - code: FERPA
    description: Federal student records
  - code: PII
    description: Personal info
redaction:
  default_exemption: FERPA
  entity_exemptions:
    US_SSN: PII
""",
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def seeded_client(tmp_path: Path, monkeypatch):
    cfg_yaml = _write_yaml(tmp_path)
    monkeypatch.setenv("FOIA_CONFIG_FILE", str(cfg_yaml))
    db_path = tmp_path / "phase6.db"

    # Build a tiny DB: one email with a known body. Schema init via Config.
    cfg = Config(
        db_path=db_path, attachment_dir=tmp_path / "att",
        log_level="WARNING", ocr_enabled=False, ocr_language="eng",
        ocr_dpi=200, tesseract_cmd=None, office_enabled=False,
        libreoffice_cmd="soffice", extraction_timeout_s=60,
    )
    from foia.db import connect, init_schema
    conn = connect(db_path)
    init_schema(conn)
    cur = conn.execute(
        """
        INSERT INTO emails (mbox_source, mbox_index, subject, body_text,
                            body_html_sanitized, ingested_at)
        VALUES ('t.mbox', 0, 'Hello world',
                'Body 572-68-1439 trailing.', '',
                ?)
        """,
        (datetime.now(timezone.utc).isoformat(),),
    )
    eid = int(cur.lastrowid)
    # Seed one PII detection so /redactions/propose has work.
    cur = conn.execute(
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

    return TestClient(create_app(cfg)), eid


def test_exemption_codes_endpoint(seeded_client):
    client, _ = seeded_client
    r = client.get("/api/v1/exemption-codes")
    assert r.status_code == 200
    codes = {row["code"] for row in r.json()}
    assert codes == {"FERPA", "PII"}


def test_create_redaction_happy(seeded_client):
    client, eid = seeded_client
    r = client.post(
        "/api/v1/redactions",
        json={
            "source_type": "email_body_text",
            "source_id": eid,
            "start_offset": 0,
            "end_offset": 4,
            "exemption_code": "FERPA",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "proposed"
    assert body["origin"] == "manual"
    assert body["exemption_code"] == "FERPA"


def test_create_rejects_unknown_exemption(seeded_client):
    client, eid = seeded_client
    r = client.post(
        "/api/v1/redactions",
        json={
            "source_type": "email_body_text",
            "source_id": eid,
            "start_offset": 0,
            "end_offset": 4,
            "exemption_code": "MADE_UP",
        },
    )
    assert r.status_code == 400
    assert "exemption_code" in r.json()["detail"].lower()


def test_create_rejects_inverted_offsets(seeded_client):
    client, eid = seeded_client
    r = client.post(
        "/api/v1/redactions",
        json={
            "source_type": "email_body_text",
            "source_id": eid,
            "start_offset": 5,
            "end_offset": 2,
            "exemption_code": "FERPA",
        },
    )
    # end_offset must be > 0 passes pydantic; service validation catches
    # the start>=end mismatch and returns 400.
    assert r.status_code in (400, 422)


def test_create_rejects_offset_past_text(seeded_client):
    client, eid = seeded_client
    r = client.post(
        "/api/v1/redactions",
        json={
            "source_type": "email_body_text",
            "source_id": eid,
            "start_offset": 0,
            "end_offset": 9999,
            "exemption_code": "FERPA",
        },
    )
    assert r.status_code == 400
    assert "exceeds source length" in r.json()["detail"]


def test_list_then_filter(seeded_client):
    client, eid = seeded_client
    client.post("/api/v1/redactions", json={
        "source_type": "email_body_text", "source_id": eid,
        "start_offset": 0, "end_offset": 4, "exemption_code": "FERPA",
    })
    client.post("/api/v1/redactions", json={
        "source_type": "email_subject", "source_id": eid,
        "start_offset": 0, "end_offset": 5, "exemption_code": "PII",
    })
    r = client.get("/api/v1/redactions").json()
    assert r["total"] == 2
    r2 = client.get(
        "/api/v1/redactions?source_type=email_subject"
    ).json()
    assert r2["total"] == 1
    r3 = client.get(
        "/api/v1/redactions?exemption_code=PII"
    ).json()
    assert r3["total"] == 1


def test_get_404(seeded_client):
    client, _ = seeded_client
    assert client.get("/api/v1/redactions/9999").status_code == 404


def test_patch_accept_requires_reviewer(seeded_client):
    client, eid = seeded_client
    rid = client.post("/api/v1/redactions", json={
        "source_type": "email_body_text", "source_id": eid,
        "start_offset": 0, "end_offset": 4, "exemption_code": "FERPA",
    }).json()["id"]
    bad = client.patch(
        f"/api/v1/redactions/{rid}", json={"status": "accepted"}
    )
    assert bad.status_code == 400
    good = client.patch(
        f"/api/v1/redactions/{rid}",
        json={"status": "accepted", "reviewer_id": "Records Clerk"},
    )
    assert good.status_code == 200
    assert good.json()["status"] == "accepted"
    assert good.json()["reviewer_id"] == "Records Clerk"


def test_patch_404(seeded_client):
    client, _ = seeded_client
    r = client.patch(
        "/api/v1/redactions/9999",
        json={"status": "accepted", "reviewer_id": "x"},
    )
    assert r.status_code == 404


def test_delete_then_404(seeded_client):
    client, eid = seeded_client
    rid = client.post("/api/v1/redactions", json={
        "source_type": "email_body_text", "source_id": eid,
        "start_offset": 0, "end_offset": 4, "exemption_code": "FERPA",
    }).json()["id"]
    r = client.delete(f"/api/v1/redactions/{rid}")
    assert r.status_code == 204
    assert client.get(f"/api/v1/redactions/{rid}").status_code == 404


def test_create_extra_field_rejected_by_pydantic(seeded_client):
    client, eid = seeded_client
    r = client.post(
        "/api/v1/redactions",
        json={
            "source_type": "email_body_text", "source_id": eid,
            "start_offset": 0, "end_offset": 4, "exemption_code": "FERPA",
            "evil_field": "x",
        },
    )
    assert r.status_code == 422


def test_stats_includes_redaction_counts(seeded_client):
    client, eid = seeded_client
    rid = client.post("/api/v1/redactions", json={
        "source_type": "email_body_text", "source_id": eid,
        "start_offset": 0, "end_offset": 4, "exemption_code": "FERPA",
    }).json()["id"]
    client.patch(
        f"/api/v1/redactions/{rid}",
        json={"status": "accepted", "reviewer_id": "x"},
    )
    s = client.get("/stats").json()
    assert s["redactions"] == 1
    assert s["redactions_accepted"] == 1
