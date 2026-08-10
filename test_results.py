"""Frontend V2 results digest tests."""

from __future__ import annotations

import math
import unittest

from web_api.results import ResultsReader


class FakeStore:
    project_id = "project-1"

    def __init__(self, *, state=None, candidates=(), evidence=()):
        self.state = dict(state or {})
        self.candidates = list(candidates)
        self.evidence = list(evidence)

    def get_state(self, project_id):
        return dict(self.state)

    def list(self):
        return list(self.candidates)

    def query(self, **filters):
        return [
            item for item in self.evidence
            if all(item.get(key) == value for key, value in filters.items())
        ]


def candidate(candidate_id, status="evaluated", metrics=None, demo_fixture=True):
    return {
        "candidate_id": candidate_id,
        "sequence": f"SEQ-{candidate_id}",
        "status": status,
        "project_id": "project-1",
        "demo_fixture": demo_fixture,
        "metrics": dict(metrics or {}),
    }


def battery(candidate_id, layer_values, passed=True, failed_layers=(), targets=("MDM2", "MDMX"), demo_fixture=True):
    return {
        "event_id": f"ev-{candidate_id}",
        "event_type": "battery_evaluated",
        "candidate_id": candidate_id,
        "project_id": "project-1",
        "demo_fixture": demo_fixture,
        "passed": passed,
        "failed_layers": list(failed_layers),
        "layer_values": dict(layer_values),
        "targets": list(targets),
        "timestamp": "2026-08-10T00:00:00+00:00",
    }


BASE_STATE = {
    "project_id": "project-1",
    "project": "Demo Project",
    "targets": {"MDM2": {}, "MDMX": {}},
}


class ResultsReaderTests(unittest.TestCase):
    def test_empty_state_reports_no_evaluated_candidates(self):
        reader = ResultsReader(FakeStore(state=BASE_STATE))
        result = reader.read()
        self.assertEqual(result["schema_version"], "frontend.results.v1")
        self.assertEqual(result["summary"]["candidates_total"], 0)
        self.assertEqual(result["summary"]["candidates_evaluated"], 0)
        self.assertEqual(result["summary"]["data_basis"], "none")
        self.assertIn("P0-D", result["conclusion"])
        self.assertEqual(result["finalists"], [])

    def test_finalists_rank_cleared_first_and_layer_stats_aggregate(self):
        store = FakeStore(
            state=BASE_STATE,
            candidates=[
                candidate("C0101", metrics={"L1_plddt": 0.87, "L2_ipsae_mdm2": 0.74}),
                candidate("C0102", metrics={"L1_plddt": 0.82, "L2_ipsae_mdm2": 0.71}),
                candidate("C0103", metrics={"L1_plddt": 0.79, "L2_ipsae_mdm2": 0.60}),
            ],
            evidence=[
                battery("C0101", {"L1_plddt": 0.87, "L2_ipsae_mdm2": 0.74}),
                battery("C0102", {"L1_plddt": 0.82, "L2_ipsae_mdm2": 0.71}),
                battery(
                    "C0103", {"L1_plddt": 0.79, "L2_ipsae_mdm2": 0.60},
                    passed=False, failed_layers=["l2_pass"],
                ),
            ],
        )
        result = ResultsReader(store).read()
        summary = result["summary"]
        self.assertEqual(summary["candidates_total"], 3)
        self.assertEqual(summary["candidates_evaluated"], 3)
        self.assertEqual(summary["hard_cleared"], 2)
        self.assertEqual(summary["hard_clearance_rate"], 2 / 3)
        self.assertEqual(summary["data_basis"], "demo_fixture")
        self.assertEqual([item["rank"] for item in result["finalists"]], [1, 2, 3])
        self.assertTrue(result["finalists"][0]["hard_cleared"])
        self.assertEqual(result["finalists"][0]["candidate_id"], "C0101")
        self.assertEqual(result["finalists"][-1]["candidate_id"], "C0103")
        layers = {layer["key"]: layer for layer in result["layers"]}
        self.assertEqual(layers["L1_plddt"]["evaluated"], 3)
        self.assertEqual(layers["L1_plddt"]["passed"], 3)
        self.assertEqual(layers["L2_ipsae"]["passed"], 2)
        self.assertIn("hard clearance", result["conclusion"])

    def test_nan_inf_layer_values_are_treated_as_missing(self):
        store = FakeStore(
            state=BASE_STATE,
            candidates=[candidate("C0901")],
            evidence=[battery("C0901", {"L1_plddt": float("nan")}, passed=False, failed_layers=["l1_pass"])],
        )
        result = ResultsReader(store).read()
        layers = {layer["key"]: layer for layer in result["layers"]}
        self.assertEqual(layers["L1_plddt"]["evaluated"], 0)
        finalist = result["finalists"][0]
        self.assertFalse(finalist["hard_cleared"])
        self.assertIsNone(finalist["desirability"])

    def test_threshold_summary_counts_consumed_entries(self):
        thresholds = {
            "L2_ipsae": {
                "value": 0.55,
                "operator": ">",
                "calibration_status": "pending",
                "targets": {
                    "MDM2": {"value": 0.8, "operator": ">=", "calibration_status": "calibrated"},
                    "MDMX": {"value": 0.75, "operator": ">=", "calibration_status": "calibrated"},
                },
            },
            "L7_scrmsd": {"value": 3.0, "operator": "<=", "calibration_status": "calibrated"},
        }
        store = FakeStore(
            state=BASE_STATE,
            candidates=[candidate("C0101")],
            evidence=[battery(
                "C0101",
                {"L2_ipsae_mdm2": 0.9, "L2_ipsae_mdmx": 0.8, "L7_scrmsd": 1.5},
            )],
        )
        summary = ResultsReader(store, thresholds=thresholds).read()["summary"]
        self.assertEqual(summary["counts"], {"calibrated": 3, "provisional": 0, "unavailable": 0})

    def test_reader_never_raises_on_missing_pieces(self):
        reader = ResultsReader(FakeStore(state={"project_id": "project-1"}))
        result = reader.read()
        self.assertEqual(result["summary"]["data_basis"], "none")
        self.assertTrue(result["layers"])
        self.assertTrue(all(layer["evaluated"] == 0 for layer in result["layers"]))



    def test_pending_candidates_are_reported_separately(self):
        store = FakeStore(
            state=BASE_STATE,
            candidates=[candidate("C0201"), candidate("C0202")],
            evidence=[battery("C0201", {"L1_plddt": 0.80})],
        )
        result = ResultsReader(store).read()
        summary = result["summary"]
        self.assertEqual(summary["candidates_evaluated"], 1)
        self.assertEqual(summary["candidates_pending_prediction"], 1)
        self.assertEqual(summary["hard_clearance_rate"], 1.0)
        self.assertEqual(
            [item["candidate_id"] for item in result["pending_candidates"]],
            ["C0202"],
        )
        self.assertEqual(len(result["finalists"]), 1)

    def test_data_basis_is_real_when_no_fixture_flags(self):
        store = FakeStore(
            state=BASE_STATE,
            candidates=[candidate("C0301", demo_fixture=False)],
            evidence=[battery("C0301", {"L1_plddt": 0.83}, demo_fixture=False)],
        )
        result = ResultsReader(store).read()
        self.assertEqual(result["summary"]["data_basis"], "real")
        self.assertIn("Current rows are real run data.", result["conclusion"])

    def test_run_trace_is_exposed_from_state(self):
        state = dict(BASE_STATE)
        state["orchestrator"] = {
            "run_id": "run-demo-1",
            "workflow_id": "workflow-demo-1",
            "plan_id": "plan-demo-1",
            "status": "completed",
        }
        result = ResultsReader(FakeStore(state=state)).read()
        self.assertEqual(result["run"]["run_id"], "run-demo-1")
        self.assertEqual(result["trace"]["workflow_id"], "workflow-demo-1")
        self.assertEqual(result["trace"]["run_id"], "run-demo-1")

    def test_per_target_buckets_and_thresholds_are_reported(self):
        thresholds = {
            "L2_ipsae": {
                "value": 0.55,
                "operator": ">",
                "calibration_status": "pending",
                "targets": {
                    "MDM2": {"value": 0.8, "operator": ">=", "calibration_status": "calibrated"},
                    "MDMX": {"value": 0.75, "operator": ">=", "calibration_status": "calibrated"},
                },
            },
        }
        store = FakeStore(
            state=BASE_STATE,
            candidates=[candidate("C0401")],
            evidence=[battery(
                "C0401",
                {"L2_ipsae_mdm2": 0.9, "L2_ipsae_mdmx": 0.7},
                passed=False,
                failed_layers=["l2_pass"],
                targets=("MDM2", "MDMX"),
            )],
        )
        layers = {layer["key"]: layer for layer in ResultsReader(store, thresholds=thresholds).read()["layers"]}
        l2 = layers["L2_ipsae"]
        self.assertEqual(l2["evaluated"], 1)
        self.assertEqual(l2["passed"], 0)
        by_target = {item["target"]: item for item in l2["per_target"]}
        self.assertEqual(by_target["mdm2"]["evaluated"], 1)
        self.assertEqual(by_target["mdmx"]["evaluated"], 1)
        threshold_by_target = {item["target"]: item for item in l2["per_target_thresholds"]}
        self.assertEqual(threshold_by_target["MDM2"]["value"], 0.8)
        self.assertEqual(threshold_by_target["MDM2"]["calibration_status"], "calibrated")
        self.assertEqual(l2["threshold"]["calibration_status"], "provisional")

if __name__ == "__main__":
    unittest.main()

