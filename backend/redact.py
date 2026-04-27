"""Phase 6 CLI — propose, review, and manage redactions.

Subcommands:
    propose         seed `proposed` redactions from existing PII detections
    list            print redactions, filterable by status / source
    show ID         detail view
    accept ID       transition to status='accepted' (requires --reviewer)
    reject ID       transition to status='rejected' (requires --reviewer)
    delete ID       remove a redaction
    exemptions      list exemption codes configured for the district

Examples:
    python redact.py propose
    python redact.py list --status proposed
    python redact.py accept 17 --reviewer "Records Clerk"
    python redact.py reject 18 --reviewer "Records Clerk" --note "Public record"
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
from foia.redaction import (
    RedactionError,
    delete_redaction,
    get_redaction,
    list_redactions,
    propose_from_detections,
    update_redaction,
)

log = logging.getLogger("foia.cli.redact")


def _emit(payload) -> None:
    json.dump(payload, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="redact",
        description="Manage redaction spans for the FOIA dataset.",
    )
    p.add_argument("--db", default=None, help="Override FOIA_DB_PATH.")
    p.add_argument("--config", default=None, help="District YAML path.")
    audit.add_actor_arg(p)

    sub = p.add_subparsers(dest="command")

    sp_pr = sub.add_parser(
        "propose",
        help="Seed proposed redactions from PII detections.",
    )
    sp_pr.add_argument("--email-id", type=int, default=None)
    sp_pr.add_argument("--attachment-id", type=int, default=None)
    sp_pr.add_argument("--min-score", type=float, default=None)

    sp_ls = sub.add_parser("list", help="List redactions.")
    sp_ls.add_argument("--source-type", default=None)
    sp_ls.add_argument("--source-id", type=int, default=None)
    sp_ls.add_argument(
        "--status", default=None,
        choices=["proposed", "accepted", "rejected"],
    )
    sp_ls.add_argument("--origin", default=None, choices=["auto", "manual"])
    sp_ls.add_argument("--exemption", default=None, dest="exemption_code")
    sp_ls.add_argument("--limit", type=int, default=100)
    sp_ls.add_argument("--offset", type=int, default=0)

    sp_show = sub.add_parser("show", help="Detail view of one redaction.")
    sp_show.add_argument("redaction_id", type=int)

    for verb in ("accept", "reject"):
        sp = sub.add_parser(verb, help=f"Transition a redaction to status='{verb}ed'.")
        sp.add_argument("redaction_id", type=int)
        sp.add_argument("--reviewer", required=True, help="Reviewer name / id.")
        sp.add_argument("--note", default=None)

    sp_del = sub.add_parser("delete", help="Delete a redaction by id.")
    sp_del.add_argument("redaction_id", type=int)

    sub.add_parser("exemptions", help="List configured exemption codes.")

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
    conn = connect(db_path)

    try:
        init_schema(conn)
        command = args.command or "list"

        if command == "propose":
            stats = propose_from_detections(
                conn, district,
                only_email_id=args.email_id,
                only_attachment_id=args.attachment_id,
                min_score=args.min_score,
            )
            audit.log_event(
                conn,
                event_type=audit.EVT_REDACTION_PROPOSE,
                actor=audit.resolve_actor(args),
                payload=stats.as_dict(),
            )
            _emit(stats.as_dict())
            return 0

        if command == "list":
            rows, total = list_redactions(
                conn,
                source_type=args.source_type,
                source_id=args.source_id,
                status=args.status,
                origin=args.origin,
                exemption_code=args.exemption_code,
                limit=args.limit,
                offset=args.offset,
            )
            _emit({"items": rows, "total": total,
                   "limit": args.limit, "offset": args.offset})
            return 0

        if command == "show":
            try:
                _emit(get_redaction(conn, args.redaction_id))
            except RedactionError as e:
                log.error("%s", e)
                return 1
            return 0

        if command in ("accept", "reject"):
            new_status = "accepted" if command == "accept" else "rejected"
            try:
                row = update_redaction(
                    conn, district, args.redaction_id,
                    status=new_status,
                    reviewer_id=args.reviewer,
                    notes=args.note,
                )
            except RedactionError as e:
                log.error("%s", e)
                return 1
            audit.log_event(
                conn,
                event_type=audit.EVT_REDACTION_UPDATE,
                actor=audit.resolve_actor(args),
                source_type="redaction",
                source_id=int(args.redaction_id),
                payload={
                    "new_status": new_status,
                    "reviewer_id": args.reviewer,
                    "note_set": args.note is not None,
                },
            )
            _emit(row)
            return 0

        if command == "delete":
            try:
                delete_redaction(conn, args.redaction_id)
            except RedactionError as e:
                log.error("%s", e)
                return 1
            audit.log_event(
                conn,
                event_type=audit.EVT_REDACTION_DELETE,
                actor=audit.resolve_actor(args),
                source_type="redaction",
                source_id=int(args.redaction_id),
            )
            return 0

        if command == "exemptions":
            _emit([
                {"code": e.code, "description": e.description}
                for e in district.exemptions
            ])
            return 0

        log.error("unknown command: %s", command)
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
