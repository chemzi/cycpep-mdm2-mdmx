"""B 组闭环测试：battery_evaluated 事件 → 经验汇总 → Design 偏好调整"""
import os, sys, tempfile
from pathlib import Path

TEST_ROOT = Path(tempfile.mkdtemp(prefix="cycpep-experience-test-"))
os.environ["CYCPEP_DATA_DIR"] = str(TEST_ROOT / "data")
os.environ["CYCPEP_EVIDENCE_DIR"] = str(TEST_ROOT / "evidence")

import data_layer  # noqa: E402
from data_layer import EvidenceLogger  # noqa: E402
import experience  # noqa: E402
from experience import (  # noqa: E402
    EVENT_BATTERY, EVENT_EXPERIENCE,
    summarize_failures, suggest_length_preference, apply_experience_preference,
)

passed = 0
failed = 0

def check(desc, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {desc}")
    else:
        failed += 1
        print(f"  [FAIL] {desc}")

def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

def make_battery(failed_layers, layer_values, length, triage_status):
    return {
        "all_layers_pass": not failed_layers,
        "competition_clearance": not failed_layers,
        "failed_layers": failed_layers,
        "hard_failures": [],
        "missing_thresholds": [],
        "triage_status": triage_status,
        "layer_values": layer_values,
        "target_pass": {},
        "required_targets": ["MDM2"],
    }

def make_candidate(cid, sequence, route="route_A"):
    return {
        "candidate_id": cid,
        "sequence": sequence,
        "source_route": route,
    }

# ============================================================
section("1. B1: battery_evaluated 结构化事件")
# ============================================================
EvidenceLogger.battery_evaluated(
    make_candidate("C0100", "ABCDEFGHIJ"),
    make_battery(["l4_pass"], {"L4_nc_distance_post": 3.5}, 10, "needs_optimization"),
)
rows = [e for e in EvidenceLogger.get_all() if e.get("event_type") == EVENT_BATTERY]
check("battery_evaluated 事件已入库", len(rows) == 1)
payload = rows[0]
check("事件带 candidate_id", payload["candidate_id"] == "C0100")
check("事件带 length", payload["length"] == 10)
check("事件带 failed_layers", payload["failed_layers"] == ["l4_pass"])
check("事件带 layer_values", payload["layer_values"]["L4_nc_distance_post"] == 3.5)
check("事件带 passed=False", payload["passed"] is False)
check("事件带 route", payload["route"] == "route_A")

# ============================================================
section("2. B2: summarize_failures 聚合")
# ============================================================
for i in range(6):
    EvidenceLogger.battery_evaluated(
        make_candidate(f"C01{i}", "ABCDEFGHIJ"),
        make_battery(["l4_pass"], {"L4_nc_distance_post": 3.0 + i * 0.1}, 10, "needs_optimization"),
    )
for i in range(6):
    EvidenceLogger.battery_evaluated(
        make_candidate(f"C02{i}", "ABCDEFGHIJKL"),
        make_battery([], {"L4_nc_distance_post": 1.1 + i * 0.1}, 12, "shortlisted"),
    )

summary = summarize_failures()
check("n_evaluated=13", summary["n_evaluated"] == 13)
check("n_failed=7 / n_passed=6", summary["n_failed"] == 7 and summary["n_passed"] == 6)
check("failed_layers 统计 l4_pass=7", summary["failed_layers"].get("l4_pass") == 7)
check("length 10: n=7 failed=7",
      summary["lengths"]["10"]["n"] == 7 and summary["lengths"]["10"]["failed"] == 7)
check("length 12: n=6 failed=0",
      summary["lengths"]["12"]["n"] == 6 and summary["lengths"]["12"]["failed"] == 0)
check("metrics L4 median_failed=3.3", summary["metrics"]["L4_nc_distance_post"]["median_failed"] == 3.3)
check("metrics L4 median_passed=1.35", summary["metrics"]["L4_nc_distance_post"]["median_passed"] == 1.35)
check("metrics 归属层 l4_pass", summary["metrics"]["L4_nc_distance_post"]["layer"] == "l4_pass")

# ============================================================
section("3. B2: suggest_length_preference 保守规则")
# ============================================================
hint = suggest_length_preference(summary)
check("强证据输出长度偏好", hint is not None and hint["lengths"] == [12])
check("偏好带 reason", hint is not None and "reason" in hint)
check("无证据返回 None", suggest_length_preference(summarize_failures([])) is None)
few = {"lengths": {"10": {"n": 3, "failed": 3}, "12": {"n": 3, "failed": 0}}}
check("证据不足(<min_failures)返回 None", suggest_length_preference(few) is None)
tie = {"lengths": {"10": {"n": 6, "failed": 3}, "12": {"n": 6, "failed": 3}}}
check("无明确更优长度返回 None", suggest_length_preference(tie) is None)
mixed = {"lengths": {"10": {"n": 6, "failed": 3}, "12": {"n": 6, "failed": 4}}}
check("最差未达 70% 返回 None", suggest_length_preference(mixed) is None)

# ============================================================
section("4. B3: apply_experience_preference 消费经验")
# ============================================================
dc_explicit = {"lengths": [8], "n": 5}
updated, hint = apply_experience_preference(dc_explicit)
check("用户显式 lengths 不被覆盖", updated["lengths"] == [8] and hint is None)
updated, hint = apply_experience_preference({})
check("空 design_config（无显式 lengths）应用偏好", hint is not None and updated["lengths"] == [12])
updated, hint = apply_experience_preference({"n": 5})
check("强证据应用长度偏好", hint is not None and updated["lengths"] == [12])
applied = [e for e in EvidenceLogger.get_all() if e.get("event_type") == EVENT_EXPERIENCE]
check("experience_applied 事件已记录", len(applied) == 2)
latest = applied[-1]
check("experience_applied 记录新旧长度",
      latest["old_lengths"] is None and latest["new_lengths"] == [12])
check("experience_applied 记录 reason", bool(latest["reason"]))

# ============================================================
section("结果汇总")
# ============================================================
total = passed + failed
print(f"\n  总计: {total} 项测试")
print(f"  通过: {passed} ({100*passed//total}%)")
print(f"  失败: {failed}")

if failed > 0:
    print("\n  [WARNING] 存在失败测试，请检查！")
    sys.exit(1)
else:
    print("\n  全部通过，B 组闭环可交付。")