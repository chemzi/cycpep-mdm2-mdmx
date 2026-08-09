"""B 组闭环测试：battery_evaluated 事件 → 经验汇总 → Design 偏好调整。"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import data_layer
from data_layer import EvidenceLogger
import experience
from experience import (
    EVENT_BATTERY,
    EVENT_EXPERIENCE,
    apply_experience_preference,
    consume_experience_preference,
    record_applied_preference,
    suggest_length_preference,
    summarize_failures,
)


class _BatteryFixtures:
    @staticmethod
    def make_battery(failed_layers, layer_values, length, triage_status, targets=("MDM2",)):
        return {
            "all_layers_pass": not failed_layers,
            "competition_clearance": not failed_layers,
            "failed_layers": failed_layers,
            "hard_failures": [],
            "missing_thresholds": [],
            "triage_status": triage_status,
            "layer_values": layer_values,
            "target_pass": {},
            "required_targets": list(targets),
        }

    @staticmethod
    def make_candidate(candidate_id, sequence, route="route_A"):
        return {
            "candidate_id": candidate_id,
            "sequence": sequence,
            "source_route": route,
        }

    def seed_mdm2_evidence(self):
        for index in range(6):
            EvidenceLogger.battery_evaluated(
                self.make_candidate(f"C01{index}", "ABCDEFGHIJ"),
                self.make_battery(
                    ["l4_pass"],
                    {"L4_nc_distance_post": 3.0 + index * 0.1},
                    10,
                    "needs_optimization",
                ),
            )
        for index in range(6):
            EvidenceLogger.battery_evaluated(
                self.make_candidate(f"C02{index}", "ABCDEFGHIJKL"),
                self.make_battery(
                    [], {"L4_nc_distance_post": 1.1 + index * 0.1}, 12, "shortlisted"
                ),
            )


class ExperienceTests(_BatteryFixtures, unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="experience-test-"))
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

    def test_battery_evaluated_event_structure(self):
        EvidenceLogger.battery_evaluated(
            self.make_candidate("C0100", "ABCDEFGHIJ"),
            self.make_battery(
                ["l4_pass"], {"L4_nc_distance_post": 3.5}, 10, "needs_optimization"
            ),
        )
        rows = [e for e in EvidenceLogger.get_all() if e.get("event_type") == EVENT_BATTERY]
        self.assertEqual(len(rows), 1)
        payload = rows[0]
        self.assertEqual(payload["candidate_id"], "C0100")
        self.assertEqual(payload["length"], 10)
        self.assertEqual(payload["failed_layers"], ["l4_pass"])
        self.assertEqual(payload["layer_values"]["L4_nc_distance_post"], 3.5)
        self.assertFalse(payload["passed"])
        self.assertEqual(payload["route"], "route_A")

    def test_summarize_failures_aggregates(self):
        self.seed_mdm2_evidence()
        summary = summarize_failures()
        self.assertEqual(summary["n_evaluated"], 12)
        self.assertEqual(summary["n_failed"], 6)
        self.assertEqual(summary["n_passed"], 6)
        self.assertEqual(summary["failed_layers"].get("l4_pass"), 6)
        self.assertEqual(summary["lengths"]["10"]["n"], 6)
        self.assertEqual(summary["lengths"]["10"]["failed"], 6)
        self.assertEqual(summary["lengths"]["12"]["failed"], 0)
        self.assertEqual(summary["metrics"]["L4_nc_distance_post"]["median_failed"], 3.25)
        self.assertEqual(summary["metrics"]["L4_nc_distance_post"]["median_passed"], 1.35)
        self.assertEqual(summary["metrics"]["L4_nc_distance_post"]["layer"], "l4_pass")
    def test_suggest_length_preference_conservative(self):
        summary = {
            "lengths": {"10": {"n": 6, "failed": 6}, "12": {"n": 6, "failed": 0}}
        }
        hint = suggest_length_preference(summary)
        self.assertIsNotNone(hint)
        self.assertEqual(hint["lengths"], [12])
        self.assertIn("reason", hint)
        self.assertIsNone(suggest_length_preference({}))
        few = {"lengths": {"10": {"n": 3, "failed": 3}, "12": {"n": 3, "failed": 0}}}
        self.assertIsNone(suggest_length_preference(few))
        tie = {"lengths": {"10": {"n": 6, "failed": 3}, "12": {"n": 6, "failed": 3}}}
        self.assertIsNone(suggest_length_preference(tie))
        mixed = {"lengths": {"10": {"n": 6, "failed": 3}, "12": {"n": 6, "failed": 4}}}
        self.assertIsNone(suggest_length_preference(mixed))

    def test_apply_never_overrides_explicit_lengths(self):
        self.seed_mdm2_evidence()
        updated, hint = apply_experience_preference({"lengths": [8], "n": 5})
        self.assertEqual(updated["lengths"], [8])
        self.assertIsNone(hint)
        updated, hint = apply_experience_preference(
            {"n": 5}, target_spec={"target_name": "MDM2", "lengths": [9]}
        )
        self.assertIsNone(hint)
        self.assertNotIn("lengths", updated)

    def test_apply_applies_when_no_explicit_lengths(self):
        self.seed_mdm2_evidence()
        updated, hint = apply_experience_preference({})
        self.assertIsNotNone(hint)
        self.assertEqual(updated["lengths"], [12])

    def test_record_applied_preference_after_merge(self):
        self.seed_mdm2_evidence()
        summary = summarize_failures()
        hint = suggest_length_preference(summary)
        record_applied_preference(None, hint, summary=summary, targets=["MDM2"])
        applied = [
            e for e in EvidenceLogger.get_all() if e.get("event_type") == EVENT_EXPERIENCE
        ]
        self.assertEqual(len(applied), 1)
        self.assertIsNone(applied[0]["old_lengths"])
        self.assertEqual(applied[0]["new_lengths"], [12])
        self.assertEqual(list(applied[0]["targets"]), ["MDM2"])

    def test_nan_inf_layer_values_are_rejected(self):
        events = [
            {
                "event_type": EVENT_BATTERY,
                "payload": {
                    "passed": False,
                    "length": 10,
                    "failed_layers": ["l4_pass"],
                    "triage_status": "needs_optimization",
                    "layer_values": {
                        "L4_nc_distance_post": float("nan"),
                        "L5_ring_closure": float("inf"),
                    },
                },
            },
        ]
        summary = summarize_failures(events)
        self.assertIsNone(summary["metrics"].get("L4_nc_distance_post"))
        self.assertIsNone(summary["metrics"].get("L5_ring_closure"))
        self.assertEqual(summary["n_failed"], 1)

    def test_float_length_key_does_not_crash(self):
        events = []
        for _ in range(6):
            events.append({
                "event_type": EVENT_BATTERY,
                "payload": {
                    "passed": True,
                    "length": 10.0,
                    "failed_layers": [],
                    "triage_status": "shortlisted",
                    "layer_values": {},
                },
            })
        for _ in range(6):
            events.append({
                "event_type": EVENT_BATTERY,
                "payload": {
                    "passed": False,
                    "length": 12.0,
                    "failed_layers": ["l4_pass"],
                    "triage_status": "needs_optimization",
                    "layer_values": {},
                },
            })
        summary = summarize_failures(events)
        self.assertIn("10", summary["lengths"])
        self.assertIn("12", summary["lengths"])
        hint = suggest_length_preference(summary)  # must not raise int("10.0")
        self.assertIsNotNone(hint)
        self.assertEqual(hint["lengths"], [10])
    def test_out_of_range_length_is_never_applied(self):
        for index in range(6):
            EvidenceLogger.battery_evaluated(
                self.make_candidate(f"E{index:04d}", "ABCDEFGHIJKL"),
                self.make_battery(["l4_pass"], {}, 12, "needs_optimization"),
            )
        for index in range(6):
            EvidenceLogger.battery_evaluated(
                self.make_candidate(f"F{index:04d}", "ABC"),
                self.make_battery([], {}, 3, "shortlisted"),
            )
        updated, hint = apply_experience_preference({})
        self.assertIsNone(hint)
        self.assertNotIn("lengths", updated)

    def test_summarize_filters_by_target(self):
        self.seed_mdm2_evidence()
        for index in range(6):
            EvidenceLogger.battery_evaluated(
                self.make_candidate(f"K01{index}", "ABCDEFGHIJ"),
                self.make_battery(
                    ["l4_pass"], {}, 10, "needs_optimization", targets=("K2",)
                ),
            )
        self.assertEqual(summarize_failures()["n_evaluated"], 18)
        self.assertEqual(summarize_failures(targets=["MDM2"])["n_evaluated"], 12)
        self.assertEqual(summarize_failures(targets=["K2"])["n_evaluated"], 6)
        updated, hint = apply_experience_preference(
            {}, target_spec={"target_name": "K2"}
        )
        self.assertIsNone(hint)

    def test_consume_experience_preference(self):
        self.seed_mdm2_evidence()
        lengths, hint = consume_experience_preference(targets=["MDM2"])
        self.assertEqual(lengths, [12])
        self.assertIsNotNone(hint)

    def test_missing_backend_never_raises(self):
        with patch.object(
            EvidenceLogger, "get_all", side_effect=RuntimeError("db down")
        ):
            summary = summarize_failures()
            self.assertEqual(summary["n_evaluated"], 0)
            self.assertIsNone(suggest_length_preference(summary))
            updated, hint = apply_experience_preference({})
            self.assertEqual(updated, {})
            self.assertIsNone(hint)

    def test_record_applied_preference_is_idempotent(self):
        # P2-1：同一偏好应用（同一靶标、同一新旧长度、同一原因）只记一条
        # experience_applied，重复 run / 重复物化不重复记账。
        self.seed_mdm2_evidence()
        summary = summarize_failures()
        hint = suggest_length_preference(summary)
        record_applied_preference(None, hint, summary=summary, targets=["MDM2"])
        record_applied_preference(None, hint, summary=summary, targets=["MDM2"])
        applied = [
            e for e in EvidenceLogger.get_all() if e.get("event_type") == EVENT_EXPERIENCE
        ]
        self.assertEqual(len(applied), 1)

    def test_empty_explicit_lengths_list_is_not_overridden(self):
        # P2-2：lengths=[] 是显式指定（尽管为空），不得被经验偏好覆盖。
        self.seed_mdm2_evidence()
        updated, hint = apply_experience_preference({"lengths": []})
        self.assertIsNone(hint)
        self.assertEqual(updated.get("lengths"), [])
        updated, hint = apply_experience_preference(
            {}, target_spec={"target_name": "MDM2", "lengths": []}
        )
        self.assertIsNone(hint)
        self.assertNotIn("lengths", updated)

    def test_min_failures_must_be_positive(self):
        summary = {
            "lengths": {"10": {"n": 6, "failed": 6}, "12": {"n": 6, "failed": 0}}
        }
        with self.assertRaises(ValueError):
            suggest_length_preference(summary, min_failures=0)
        hint = suggest_length_preference(summary, min_failures=1)
        self.assertIsNotNone(hint)

    def test_route_a_scopes_experience_to_resolved_target(self):
        # P1-1：CLI 不带 --target 时 target_spec 无 target_name，route_a 必须
        # 先解析出本靶标再消费经验，避免全靶标证据污染。
        import agents.design.route_a as route_a

        captured = {}

        def fake_apply(design_config, target_spec=None, targets=None, min_failures=5):
            captured["targets"] = targets
            return dict(design_config or {}), None

        def fake_resolve_target(project, target_spec, design_config):
            return {"id": "MDM2"}

        def fake_merge(target_spec, design_config, project_config=None):
            return {
                "target_id": "MDM2",
                "target_name": "MDM2",
                "seed": 1,
                "lengths": [12],
                "n": 1,
            }

        class _FakeContext:
            project_config = {"project_id": "p", "targets": [{"id": "MDM2"}]}
            output_dir = str(self.root)

        with patch.object(route_a, "_resolve_target", fake_resolve_target), patch.object(
            route_a, "_merge_config", fake_merge
        ), patch.object(
            route_a, "_route_a_generate_backbones", lambda config, batch_dir: ([], 0)
        ), patch.object(route_a, "_load_existing_sequences", lambda: set()), patch.object(
            route_a, "_collect_raw_sequences", lambda entries: ({}, {})
        ), patch.object(
            route_a,
            "_cheap_filter_sequences",
            lambda seqs, seen_seqs=None, top_k=None: [],
        ), patch.object(route_a, "EvidenceLogger"), patch(
            "experience.apply_experience_preference", fake_apply
        ):
            route_a.design_rfpeptides(
                target_spec={}, design_config={}, context=_FakeContext()
            )
        self.assertEqual(captured["targets"], ["MDM2"])


if __name__ == "__main__":
    unittest.main()