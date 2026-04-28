"""LDAPS-backed authentication.

Hard rules from the spec:
* LDAPS only — no plain LDAP, no STARTTLS.
* TLS certificate validation is mandatory; reject self-signed unless an
  explicit CA cert path is provided.
* The service-account password is read from env, never logged.
* Bind / group / login outcomes all emit audit-log entries.
* Lockout after N failures within a window (configurable, defaults
  5/15min).
* Group membership is re-checked on session refresh, not just at login,
  so removing a user from the directory revokes access on their next
  request.

Dev mode: setting ``PAPERCLIP_AUTH_DEV_MODE=true`` skips LDAPS entirely
and accepts any username listed in ``PAPERCLIP_AUTH_DEV_USERS`` with
*any* password. This exists for local laptop runs and tests; it MUST
NOT be enabled in production. The ``/health`` endpoint reports it so
operators can spot misconfiguration.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from . import audit

log = logging.getLogger(__name__)


SESSION_TOKEN_BYTES = 32       # → 64-char hex token


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


class AuthError(Exception):
    """Raised when authentication or session checks fail.

    The HTTP layer always renders this as a generic message — we do not
    leak which condition failed (bind vs group vs lockout). The reason
    string is logged for security review.
    """

    def __init__(self, reason: str, *, http_status: int = 401):
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status


@dataclass(frozen=True)
class DirectoryUser:
    """The minimum data we mirror from the directory after a bind."""
    username: str
    directory_dn: str
    display_name: str | None
    email: str | None


@dataclass(frozen=True)
class AuthenticatedSession:
    """What ``verify_session_token`` returns for an active request."""
    user_id: int
    username: str
    display_name: str | None
    email: str | None
    session_id: int
    expires_at: str


# ---------------------------------------------------------------------------
# LDAPS adapter (pluggable for tests)
# ---------------------------------------------------------------------------


class LdapsAdapter(Protocol):
    """Minimal surface so tests can swap in fakes without ldap3 mocks."""

    def authenticate(
        self, username: str, password: str
    ) -> DirectoryUser:
        """Bind as the user; return their directory record on success.

        Implementations MUST raise :class:`AuthError` for any failure
        (bad password, user not found, network error, group missing).
        Reason strings flow into the audit log but never to the client.
        """

    def is_member_of_group(self, user_dn: str) -> bool:
        """Return True iff the configured group lists this DN as a member."""


class _LdapsConfig:
    """Wraps the bits of :class:`Config` we need; keeps imports light."""

    def __init__(
        self,
        *,
        uri: str,
        bind_dn: str,
        bind_password: str,
        user_base_dn: str,
        user_filter: str,
        group_dn: str,
        ca_cert_path: str | None,
        timeout_seconds: int,
    ):
        if not uri.lower().startswith("ldaps://"):
            raise AuthError(
                "PAPERCLIP_LDAP_URI must use ldaps://; plain ldap:// "
                "is rejected by policy",
                http_status=500,
            )
        if "{username}" not in user_filter:
            # Without the placeholder, every login produces an LDAP
            # search that doesn't match the supplied username — every
            # login would then fail with "user not found", which from
            # the operator's seat looks like the directory itself is
            # broken. Fail loudly at startup instead.
            raise AuthError(
                "PAPERCLIP_LDAP_USER_FILTER must contain the literal "
                "'{username}' placeholder. Example: "
                "(sAMAccountName={username})",
                http_status=500,
            )
        self.uri = uri
        self.bind_dn = bind_dn
        self.bind_password = bind_password
        self.user_base_dn = user_base_dn
        self.user_filter = user_filter
        self.group_dn = group_dn
        self.ca_cert_path = ca_cert_path
        self.timeout_seconds = timeout_seconds


class _Ldap3Adapter:
    """Production adapter using the ``ldap3`` library.

    Tested only at the unit level via dependency injection of a fake
    adapter; this code is exercised live against a real DC.
    """

    def __init__(self, cfg: _LdapsConfig):
        self.cfg = cfg
        # Imported here so the bare module import path works without
        # ldap3 installed (e.g. in dev-mode tests).
        from ldap3 import Connection, Server, Tls
        validate = ssl.CERT_REQUIRED
        tls = Tls(
            validate=validate,
            ca_certs_file=cfg.ca_cert_path,
            version=ssl.PROTOCOL_TLS_CLIENT,
        )
        self._Server = Server
        self._Connection = Connection
        self._server = Server(
            cfg.uri, use_ssl=True, tls=tls,
            connect_timeout=cfg.timeout_seconds,
        )

    def _service_conn(self):
        return self._Connection(
            self._server,
            user=self.cfg.bind_dn,
            password=self.cfg.bind_password,
            auto_bind=True,
            receive_timeout=self.cfg.timeout_seconds,
        )

    def authenticate(self, username: str, password: str) -> DirectoryUser:
        if not password:
            raise AuthError("empty password")
        try:
            with self._service_conn() as svc:
                # Literal substitution of the ``{username}`` placeholder.
                # We deliberately do NOT use ``str.format()`` here because
                # real-world LDAP filters often contain other curly braces
                # (typos, alternate placeholder syntaxes, copy-pasted DNs)
                # that would otherwise raise ``ValueError: Single '}'
                # encountered in format string`` and bounce every login
                # with a generic 401.
                search_filter = self.cfg.user_filter.replace(
                    "{username}", _escape_ldap(username),
                )
                svc.search(
                    self.cfg.user_base_dn,
                    search_filter,
                    attributes=["distinguishedName", "displayName", "mail"],
                )
                if len(svc.entries) != 1:
                    raise AuthError("user not found in directory")
                entry = svc.entries[0]
                user_dn = str(entry.distinguishedName.value)
                display_name = (
                    str(entry.displayName.value)
                    if entry.displayName.value else None
                )
                email = (
                    str(entry.mail.value) if entry.mail.value else None
                )

            # Now bind as the user with their password.
            user_conn = self._Connection(
                self._server, user=user_dn, password=password,
                receive_timeout=self.cfg.timeout_seconds,
            )
            if not user_conn.bind():
                raise AuthError("bind as user failed")
            user_conn.unbind()
        except AuthError:
            raise
        except Exception as e:
            raise AuthError(f"ldap error: {e!r}") from e

        return DirectoryUser(
            username=username,
            directory_dn=user_dn,
            display_name=display_name,
            email=email,
        )

    def is_member_of_group(self, user_dn: str) -> bool:
        try:
            with self._service_conn() as svc:
                # Fast path: check the group's member attribute.
                ok = svc.search(
                    self.cfg.group_dn,
                    "(objectClass=*)",
                    attributes=["member"],
                )
                if not ok or not svc.entries:
                    return False
                members = svc.entries[0].member.values or []
                return any(
                    _dn_equal(str(m), user_dn) for m in members
                )
        except Exception:
            log.exception("group membership check failed")
            return False


def _escape_ldap(s: str) -> str:
    """Minimal LDAP filter escape for the username field."""
    return (
        s.replace("\\", r"\5c")
         .replace("*", r"\2a")
         .replace("(", r"\28")
         .replace(")", r"\29")
         .replace("\x00", r"\00")
    )


def _dn_equal(a: str, b: str) -> bool:
    """Case-insensitive comparison good enough for AD ``member`` lists."""
    return a.strip().lower() == b.strip().lower()


# ---------------------------------------------------------------------------
# Dev-mode adapter
# ---------------------------------------------------------------------------


class _DevAdapter:
    """Trust-everyone adapter for laptop dev / tests.

    Accepts any username from the configured allowlist with any
    non-empty password. Always reports group membership = True.
    """

    def __init__(self, allowed_usernames: tuple[str, ...]):
        self._allowed = {u.lower() for u in allowed_usernames}

    def authenticate(self, username: str, password: str) -> DirectoryUser:
        if not password:
            raise AuthError("empty password")
        if self._allowed and username.lower() not in self._allowed:
            raise AuthError("user not allowlisted in dev mode")
        return DirectoryUser(
            username=username,
            directory_dn=f"CN={username},OU=Dev",
            display_name=username.replace(".", " ").title(),
            email=f"{username}@dev.local",
        )

    def is_member_of_group(self, user_dn: str) -> bool:
        return True


def build_adapter(cfg) -> LdapsAdapter:
    """Choose between dev-mode and production LDAPS based on Config."""
    if cfg.auth_dev_mode:
        log.warning(
            "Authentication running in DEV MODE — directory bypass enabled. "
            "Do not use in production."
        )
        return _DevAdapter(cfg.auth_dev_users)
    if not cfg.ldap_uri:
        raise AuthError(
            "Authentication is not configured. Set PAPERCLIP_LDAP_URI "
            "or PAPERCLIP_AUTH_DEV_MODE=true.",
            http_status=500,
        )
    return _Ldap3Adapter(
        _LdapsConfig(
            uri=cfg.ldap_uri,
            bind_dn=cfg.ldap_bind_dn or "",
            bind_password=cfg.ldap_bind_password or "",
            user_base_dn=cfg.ldap_user_base_dn or "",
            user_filter=cfg.ldap_user_filter,
            group_dn=cfg.ldap_group_dn or "",
            ca_cert_path=cfg.ldap_ca_cert_path,
            timeout_seconds=cfg.ldap_timeout_seconds,
        )
    )


# ---------------------------------------------------------------------------
# Lockout policy
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _failures_in_window(
    conn: sqlite3.Connection,
    username: str,
    window_minutes: int,
) -> int:
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    ).isoformat()
    return int(conn.execute(
        "SELECT COUNT(*) FROM auth_failed_logins "
        "WHERE username = ? AND attempted_at >= ?",
        (username, cutoff),
    ).fetchone()[0])


def _record_failed_login(
    conn: sqlite3.Connection,
    username: str,
    source_ip: str | None,
    reason: str,
) -> None:
    conn.execute(
        "INSERT INTO auth_failed_logins "
        "(username, source_ip, reason, attempted_at) VALUES (?, ?, ?, ?)",
        (username, source_ip, reason, _now()),
    )
    audit.log_event(
        conn,
        event_type="auth.login_failed",
        actor=username,
        origin="api",
        payload={"reason": reason, "source_ip": source_ip},
    )


# ---------------------------------------------------------------------------
# Session tokens
# ---------------------------------------------------------------------------


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _upsert_user(
    conn: sqlite3.Connection, du: DirectoryUser, now: str,
) -> int:
    row = conn.execute(
        "SELECT id FROM users WHERE username = ?", (du.username,)
    ).fetchone()
    if row:
        user_id = int(row["id"])
        conn.execute(
            "UPDATE users SET directory_dn = ?, display_name = ?, email = ?, "
            "       last_login_at = ?, last_seen_at = ?, updated_at = ?, is_active = 1 "
            "WHERE id = ?",
            (du.directory_dn, du.display_name, du.email, now, now, now, user_id),
        )
        return user_id
    cur = conn.execute(
        """
        INSERT INTO users (
            username, directory_dn, display_name, email,
            is_active, last_login_at, last_seen_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)
        """,
        (
            du.username, du.directory_dn, du.display_name, du.email,
            now, now, now, now,
        ),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def login(
    conn: sqlite3.Connection,
    cfg,
    *,
    username: str,
    password: str,
    source_ip: str | None,
    adapter: LdapsAdapter | None = None,
) -> tuple[str, AuthenticatedSession]:
    """Authenticate a user and issue a fresh session token.

    Returns ``(plaintext_token, session_record)``. The token is shown to
    the client once; only its SHA-256 hash is stored.
    """
    if not username or not username.strip():
        # Empty username is a client bug, not a directory error.
        raise AuthError("missing username", http_status=400)
    username = username.strip()

    # Lockout enforcement (always runs, even in dev mode).
    failures = _failures_in_window(
        conn, username, cfg.auth_lockout_window_minutes,
    )
    if failures >= cfg.auth_lockout_threshold:
        # We don't reveal that lockout is the reason — same generic
        # 401 as a wrong password.
        _record_failed_login(conn, username, source_ip, "lockout")
        conn.commit()
        raise AuthError("locked out")

    a = adapter or build_adapter(cfg)
    try:
        directory_user = a.authenticate(username, password)
        if not a.is_member_of_group(directory_user.directory_dn):
            raise AuthError("group_missing")
    except AuthError as e:
        # Single recording site — anything bubbling up here counts as
        # one failed attempt. (The lockout branch above also goes
        # through _record_failed_login but never reaches this code.)
        _record_failed_login(conn, username, source_ip, e.reason)
        conn.commit()
        raise

    now = _now()
    user_id = _upsert_user(conn, directory_user, now)

    token = secrets.token_hex(SESSION_TOKEN_BYTES)
    expires = (
        datetime.now(timezone.utc)
        + timedelta(hours=cfg.auth_session_lifetime_hours)
    ).isoformat()
    cur = conn.execute(
        """
        INSERT INTO user_sessions (
            user_id, token_hash, issued_at, expires_at,
            last_refresh_at, last_group_check_at, source_ip
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, _hash_token(token), now, expires, now, now, source_ip),
    )
    session_id = int(cur.lastrowid)

    audit.log_event(
        conn,
        event_type="auth.login_succeeded",
        actor=username,
        user_id=user_id,
        origin="api",
        payload={"session_id": session_id, "source_ip": source_ip},
    )
    conn.commit()

    return token, AuthenticatedSession(
        user_id=user_id,
        username=directory_user.username,
        display_name=directory_user.display_name,
        email=directory_user.email,
        session_id=session_id,
        expires_at=expires,
    )


def verify_session_token(
    conn: sqlite3.Connection,
    cfg,
    token: str,
    *,
    source_ip: str | None = None,
    adapter: LdapsAdapter | None = None,
) -> AuthenticatedSession:
    """Validate a token, refresh activity, and re-check group on cadence.

    Per the spec: removing a user from the FOIA group must revoke their
    access on their next request. We re-check group membership every
    ``auth_group_recheck_minutes`` so the latency is bounded without
    hammering the DC on every API call.
    """
    if not token:
        raise AuthError("missing token")
    row = conn.execute(
        """
        SELECT s.id, s.user_id, s.expires_at, s.revoked_at,
               s.last_group_check_at,
               u.username, u.directory_dn, u.display_name, u.email,
               u.is_active
        FROM user_sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.token_hash = ?
        """,
        (_hash_token(token),),
    ).fetchone()
    if row is None:
        raise AuthError("unknown session")
    if row["revoked_at"]:
        raise AuthError("session revoked")
    if not row["is_active"]:
        raise AuthError("user disabled")
    now_dt = datetime.now(timezone.utc)
    if datetime.fromisoformat(row["expires_at"]) <= now_dt:
        raise AuthError("session expired")

    # Periodic group re-check.
    last_check = row["last_group_check_at"]
    needs_check = True
    if last_check:
        try:
            elapsed = now_dt - datetime.fromisoformat(last_check)
            needs_check = elapsed >= timedelta(
                minutes=cfg.auth_group_recheck_minutes,
            )
        except ValueError:
            needs_check = True

    if needs_check:
        a = adapter or build_adapter(cfg)
        if not a.is_member_of_group(row["directory_dn"] or ""):
            now = _now()
            conn.execute(
                "UPDATE user_sessions SET revoked_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            conn.execute(
                "UPDATE users SET is_active = 0, updated_at = ? WHERE id = ?",
                (now, row["user_id"]),
            )
            audit.log_event(
                conn,
                event_type="auth.session_revoked",
                actor=row["username"],
                user_id=int(row["user_id"]),
                origin="api",
                payload={
                    "reason": "group_membership_lost",
                    "source_ip": source_ip,
                },
            )
            conn.commit()
            raise AuthError("session revoked")

    now = _now()
    conn.execute(
        "UPDATE user_sessions SET last_refresh_at = ?, "
        "       last_group_check_at = CASE WHEN ? THEN ? ELSE last_group_check_at END "
        "WHERE id = ?",
        (now, 1 if needs_check else 0, now, row["id"]),
    )
    conn.execute(
        "UPDATE users SET last_seen_at = ? WHERE id = ?",
        (now, row["user_id"]),
    )
    conn.commit()

    return AuthenticatedSession(
        user_id=int(row["user_id"]),
        username=row["username"],
        display_name=row["display_name"],
        email=row["email"],
        session_id=int(row["id"]),
        expires_at=row["expires_at"],
    )


def logout(
    conn: sqlite3.Connection, token: str,
) -> bool:
    """Revoke a session by token. Returns True if a session was revoked."""
    cur = conn.execute(
        "UPDATE user_sessions SET revoked_at = ? "
        "WHERE token_hash = ? AND revoked_at IS NULL",
        (_now(), _hash_token(token)),
    )
    conn.commit()
    return cur.rowcount > 0


__all__ = [
    "AuthenticatedSession",
    "AuthError",
    "DirectoryUser",
    "LdapsAdapter",
    "build_adapter",
    "login",
    "logout",
    "verify_session_token",
]
