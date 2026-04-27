"""mbox ingestion pipeline.

Parses an .mbox file into SQLite, preserving the original RFC822 bytes of
each message and extracting attachments to disk.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mailbox
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Iterable

from .sanitizer import sanitize_html

log = logging.getLogger(__name__)


@dataclass
class IngestStats:
    mbox_source: str
    emails_ingested: int = 0
    emails_skipped_duplicate: int = 0
    attachments_saved: int = 0
    errors: int = 0

    def as_dict(self) -> dict:
        return {
            "mbox_source": self.mbox_source,
            "emails_ingested": self.emails_ingested,
            "emails_skipped_duplicate": self.emails_skipped_duplicate,
            "attachments_saved": self.attachments_saved,
            "errors": self.errors,
        }


_SAFE_FILENAME_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


def _safe_filename(name: str | None, fallback_ext: str = "") -> str:
    if not name:
        return f"unnamed{fallback_ext}"
    cleaned = "".join(c if c in _SAFE_FILENAME_CHARS else "_" for c in name)
    cleaned = cleaned.strip("._") or f"unnamed{fallback_ext}"
    return cleaned[:180]


def _header_str(msg: Message, name: str) -> str | None:
    v = msg.get(name)
    return None if v is None else str(v)


def _parse_address_list(header_value) -> list[str]:
    if not header_value:
        return []
    pairs = getaddresses([str(header_value)])
    out: list[str] = []
    for name, addr in pairs:
        if addr:
            out.append(f"{name} <{addr}>".strip() if name else addr)
    return out


def _parse_date(header_value) -> str | None:
    if not header_value:
        return None
    try:
        dt = parsedate_to_datetime(str(header_value))
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _headers_to_json(msg: Message) -> str:
    out: dict[str, list[str]] = {}
    for k, v in msg.items():
        out.setdefault(k, []).append(str(v))
    return json.dumps(out, ensure_ascii=False)


def _body_parts(msg: Message) -> tuple[str, str]:
    """Return (plain_text, raw_html). Both may be empty strings."""
    text_part: str = ""
    html_part: str = ""

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            ctype = part.get_content_type()
            if ctype == "text/plain" and not text_part:
                text_part = _decode_part(part)
            elif ctype == "text/html" and not html_part:
                html_part = _decode_part(part)
    else:
        ctype = msg.get_content_type()
        decoded = _decode_part(msg)
        if ctype == "text/html":
            html_part = decoded
        else:
            text_part = decoded
    return text_part, html_part


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def _iter_attachment_parts(msg: Message) -> Iterable[Message]:
    """Yield parts that should be persisted as attachments.

    This includes:
      * explicit attachments (Content-Disposition: attachment)
      * nested message/rfc822 parts (yielded whole; not descended into)
      * binary parts (images, PDFs, etc.) that are not inline text bodies
    """

    def _walk(part: Message, depth: int) -> Iterable[Message]:
        ctype = part.get_content_type()
        if ctype == "message/rfc822":
            if depth > 0:
                yield part
            return
        if part.is_multipart():
            for sub in part.iter_parts():
                yield from _walk(sub, depth + 1)
            return
        if depth == 0:
            return
        disp = (part.get("Content-Disposition") or "").lower()
        maintype = part.get_content_maintype()
        if "attachment" in disp:
            yield part
        elif maintype in {"image", "application", "audio", "video"}:
            yield part

    yield from _walk(msg, 0)


def _attachment_bytes(part: Message) -> bytes:
    if part.get_content_type() == "message/rfc822":
        inner = part.get_payload()
        if isinstance(inner, list) and inner:
            return bytes(inner[0])
        if isinstance(inner, Message):
            return bytes(inner)
        if isinstance(inner, (bytes, bytearray)):
            return bytes(inner)
        return str(inner).encode("utf-8", errors="replace")
    payload = part.get_payload(decode=True)
    if payload is None:
        return b""
    return payload


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _store_attachment(
    conn: sqlite3.Connection,
    email_id: int,
    part: Message,
    attachment_dir: Path,
    stats: IngestStats,
) -> None:
    data = _attachment_bytes(part)
    if not data:
        return

    sha = _sha256_hex(data)
    ctype = part.get_content_type()
    disp_header = part.get("Content-Disposition") or ""
    is_inline = 1 if "inline" in disp_header.lower() else 0
    is_nested_eml = 1 if ctype == "message/rfc822" else 0

    raw_name = part.get_filename()
    if not raw_name and is_nested_eml:
        inner = part.get_payload()
        inner_msg = inner[0] if isinstance(inner, list) and inner else None
        subj = (
            str(inner_msg.get("Subject")) if isinstance(inner_msg, Message) and inner_msg.get("Subject") else None
        )
        raw_name = f"{subj or 'nested'}.eml"

    fallback_ext = ""
    if ctype == "application/pdf":
        fallback_ext = ".pdf"
    elif ctype.startswith("image/"):
        fallback_ext = "." + ctype.split("/", 1)[1].split(";")[0]
    elif is_nested_eml:
        fallback_ext = ".eml"

    filename = _safe_filename(raw_name, fallback_ext=fallback_ext)
    storage_name = f"{sha[:2]}/{sha}_{filename}"
    storage_path = attachment_dir / storage_name
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    if not storage_path.exists():
        storage_path.write_bytes(data)

    conn.execute(
        """
        INSERT INTO attachments (
            email_id, filename, content_type, content_disposition,
            size_bytes, sha256, storage_path, is_inline, is_nested_eml
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            email_id,
            filename,
            ctype,
            disp_header or None,
            len(data),
            sha,
            str(storage_path),
            is_inline,
            is_nested_eml,
        ),
    )
    stats.attachments_saved += 1


def _insert_email(
    conn: sqlite3.Connection,
    msg: Message,
    raw_bytes: bytes,
    mbox_source: str,
    mbox_index: int,
) -> int | None:
    """Insert an email row and its raw_content. Returns row id, or None if duplicate."""
    message_id = (_header_str(msg, "Message-ID") or "").strip() or None
    subject = _header_str(msg, "Subject")
    from_addrs = _parse_address_list(msg.get("From"))
    to_addrs = _parse_address_list(msg.get("To"))
    cc_addrs = _parse_address_list(msg.get("Cc"))
    bcc_addrs = _parse_address_list(msg.get("Bcc"))
    date_raw = _header_str(msg, "Date")
    date_sent = _parse_date(date_raw)

    body_text, body_html_raw = _body_parts(msg)
    body_html_sanitized = sanitize_html(body_html_raw) if body_html_raw else ""

    headers_json = _headers_to_json(msg)
    ingested_at = datetime.now(timezone.utc).isoformat()

    try:
        cur = conn.execute(
            """
            INSERT INTO emails (
                message_id, mbox_source, mbox_index, subject,
                from_addr, to_addrs, cc_addrs, bcc_addrs,
                date_sent, date_raw,
                body_text, body_html_sanitized, headers_json,
                ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                mbox_source,
                mbox_index,
                subject,
                from_addrs[0] if from_addrs else None,
                json.dumps(to_addrs, ensure_ascii=False),
                json.dumps(cc_addrs, ensure_ascii=False),
                json.dumps(bcc_addrs, ensure_ascii=False),
                date_sent,
                date_raw,
                body_text,
                body_html_sanitized,
                headers_json,
                ingested_at,
            ),
        )
    except sqlite3.IntegrityError:
        return None

    email_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO raw_content (email_id, raw_rfc822, raw_sha256) VALUES (?, ?, ?)",
        (email_id, raw_bytes, _sha256_hex(raw_bytes)),
    )
    return email_id


def ingest_mbox(
    mbox_file: Path,
    conn: sqlite3.Connection,
    attachment_dir: Path,
    *,
    source_label: str | None = None,
) -> IngestStats:
    """Ingest every message in `mbox_file` into the given connection."""
    if not mbox_file.exists():
        raise FileNotFoundError(mbox_file)

    attachment_dir.mkdir(parents=True, exist_ok=True)
    label = source_label or str(mbox_file.resolve())
    stats = IngestStats(mbox_source=label)

    mbox = mailbox.mbox(str(mbox_file))
    parser = BytesParser(policy=policy.default)
    try:
        for index, key in enumerate(mbox.keys()):
            try:
                raw_bytes = mbox.get_bytes(key)
                msg = parser.parsebytes(raw_bytes)
            except Exception:
                log.exception("Failed to read message index=%s from %s", index, label)
                stats.errors += 1
                continue

            try:
                email_id = _insert_email(conn, msg, raw_bytes, label, index)
                if email_id is None:
                    stats.emails_skipped_duplicate += 1
                    continue
                for part in _iter_attachment_parts(msg):
                    try:
                        _store_attachment(conn, email_id, part, attachment_dir, stats)
                    except Exception:
                        log.exception(
                            "Failed to store attachment for email index=%s", index
                        )
                        stats.errors += 1
                stats.emails_ingested += 1
                conn.commit()
            except Exception:
                log.exception("Failed to ingest message index=%s", index)
                conn.rollback()
                stats.errors += 1
    finally:
        mbox.close()
    return stats


__all__ = ["IngestStats", "ingest_mbox"]
