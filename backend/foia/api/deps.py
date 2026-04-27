"""Shared FastAPI dependencies: DB connection, pagination params."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterator

from fastapi import Header, Query, Request

from ..config import Config
from ..db import connect, init_schema


def _config(request: Request) -> Config:
    cfg = getattr(request.app.state, "config", None)
    if cfg is None:
        cfg = Config.from_env()
    return cfg


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """Open a connection, creating + migrating the DB if it doesn't exist.

    The fresh-install path wants the API to auto-create — the import
    endpoint is the user's first contact with the system. ``connect()``
    handles the mkdir; ``init_schema()`` is idempotent and applies any
    pending phase migrations on every open.
    """
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


def get_actor(
    x_foia_reviewer: str | None = Header(default=None),
) -> str:
    """Phase 9 — resolve the audit-log actor for this request.

    The Phase 7 UI keeps the reviewer name in localStorage and sends it
    as ``X-FOIA-Reviewer``. Anonymous calls land in the audit log too
    so we can still see *that* something happened.
    """
    return (x_foia_reviewer or "").strip() or "api:anonymous"


__all__ = ["get_db", "get_actor", "pagination", "Pagination"]
