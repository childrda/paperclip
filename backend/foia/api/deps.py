"""Shared FastAPI dependencies: DB connection, pagination, auth identity."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterator

from fastapi import Depends, Header, HTTPException, Query, Request

from .. import auth_service
from ..config import Config
from ..db import connect, init_schema


SESSION_COOKIE = "paperclip_session"


def _config(request: Request) -> Config:
    cfg = getattr(request.app.state, "config", None)
    if cfg is None:
        cfg = Config.from_env()
    return cfg


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """Open a connection, creating + migrating the DB if it doesn't exist."""
    cfg = _config(request)
    conn = connect(cfg.db_path)
    try:
        init_schema(conn)
        yield conn
    finally:
        conn.close()


@dataclass
class Pagination:
    limit: int
    offset: int


def pagination(
    limit: int = Query(50, ge=1, le=500, description="Page size (1–500)."),
    offset: int = Query(0, ge=0, description="Offset in rows."),
) -> Pagination:
    return Pagination(limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallerIdentity:
    """Who is making this request.

    Two flavours:
    * ``user_id`` set: a real authenticated user from a session cookie.
      The audit log captures both the user FK and the username string.
    * ``user_id`` None: legacy ``X-FOIA-Reviewer`` header (or empty).
      The audit log captures only ``actor``. Used by tests and any
      pre-auth tooling.
    """
    actor: str
    user_id: int | None
    username: str | None


def get_caller(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    x_foia_reviewer: str | None = Header(default=None),
) -> CallerIdentity:
    """Prefer a verified session cookie; fall back to the legacy header.

    A valid cookie short-circuits everything: we record the real user
    FK in the audit log. When no cookie is present we fall back to the
    pre-auth ``X-FOIA-Reviewer`` header — anonymous if neither.

    To force authentication on a specific endpoint (refusing the
    fallback), use :func:`require_user` instead.
    """
    cfg = _config(request)
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        try:
            sess = auth_service.verify_session_token(
                conn, cfg, token,
                source_ip=request.client.host if request.client else None,
            )
            return CallerIdentity(
                actor=sess.username, user_id=sess.user_id,
                username=sess.username,
            )
        except auth_service.AuthError:
            # Bad / expired / revoked cookie. Fall through to the
            # header path so legacy CLIs and unauth'd test clients
            # still work; real production deployments have no
            # X-FOIA-Reviewer source so they end up anonymous.
            pass
    actor = (x_foia_reviewer or "").strip() or "api:anonymous"
    return CallerIdentity(actor=actor, user_id=None, username=None)


def require_user(
    caller: CallerIdentity = Depends(get_caller),
) -> CallerIdentity:
    """Endpoint guard: rejects requests without a real session cookie.

    Used on endpoints where attribution must be verifiable — case
    creation, redaction-policy changes, exports, etc.
    """
    if caller.user_id is None:
        raise HTTPException(401, "authentication required")
    return caller


def get_actor(caller: CallerIdentity = Depends(get_caller)) -> str:
    """Backwards-compatible: returns just the actor string.

    Existing routers that consume the actor as a string keep working;
    new routers should consume :class:`CallerIdentity` directly so the
    user FK can flow into the audit log.
    """
    return caller.actor


__all__ = [
    "CallerIdentity",
    "Pagination",
    "SESSION_COOKIE",
    "get_actor",
    "get_caller",
    "get_db",
    "pagination",
    "require_user",
]
