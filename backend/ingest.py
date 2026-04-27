"""Phase 1 CLI.

Example:
    python ingest.py --file sample.mbox
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
from foia.ingestion import ingest_mbox

log = logging.getLogger("foia.cli")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ingest",
        description="Ingest an .mbox file into the FOIA SQLite store.",
    )
    p.add_argument("--file", "-f", required=True, help="Path to .mbox file")
    p.add_argument(
        "--label",
        default=None,
        help="Optional source label (defaults to absolute path).",
    )
    p.add_argument(
        "--db",
        default=None,
        help="Override FOIA_DB_PATH for this run.",
    )
    p.add_argument(
        "--attachments",
        default=None,
        help="Override FOIA_ATTACHMENT_DIR for this run.",
    )
    audit.add_actor_arg(p)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.from_env()
    configure_logging(cfg.log_level)

    db_path = Path(args.db).resolve() if args.db else cfg.db_path
    attachment_dir = (
        Path(args.attachments).resolve() if args.attachments else cfg.attachment_dir
    )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    attachment_dir.mkdir(parents=True, exist_ok=True)

    mbox_file = Path(args.file).resolve()
    if not mbox_file.exists():
        log.error("mbox file not found: %s", mbox_file)
        return 2

    log.info("Ingesting %s -> %s", mbox_file, db_path)
    conn = connect(db_path)
    try:
        init_schema(conn)
        stats = ingest_mbox(
            mbox_file,
            conn,
            attachment_dir,
            source_label=args.label,
        )
        audit.log_event(
            conn,
            event_type=audit.EVT_INGEST_RUN,
            actor=audit.resolve_actor(args),
            source_type="mbox",
            payload={
                "mbox_file": str(mbox_file),
                "label": args.label,
                **stats.as_dict(),
            },
        )
    finally:
        conn.close()

    json.dump(stats.as_dict(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if stats.errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
