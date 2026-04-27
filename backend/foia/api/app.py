"""FastAPI application factory.

Exposes the Phase 1-4 data layer as a versioned REST API. All endpoints
are read-only — the writer CLIs (ingest, extract, detect, resolve) remain
the only authority for mutating the store. This keeps the API safe to
expose internally while Phase 6+ redaction work is in progress.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..config import Config, configure_logging
from .deps import get_db
from .routes import (
    ai as ai_routes,
    attachments,
    audit as audit_routes,
    detections,
    emails,
    exports,
    persons,
    redactions,
    search,
)
from .schemas import Stats


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or Config.from_env()
    configure_logging(cfg.log_level)

    app = FastAPI(
        title="FOIA Redaction Tool API",
        description=(
            "Read-only API over ingested emails, extracted attachment text, "
            "PII detections, and unified person records. "
            "Write operations happen via the project's CLI tools."
        ),
        version="0.10.0",
    )
    app.state.config = cfg

    if cfg.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cfg.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["*"],
        )

    @app.get("/health", tags=["meta"], summary="Liveness check")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "db_path": str(cfg.db_path),
            "db_exists": cfg.db_path.exists(),
            "version": app.version,
        }

    @app.get("/stats", response_model=Stats, tags=["meta"], summary="Row counts")
    def stats(conn: sqlite3.Connection = Depends(get_db)) -> Stats:
        def count(sql: str) -> int:
            return int(conn.execute(sql).fetchone()[0])
        return Stats(
            emails=count("SELECT COUNT(*) FROM emails"),
            attachments=count("SELECT COUNT(*) FROM attachments"),
            attachments_with_text=count(
                "SELECT COUNT(*) FROM attachments_text WHERE extraction_status = 'ok'"
            ),
            pii_detections=count("SELECT COUNT(*) FROM pii_detections"),
            persons=count("SELECT COUNT(*) FROM persons"),
            redactions=count("SELECT COUNT(*) FROM redactions"),
            redactions_accepted=count(
                "SELECT COUNT(*) FROM redactions WHERE status = 'accepted'"
            ),
        )

    api_prefix = "/api/v1"
    app.include_router(emails.router, prefix=api_prefix)
    app.include_router(attachments.router, prefix=api_prefix)
    app.include_router(detections.router, prefix=api_prefix)
    app.include_router(persons.router, prefix=api_prefix)
    app.include_router(search.router, prefix=api_prefix)
    app.include_router(redactions.router, prefix=api_prefix)
    app.include_router(exports.router, prefix=api_prefix)
    app.include_router(audit_routes.router, prefix=api_prefix)
    app.include_router(ai_routes.router, prefix=api_prefix)

    return app


# Uvicorn entrypoint: `uvicorn foia.api.app:app`.
app = create_app()
