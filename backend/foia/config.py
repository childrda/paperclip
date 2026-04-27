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
    # Post-Phase-10: UI-driven imports
    inbox_dir: Path | None = None
    # Authentication (LDAPS)
    ldap_uri: str | None = None
    ldap_bind_dn: str | None = None
    ldap_bind_password: str | None = None
    ldap_user_base_dn: str | None = None
    ldap_user_filter: str = "(sAMAccountName={username})"
    ldap_group_dn: str | None = None
    ldap_ca_cert_path: str | None = None
    ldap_timeout_seconds: int = 10
    auth_lockout_threshold: int = 5
    auth_lockout_window_minutes: int = 15
    auth_session_lifetime_hours: int = 8
    auth_group_recheck_minutes: int = 15
    auth_dev_mode: bool = False
    auth_dev_users: tuple[str, ...] = ()

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
            inbox_dir=Path(
                os.environ.get("FOIA_INBOX_DIR", "./data/inbox")
            ).resolve(),
            ldap_uri=os.environ.get("PAPERCLIP_LDAP_URI") or None,
            ldap_bind_dn=os.environ.get("PAPERCLIP_LDAP_BIND_DN") or None,
            ldap_bind_password=(
                os.environ.get("PAPERCLIP_LDAP_BIND_PASSWORD") or None
            ),
            ldap_user_base_dn=(
                os.environ.get("PAPERCLIP_LDAP_USER_BASE_DN") or None
            ),
            ldap_user_filter=os.environ.get(
                "PAPERCLIP_LDAP_USER_FILTER", "(sAMAccountName={username})"
            ),
            ldap_group_dn=os.environ.get("PAPERCLIP_LDAP_GROUP_DN") or None,
            ldap_ca_cert_path=(
                os.environ.get("PAPERCLIP_LDAP_CA_CERT_PATH") or None
            ),
            ldap_timeout_seconds=_env_int(
                "PAPERCLIP_LDAP_TIMEOUT_SECONDS", 10
            ),
            auth_lockout_threshold=_env_int(
                "PAPERCLIP_AUTH_LOCKOUT_THRESHOLD", 5
            ),
            auth_lockout_window_minutes=_env_int(
                "PAPERCLIP_AUTH_LOCKOUT_WINDOW_MINUTES", 15
            ),
            auth_session_lifetime_hours=_env_int(
                "PAPERCLIP_AUTH_SESSION_LIFETIME_HOURS", 8
            ),
            auth_group_recheck_minutes=_env_int(
                "PAPERCLIP_AUTH_GROUP_RECHECK_MINUTES", 15
            ),
            auth_dev_mode=_env_bool("PAPERCLIP_AUTH_DEV_MODE", False),
            auth_dev_users=tuple(
                u.strip()
                for u in os.environ.get(
                    "PAPERCLIP_AUTH_DEV_USERS", ""
                ).split(",")
                if u.strip()
            ),
        )


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
