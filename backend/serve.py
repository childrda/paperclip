"""Phase 5 CLI — launch the FastAPI backend via uvicorn.

Example:
    python serve.py                              # binds 127.0.0.1:8000
    python serve.py --host 0.0.0.0 --port 8080   # for containers / LAN
    python serve.py --reload                     # dev autoreload
"""

from __future__ import annotations

import argparse

import uvicorn


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="serve", description="Run the FOIA API server.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true", help="Enable autoreload (dev only).")
    p.add_argument(
        "--log-level", default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    uvicorn.run(
        "foia.api.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
