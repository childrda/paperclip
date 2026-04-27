"""Phase 3 CLI — scan emails and extracted attachment text for PII.

Example:
    python detect.py
    python detect.py --email-id 12
    python detect.py --attachment-id 4
    python detect.py --config config/district.yaml
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
from foia.detection import PiiDetector
from foia.detection_driver import run_detection
from foia.district import load_district_config

log = logging.getLogger("foia.cli.detect")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="detect",
        description="Detect PII in the ingested and extracted text.",
    )
    p.add_argument("--db", default=None, help="Override FOIA_DB_PATH.")
    p.add_argument("--config", default=None, help="District YAML path.")
    p.add_argument(
        "--email-id", type=int, default=None,
        help="Only scan the given email id (and no attachments).",
    )
    p.add_argument(
        "--attachment-id", type=int, default=None,
        help="Only scan the given attachment id (and no emails).",
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

    district = load_district_config(args.config)
    log.info(
        "district=%s builtins=%d custom=%d min_score=%.2f ner=%s",
        district.name,
        len(district.pii.builtins),
        len(district.pii.custom_recognizers),
        district.pii.min_score,
        district.pii.enable_ner,
    )
    detector = PiiDetector(district.pii)

    conn = connect(db_path)
    try:
        init_schema(conn)
        stats = run_detection(
            conn,
            detector,
            only_email_id=args.email_id,
            only_attachment_id=args.attachment_id,
        )
        scope_source_type = (
            "email" if args.email_id else
            "attachment" if args.attachment_id else None
        )
        scope_source_id = args.email_id or args.attachment_id
        audit.log_event(
            conn,
            event_type=audit.EVT_DETECTION_RUN,
            actor=audit.resolve_actor(args),
            source_type=scope_source_type,
            source_id=scope_source_id,
            payload={
                "district": district.name,
                "min_score": district.pii.min_score,
                **stats.as_dict(),
            },
        )
    finally:
        conn.close()

    json.dump(stats.as_dict(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
