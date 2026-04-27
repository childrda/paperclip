"""Environment-backed configuration.

All paths and tunables come from env vars so containers and local runs
share the same code. .env is loaded once at import time if present.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None or not v.strip():
        return default
    try:
        return int(v)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    db_path: Path
    attachment_dir: Path
    log_level: str
    # Phase 2
    ocr_enabled: bool
    ocr_language: str
    ocr_dpi: int
    tesseract_cmd: str | None
    office_enabled: bool
    libreoffice_cmd: str
    extraction_timeout_s: int
    # Phase 7
    cors_origins: tuple[str, ...] = ()
    # Phase 8
    export_dir: Path | None = None

    @classmethod
    def from_env(cls) -> "Config":
        db_path = Path(os.environ.get("FOIA_DB_PATH", "./data/foia.db")).resolve()
        attachment_dir = Path(
            os.environ.get("FOIA_ATTACHMENT_DIR", "./data/attachments")
        ).resolve()
        log_level = os.environ.get("FOIA_LOG_LEVEL", "INFO").upper()
        return cls(
            db_path=db_path,
            attachment_dir=attachment_dir,
            log_level=log_level,
            ocr_enabled=_env_bool("FOIA_OCR_ENABLED", True),
            ocr_language=os.environ.get("FOIA_OCR_LANG", "eng"),
            ocr_dpi=_env_int("FOIA_OCR_DPI", 200),
            tesseract_cmd=os.environ.get("FOIA_TESSERACT_CMD") or None,
            office_enabled=_env_bool("FOIA_OFFICE_ENABLED", True),
            libreoffice_cmd=os.environ.get("FOIA_LIBREOFFICE_CMD", "soffice"),
            extraction_timeout_s=_env_int("FOIA_EXTRACTION_TIMEOUT_S", 180),
            cors_origins=tuple(
                o.strip()
                for o in os.environ.get(
                    "FOIA_CORS_ORIGINS", "http://localhost:5173"
                ).split(",")
                if o.strip()
            ),
            export_dir=Path(
                os.environ.get("FOIA_EXPORT_DIR", "./data/exports")
            ).resolve(),
        )


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
