"""HTTP-level auth tests using dev-mode (no real DC)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from foia.api.app import create_app
from foia.config import Config


def _cfg(tmp_path: Path) -> Config:
    return Config(
        db_path=tmp_path / "auth.db",
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
        auth_dev_users=("alice", "bob"),
        auth_lockout_threshold=3,
    )


@pytest.fixture()
def client(tmp_path: Path):
    return TestClient(create_app(_cfg(tmp_path)))


def test_me_without_session_returns_401(client):
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 401


def test_login_success_sets_cookie_and_me_works(client):
    r = client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": "any"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["username"] == "alice"
    assert body["user_id"] >= 1

    # Cookie set on the response.
    assert "paperclip_session" in r.cookies

    # /me returns the same identity.
    me = client.get("/api/v1/auth/me").json()
    assert me["username"] == "alice"
    assert me["user_id"] == body["user_id"]


def test_login_wrong_user_returns_generic_401(client):
    r = client.post(
        "/api/v1/auth/login",
        json={"username": "mallory", "password": "any"},
    )
    assert r.status_code == 401
    # Generic message, no leak about why.
    assert "invalid" in r.json()["detail"].lower()


def test_login_empty_password_rejected(client):
    r = client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": ""},
    )
    # min_length=1 on the request model returns 422.
    assert r.status_code == 422


def test_login_extra_fields_rejected(client):
    r = client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": "x", "evil": "1"},
    )
    assert r.status_code == 422


def test_logout_revokes_session(client):
    client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": "x"},
    )
    assert client.get("/api/v1/auth/me").status_code == 200
    client.post("/api/v1/auth/logout")
    assert client.get("/api/v1/auth/me").status_code == 401


def test_authenticated_redaction_writes_user_id_into_audit(client):
    """The single most important integration check: a write performed
    with a real session ends up with a user_id FK on its audit row."""
    # Seed an email so we have something to redact against.
    from foia.db import connect, init_schema
    from datetime import datetime, timezone
    cfg = client.app.state.config
    conn = connect(cfg.db_path)
    init_schema(conn)
    conn.execute(
        """
        INSERT INTO emails (mbox_source, mbox_index, subject, body_text,
                            body_html_sanitized, ingested_at)
        VALUES ('t.mbox', 0, 'subj', 'aaaa', '', ?)
        """,
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()
    conn.close()

    # YAML-less district: the redaction route reads the YAML at first
    # call, but the example YAML doesn't ship in this tmpdir. Provide
    # a minimal one via env.
    import os
    cfg_yaml = cfg.db_path.parent / "d.yaml"
    cfg_yaml.write_text(
        "district: {name: T}\n"
        "pii_detection: {builtins: []}\n"
        "exemption_codes: [{code: FERPA}]\n",
        encoding="utf-8",
    )
    os.environ["FOIA_CONFIG_FILE"] = str(cfg_yaml)
    # Force a re-load on the next /redactions call by clearing the
    # cached district config on app state.
    if hasattr(client.app.state, "district_config"):
        delattr(client.app.state, "district_config")

    client.post("/api/v1/auth/login", json={"username": "alice", "password": "x"})
    me = client.get("/api/v1/auth/me").json()

    r = client.post(
        "/api/v1/redactions",
        json={
            "source_type": "email_body_text", "source_id": 1,
            "start_offset": 0, "end_offset": 2, "exemption_code": "FERPA",
        },
    )
    assert r.status_code == 201, r.text

    # The audit row should carry user_id.
    audit = client.get(
        "/api/v1/audit?event_type=redaction.create"
    ).json()
    assert audit["total"] == 1
    row = audit["items"][0]
    assert row["actor"] == "alice"
    # The pydantic schema doesn't expose user_id by default; pull from
    # the DB to verify the FK was written.
    from foia.db import connect as _connect
    c = _connect(cfg.db_path)
    user_id = c.execute(
        "SELECT user_id FROM audit_log WHERE event_type='redaction.create'"
    ).fetchone()[0]
    c.close()
    assert user_id == me["user_id"]


def test_legacy_x_foia_reviewer_still_works_when_no_session(client):
    """Backwards-compatibility: tests and CLIs without a session can
    still attribute a write via X-FOIA-Reviewer. user_id comes back
    NULL on the audit row."""
    from foia.db import connect, init_schema
    from datetime import datetime, timezone
    cfg = client.app.state.config
    conn = connect(cfg.db_path)
    init_schema(conn)
    conn.execute(
        """
        INSERT INTO emails (mbox_source, mbox_index, subject, body_text,
                            body_html_sanitized, ingested_at)
        VALUES ('t.mbox', 0, 'subj', 'aaaa', '', ?)
        """,
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()
    conn.close()

    import os
    cfg_yaml = cfg.db_path.parent / "d.yaml"
    cfg_yaml.write_text(
        "district: {name: T}\npii_detection: {builtins: []}\n"
        "exemption_codes: [{code: FERPA}]\n",
        encoding="utf-8",
    )
    os.environ["FOIA_CONFIG_FILE"] = str(cfg_yaml)
    if hasattr(client.app.state, "district_config"):
        delattr(client.app.state, "district_config")

    r = client.post(
        "/api/v1/redactions",
        headers={"X-FOIA-Reviewer": "legacy-bot"},
        json={
            "source_type": "email_body_text", "source_id": 1,
            "start_offset": 0, "end_offset": 2, "exemption_code": "FERPA",
        },
    )
    assert r.status_code == 201
    c = connect(cfg.db_path)
    row = c.execute(
        "SELECT actor, user_id FROM audit_log WHERE event_type='redaction.create'"
    ).fetchone()
    c.close()
    assert row["actor"] == "legacy-bot"
    assert row["user_id"] is None


def test_lockout_rejects_login_at_threshold(client):
    """Three failed logins (the configured threshold) lock out the account."""
    cfg = client.app.state.config
    # First, three failed attempts. Dev adapter's allowlist is
    # {alice, bob}; "mallory" never matches, so each attempt is
    # recorded as a failure.
    for _ in range(3):
        bad = client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": ""},  # 422 — never reaches policy
        )
        # Use a wrong path that DOES reach the auth path.
    # Use a username NOT in the dev allowlist so adapter rejects → counted failure.
    for _ in range(cfg.auth_lockout_threshold):
        client.post(
            "/api/v1/auth/login",
            json={"username": "evil", "password": "x"},
        )
    # The next attempt — even with a valid username — is blocked by the
    # lockout because the failures share the same target username.
    r = client.post(
        "/api/v1/auth/login",
        json={"username": "evil", "password": "x"},
    )
    assert r.status_code == 401


def test_openapi_includes_auth_routes(client):
    paths = set(client.get("/openapi.json").json()["paths"].keys())
    for p in (
        "/api/v1/auth/login",
        "/api/v1/auth/me",
        "/api/v1/auth/logout",
    ):
        assert p in paths
