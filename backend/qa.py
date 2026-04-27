"""Phase 10 CLI — AI QA layer.

AI never auto-redacts. The ``promote`` subcommand is the only path
from an AI flag to a redaction, and it's a human action.

Subcommands:
    run              scan documents with the configured AI provider
    list             list flags with optional filters
    show ID          detail view of one flag
    dismiss ID       mark a flag as not actionable
    promote ID       create a *proposed* redaction from this flag

Examples:
    python qa.py run --actor analyst                       # full DB
    python qa.py run --email-id 7
    python qa.py run --provider null                       # dry-run
    python qa.py run --provider openai --model gpt-4o-mini
    python qa.py list --status open
    python qa.py promote 17 --actor "Records Clerk"
    python qa.py dismiss 18 --actor "Records Clerk" --note "false positive"
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from foia import audit
from foia.ai import AiProviderError, build_provider
from foia.ai_driver import (
    AiFlagError,
    dismiss_flag,
    get_flag,
    list_flags,
    promote_flag,
    run_ai_qa,
)
from foia.config import Config, configure_logging
from foia.db import connect, init_schema
from foia.district import load_district_config

log = logging.getLogger("foia.cli.qa")


def _emit(payload) -> None:
    json.dump(payload, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qa",
        description="Run AI QA over the corpus and review flags. "
                    "AI never auto-redacts — promote requires a human.",
    )
    p.add_argument("--db", default=None, help="Override FOIA_DB_PATH.")
    p.add_argument("--config", default=None, help="District YAML path.")
    audit.add_actor_arg(p)

    sub = p.add_subparsers(dest="command")

    sp_run = sub.add_parser("run", help="Run an AI QA scan.")
    sp_run.add_argument("--email-id", type=int, default=None)
    sp_run.add_argument("--attachment-id", type=int, default=None)
    sp_run.add_argument(
        "--provider", default=None,
        choices=["null", "openai", "anthropic", "azure", "ollama"],
        help="Override the configured provider for this run.",
    )
    sp_run.add_argument(
        "--model", default=None,
        help="Override the configured model for this run.",
    )

    sp_ls = sub.add_parser("list", help="List AI flags.")
    sp_ls.add_argument(
        "--status", default=None,
        choices=["open", "dismissed", "promoted"],
    )
    sp_ls.add_argument("--source-type", default=None)
    sp_ls.add_argument("--source-id", type=int, default=None)
    sp_ls.add_argument("--entity-type", default=None)
    sp_ls.add_argument("--provider", default=None)
    sp_ls.add_argument("--qa-run-id", default=None)
    sp_ls.add_argument("--limit", type=int, default=100)
    sp_ls.add_argument("--offset", type=int, default=0)

    sp_show = sub.add_parser("show", help="Detail view of one flag.")
    sp_show.add_argument("flag_id", type=int)

    sp_d = sub.add_parser("dismiss", help="Mark a flag as not actionable.")
    sp_d.add_argument("flag_id", type=int)
    sp_d.add_argument("--note", default=None)

    sp_p = sub.add_parser("promote", help="Create a *proposed* redaction from this flag.")
    sp_p.add_argument("flag_id", type=int)
    sp_p.add_argument(
        "--exemption", default=None, dest="exemption_code",
        help="Override the suggested exemption code.",
    )
    sp_p.add_argument("--note", default=None)

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
    actor = audit.resolve_actor(args)
    conn = connect(db_path)

    try:
        init_schema(conn)
        command = args.command or "list"

        if command == "run":
            try:
                provider = build_provider(
                    district.ai,
                    override_provider=args.provider,
                    override_model=args.model,
                )
            except AiProviderError as e:
                log.error("could not build AI provider: %s", e)
                return 2
            stats = run_ai_qa(
                conn, provider,
                only_email_id=args.email_id,
                only_attachment_id=args.attachment_id,
            )
            audit.log_event(
                conn,
                event_type="ai_qa.run",
                actor=actor,
                payload={
                    "provider": provider.name,
                    "model": provider.model,
                    "email_id": args.email_id,
                    "attachment_id": args.attachment_id,
                    **stats.as_dict(),
                },
            )
            _emit(stats.as_dict())
            return 0

        if command == "list":
            rows, total = list_flags(
                conn,
                review_status=args.status,
                source_type=args.source_type,
                source_id=args.source_id,
                entity_type=args.entity_type,
                provider=args.provider,
                qa_run_id=args.qa_run_id,
                limit=args.limit,
                offset=args.offset,
            )
            _emit({"items": rows, "total": total,
                   "limit": args.limit, "offset": args.offset})
            return 0

        if command == "show":
            try:
                _emit(get_flag(conn, args.flag_id))
            except AiFlagError as e:
                log.error("%s", e)
                return 1
            return 0

        if command == "dismiss":
            try:
                row = dismiss_flag(conn, args.flag_id, actor=actor, note=args.note)
            except AiFlagError as e:
                log.error("%s", e)
                return 1
            audit.log_event(
                conn, event_type="ai_qa.dismiss", actor=actor,
                source_type="ai_flag", source_id=int(args.flag_id),
                payload={"note": args.note},
            )
            _emit(row)
            return 0

        if command == "promote":
            try:
                row = promote_flag(
                    conn, district, args.flag_id,
                    actor=actor,
                    exemption_code=args.exemption_code,
                    note=args.note,
                )
            except AiFlagError as e:
                log.error("%s", e)
                return 1
            audit.log_event(
                conn, event_type="ai_qa.promote", actor=actor,
                source_type="ai_flag", source_id=int(args.flag_id),
                payload={
                    "redaction_id": int(row["redaction"]["id"]),
                    "exemption_code": row["redaction"]["exemption_code"],
                    "note": args.note,
                },
            )
            _emit(row)
            return 0

        log.error("unknown command: %s", command)
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
