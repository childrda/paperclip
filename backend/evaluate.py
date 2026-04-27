"""Phase 3 CLI — evaluate the PII detector against a synthetic dataset.

Generates a deterministic, labelled K–12 dataset on the fly, runs the
detector over it, and prints precision / recall / F1 per entity type.

Example:
    python evaluate.py
    python evaluate.py --n 500 --seed 42
    python evaluate.py --config config/district.yaml --n 200
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from foia.config import Config, configure_logging
from foia.detection import PiiDetector
from foia.district import load_district_config
from foia.evaluation import evaluate, generate_dataset

log = logging.getLogger("foia.cli.evaluate")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="evaluate",
        description="Evaluate PII detection against a labelled synthetic dataset.",
    )
    p.add_argument("--config", default=None, help="District YAML path.")
    p.add_argument("--n", type=int, default=200, help="Number of docs to generate.")
    p.add_argument("--seed", type=int, default=0, help="RNG seed for reproducibility.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.from_env()
    configure_logging(cfg.log_level)

    district = load_district_config(args.config)
    detector = PiiDetector(district.pii)

    dataset = generate_dataset(args.n, seed=args.seed)
    report = evaluate(detector, dataset)

    json.dump(report.as_dict(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
