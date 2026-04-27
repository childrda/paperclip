"""Phase 7 backend additions: /emails/{id}/redactions and CORS preflight."""

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
from foia.db import connect, init_schema


def _make_cfg(tmp_path: Path, *, cors_origins=("http://localhost:5173",)) -> tuple[Config, Path]:
    cfg_yaml = tmp_path / "d.yaml"
    cfg_yaml.write_text(
        "district:\n"
        "  name: Test\n"
        "pii_detection:\n"
        "  builtins: []\n"
        "exemption_codes:\n  - code: FERPA\n  - code: PII\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "p7.db"
    cfg = Config(
        db_path=db_path, attachment_dir=tmp_path / "att",
        log_level="WARNING", ocr_enabled=False, ocr_language="eng",
        ocr_dpi=200, tesseract_cmd=None, office_enabled=False,
        libreoffice_cmd="soffice", extraction_timeout_s=60,
        cors_origins=cors_origins,
    )
    return cfg, cfg_yaml


def _seed(db_path: Path, cfg_yaml: Path) -> int:
    """Insert one email and one redaction; return the email id."""
    import os
    os.environ["FOIA_CONFIG_FILE"] = str(cfg_yaml)
    conn = connect(db_path)
    init_schema(conn)
    cur = conn.execute(
        """
        INSERT INTO emails (mbox_source, mbox_index, subject, body_text,
                            body_html_sanitized, ingested_at)
        VALUES ('t.mbox', 0, 'Hello', 'Body content here.', '', ?)
        """,
        (datetime.now(timezone.utc).isoformat(),),
    )
    eid = int(cur.lastrowid)
    conn.execute(
        """
        INSERT INTO redactions (
            source_type, source_id, start_offset, end_offset,
            exemption_code, status, origin, created_at, updated_at
        ) VALUES ('email_body_text', ?, 0, 4, 'FERPA', 'proposed', 'manual',
                  ?, ?)
        """,
        (eid, datetime.now(timezone.utc).isoformat(),
         datetime.now(timezone.utc).isoformat()),
    )
    # And one against a different email id so we can test scoping.
    cur2 = conn.execute(
        """
        INSERT INTO emails (mbox_source, mbox_index, subject, body_text,
                            body_html_sanitized, ingested_at)
        VALUES ('t.mbox', 1, 'Other', 'Other body here.', '', ?)
        """,
        (datetime.now(timezone.utc).isoformat(),),
    )
    other_eid = int(cur2.lastrowid)
    conn.execute(
        """
        INSERT INTO redactions (
            source_type, source_id, start_offset, end_offset,
            exemption_code, status, origin, created_at, updated_at
        ) VALUES ('email_body_text', ?, 0, 4, 'PII', 'proposed', 'manual',
                  ?, ?)
        """,
        (other_eid, datetime.now(timezone.utc).isoformat(),
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    return eid


@pytest.fixture()
def client(tmp_path: Path):
    cfg, yaml_path = _make_cfg(tmp_path)
    eid = _seed(cfg.db_path, yaml_path)
    return TestClient(create_app(cfg)), eid


def test_email_redactions_endpoint_returns_only_email_scope(client):
    c, eid = client
    r = c.get(f"/api/v1/emails/{eid}/redactions")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["source_id"] == eid
    assert rows[0]["source_type"] == "email_body_text"


def test_email_redactions_404_when_email_missing(client):
    c, _ = client
    r = c.get("/api/v1/emails/9999/redactions")
    assert r.status_code == 404


def test_email_redactions_empty_list_when_no_redactions(tmp_path: Path):
    cfg, yaml_path = _make_cfg(tmp_path)
    # Create an email with no redactions.
    import os
    os.environ["FOIA_CONFIG_FILE"] = str(yaml_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    cur = conn.execute(
        """
        INSERT INTO emails (mbox_source, mbox_index, subject, body_text,
                            body_html_sanitized, ingested_at)
        VALUES ('t.mbox', 0, 'a', 'b', '', ?)
        """,
        (datetime.now(timezone.utc).isoformat(),),
    )
    eid = int(cur.lastrowid)
    conn.commit()
    conn.close()

    c = TestClient(create_app(cfg))
    r = c.get(f"/api/v1/emails/{eid}/redactions")
    assert r.status_code == 200
    assert r.json() == []


def test_cors_preflight_returns_allow_origin(client):
    c, _ = client
    # OPTIONS preflight from the configured origin must echo it back.
    r = c.options(
        "/api/v1/emails",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "GET" in r.headers.get("access-control-allow-methods", "")


def test_cors_other_origin_rejected(client):
    c, _ = client
    r = c.options(
        "/api/v1/emails",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    # Starlette/FastAPI returns 400 for disallowed preflight origins.
    assert r.status_code in (400, 403)


def test_cors_disabled_when_origins_empty(tmp_path: Path):
    cfg, yaml_path = _make_cfg(tmp_path, cors_origins=())
    _ = _seed(cfg.db_path, yaml_path)
    c = TestClient(create_app(cfg))
    r = c.options(
        "/api/v1/emails",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    # With CORS middleware disabled, an OPTIONS without route handler 405s.
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers.keys()}


def test_openapi_includes_redactions_endpoint(client):
    c, _ = client
    doc = c.get("/openapi.json").json()
    paths = set(doc["paths"].keys())
    assert "/api/v1/emails/{email_id}/redactions" in paths
    assert "/api/v1/redactions" in paths
    assert "/api/v1/exemption-codes" in paths
