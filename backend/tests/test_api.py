"""FastAPI endpoint tests against a seeded DB."""

from __future__ import annotations

import json
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

from foia.api.app import create_app
from foia.config import Config


# ---------------------------------------------------------------------------
# Fixture: seed a realistic DB end-to-end with all four phases
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded_db(tmp_path: Path, text_pdf_factory) -> Path:
    """Run Phase 1 → 2 → 3 → 4 over a custom mbox."""
    import mailbox
    # Message 1: rich PII + PDF attachment (real text, so extraction succeeds).
    m1 = EmailMessage()
    m1["Message-ID"] = "<m1@district.example.org>"
    m1["From"] = "Principal Doe <principal@district.example.org>"
    m1["To"] = "Parent A <parent.a@example.com>"
    m1["Cc"] = "counselor@district.example.org"
    m1["Subject"] = "Student 12345678 — follow up"
    m1["Date"] = "Mon, 1 Jan 2024 09:00:00 +0000"
    m1.set_content(
        "Hello,\n\n"
        "Following up on student 12345678. Their SSN on file is 572-68-1439. "
        "Parent phone (571) 555-0123. Email parent.a@example.com.\n\n"
        "Best,\nPrincipal Doe\nDistrict HQ\nprincipal.alt@district.example.org\n"
    )
    pdf = text_pdf_factory(["Attached memo — SSN 572-55-1234 for review."])
    m1.add_attachment(
        pdf.read_bytes(), maintype="application", subtype="pdf",
        filename="memo.pdf",
    )

    # Message 2: no attachments, different person.
    m2 = EmailMessage()
    m2["Message-ID"] = "<m2@example.com>"
    m2["From"] = "Parent A <parent.a@example.com>"
    m2["To"] = "Principal Doe <principal@district.example.org>"
    m2["Subject"] = "Thanks for the follow-up"
    m2["Date"] = "Tue, 2 Jan 2024 14:00:00 +0000"
    m2.set_content("Thanks, no further questions.\n")

    mbox_path = tmp_path / "api.mbox"
    if mbox_path.exists():
        mbox_path.unlink()
    box = mailbox.mbox(str(mbox_path))
    box.lock()
    try:
        for m in (m1, m2):
            box.add(m)
        box.flush()
    finally:
        box.unlock(); box.close()

    db_path = tmp_path / "api.db"
    att_dir = tmp_path / "att"
    import ingest as ingest_cli
    assert ingest_cli.main(
        ["--file", str(mbox_path), "--db", str(db_path),
         "--attachments", str(att_dir)]
    ) == 0

    import extract as extract_cli
    assert extract_cli.main(
        ["--db", str(db_path), "--no-ocr", "--no-office"]
    ) == 0

    cfg_path = tmp_path / "d.yaml"
    cfg_path.write_text(
        "district:\n"
        "  name: Test\n"
        "  email_domains: [district.example.org]\n"
        "pii_detection:\n"
        "  builtins: [US_SSN, EMAIL_ADDRESS, PHONE_NUMBER]\n"
        "  min_score: 0.3\n"
        "  custom_recognizers:\n"
        "    - name: SID\n"
        "      entity_type: STUDENT_ID\n"
        "      patterns: [{regex: '\\b\\d{8}\\b', score: 0.7}]\n",
        encoding="utf-8",
    )

    import detect as detect_cli
    assert detect_cli.main(
        ["--db", str(db_path), "--config", str(cfg_path)]
    ) == 0

    import resolve as resolve_cli
    assert resolve_cli.main(
        ["--db", str(db_path), "--config", str(cfg_path), "run"]
    ) == 0

    return db_path


@pytest.fixture()
def client(seeded_db: Path, tmp_path: Path) -> TestClient:
    cfg = Config(
        db_path=seeded_db,
        attachment_dir=tmp_path / "att",
        log_level="WARNING",
        ocr_enabled=False,
        ocr_language="eng",
        ocr_dpi=200,
        tesseract_cmd=None,
        office_enabled=False,
        libreoffice_cmd="soffice",
        extraction_timeout_s=60,
    )
    return TestClient(create_app(cfg))


# ---------------------------------------------------------------------------
# Meta endpoints
# ---------------------------------------------------------------------------


def test_health(client: TestClient):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db_exists"] is True


def test_stats(client: TestClient):
    r = client.get("/stats")
    assert r.status_code == 200
    s = r.json()
    assert s["emails"] == 2
    assert s["attachments"] == 1
    assert s["attachments_with_text"] == 1
    assert s["pii_detections"] >= 3
    assert s["persons"] >= 3


# ---------------------------------------------------------------------------
# /emails
# ---------------------------------------------------------------------------


def test_list_emails_default(client: TestClient):
    r = client.get("/api/v1/emails")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    # Sorted desc by date_sent — m2 (Jan 2) first.
    assert data["items"][0]["subject"].startswith("Thanks")


def test_list_emails_pagination(client: TestClient):
    r = client.get("/api/v1/emails?limit=1&offset=0")
    assert r.status_code == 200
    d = r.json()
    assert len(d["items"]) == 1
    assert d["total"] == 2
    assert d["limit"] == 1
    assert d["offset"] == 0


def test_list_emails_filter_subject(client: TestClient):
    r = client.get("/api/v1/emails?subject_contains=follow")
    assert r.status_code == 200
    assert r.json()["total"] == 2
    r2 = client.get("/api/v1/emails?subject_contains=student")
    assert r2.json()["total"] == 1


def test_list_emails_filter_has_attachments(client: TestClient):
    r_yes = client.get("/api/v1/emails?has_attachments=true").json()
    r_no = client.get("/api/v1/emails?has_attachments=false").json()
    assert r_yes["total"] == 1
    assert r_no["total"] == 1


def test_list_emails_filter_has_pii(client: TestClient):
    r = client.get("/api/v1/emails?has_pii=true").json()
    assert r["total"] >= 1


def test_list_emails_date_range(client: TestClient):
    # Dates stored as ISO 8601 UTC — filter inclusive-lower, exclusive-upper.
    r = client.get(
        "/api/v1/emails?date_from=2024-01-02T00:00:00+00:00&date_to=2024-01-03T00:00:00+00:00"
    ).json()
    assert r["total"] == 1
    assert r["items"][0]["subject"].startswith("Thanks")


def test_limit_out_of_bounds(client: TestClient):
    assert client.get("/api/v1/emails?limit=0").status_code == 422
    assert client.get("/api/v1/emails?limit=9999").status_code == 422


def test_get_email_detail(client: TestClient):
    lst = client.get("/api/v1/emails").json()
    eid = next(
        i["id"] for i in lst["items"] if i["subject"].startswith("Student")
    )
    r = client.get(f"/api/v1/emails/{eid}")
    assert r.status_code == 200
    d = r.json()
    assert d["from_addr"].startswith("Principal")
    assert "parent.a@example.com" in d["to_addrs"][0]
    assert d["attachments"]
    assert d["attachments"][0]["filename"] == "memo.pdf"
    assert d["pii_detections"]
    entity_types = {x["entity_type"] for x in d["pii_detections"]}
    assert "STUDENT_ID" in entity_types
    assert "US_SSN" in entity_types


def test_get_email_404(client: TestClient):
    assert client.get("/api/v1/emails/9999").status_code == 404


def test_get_email_raw(client: TestClient):
    eid = client.get("/api/v1/emails").json()["items"][0]["id"]
    r = client.get(f"/api/v1/emails/{eid}/raw")
    assert r.status_code == 200
    assert r.headers["content-type"] == "message/rfc822"
    assert r.content.startswith(b"Message-ID:") or b"From:" in r.content[:200]


# ---------------------------------------------------------------------------
# /attachments
# ---------------------------------------------------------------------------


def test_list_attachments(client: TestClient):
    r = client.get("/api/v1/attachments").json()
    assert r["total"] == 1
    assert r["items"][0]["filename"] == "memo.pdf"
    assert r["items"][0]["extraction_status"] == "ok"


def test_list_attachments_filter_content_type_prefix(client: TestClient):
    r = client.get("/api/v1/attachments?content_type_prefix=application/").json()
    assert r["total"] == 1
    r2 = client.get("/api/v1/attachments?content_type_prefix=image/").json()
    assert r2["total"] == 0


def test_attachment_detail_includes_extracted_text(client: TestClient):
    aid = client.get("/api/v1/attachments").json()["items"][0]["id"]
    d = client.get(f"/api/v1/attachments/{aid}").json()
    assert d["extraction_status"] == "ok"
    assert "memo" in (d["extracted_text"] or "").lower()
    assert d["sha256"]


def test_attachment_download(client: TestClient):
    aid = client.get("/api/v1/attachments").json()["items"][0]["id"]
    r = client.get(f"/api/v1/attachments/{aid}/download")
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")


def test_attachment_404(client: TestClient):
    assert client.get("/api/v1/attachments/9999").status_code == 404
    assert client.get("/api/v1/attachments/9999/download").status_code == 404


# ---------------------------------------------------------------------------
# /detections
# ---------------------------------------------------------------------------


def test_list_detections(client: TestClient):
    r = client.get("/api/v1/detections").json()
    assert r["total"] >= 3
    kinds = {d["entity_type"] for d in r["items"]}
    assert "US_SSN" in kinds


def test_filter_detections_by_entity(client: TestClient):
    r = client.get("/api/v1/detections?entity_type=US_SSN").json()
    assert r["total"] >= 1
    assert all(d["entity_type"] == "US_SSN" for d in r["items"])


def test_filter_detections_min_score(client: TestClient):
    r = client.get("/api/v1/detections?min_score=0.9").json()
    assert all(d["score"] >= 0.9 for d in r["items"])


def test_entity_counts(client: TestClient):
    r = client.get("/api/v1/detections/entities").json()
    assert isinstance(r, list)
    kinds = {row["entity_type"] for row in r}
    assert "US_SSN" in kinds
    # Counts are positive.
    for row in r:
        assert row["count"] >= 1


# ---------------------------------------------------------------------------
# /persons
# ---------------------------------------------------------------------------


def test_list_persons(client: TestClient):
    r = client.get("/api/v1/persons").json()
    assert r["total"] >= 3
    emails = {p["primary_email"] for p in r["items"]}
    assert "principal@district.example.org" in emails


def test_filter_persons_internal(client: TestClient):
    r_internal = client.get("/api/v1/persons?is_internal=true").json()
    r_external = client.get("/api/v1/persons?is_internal=false").json()
    assert r_internal["total"] >= 2      # principal + counselor + sig alt
    assert r_external["total"] >= 1      # parent


def test_filter_persons_email_domain(client: TestClient):
    r = client.get(
        "/api/v1/persons?email_domain=district.example.org"
    ).json()
    assert r["total"] >= 2


def test_person_detail(client: TestClient):
    pid = client.get(
        "/api/v1/persons?email_domain=example.com"
    ).json()["items"][0]["id"]
    d = client.get(f"/api/v1/persons/{pid}").json()
    assert d["is_internal"] is False
    assert d["emails"]
    assert "email_from" in d["occurrences_by_type"] or "email_to" in d["occurrences_by_type"]


def test_person_404(client: TestClient):
    assert client.get("/api/v1/persons/9999").status_code == 404


# ---------------------------------------------------------------------------
# /search
# ---------------------------------------------------------------------------


def test_search_finds_email_body(client: TestClient):
    r = client.get("/api/v1/search?q=follow").json()
    assert r["total"] >= 1
    assert any(h["source_type"] == "email" for h in r["items"])


def test_search_finds_attachment_text(client: TestClient):
    r = client.get("/api/v1/search?q=memo").json()
    assert any(h["source_type"] == "attachment" for h in r["items"])


def test_search_scope_emails_only(client: TestClient):
    r = client.get("/api/v1/search?q=memo&scope=emails").json()
    for h in r["items"]:
        assert h["source_type"] == "email"


def test_search_scope_attachments_only(client: TestClient):
    r = client.get("/api/v1/search?q=memo&scope=attachments").json()
    for h in r["items"]:
        assert h["source_type"] == "attachment"


def test_search_snippet_has_mark_tags(client: TestClient):
    r = client.get("/api/v1/search?q=follow").json()
    assert r["items"]
    assert "<mark>" in r["items"][0]["snippet"].lower()


def test_search_rejects_empty_query(client: TestClient):
    # min_length=1 → 422 from query validation.
    assert client.get("/api/v1/search?q=").status_code == 422


def test_search_rejects_whitespace_only(client: TestClient):
    assert client.get("/api/v1/search?q=%20").status_code == 400


def test_search_no_results(client: TestClient):
    r = client.get("/api/v1/search?q=zzzzyyxx").json()
    assert r["total"] == 0
    assert r["items"] == []


def test_openapi_schema_reachable(client: TestClient):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    doc = r.json()
    assert doc["info"]["title"] == "FOIA Redaction Tool API"
    # Every router is present.
    paths = set(doc["paths"].keys())
    for expected in (
        "/api/v1/emails",
        "/api/v1/emails/{email_id}",
        "/api/v1/emails/{email_id}/raw",
        "/api/v1/attachments",
        "/api/v1/attachments/{attachment_id}",
        "/api/v1/attachments/{attachment_id}/download",
        "/api/v1/detections",
        "/api/v1/detections/entities",
        "/api/v1/persons",
        "/api/v1/persons/{person_id}",
        "/api/v1/search",
        "/health",
        "/stats",
    ):
        assert expected in paths, f"missing {expected} in OpenAPI paths"
