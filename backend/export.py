"""Phase 8 CLI — generate the redacted PDF + CSV log.

Examples:
    python export.py --out exports/2026-04-27/
    python export.py --out exports/case-1234/ --emails 1,2,3
    python export.py --out exports/case-1234/ --no-attachments
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
from foia.district import load_district_config
from foia.export import ExportConfig, run_export

log = logging.getLogger("foia.cli.export")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="export",
        description="Generate a redacted PDF + CSV log for a FOIA production.",
    )
    p.add_argument("--db", default=None, help="Override FOIA_DB_PATH.")
    p.add_argument("--config", default=None, help="District YAML path.")
    p.add_argument(
        "--out", required=True,
        help="Output directory; will be created if needed.",
    )
    p.add_argument(
        "--emails",
        default=None,
        help="Comma-separated email ids to include (default: all).",
    )
    p.add_argument(
        "--no-attachments",
        action="store_true",
        help="Skip extracted attachment text from the export.",
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

    only_emails: list[int] | None = None
    if args.emails:
        try:
            only_emails = [int(x) for x in args.emails.split(",") if x.strip()]
        except ValueError:
            log.error("--emails must be a comma-separated list of integers")
            return 2

    district = load_district_config(args.config)
    out_dir = Path(args.out).resolve()
    export_cfg = ExportConfig(output_dir=out_dir)

    conn = connect(db_path)
    try:
        init_schema(conn)
        stats = run_export(
            conn, district, export_cfg,
            only_email_ids=only_emails,
            include_attachments=not args.no_attachments,
        )
        audit.log_event(
            conn,
            event_type=audit.EVT_EXPORT_RUN,
            actor=audit.resolve_actor(args),
            source_type="export",
            payload={
                "output_dir": str(export_cfg.output_dir),
                "only_email_ids": only_emails,
                "include_attachments": not args.no_attachments,
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
