"""Tests for the UI-driven /api/v1/imports endpoint."""

from __future__ import annotations

import io
import mailbox
import sys
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
""",
        encoding="utf-8",
    )
    return p


def _build_mbox_bytes() -> bytes:
    """Synthesise an .mbox in memory and return its bytes."""
    m = EmailMessage()
    m["Message-ID"] = make_msgid(domain="district.example.org")
    m["From"] = "Alice <alice@district.example.org>"
    m["To"] = "Bob <bob@example.com>"
    m["Subject"] = "Bus update"
    m["Date"] = formatdate()
    m.set_content(
        "Student 12345678 has an SSN of 572-68-1439. "
        "Phone: (571) 555-0199."
    )

    tmp_path = Path("__upload_tmp__.mbox").resolve()
    if tmp_path.exists():
        tmp_path.unlink()
    box = mailbox.mbox(str(tmp_path))
    box.lock()
    try:
        box.add(m)
        box.flush()
    finally:
        box.unlock()
        box.close()
    data = tmp_path.read_bytes()
    tmp_path.unlink()
    return data


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    cfg_yaml = _yaml(tmp_path)
    monkeypatch.setenv("FOIA_CONFIG_FILE", str(cfg_yaml))
    cfg = Config(
        db_path=tmp_path / "imp.db",
        attachment_dir=tmp_path / "att",
        log_level="WARNING",
        ocr_enabled=False, ocr_language="eng", ocr_dpi=200,
        tesseract_cmd=None,
        office_enabled=False, libreoffice_cmd="soffice",
        extraction_timeout_s=60,
        cors_origins=("http://localhost:5173",),
        export_dir=tmp_path / "exports",
        inbox_dir=tmp_path / "inbox",
    )
    return TestClient(create_app(cfg)), cfg


def test_import_runs_full_pipeline(client):
    c, cfg = client
    payload = _build_mbox_bytes()
    r = c.post(
        "/api/v1/imports",
        headers={"X-FOIA-Reviewer": "alice"},
        files={"file": ("case.mbox", payload, "application/octet-stream")},
        data={"label": "case-2024-01"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["filename"] == "case.mbox"
    assert body["label"] == "case-2024-01"
    assert body["import_id"]
    # Saved copy lives under the configured inbox.
    assert Path(body["saved_path"]).exists()

    # Every stage ran.
    assert body["stages"]["ingest"]["emails_ingested"] == 1
    assert body["stages"]["extract"]["total"] >= 0
    assert body["stages"]["detect"]["sources_scanned"] >= 1
    assert body["stages"]["detect"]["detections_written"] >= 2  # SSN, phone (and SID)
    assert body["stages"]["resolve"]["persons_created"] >= 2
    assert body["stages"]["propose"]["proposed"] >= 2

    # Audit row recorded the import as one event.
    audit = c.get("/api/v1/audit?event_type=import.run").json()
    assert audit["total"] == 1
    assert audit["items"][0]["actor"] == "alice"


def test_import_rejects_empty_file(client):
    c, _ = client
    r = c.post(
        "/api/v1/imports",
        files={"file": ("empty.mbox", b"", "application/octet-stream")},
    )
    assert r.status_code == 400


def test_import_filename_is_sanitized(client):
    c, _ = client
    payload = _build_mbox_bytes()
    # Path-traversal-ish filename should be reduced to its basename.
    r = c.post(
        "/api/v1/imports",
        files={"file": ("../../etc/passwd", payload, "application/octet-stream")},
    )
    assert r.status_code == 200
    saved = Path(r.json()["saved_path"])
    assert "passwd" in saved.name           # basename preserved
    assert ".." not in str(saved)            # traversal stripped


def test_import_propose_redactions_off(client):
    c, _ = client
    payload = _build_mbox_bytes()
    r = c.post(
        "/api/v1/imports",
        files={"file": ("case.mbox", payload, "application/octet-stream")},
        data={"propose_redactions": "false"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["stages"]["propose"] == {"skipped": True}


def test_list_imports_after_one(client):
    c, _ = client
    payload = _build_mbox_bytes()
    c.post(
        "/api/v1/imports",
        headers={"X-FOIA-Reviewer": "ops"},
        files={"file": ("case.mbox", payload, "application/octet-stream")},
    )
    r = c.get("/api/v1/imports")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["filename"] == "case.mbox"
    assert rows[0]["actor"] == "ops"
    assert "stages" in rows[0]


def test_openapi_includes_imports(client):
    c, _ = client
    paths = set(c.get("/openapi.json").json()["paths"].keys())
    assert "/api/v1/imports" in paths
