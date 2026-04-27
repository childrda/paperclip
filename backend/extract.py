"""Phase 2 CLI.

Extracts searchable text from every attachment recorded in the FOIA
database that has not yet been processed.

Example:
    python extract.py
    python extract.py --force                 # re-process everything
    python extract.py --attachment-id 7       # just one
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from foia import audit
from foia.config import Config, configure_logging
from foia.db import connect, init_schema
from foia.extraction import ExtractionOptions
from foia.processing import process_attachments

log = logging.getLogger("foia.cli.extract")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="extract",
        description="Extract searchable text from stored attachments.",
    )
    p.add_argument("--db", default=None, help="Override FOIA_DB_PATH for this run.")
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-process attachments that already have extracted text.",
    )
    p.add_argument(
        "--attachment-id",
        type=int,
        default=None,
        help="Only process this attachment id.",
    )
    p.add_argument(
        "--no-ocr",
        action="store_true",
        help="Disable OCR for this run even if FOIA_OCR_ENABLED=true.",
    )
    p.add_argument(
        "--no-office",
        action="store_true",
        help="Disable LibreOffice conversion for this run.",
    )
    audit.add_actor_arg(p)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.from_env()
    configure_logging(cfg.log_level)

    db_path = Path(args.db).resolve() if args.db else cfg.db_path
    if not db_path.exists():
        log.error(
            "database not found at %s; run `python ingest.py` first", db_path
        )
        return 2

    opts = ExtractionOptions(
        ocr_enabled=cfg.ocr_enabled and not args.no_ocr,
        ocr_language=cfg.ocr_language,
        ocr_dpi=cfg.ocr_dpi,
        tesseract_cmd=cfg.tesseract_cmd,
        office_enabled=cfg.office_enabled and not args.no_office,
        libreoffice_cmd=cfg.libreoffice_cmd,
        timeout_s=cfg.extraction_timeout_s,
    )

    conn = connect(db_path)
    try:
        init_schema(conn)
        stats = process_attachments(
            conn,
            options=opts,
            force=args.force,
            only_attachment_id=args.attachment_id,
        )
        audit.log_event(
            conn,
            event_type=audit.EVT_EXTRACT_RUN,
            actor=audit.resolve_actor(args),
            source_type=("attachment" if args.attachment_id else None),
            source_id=args.attachment_id,
            payload={
                "force": args.force,
                "ocr_enabled": opts.ocr_enabled,
                "office_enabled": opts.office_enabled,
                **stats.as_dict(),
            },
        )
    finally:
        conn.close()

    json.dump(stats.as_dict(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if stats.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
