"""/auth — login, logout, identity check.

Sessions are HttpOnly cookies. The plaintext token is shown only to
``set-cookie`` so JS in the browser cannot read it. The DB stores only
the SHA-256 hash; ``logout`` revokes the row.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ... import auth_service
from ..deps import get_db

router = APIRouter(prefix="/auth", tags=["auth"])

SESSION_COOKIE = "paperclip_session"


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(..., min_length=1, max_length=256)
    password: str = Field(..., min_length=1, max_length=4096)


class CurrentUserOut(BaseModel):
    user_id: int
    username: str
    display_name: str | None
    email: str | None
    expires_at: str


def _client_ip(request: Request) -> str | None:
    # Prefer the proxy-forwarded IP when behind a reverse proxy that
    # sets X-Forwarded-For. Falls back to the socket peer.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _set_session_cookie(response: Response, token: str, expires_at: str) -> None:
    # SameSite=Lax: works across the same-origin Vite proxy and a
    # bundled-frontend deployment. Set Secure=True only when behind
    # HTTPS — Starlette doesn't probe TLS, so we leave it on by default
    # since the spec mandates LDAPS-only directory comms; the public
    # surface of this app is also expected to be TLS in production.
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        secure=False,  # flip to True when deployed behind HTTPS
        max_age=8 * 3600,
        path="/",
    )


@router.post("/login", response_model=CurrentUserOut, summary="Authenticate via LDAPS")
def login_endpoint(
    payload: LoginRequest,
    request: Request,
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
):
    cfg = request.app.state.config
    try:
        token, sess = auth_service.login(
            conn, cfg,
            username=payload.username,
            password=payload.password,
            source_ip=_client_ip(request),
        )
    except auth_service.AuthError as e:
        # Generic 401 for any auth failure. Reasons go to the audit log
        # for security review, not back to the client.
        raise HTTPException(
            status_code=e.http_status,
            detail="invalid credentials" if e.http_status == 401 else e.reason,
        )
    _set_session_cookie(response, token, sess.expires_at)
    return CurrentUserOut(
        user_id=sess.user_id,
        username=sess.username,
        display_name=sess.display_name,
        email=sess.email,
        expires_at=sess.expires_at,
    )


@router.post("/logout", summary="Revoke the current session")
def logout_endpoint(
    request: Request,
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        auth_service.logout(conn, token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me", response_model=CurrentUserOut, summary="Current authenticated user")
def me_endpoint(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "not authenticated")
    cfg = request.app.state.config
    try:
        sess = auth_service.verify_session_token(
            conn, cfg, token, source_ip=_client_ip(request),
        )
    except auth_service.AuthError:
        raise HTTPException(401, "not authenticated")
    return CurrentUserOut(
        user_id=sess.user_id,
        username=sess.username,
        display_name=sess.display_name,
        email=sess.email,
        expires_at=sess.expires_at,
    )
