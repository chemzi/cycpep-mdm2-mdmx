"""Compute and record the exploration shortlist from battery evidence (P0-E).

Usage:
    CYCPEP_DATA_DIR=... CYCPEP_EVIDENCE_DIR=... \\
        python scripts/exploration_report.py [--targets MDM2,MDMX] [--k 5] \\
            [--round N] [--dry-run] [--json]

Reads ``battery_evaluated`` events (累计全部轮次), computes desirability +
Pareto shortlist, prints it, and appends one ``exploration_shortlist``
evidence event (unless --dry-run). shortlist 不是 scientific pass：
入选者的 passed 与其 battery 原始判定一致。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from exploration import exploration_shortlist, record_exploration_shortlist


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", default=None,
                        help="comma-separated target IDs; default: all evidence")
    parser.add_argument("--k", type=_positive_int, default=5,
                        help="shortlist size (default 5)")
    parser.add_argument("--round", type=_positive_int, default=None, dest="round_num",
                        help="round number written to the event envelope")
    parser.add_argument("--dry-run", action="store_true",
                        help="print only; do not record the evidence event")
    parser.add_argument("--json", action="store_true", help="print raw result as JSON")
    args = parser.parse_args()

    targets = (
        [item.strip() for item in args.targets.split(",") if item.strip()]
        if args.targets else None
    )
    result = exploration_shortlist(targets=targets, k=args.k)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("===== 探索 shortlist (battery_evaluated → desirability + Pareto) =====")
        print(f"评估候选: {result['n_evaluated']}  |  hard clearance 通过: "
              f"{result['n_passed']}  |  top-k: {result['k']}")
        cal = result["calibration"]
        print(f"阈值标定: calibrated={cal['calibrated']}  provisional="
              f"{cal['provisional']}  unavailable={cal['unavailable']}")
        print()
        if result["shortlist"]:
            for rank, entry in enumerate(result["shortlist"], start=1):
                desirability = entry["desirability"]
                score = f"{desirability:+.3f}" if desirability is not None else "n/a"
                front = "Pareto front" if entry["pareto_front"] else "non-front"
                print(f"  {rank}. {entry['candidate_id']}  "
                      f"passed={entry['passed']}  desirability={score}  "
                      f"{front}  ({entry['reason']}"
                      + (f", 最强项 {entry['top_margin_metric']}"
                         if entry["top_margin_metric"] else "")
                      + ")")
            if result["n_passed"] == 0:
                print()
                print("  ※ 本批 hard clearance 全灭：以上为下一轮最值得探索的候选，"
                      "均保持 not passed。")
        else:
            print("  （无证据，不产出 shortlist）")

    if args.dry_run or not result["shortlist"]:
        return 0
    event_id = record_exploration_shortlist(
        result, targets=targets, round_num=args.round_num
    )
    print(f"\n已记录 exploration_shortlist 事件: {event_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
