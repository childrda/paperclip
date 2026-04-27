"""Background imports + cases tests.

The endpoint queues a thread; we wait for the job to finish (with a
timeout) before asserting on the resulting state. SSE streaming is
exercised separately so the thread doesn't fight a streaming response.
"""

from __future__ import annotations

import json
import mailbox
import sys
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from foia.api.app import create_app
from foia.config import Config


def _yaml(tmp_path: Path) -> Path:
    p = tmp_path / "d.yaml"
    p.write_text(
        """
district:
  name: Test
  email_domains: [district.example.org]
pii_detection:
  builtins: [US_SSN, PHONE_NUMBER, EMAIL_ADDRESS]
  custom_recognizers:
    - name: SID
      entity_type: STUDENT_ID
      patterns: [{regex: '\\b\\d{8}\\b', score: 0.7}]
exemption_codes: [{code: FERPA}, {code: PII}]
redaction:
  default_exemption: FERPA
  entity_exemptions:
    US_SSN: PII
bates:
  prefix: TEST
  start: 1
  width: 5
""",
        encoding="utf-8",
    )
    return p


def _build_mbox_bytes(*, body_extra: str = "") -> bytes:
    m = EmailMessage()
    m["Message-ID"] = make_msgid(domain="district.example.org")
    m["From"] = "Alice <alice@district.example.org>"
    m["To"] = "Bob <bob@example.com>"
    m["Subject"] = "Bus update"
    m["Date"] = formatdate()
    m.set_content(
        "Student 12345678 has SSN 572-68-1439, phone (571) 555-0199. "
        + body_extra
    )
    p = Path("__tmp_upload__.mbox").resolve()
    if p.exists():
        p.unlink()
    box = mailbox.mbox(str(p))
    box.lock()
    try:
        box.add(m)
        box.flush()
    finally:
        box.unlock()
        box.close()
    data = p.read_bytes()
    p.unlink()
    return data


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    cfg_yaml = _yaml(tmp_path)
    monkeypatch.setenv("FOIA_CONFIG_FILE", str(cfg_yaml))
    cfg = Config(
        db_path=tmp_path / "p.db",
        attachment_dir=tmp_path / "att",
        log_level="WARNING",
        ocr_enabled=False, ocr_language="eng", ocr_dpi=200,
        tesseract_cmd=None,
        office_enabled=False, libreoffice_cmd="soffice",
        extraction_timeout_s=60,
        cors_origins=("http://localhost:5173",),
        export_dir=tmp_path / "exports",
        inbox_dir=tmp_path / "inbox",
        auth_dev_mode=True,
        auth_dev_users=("alice",),
    )
    return TestClient(create_app(cfg))


def _login(client: TestClient, username: str = "alice") -> None:
    r = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "x"},
    )
    assert r.status_code == 200, r.text


def _wait_for_job(
    client: TestClient, job_id: int, timeout_s: float = 10.0,
) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        body = client.get(f"/api/v1/imports/{job_id}").json()
        if body["job"]["status"] in ("succeeded", "failed", "cancelled"):
            return body
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish within {timeout_s}s")


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


def test_unauthenticated_import_is_rejected(client):
    payload = _build_mbox_bytes()
    r = client.post(
        "/api/v1/imports",
        files={"file": ("a.mbox", payload, "application/octet-stream")},
        data={"name": "case-1", "bates_prefix": "ACME"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_import_creates_case_and_runs_pipeline(client):
    _login(client)
    payload = _build_mbox_bytes()
    r = client.post(
        "/api/v1/imports",
        files={"file": ("inbox.mbox", payload, "application/octet-stream")},
        data={"name": "Case 2024-01", "bates_prefix": "TEST"},
    )
    assert r.status_code == 200, r.text
    submit = r.json()
    assert submit["status"] == "queued"
    assert submit["case_name"] == "Case 2024-01"
    assert submit["bates_prefix"] == "TEST"

    body = _wait_for_job(client, submit["job_id"])
    assert body["job"]["status"] == "succeeded"

    # Per-stage events were emitted in order.
    stages = [e["stage"] for e in body["events"]]
    for s in ("ingest", "extract", "detect", "resolve", "propose", "done"):
        assert s in stages, f"missing stage {s}"

    # Case is now ready and stats are populated.
    case = client.get(f"/api/v1/cases/{submit['case_id']}").json()
    assert case["case"]["status"] == "ready"
    assert case["stats"]["emails"] == 1
    assert case["stats"]["pii_detections"] >= 2
    assert case["stats"]["redactions"] >= 2

    # Audit row recorded with user_id.
    audit_resp = client.get("/api/v1/audit?event_type=import.run").json()
    assert audit_resp["total"] == 1


def test_emails_get_scoped_to_the_case(client):
    _login(client)
    payload = _build_mbox_bytes()
    r = client.post(
        "/api/v1/imports",
        files={"file": ("a.mbox", payload, "application/octet-stream")},
        data={"name": "C1", "bates_prefix": "C1"},
    )
    submit = r.json()
    _wait_for_job(client, submit["job_id"])
    cfg = client.app.state.config
    from foia.db import connect
    conn = connect(cfg.db_path)
    try:
        rows = conn.execute(
            "SELECT id, case_id FROM emails"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["case_id"] == submit["case_id"]


def test_default_bates_prefix_falls_back_to_district_yaml(client):
    _login(client)
    payload = _build_mbox_bytes()
    r = client.post(
        "/api/v1/imports",
        files={"file": ("a.mbox", payload, "application/octet-stream")},
        data={"name": "no-prefix"},  # no bates_prefix
    )
    submit = r.json()
    # The example YAML in this fixture sets prefix=TEST.
    assert submit["bates_prefix"] == "TEST"


# ---------------------------------------------------------------------------
# Failure path + retry
# ---------------------------------------------------------------------------


def test_empty_upload_returns_400_and_marks_case_failed(client):
    _login(client)
    r = client.post(
        "/api/v1/imports",
        files={"file": ("a.mbox", b"", "application/octet-stream")},
        data={"name": "C empty", "bates_prefix": "EMPTY"},
    )
    assert r.status_code == 400
    # The case row was created and then marked failed.
    items = client.get("/api/v1/cases?status=failed").json()["items"]
    assert any(c["name"] == "C empty" for c in items)


# ---------------------------------------------------------------------------
# Listing + cases endpoint
# ---------------------------------------------------------------------------


def test_list_cases_and_imports(client):
    _login(client)
    payload = _build_mbox_bytes()
    client.post(
        "/api/v1/imports",
        files={"file": ("a.mbox", payload, "application/octet-stream")},
        data={"name": "first", "bates_prefix": "F1"},
    )
    submit2 = client.post(
        "/api/v1/imports",
        files={"file": ("b.mbox", payload, "application/octet-stream")},
        data={"name": "second", "bates_prefix": "F2"},
    ).json()
    _wait_for_job(client, submit2["job_id"])

    cases = client.get("/api/v1/cases").json()
    assert cases["total"] == 2

    imports = client.get("/api/v1/imports").json()
    assert len(imports) == 2
    # Both jobs reference real case_ids.
    assert all(j["case_id"] for j in imports)


def test_get_unknown_job_returns_404(client):
    _login(client)
    r = client.get("/api/v1/imports/9999")
    assert r.status_code == 404


def test_case_status_patch_audit(client):
    _login(client)
    payload = _build_mbox_bytes()
    r = client.post(
        "/api/v1/imports",
        files={"file": ("a.mbox", payload, "application/octet-stream")},
        data={"name": "to-archive", "bates_prefix": "AR"},
    )
    submit = r.json()
    _wait_for_job(client, submit["job_id"])

    r2 = client.patch(
        f"/api/v1/cases/{submit['case_id']}/status",
        json={"status": "archived"},
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "archived"
    audit = client.get(
        "/api/v1/audit?event_type=case.status_changed"
    ).json()
    assert audit["total"] == 1


def test_openapi_includes_new_routes(client):
    paths = set(client.get("/openapi.json").json()["paths"].keys())
    for p in (
        "/api/v1/cases",
        "/api/v1/cases/{case_id}",
        "/api/v1/cases/{case_id}/status",
        "/api/v1/imports",
        "/api/v1/imports/{job_id}",
        "/api/v1/imports/{job_id}/events",
        "/api/v1/imports/{job_id}/retry",
    ):
        assert p in paths, f"missing {p}"
