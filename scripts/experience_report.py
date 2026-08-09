"""Print the failure-experience summary from the evidence ledger (B 组闭环).

Usage:
    CYCPEP_DATA_DIR=... CYCPEP_EVIDENCE_DIR=... \\
        python scripts/experience_report.py [--min-failures 5]

Reads ``battery_evaluated`` events from the evidence ledger and prints:
  1. overall pass/fail counts and triage breakdown
  2. per-layer failure counts
  3. per-length failure rates
  4. per-metric median values among failed vs passed candidates
  5. the conservative length preference (if any) for the next Design round
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import experience
from experience import summarize_failures, suggest_length_preference


def _length_sort_key(item):
    """Sort length stats numerically; unparseable keys sort last."""
    try:
        return int(float(item[0]))
    except (TypeError, ValueError):
        return 10**9


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("--min-failures must be >= 1")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-failures", type=_positive_int, default=5,
                        help="minimum evaluated samples per length before a hint is emitted")
    parser.add_argument("--json", action="store_true", help="print raw summary as JSON")
    args = parser.parse_args()

    summary = summarize_failures()
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print("===== 失败经验库汇总 (battery_evaluated) =====")
    print(f"评估候选: {summary['n_evaluated']}  |  通过: {summary['n_passed']}  |  失败: {summary['n_failed']}")
    print(f"分诊分布: {summary['triage_status'] or '（无）'}")
    print()
    print("按层失败次数:")
    if summary["failed_layers"]:
        for layer, count in sorted(summary["failed_layers"].items()):
            print(f"  {layer}: {count}")
    else:
        print("  （无失败层）")
    print()
    print("按长度失败率:")
    if summary["lengths"]:
        for length, stat in sorted(summary["lengths"].items(), key=_length_sort_key):
            rate = stat["failed"] / stat["n"] if stat["n"] else 0
            print(f"  length {length}: {stat['failed']}/{stat['n']} 失败 ({rate:.0%})")
    else:
        print("  （无长度数据）")
    print()
    print("关键指标中位值 (失败 vs 通过):")
    if summary["metrics"]:
        for key, stat in sorted(summary["metrics"].items()):
            print(f"  {key}: failed={stat['median_failed']}  passed={stat['median_passed']}")
    else:
        print("  （无指标数据）")
    print()
    hint = suggest_length_preference(summary, min_failures=args.min_failures)
    if hint:
        print(f"→ 下轮长度偏好建议: {hint['lengths']}")
        print(f"  原因: {hint['reason']}")
    else:
        print("→ 证据不足或无明显更优长度，保持当前生成偏好（保守模式）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())