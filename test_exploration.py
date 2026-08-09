"""P0-E 测试：battery 证据 → desirability → Pareto → exploration shortlist。"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import data_layer
from data_layer import EvidenceLogger
from exploration import (
    EVENT_SHORTLIST,
    desirability,
    exploration_shortlist,
    record_exploration_shortlist,
)

THRESHOLDS = {
    # 方向：">=" 越大越好；"<=" 越小越好（P0-C 接口契约：标定只产出 >= / <=）
    "L2_ipsae": {"value": 0.8, "operator": ">=", "calibration_status": "calibrated"},
    "L4_nc_term_dist": {"value": 2.0, "operator": "<=", "calibration_status": "team_provisional"},
    "L7_scrmsd": {"value": 1.5, "operator": "<=", "calibration_status": "unavailable"},
}


def make_battery_row(candidate_id, layer_values, passed=False, targets=("MDM2",),
                     event_id=None):
    row = {
        "event_type": "battery_evaluated",
        "candidate_id": candidate_id,
        "passed": passed,
        "failed_layers": [] if passed else ["l4_pass"],
        "layer_values": layer_values,
        "targets": list(targets),
    }
    if event_id is not None:
        row["event_id"] = event_id
    return row


class DesirabilityTests(unittest.TestCase):
    def test_margin_directions(self):
        # ">="：超过阈值 margin 为正；"<="：低于阈值 margin 为正
        score, top, margins, _ = desirability(
            {"L2_ipsae_mdm2": 0.9, "L4_nc_distance_post": 1.0}, THRESHOLDS
        )
        self.assertAlmostEqual(margins["L2_ipsae_mdm2"], (0.9 - 0.8) / 0.8)
        self.assertAlmostEqual(margins["L4_nc_distance_post"], (2.0 - 1.0) / 2.0)
        self.assertAlmostEqual(score, sum(margins.values()) / 2)
        self.assertEqual(top, "L4_nc_distance_post")

    def test_margin_clipped_to_unit_interval(self):
        _, _, margins, _ = desirability({"L2_ipsae_mdm2": 100.0}, THRESHOLDS)
        self.assertEqual(margins["L2_ipsae_mdm2"], 1.0)
        _, _, margins, _ = desirability({"L2_ipsae_mdm2": -100.0}, THRESHOLDS)
        self.assertEqual(margins["L2_ipsae_mdm2"], -1.0)

    def test_unusable_inputs_are_skipped(self):
        # 无 threshold 条目、条目缺 value/operator、值缺失、键不可映射、NaN → 全部跳过
        score, top, margins, _ = desirability(
            {
                "L1_plddt": 90.0,                    # 无对应 threshold 条目
                "L2_ipsae_mdm2": None,               # 值缺失
                "L4_nc_distance_post": float("nan"),  # NaN
                "unknown_metric": 1.0,               # 不可映射
            },
            {"L1_plddt": {"operator": ">=", "calibration_status": "pending"}},
        )
        self.assertIsNone(score)
        self.assertIsNone(top)
        self.assertEqual(margins, {})

    def test_zero_threshold_degrades_to_sign_check(self):
        thresholds = {"L2_ipsae": {"value": 0, "operator": ">="}}
        score, _, _, _ = desirability({"L2_ipsae_mdm2": 0.5}, thresholds)
        self.assertEqual(score, 0.0)
        score, _, _, _ = desirability({"L2_ipsae_mdm2": -0.5}, thresholds)
        self.assertEqual(score, -1.0)

    def test_zero_threshold_respects_strict_operator(self):
        # review #5：threshold=0 时严格 operator 不含等号
        strict = {"L2_ipsae": {"value": 0, "operator": ">"}}
        score, _, _, _ = desirability({"L2_ipsae_mdm2": 0.0}, strict)
        self.assertEqual(score, -1.0)
        score, _, _, _ = desirability({"L2_ipsae_mdm2": 0.1}, strict)
        self.assertEqual(score, 0.0)

    def test_per_target_override_wins_over_base(self):
        # review #1：target 级 override（P0-C 标定主产物）必须优先于 base 条目
        thresholds = {
            "L2_ipsae": {
                "value": 0.8, "operator": ">=",
                "calibration_status": "team_provisional",
                "targets": {
                    "MDM2": {"value": 0.5, "operator": ">=",
                             "calibration_status": "calibrated"},
                },
            },
        }
        # base: (0.6-0.8)/0.8 = -0.25；override: (0.6-0.5)/0.5 = +0.2
        score, _, margins, consumed = desirability(
            {"L2_ipsae_mdm2": 0.6}, thresholds, target_ids=["MDM2"]
        )
        self.assertAlmostEqual(margins["L2_ipsae_mdm2"], 0.2)
        self.assertEqual(consumed, [("L2_ipsae", "MDM2", "calibrated")])
        # 其他靶标不受 MDM2 override 影响
        _, _, margins, consumed = desirability(
            {"L2_ipsae_mdmx": 0.6}, thresholds, target_ids=["MDMX"]
        )
        self.assertAlmostEqual(margins["L2_ipsae_mdmx"], -0.25)
        self.assertEqual(consumed, [("L2_ipsae", "MDMX", "team_provisional")])


class ShortlistTests(unittest.TestCase):
    def test_all_fail_batch_still_yields_shortlist(self):
        events = [
            make_battery_row(f"C{i:04d}", {
                "L2_ipsae_mdm2": 0.5 + index * 0.05,
                "L4_nc_distance_post": 4.0 - index * 0.2,
            }, event_id=f"ev{i:04d}")
            for index, i in enumerate(range(6))
        ]
        result = exploration_shortlist(events, targets=["MDM2"], k=3,
                                       thresholds=THRESHOLDS)
        self.assertEqual(result["n_evaluated"], 6)
        self.assertEqual(result["n_passed"], 0)
        self.assertEqual(len(result["shortlist"]), 3)
        # 科学红线：全灭批次入选者 passed 必须全为 false
        self.assertTrue(all(not e["passed"] for e in result["shortlist"]))
        self.assertTrue(all(e["candidate_id"] for e in result["shortlist"]))
        self.assertEqual(len(result["source_event_ids"]), 6)

    def test_pareto_front_members_rank_first(self):
        # C1 在 ipsae 上最优、C2 在距离上最优（互不支配，同属 front）；
        # C3 两项都差但被 C1/C2 支配；k=2 时两个 front 成员必须入选
        events = [
            make_battery_row("C1", {"L2_ipsae_mdm2": 0.9, "L4_nc_distance_post": 3.5}),
            make_battery_row("C2", {"L2_ipsae_mdm2": 0.4, "L4_nc_distance_post": 1.0}),
            make_battery_row("C3", {"L2_ipsae_mdm2": 0.3, "L4_nc_distance_post": 3.9}),
        ]
        result = exploration_shortlist(events, targets=["MDM2"], k=2,
                                       thresholds=THRESHOLDS)
        picked = {e["candidate_id"]: e for e in result["shortlist"]}
        self.assertEqual(set(picked), {"C1", "C2"})
        self.assertTrue(all(e["pareto_front"] for e in picked.values()))
        self.assertTrue(all(e["reason"] == "pareto_front" for e in picked.values()))

    def test_desirability_orders_non_front_candidates(self):
        # 单目标时只有最优者在 front，其余按 desirability 降序补足
        events = [
            make_battery_row("C1", {"L2_ipsae_mdm2": 0.6}),
            make_battery_row("C2", {"L2_ipsae_mdm2": 0.9}),
            make_battery_row("C3", {"L2_ipsae_mdm2": 0.7}),
        ]
        result = exploration_shortlist(events, targets=["MDM2"], k=3,
                                       thresholds=THRESHOLDS)
        self.assertEqual(
            [e["candidate_id"] for e in result["shortlist"]], ["C2", "C3", "C1"]
        )
        self.assertEqual(result["shortlist"][0]["reason"], "pareto_front")
        self.assertEqual(result["shortlist"][1]["reason"], "desirability_rank")

    def test_empty_evidence_returns_empty_shortlist(self):
        result = exploration_shortlist([], targets=["MDM2"], thresholds=THRESHOLDS)
        self.assertEqual(result["n_evaluated"], 0)
        self.assertEqual(result["shortlist"], [])
        self.assertEqual(result["source_event_ids"], [])

    def test_shortlist_filters_by_target(self):
        events = [
            make_battery_row("M1", {"L2_ipsae_mdm2": 0.9}, targets=("MDM2",)),
            make_battery_row("K1", {"L2_ipsae_k2": 0.9}, targets=("K2",)),
        ]
        result = exploration_shortlist(events, targets=["MDM2"], thresholds=THRESHOLDS)
        self.assertEqual(result["n_evaluated"], 1)
        self.assertEqual(result["shortlist"][0]["candidate_id"], "M1")

    def test_calibration_summary_buckets(self):
        events = [make_battery_row("C1", {
            "L2_ipsae_mdm2": 0.9,          # calibrated
            "L4_nc_distance_post": 1.0,    # team_provisional → provisional
            "L7_scrmsd": 1.0,              # unavailable
        })]
        result = exploration_shortlist(events, targets=["MDM2"],
                                       thresholds=THRESHOLDS)
        self.assertEqual(result["calibration"],
                         {"calibrated": 1, "provisional": 1, "unavailable": 1})

    def test_k_must_be_positive(self):
        with self.assertRaises(ValueError):
            exploration_shortlist([], k=0, thresholds=THRESHOLDS)

    def test_duplicate_candidate_keeps_latest_evaluation(self):
        # review #3：同一 candidate_id 跨轮重评估，只保留最新一行
        events = [
            make_battery_row("C1", {"L2_ipsae_mdm2": 0.5}, event_id="ev-old"),
            make_battery_row("C2", {"L2_ipsae_mdm2": 0.7}, event_id="ev-c2"),
            make_battery_row("C1", {"L2_ipsae_mdm2": 0.9}, event_id="ev-new"),
        ]
        result = exploration_shortlist(events, targets=["MDM2"], thresholds=THRESHOLDS)
        self.assertEqual(result["n_evaluated"], 2)
        self.assertEqual(
            [e["candidate_id"] for e in result["shortlist"]], ["C1", "C2"]
        )
        self.assertEqual(
            sorted(result["source_event_ids"]), ["ev-c2", "ev-new"]
        )

    def test_unmapped_metrics_are_reported(self):
        # review #2：映射不上的指标不得静默跳过，结果中可见
        events = [make_battery_row("C1", {
            "L2_ipsae_mdm2": 0.9, "totally_unknown": 1.0,
        })]
        result = exploration_shortlist(events, targets=["MDM2"],
                                       thresholds=THRESHOLDS)
        self.assertEqual(result["unmapped_metrics"], ["totally_unknown"])

    def test_missing_backend_never_raises(self):
        with patch.object(
            EvidenceLogger, "get_all", side_effect=RuntimeError("db down")
        ):
            result = exploration_shortlist(targets=["MDM2"], thresholds=THRESHOLDS)
        self.assertEqual(result["n_evaluated"], 0)
        self.assertEqual(result["shortlist"], [])


class ShortlistEventTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="exploration-test-"))
        self.original_paths = (
            data_layer.DATA_DIR,
            data_layer.EVIDENCE_DIR,
            data_layer.STATE_PATH,
            data_layer.LOG_PATH,
            data_layer.INDEX_PATH,
        )
        data_layer.DATA_DIR = self.root / "data"
        data_layer.EVIDENCE_DIR = self.root / "evidence"
        data_layer.STATE_PATH = data_layer.DATA_DIR / "state.json"
        data_layer.LOG_PATH = data_layer.EVIDENCE_DIR / "evidence_log.jsonl"
        data_layer.INDEX_PATH = data_layer.DATA_DIR / "candidate_index.csv"

    def tearDown(self):
        (
            data_layer.DATA_DIR,
            data_layer.EVIDENCE_DIR,
            data_layer.STATE_PATH,
            data_layer.LOG_PATH,
            data_layer.INDEX_PATH,
        ) = self.original_paths
        shutil.rmtree(self.root, ignore_errors=True)

    def _seed_battery(self):
        for index in range(3):
            EvidenceLogger.battery_evaluated(
                {"candidate_id": f"C{index:04d}",
                 "sequence": "ABCDEFGHIJ", "source_route": "route_A"},
                {"all_layers_pass": False, "competition_clearance": False,
                 "failed_layers": ["l4_pass"], "hard_failures": [],
                 "missing_thresholds": [], "triage_status": "needs_optimization",
                 "layer_values": {"L2_ipsae_mdm2": 0.5 + index * 0.1,
                                  "L4_nc_distance_post": 3.0 - index * 0.5},
                 "target_pass": {}, "required_targets": ["MDM2"]},
            )

    def test_record_writes_envelope_and_payload(self):
        self._seed_battery()
        result = exploration_shortlist(targets=["MDM2"], k=2,
                                       thresholds=THRESHOLDS)
        event_id = record_exploration_shortlist(result, targets=["MDM2"],
                                                round_num=2)
        self.assertTrue(event_id)
        rows = [e for e in EvidenceLogger.get_all()
                if e.get("event_type") == EVENT_SHORTLIST]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # envelope：targets / round 在顶层，payload 不重复 targets
        self.assertEqual(list(row["targets"]), ["MDM2"])
        self.assertEqual(row["round"], 2)
        self.assertEqual(row["k"], 2)
        self.assertEqual(row["n_evaluated"], 3)
        self.assertEqual(row["n_passed"], 0)
        self.assertEqual(len(row["shortlist"]), 2)
        self.assertEqual(len(row["source_event_ids"]), 3)
        self.assertIn("calibration", row)

    def test_source_event_ids_match_battery_events(self):
        self._seed_battery()
        battery_ids = {
            row["event_id"] for row in EvidenceLogger.get_all()
            if row.get("event_type") == "battery_evaluated"
        }
        result = exploration_shortlist(targets=["MDM2"], thresholds=THRESHOLDS)
        self.assertEqual(set(result["source_event_ids"]), battery_ids)

    def test_thresholds_default_path_reads_state(self):
        # review #4：CLI 真实路径（thresholds=None → State.load）必须被测试覆盖
        self._seed_battery()
        data_layer.State.save({"project_id": "exploration-test",
                               "thresholds": dict(THRESHOLDS)})
        result = exploration_shortlist(targets=["MDM2"], k=2)
        self.assertEqual(result["n_evaluated"], 3)
        self.assertTrue(all(e["desirability"] is not None
                            for e in result["shortlist"]))
        self.assertEqual(result["calibration"]["calibrated"], 1)

    def test_repeat_generation_appends_without_mutating_history(self):
        self._seed_battery()
        result = exploration_shortlist(targets=["MDM2"], thresholds=THRESHOLDS)
        record_exploration_shortlist(result, targets=["MDM2"], round_num=1)
        record_exploration_shortlist(result, targets=["MDM2"], round_num=2)
        rows = [e for e in EvidenceLogger.get_all()
                if e.get("event_type") == EVENT_SHORTLIST]
        self.assertEqual(len(rows), 2)
        # 轮次由 envelope round 区分（P0-B 接口约束）
        self.assertEqual([row["round"] for row in rows], [1, 2])
        # 既有 battery 事件原样保留（append-only）
        battery_rows = [e for e in EvidenceLogger.get_all()
                        if e.get("event_type") == "battery_evaluated"]
        self.assertEqual(len(battery_rows), 3)


if __name__ == "__main__":
    unittest.main()
