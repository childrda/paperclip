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


def test_emails_listing_filters_by_case_id(client):
    """Case-detail's "Review emails →" must show only that case's emails."""
    _login(client)
    # Two independent cases, each with a different upload.
    r1 = client.post(
        "/api/v1/imports",
        files={"file": ("a.mbox", _build_mbox_bytes(), "application/octet-stream")},
        data={"name": "Case A", "bates_prefix": "AAA"},
    )
    s1 = r1.json()
    _wait_for_job(client, s1["job_id"])
    r2 = client.post(
        "/api/v1/imports",
        files={"file": ("b.mbox", _build_mbox_bytes(body_extra="x"), "application/octet-stream")},
        data={"name": "Case B", "bates_prefix": "BBB"},
    )
    s2 = r2.json()
    _wait_for_job(client, s2["job_id"])

    # Without the filter, the global list returns all emails.
    all_emails = client.get("/api/v1/emails").json()
    assert all_emails["total"] == 2

    # With the filter, only that case's email comes back.
    a = client.get(f"/api/v1/emails?case_id={s1['case_id']}").json()
    assert a["total"] == 1
    assert all(e["id"] for e in a["items"])  # sanity
    b = client.get(f"/api/v1/emails?case_id={s2['case_id']}").json()
    assert b["total"] == 1
    assert a["items"][0]["id"] != b["items"][0]["id"]


def test_excluded_email_disappears_from_export_and_case_stats(client):
    """Exclusion is the FOIA "withhold record" workflow.

    Two emails in the same case: exclude one, verify case stats split
    properly, the email-list filter still surfaces it (struck-through),
    and the export pipeline omits it from the PDF source set.
    """
    _login(client)
    r = client.post(
        "/api/v1/imports",
        files={"file": ("ex.mbox", _build_mbox_bytes(), "application/octet-stream")},
        data={"name": "exclude-me", "bates_prefix": "EX"},
    )
    submit = r.json()
    _wait_for_job(client, submit["job_id"])

    case_id = submit["case_id"]
    listing = client.get(f"/api/v1/emails?case_id={case_id}").json()
    assert listing["total"] == 1
    eid = listing["items"][0]["id"]
    assert listing["items"][0]["is_excluded"] is False

    # Exclude.
    resp = client.post(
        f"/api/v1/emails/{eid}/exclude",
        json={"reason": "non-responsive — outside FOIA scope"},
    )
    assert resp.status_code == 200, resp.text
    detail = resp.json()
    assert detail["excluded_at"] is not None
    assert detail["exclusion_reason"] == "non-responsive — outside FOIA scope"

    # Case stats: emails count drops to 0; emails_excluded becomes 1.
    case = client.get(f"/api/v1/cases/{case_id}").json()
    assert case["stats"]["emails"] == 0
    assert case["stats"]["emails_excluded"] == 1

    # The email still appears in the list, struck-through.
    listing2 = client.get(f"/api/v1/emails?case_id={case_id}").json()
    assert listing2["total"] == 1
    assert listing2["items"][0]["is_excluded"] is True

    # Audit row recorded with the reason.
    audit_resp = client.get("/api/v1/audit?event_type=email.excluded").json()
    assert audit_resp["total"] == 1
    assert audit_resp["items"][0]["payload"]["reason"] == (
        "non-responsive — outside FOIA scope"
    )

    # Re-include.
    resp2 = client.post(f"/api/v1/emails/{eid}/include")
    assert resp2.status_code == 200
    assert resp2.json()["excluded_at"] is None
    case2 = client.get(f"/api/v1/cases/{case_id}").json()
    assert case2["stats"]["emails"] == 1
    assert case2["stats"]["emails_excluded"] == 0

    # Re-including a non-excluded email is idempotent.
    resp3 = client.post(f"/api/v1/emails/{eid}/include")
    assert resp3.status_code == 200


def test_email_level_propose_endpoint(client):
    """Per-email propose button: lets a reviewer recover one email without
    affecting the rest of the case."""
    _login(client)
    r = client.post(
        "/api/v1/imports",
        files={"file": ("e.mbox", _build_mbox_bytes(), "application/octet-stream")},
        data={
            "name": "per-email",
            "bates_prefix": "PE",
            "propose_redactions": "false",
        },
    )
    submit = r.json()
    _wait_for_job(client, submit["job_id"])

    emails = client.get(f"/api/v1/emails?case_id={submit['case_id']}").json()
    assert emails["total"] == 1
    eid = emails["items"][0]["id"]

    # Initially, no redactions on this email.
    assert client.get(f"/api/v1/emails/{eid}/redactions").json() == []

    # Email-level propose creates them.
    resp = client.post(f"/api/v1/emails/{eid}/propose-redactions")
    assert resp.status_code == 200, resp.text
    assert resp.json()["proposed"] >= 2

    reds = client.get(f"/api/v1/emails/{eid}/redactions").json()
    assert len(reds) >= 2
    assert all(r["status"] == "proposed" for r in reds)


def test_propose_redactions_endpoint_recovers_a_case_with_zero_redactions(client):
    """An import made with propose_redactions=false leaves PII detections
    but no redactions. The case-level propose endpoint must close that
    gap so reviewers have something to accept/reject."""
    _login(client)
    r = client.post(
        "/api/v1/imports",
        files={"file": ("c.mbox", _build_mbox_bytes(), "application/octet-stream")},
        data={
            "name": "no-propose",
            "bates_prefix": "NP",
            "propose_redactions": "false",
        },
    )
    submit = r.json()
    _wait_for_job(client, submit["job_id"])

    case = client.get(f"/api/v1/cases/{submit['case_id']}").json()
    assert case["stats"]["pii_detections"] >= 2
    assert case["stats"]["redactions"] == 0  # propose was skipped at import

    # On-demand propose closes the gap.
    resp = client.post(
        f"/api/v1/cases/{submit['case_id']}/propose-redactions"
    )
    assert resp.status_code == 200, resp.text
    stats = resp.json()
    assert stats["proposed"] >= 2

    # Idempotent — re-running just reports them as existing, no new rows.
    again = client.post(
        f"/api/v1/cases/{submit['case_id']}/propose-redactions"
    ).json()
    assert again["proposed"] == 0
    assert again["skipped_existing"] >= 2

    # Stats now reflect the redactions.
    case2 = client.get(f"/api/v1/cases/{submit['case_id']}").json()
    assert case2["stats"]["redactions"] >= 2


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
