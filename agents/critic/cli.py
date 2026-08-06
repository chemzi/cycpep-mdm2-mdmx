"""cli - split from agents/critic.py (PR6)."""

from __future__ import annotations

import argparse, json
from .config import CriticConfig
from .errors import CriticContractError
from .report import run

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser("review", help="review a Prediction handoff")
    command.add_argument("--handoff", required=True)
    command.add_argument("--output")
    command.add_argument("--min-cohort", type=int, default=3)
    command.add_argument("--low-diversity-similarity", type=float, default=0.80)
    return parser

def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command != "review":
            raise AssertionError(args.command)
        result = run(
            handoff_path=args.handoff,
            output_path=args.output,
            config=CriticConfig(
                min_cohort_for_distribution=args.min_cohort,
                low_diversity_median_similarity=args.low_diversity_similarity,
            ),
        )
    except (CriticContractError, OSError) as exc:
        print(json.dumps({
            "status": "error",
            "code": getattr(exc, "code", exc.__class__.__name__),
            "message": str(exc),
        }, ensure_ascii=False))
        return 2
    print(json.dumps({
        "status": "complete",
        "report_path": result["report_path"],
        "report_sha256": result["report_sha256"],
        "report_id": result["report"]["report_id"],
        "verdict": result["report"]["verdict"],
        "issue_codes": [item["code"] for item in result["report"]["issues"]],
    }, ensure_ascii=False, indent=2))
    return 0
