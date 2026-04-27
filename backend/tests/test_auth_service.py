"""LDAPS auth tests via injected adapter — no real DC required.

The production adapter is exercised live against a real Active Directory.
Here we verify the policy code: lockout, group revocation on next request,
session lifecycle, audit logging, hashing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from foia.auth_service import (
    AuthError,
    AuthenticatedSession,
    DirectoryUser,
    LdapsAdapter,
    login,
    logout,
    verify_session_token,
)
from foia.config import Config


def _cfg(**overrides) -> Config:
    base = dict(
        db_path="/tmp/x",  # not used here — the test passes the conn directly
        attachment_dir="/tmp/y",
        log_level="WARNING",
        ocr_enabled=False, ocr_language="eng", ocr_dpi=200,
        tesseract_cmd=None,
        office_enabled=False, libreoffice_cmd="soffice",
        extraction_timeout_s=60,
        ldap_uri="ldaps://example",
        ldap_bind_dn="CN=svc,OU=svc,DC=example",
        ldap_bind_password="x",
        ldap_user_base_dn="OU=staff,DC=example",
        ldap_group_dn="CN=FOIA,OU=Groups,DC=example",
        auth_lockout_threshold=3,
        auth_lockout_window_minutes=15,
        auth_session_lifetime_hours=8,
        auth_group_recheck_minutes=15,
    )
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


@dataclass
class ScriptedAdapter:
    """Tiny LdapsAdapter implementation driven by test setup."""
    accept_username: str = "alice"
    accept_password: str = "correct"
    user_dn: str = "CN=Alice,OU=Staff,DC=example"
    user_display: str = "Alice Records"
    user_email: str = "alice@example.org"
    in_group: bool = True
    auth_calls: int = 0
    group_calls: int = 0
    raise_on_authenticate: AuthError | None = None

    def authenticate(self, username: str, password: str) -> DirectoryUser:
        self.auth_calls += 1
        if self.raise_on_authenticate is not None:
            raise self.raise_on_authenticate
        if (
            username != self.accept_username
            or password != self.accept_password
        ):
            raise AuthError("bad credentials")
        return DirectoryUser(
            username=username,
            directory_dn=self.user_dn,
            display_name=self.user_display,
            email=self.user_email,
        )

    def is_member_of_group(self, user_dn: str) -> bool:
        self.group_calls += 1
        return self.in_group


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_login_succeeds_and_persists_user(db_conn):
    adapter = ScriptedAdapter()
    token, sess = login(
        db_conn, _cfg(),
        username="alice", password="correct",
        source_ip="10.0.0.5", adapter=adapter,
    )
    assert isinstance(token, str) and len(token) >= 32
    assert sess.username == "alice"
    assert sess.user_id > 0
    # Mirror row created.
    row = db_conn.execute(
        "SELECT username, directory_dn, display_name, email, is_active "
        "FROM users WHERE username = ?",
        ("alice",),
    ).fetchone()
    assert row["directory_dn"] == "CN=Alice,OU=Staff,DC=example"
    assert row["display_name"] == "Alice Records"
    assert row["email"] == "alice@example.org"
    assert row["is_active"] == 1
    # Audit row recorded with user_id FK.
    audit_row = db_conn.execute(
        "SELECT actor, user_id, request_origin "
        "FROM audit_log WHERE event_type = 'auth.login_succeeded'",
    ).fetchone()
    assert audit_row["actor"] == "alice"
    assert audit_row["user_id"] == sess.user_id
    assert audit_row["request_origin"] == "api"


def test_repeat_login_updates_last_login_and_keeps_user_id(db_conn):
    adapter = ScriptedAdapter()
    cfg = _cfg()
    _, s1 = login(
        db_conn, cfg, username="alice", password="correct",
        source_ip=None, adapter=adapter,
    )
    _, s2 = login(
        db_conn, cfg, username="alice", password="correct",
        source_ip=None, adapter=adapter,
    )
    assert s1.user_id == s2.user_id
    # Two distinct sessions.
    assert s1.session_id != s2.session_id


def test_token_is_stored_hashed_not_plaintext(db_conn):
    adapter = ScriptedAdapter()
    token, sess = login(
        db_conn, _cfg(),
        username="alice", password="correct",
        source_ip=None, adapter=adapter,
    )
    row = db_conn.execute(
        "SELECT token_hash FROM user_sessions WHERE id = ?",
        (sess.session_id,),
    ).fetchone()
    # Plaintext token must not appear in the row; only its SHA-256 hex.
    assert row["token_hash"] != token
    assert len(row["token_hash"]) == 64


# ---------------------------------------------------------------------------
# Wrong-credential paths leak nothing
# ---------------------------------------------------------------------------


def test_wrong_password_logs_failure_and_raises(db_conn):
    adapter = ScriptedAdapter()
    with pytest.raises(AuthError) as exc:
        login(
            db_conn, _cfg(),
            username="alice", password="WRONG",
            source_ip="10.0.0.7", adapter=adapter,
        )
    assert exc.value.http_status == 401
    failures = db_conn.execute(
        "SELECT COUNT(*) FROM auth_failed_logins WHERE username = ?",
        ("alice",),
    ).fetchone()[0]
    assert failures == 1
    assert db_conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE event_type = 'auth.login_failed'"
    ).fetchone()[0] == 1


def test_user_not_in_group_logs_failure(db_conn):
    adapter = ScriptedAdapter(in_group=False)
    with pytest.raises(AuthError):
        login(
            db_conn, _cfg(),
            username="alice", password="correct",
            source_ip="10.0.0.7", adapter=adapter,
        )
    rows = db_conn.execute(
        "SELECT reason FROM auth_failed_logins WHERE username = 'alice'"
    ).fetchall()
    assert len(rows) == 1
    assert "group" in rows[0]["reason"]


def test_unknown_user_still_records_attempt(db_conn):
    """Important per spec: log the supplied username even when no such
    user exists, so security review can see brute-force probes."""
    adapter = ScriptedAdapter(raise_on_authenticate=AuthError("user not found"))
    with pytest.raises(AuthError):
        login(
            db_conn, _cfg(),
            username="enumerate-me", password="anything",
            source_ip="10.0.0.7", adapter=adapter,
        )
    failures = db_conn.execute(
        "SELECT username FROM auth_failed_logins"
    ).fetchall()
    assert len(failures) == 1
    assert failures[0]["username"] == "enumerate-me"


# ---------------------------------------------------------------------------
# Lockout
# ---------------------------------------------------------------------------


def test_lockout_after_threshold(db_conn):
    cfg = _cfg(auth_lockout_threshold=3, auth_lockout_window_minutes=15)
    adapter = ScriptedAdapter()
    for _ in range(3):
        with pytest.raises(AuthError):
            login(
                db_conn, cfg, username="alice", password="WRONG",
                source_ip=None, adapter=adapter,
            )
    # 4th attempt — even with the right password, locked out.
    with pytest.raises(AuthError):
        login(
            db_conn, cfg, username="alice", password="correct",
            source_ip=None, adapter=adapter,
        )
    # The lockout itself records a failure with reason='lockout'.
    reasons = [
        r["reason"] for r in db_conn.execute(
            "SELECT reason FROM auth_failed_logins WHERE username='alice' "
            "ORDER BY id"
        )
    ]
    assert reasons[-1] == "lockout"


def test_lockout_window_resets_eventually(db_conn):
    cfg = _cfg(auth_lockout_threshold=2, auth_lockout_window_minutes=15)
    adapter = ScriptedAdapter()
    # Two failures, both far in the past.
    old = (
        datetime.now(timezone.utc) - timedelta(minutes=30)
    ).isoformat()
    for _ in range(3):
        db_conn.execute(
            "INSERT INTO auth_failed_logins "
            "(username, source_ip, reason, attempted_at) VALUES (?, ?, ?, ?)",
            ("alice", None, "wrong", old),
        )
    db_conn.commit()
    # Despite many old failures, the recent-window count is 0, so login proceeds.
    token, _ = login(
        db_conn, cfg, username="alice", password="correct",
        source_ip=None, adapter=adapter,
    )
    assert token


# ---------------------------------------------------------------------------
# Session verification + group re-check on cadence
# ---------------------------------------------------------------------------


def test_verify_returns_session(db_conn):
    adapter = ScriptedAdapter()
    cfg = _cfg()
    token, _ = login(
        db_conn, cfg, username="alice", password="correct",
        source_ip=None, adapter=adapter,
    )
    sess = verify_session_token(db_conn, cfg, token, adapter=adapter)
    assert sess.username == "alice"


def test_verify_unknown_token_raises(db_conn):
    cfg = _cfg()
    with pytest.raises(AuthError):
        verify_session_token(db_conn, cfg, "deadbeef" * 8, adapter=ScriptedAdapter())


def test_verify_expired_session_raises(db_conn):
    adapter = ScriptedAdapter()
    cfg = _cfg()
    token, sess = login(
        db_conn, cfg, username="alice", password="correct",
        source_ip=None, adapter=adapter,
    )
    # Force the session into the past.
    db_conn.execute(
        "UPDATE user_sessions SET expires_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", sess.session_id),
    )
    db_conn.commit()
    with pytest.raises(AuthError):
        verify_session_token(db_conn, cfg, token, adapter=adapter)


def test_logout_revokes_session(db_conn):
    adapter = ScriptedAdapter()
    cfg = _cfg()
    token, _ = login(
        db_conn, cfg, username="alice", password="correct",
        source_ip=None, adapter=adapter,
    )
    assert logout(db_conn, token) is True
    with pytest.raises(AuthError) as exc:
        verify_session_token(db_conn, cfg, token, adapter=adapter)
    assert "revoked" in exc.value.reason


def test_group_recheck_revokes_when_membership_lost(db_conn):
    """Per spec: removal from the FOIA group revokes access on the
    *next request*, not the next login."""
    adapter = ScriptedAdapter()
    # Force a near-zero re-check window so verify always re-checks.
    cfg = _cfg(auth_group_recheck_minutes=0)
    token, sess = login(
        db_conn, cfg, username="alice", password="correct",
        source_ip=None, adapter=adapter,
    )
    # Now the directory removes this user from the group.
    adapter.in_group = False

    with pytest.raises(AuthError) as exc:
        verify_session_token(db_conn, cfg, token, adapter=adapter)
    assert "revoked" in exc.value.reason
    # Session row marked revoked; user marked inactive.
    sess_row = db_conn.execute(
        "SELECT revoked_at FROM user_sessions WHERE id = ?",
        (sess.session_id,),
    ).fetchone()
    assert sess_row["revoked_at"] is not None
    user_row = db_conn.execute(
        "SELECT is_active FROM users WHERE id = ?", (sess.user_id,),
    ).fetchone()
    assert user_row["is_active"] == 0
    # Audit row written.
    audit_rows = db_conn.execute(
        "SELECT user_id FROM audit_log WHERE event_type = 'auth.session_revoked'"
    ).fetchall()
    assert len(audit_rows) == 1


def test_group_recheck_skipped_within_window(db_conn):
    """If we just re-checked, the next call should reuse the cached
    decision and not call the adapter again."""
    adapter = ScriptedAdapter()
    cfg = _cfg(auth_group_recheck_minutes=60)  # long window
    token, _ = login(
        db_conn, cfg, username="alice", password="correct",
        source_ip=None, adapter=adapter,
    )
    pre_calls = adapter.group_calls
    verify_session_token(db_conn, cfg, token, adapter=adapter)
    verify_session_token(db_conn, cfg, token, adapter=adapter)
    # Login itself does one group check; subsequent verify within the
    # cached window should not add more.
    assert adapter.group_calls == pre_calls


# ---------------------------------------------------------------------------
# Configuration safety rails
# ---------------------------------------------------------------------------


def test_plain_ldap_uri_is_rejected_by_adapter_factory():
    from foia.auth_service import build_adapter
    cfg = _cfg(ldap_uri="ldap://example.org")  # NOT ldaps
    with pytest.raises(AuthError) as exc:
        build_adapter(cfg)
    assert "ldaps" in exc.value.reason.lower()


def test_dev_mode_accepts_any_password_for_allowlisted_users(db_conn):
    cfg = _cfg(
        ldap_uri=None,
        auth_dev_mode=True,
        auth_dev_users=("alice", "bob"),
    )
    from foia.auth_service import build_adapter
    adapter = build_adapter(cfg)
    user = adapter.authenticate("alice", "anything")
    assert user.username == "alice"
    with pytest.raises(AuthError):
        adapter.authenticate("mallory", "anything")


def test_no_ldap_no_devmode_blocks_login():
    cfg = _cfg(ldap_uri=None, auth_dev_mode=False)
    from foia.auth_service import build_adapter
    with pytest.raises(AuthError) as exc:
        build_adapter(cfg)
    assert exc.value.http_status == 500
