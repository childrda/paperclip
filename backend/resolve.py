"""Phase 4 CLI — entity resolution.

Subcommands:
    run         scan emails and build/update the persons table
    list        print all persons sorted by occurrence count
    show ID     detail view of a single person
    merge L W   merge person L (loser) into W (winner)
    rename ID   override a person's display name
    note ID     attach a free-form note

Examples:
    python resolve.py                       # default is `run`
    python resolve.py run
    python resolve.py list
    python resolve.py show 3
    python resolve.py merge 12 7
    python resolve.py rename 7 "Principal Jane Doe"
    python resolve.py note 7 "Contact through principal@district.example.org"
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
from foia.er_driver import (
    MergeError,
    annotate_person,
    list_persons,
    merge_persons,
    rename_person,
    run_resolution,
    show_person,
)

log = logging.getLogger("foia.cli.resolve")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="resolve",
        description="Entity resolution: unify identities across emails.",
    )
    p.add_argument("--db", default=None, help="Override FOIA_DB_PATH.")
    p.add_argument("--config", default=None, help="District YAML path.")
    audit.add_actor_arg(p)

    sub = p.add_subparsers(dest="command")

    sp_run = sub.add_parser("run", help="Scan emails and update persons table.")
    sp_run.add_argument(
        "--email-id", type=int, default=None,
        help="Restrict to a single email id.",
    )

    sub.add_parser("list", help="List all persons.")

    sp_show = sub.add_parser("show", help="Show details for one person.")
    sp_show.add_argument("person_id", type=int)

    sp_merge = sub.add_parser("merge", help="Merge person LOSER into WINNER.")
    sp_merge.add_argument("loser_id", type=int)
    sp_merge.add_argument("winner_id", type=int)

    sp_rename = sub.add_parser("rename", help="Override the display name.")
    sp_rename.add_argument("person_id", type=int)
    sp_rename.add_argument("name", type=str)

    sp_note = sub.add_parser("note", help="Attach a free-form note.")
    sp_note.add_argument("person_id", type=int)
    sp_note.add_argument("text", type=str)

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

    conn = connect(db_path)
    try:
        init_schema(conn)
        command = args.command or "run"

        if command == "run":
            district = load_district_config(args.config)
            stats = run_resolution(
                conn,
                internal_domains=district.email_domains,
                only_email_id=getattr(args, "email_id", None),
            )
            audit.log_event(
                conn,
                event_type=audit.EVT_RESOLVE_RUN,
                actor=audit.resolve_actor(args),
                source_type=("email" if getattr(args, "email_id", None) else None),
                source_id=getattr(args, "email_id", None),
                payload=stats.as_dict(),
            )
            json.dump(stats.as_dict(), sys.stdout, indent=2)
            sys.stdout.write("\n")
            return 0

        if command == "list":
            payload = list_persons(conn)
            json.dump(payload, sys.stdout, indent=2)
            sys.stdout.write("\n")
            return 0

        if command == "show":
            data = show_person(conn, args.person_id)
            if data is None:
                log.error("person id %s not found", args.person_id)
                return 1
            json.dump(data, sys.stdout, indent=2)
            sys.stdout.write("\n")
            return 0

        if command == "merge":
            try:
                result = merge_persons(conn, args.loser_id, args.winner_id)
            except MergeError as e:
                log.error("%s", e)
                return 1
            audit.log_event(
                conn,
                event_type=audit.EVT_RESOLVE_MERGE,
                actor=audit.resolve_actor(args),
                source_type="person",
                source_id=int(result["winner_id"]),
                payload={
                    "loser_id": args.loser_id,
                    "winner_id": args.winner_id,
                    "merged_name": result.get("merged_name"),
                },
            )
            json.dump(result, sys.stdout, indent=2)
            sys.stdout.write("\n")
            return 0

        if command == "rename":
            try:
                result = rename_person(conn, args.person_id, args.name)
            except MergeError as e:
                log.error("%s", e)
                return 1
            audit.log_event(
                conn,
                event_type=audit.EVT_RESOLVE_RENAME,
                actor=audit.resolve_actor(args),
                source_type="person",
                source_id=int(args.person_id),
                payload={"new_display_name": result.get("display_name")},
            )
            json.dump(result, sys.stdout, indent=2)
            sys.stdout.write("\n")
            return 0

        if command == "note":
            annotate_person(conn, args.person_id, args.text)
            audit.log_event(
                conn,
                event_type=audit.EVT_RESOLVE_NOTE,
                actor=audit.resolve_actor(args),
                source_type="person",
                source_id=int(args.person_id),
                payload={"note": args.text},
            )
            return 0

        log.error("unknown command: %s", command)
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
