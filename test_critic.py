"""Critic contract and integration tests; no model or GPU dependency."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import data_layer
from agents.critic import CriticConfig, CriticContractError, review, run
from prediction_pipeline.contracts import file_sha256


TARGETS = ("MDM2", "MDMX")


def complete_battery(*, failed=(), unjustified=()):
    failed = set(failed)
    return {
        **{key: key not in failed for key in (f"l{i}_pass" for i in range(1, 8))},
        "all_layers_pass": not failed,
        "failed_layers": sorted(failed),
        "missing_evidence": [],
        "threshold_audit": {
            key: {
                "justified": key not in unjustified,
                "reason": "paper_explicit" if key not in unjustified else "team_provisional",
            }
            for key in (
                "L1_plddt", "L2_ipsae:MDM2", "L2_ipsae:MDMX",
                "L3_dg:MDM2", "L3_dg:MDMX", "L4_nc_term_dist",
            )
        },
    }


def metrics(*, ipsae_mdm2=0.72, ipsae_mdmx=0.70):
    return {
        "global": {
            "plddt": 0.9,
            "nc_distance_pre": 1.33,
            "nc_distance_post": 1.33,
            "post_relax_backbone_rmsd": 0.1,
            "scrmsd": 0.8,
        },
        "targets": {
            "MDM2": {
                "ipsae": ipsae_mdm2,
                "iptm": 0.91,
                "dg": -12.0,
                "dg_method": "prodigy",
                "sc": 0.7,
                "dsasa": 900.0,
                "hotspot_cov": 1.0,
                "site_consistency": True,
                "pose_rmsd": 0.8,
                "seed_convergence": 1.0,
            },
            "MDMX": {
                "ipsae": ipsae_mdmx,
                "iptm": 0.89,
                "dg": -11.0,
                "dg_method": "prodigy",
                "sc": 0.68,
                "dsasa": 850.0,
                "hotspot_cov": 1.0,
                "site_consistency": True,
                "pose_rmsd": 0.9,
                "seed_convergence": 1.0,
            },
        },
    }


class CriticTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="critic-test-"))
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

    def _record(
        self,
        candidate_id,
        sequence,
        status,
        *,
        battery=None,
        metric_values=None,
        issues=None,
    ):
        path = self.root / "records" / f"{candidate_id}.json"
        path.parent.mkdir(exist_ok=True)
        value = {
            "schema_version": 2,
            "pipeline_version": "1.5.0",
            "run_id": "critic_fixture",
            "candidate": {"candidate_id": candidate_id, "sequence": sequence},
            "status": status,
            "metrics": metric_values if metric_values is not None else metrics(),
            "battery": battery,
            "issues": list(issues or []),
            "provenance": [],
            "artifact_inventory": [],
        }
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def _handoff(self, entries, *, project_id="critic_test"):
        categories = {}
        for status, candidate_id, path in entries:
            categories.setdefault(status, []).append({
                "candidate_id": candidate_id,
                "record_path": str(path),
                "record_sha256": file_sha256(path),
                "issues": [],
            })
        path = self.root / "prediction_handoff.json"
        path.write_text(json.dumps({
            "schema_version": 2,
            "pipeline_version": "1.5.0",
            "run_id": "critic_fixture",
            "project_id": project_id,
            "required_targets": list(TARGETS),
            "categories": categories,
            "downstream": {"authoritative_record_field": "record_path"},
        }), encoding="utf-8")
        return path

    @staticmethod
    def _rows(*values):
        return [
            {"candidate_id": candidate_id, "sequence": sequence, "source_route": route}
            for candidate_id, sequence, route in values
        ]

    def test_c0514_semantics_separate_metric_failure_from_missing_evidence(self):
        battery = complete_battery(
            failed=("l2_pass", "l3_pass"),
            unjustified=(
                "L2_ipsae:MDM2", "L2_ipsae:MDMX",
                "L3_dg:MDM2", "L3_dg:MDMX", "L4_nc_term_dist",
            ),
        )
        record = self._record(
            "C0514", "TGTGETLEEFQE", "needs_optimization",
            battery=battery,
            metric_values=metrics(ipsae_mdm2=0.2988, ipsae_mdmx=0.2867),
        )
        handoff = self._handoff([("needs_optimization", "C0514", record)])
        report = review(
            handoff_path=handoff,
            state={"project_id": "critic_test", "thresholds": {}},
            candidate_rows=self._rows(("C0514", "TGTGETLEEFQE", "route_A")),
        )
        codes = {item["code"] for item in report["issues"]}
        self.assertEqual(report["verdict"], "iterate")
        self.assertIn("l2_interface_confidence_low", codes)
        self.assertIn("l3_interface_physics_low", codes)
        self.assertIn("threshold_calibration_pending", codes)
        self.assertNotIn("prediction_evidence_incomplete", codes)
        l2 = next(
            item for item in report["issues"]
            if item["code"] == "l2_interface_confidence_low"
        )
        evidence_text = json.dumps(l2["evidence"])
        self.assertIn("ipsae", evidence_text)
        self.assertNotIn("iptm", evidence_text)
        actions = {item["action"] for item in report["recommendations"]}
        self.assertIn("iterate_interface_design", actions)
        self.assertNotIn("complete_prediction_evidence", actions)
        self.assertIn(
            "reuse_complete_prediction_evidence",
            report["planner_handoff"]["policy_constraints"],
        )

    def test_pending_evidence_is_operational_feedback(self):
        battery = complete_battery()
        battery.update({key: None for key in (f"l{i}_pass" for i in range(1, 8))})
        battery["all_layers_pass"] = False
        battery["missing_evidence"] = ["l4_post_relax_missing"]
        record = self._record(
            "C0001", "ACDEFGHI", "prediction_pending", battery=battery,
            issues=[{"code": "l4_post_relax_missing"}],
        )
        handoff = self._handoff([("prediction_pending", "C0001", record)])
        report = review(
            handoff_path=handoff,
            state={"project_id": "critic_test", "thresholds": {}},
            candidate_rows=self._rows(("C0001", "ACDEFGHI", "route_B")),
        )
        issue = next(
            item for item in report["issues"]
            if item["code"] == "prediction_evidence_incomplete"
        )
        self.assertEqual(issue["category"], "operational")
        self.assertIn("complete_prediction_evidence", {
            item["action"] for item in report["recommendations"]
        })

    def test_threshold_only_pending_does_not_request_more_gpu_evidence(self):
        battery = complete_battery(
            unjustified=("L2_ipsae:MDM2",)
        )
        battery["all_layers_pass"] = False
        battery["l5_pass"] = False
        battery["failed_layers"] = ["l5_pass"]
        battery["missing_thresholds"] = ["L5_hotspot_coverage:MDM2"]
        record = self._record(
            "C0001", "ACDEFGHI", "prediction_pending", battery=battery
        )
        handoff = self._handoff([("prediction_pending", "C0001", record)])
        report = review(
            handoff_path=handoff,
            state={"project_id": "critic_test", "thresholds": {}},
            candidate_rows=self._rows(("C0001", "ACDEFGHI", "benchmark_reference_replay")),
            config=CriticConfig(min_cohort_for_distribution=1),
        )
        codes = {item["code"] for item in report["issues"]}
        actions = {item["action"] for item in report["recommendations"]}
        self.assertIn("threshold_calibration_pending", codes)
        self.assertNotIn("prediction_evidence_incomplete", codes)
        self.assertNotIn("complete_prediction_evidence", actions)

    def test_threshold_pending_keeps_other_complete_metric_failures(self):
        battery = complete_battery(
            failed=("l2_pass", "l5_pass"),
            unjustified=("L2_ipsae:MDM2",),
        )
        battery["missing_thresholds"] = ["L5_hotspot_coverage:MDM2"]
        record = self._record(
            "C0001", "ACDEFGHI", "prediction_pending", battery=battery
        )
        handoff = self._handoff([("prediction_pending", "C0001", record)])
        report = review(
            handoff_path=handoff,
            state={"project_id": "critic_test", "thresholds": {}},
            candidate_rows=self._rows(("C0001", "ACDEFGHI", "route_A")),
            config=CriticConfig(min_cohort_for_distribution=1),
        )
        codes = {item["code"] for item in report["issues"]}
        actions = {item["action"] for item in report["recommendations"]}
        self.assertIn("l2_interface_confidence_low", codes)
        self.assertIn("iterate_interface_design", actions)
        self.assertNotIn("l5_hotspot_coverage_low", codes)
        self.assertNotIn("complete_prediction_evidence", actions)

    def test_missing_l7_reference_is_owned_by_design(self):
        battery = complete_battery()
        battery["l7_pass"] = None
        battery["all_layers_pass"] = False
        battery["missing_evidence"] = ["scrmsd"]
        record = self._record(
            "C1250", "ACDEFGHI", "prediction_pending", battery=battery,
            issues=[{
                "code": "l7_reference_missing",
                "layer": 7,
                "message": "Design reference backbone unavailable",
            }],
        )
        handoff = self._handoff([("prediction_pending", "C1250", record)])
        report = review(
            handoff_path=handoff,
            state={"project_id": "critic_test", "thresholds": {}},
            candidate_rows=self._rows(("C1250", "ACDEFGHI", "route_C")),
            config=CriticConfig(min_cohort_for_distribution=1),
        )
        codes = {item["code"] for item in report["issues"]}
        actions = {item["action"] for item in report["recommendations"]}
        self.assertIn("design_reference_missing", codes)
        self.assertIn("regenerate_design_reference", actions)
        self.assertNotIn("prediction_evidence_incomplete", codes)
        self.assertNotIn("complete_prediction_evidence", actions)

    def test_complete_l6_failure_is_design_feedback(self):
        battery = complete_battery(failed=("l6_pass",))
        record = self._record(
            "C0001", "ACDEFGHI", "needs_optimization", battery=battery
        )
        handoff = self._handoff([("needs_optimization", "C0001", record)])
        report = review(
            handoff_path=handoff,
            state={"project_id": "critic_test", "thresholds": {}},
            candidate_rows=self._rows(("C0001", "ACDEFGHI", "route_A")),
            config=CriticConfig(min_cohort_for_distribution=1),
        )
        recommendation = next(
            item for item in report["recommendations"]
            if item["action"] == "improve_pose_robustness"
        )
        self.assertEqual(recommendation["owner_hint"], "design")
        self.assertNotIn("complete_prediction_evidence", {
            item["action"] for item in report["recommendations"]
        })

    def test_invalid_record_blocks_planning(self):
        record = self._record(
            "C0001", "ACDEFGHI", "invalid", battery=None,
            issues=[{"code": "artifact_sequence_mismatch"}],
        )
        handoff = self._handoff([("invalid", "C0001", record)])
        report = review(
            handoff_path=handoff,
            state={"project_id": "critic_test", "thresholds": {}},
            candidate_rows=self._rows(("C0001", "ACDEFGHI", "route_B")),
        )
        self.assertEqual(report["verdict"], "blocked")
        self.assertIn("invalid_prediction_artifact", {
            item["code"] for item in report["issues"]
        })

    def test_record_hash_mismatch_fails_closed(self):
        record = self._record(
            "C0001", "ACDEFGHI", "finalized", battery=complete_battery()
        )
        handoff = self._handoff([("finalized", "C0001", record)])
        record.write_text(record.read_text() + "\n", encoding="utf-8")
        with self.assertRaisesRegex(CriticContractError, "SHA-256"):
            review(
                handoff_path=handoff,
                state={"project_id": "critic_test", "thresholds": {}},
                candidate_rows=self._rows(("C0001", "ACDEFGHI", "route_A")),
            )

    def test_candidate_index_sequence_drift_blocks_report(self):
        record = self._record(
            "C0001", "ACDEFGHI", "finalized", battery=complete_battery()
        )
        handoff = self._handoff([("finalized", "C0001", record)])
        report = review(
            handoff_path=handoff,
            state={"project_id": "critic_test", "thresholds": {}},
            candidate_rows=self._rows(("C0001", "KLMNPQRS", "route_A")),
            config=CriticConfig(min_cohort_for_distribution=1),
        )
        self.assertEqual(report["verdict"], "blocked")
        self.assertIn("candidate_index_sequence_mismatch", {
            item["code"] for item in report["issues"]
        })

    def test_duplicate_sequences_are_reported(self):
        first = self._record(
            "C0001", "ACDEFGHI", "finalized", battery=complete_battery()
        )
        second = self._record(
            "C0002", "ACDEFGHI", "finalized", battery=complete_battery()
        )
        handoff = self._handoff([
            ("finalized", "C0001", first), ("finalized", "C0002", second),
        ])
        report = review(
            handoff_path=handoff,
            state={"project_id": "critic_test", "thresholds": {}},
            candidate_rows=self._rows(
                ("C0001", "ACDEFGHI", "route_A"),
                ("C0002", "ACDEFGHI", "route_B"),
            ),
        )
        self.assertIn("duplicate_sequences", {
            item["code"] for item in report["issues"]
        })

    def test_three_distinct_finalized_candidates_can_clear(self):
        inputs = (
            ("C0001", "ACDEFGHI", "route_A"),
            ("C0002", "KLMNPQRS", "route_B"),
            ("C0003", "TVWYACDE", "route_C"),
        )
        entries = []
        for candidate_id, sequence, _ in inputs:
            path = self._record(
                candidate_id, sequence, "finalized", battery=complete_battery()
            )
            entries.append(("finalized", candidate_id, path))
        report = review(
            handoff_path=self._handoff(entries),
            state={"project_id": "critic_test", "thresholds": {}},
            candidate_rows=self._rows(*inputs),
        )
        self.assertEqual(report["verdict"], "clear")
        self.assertTrue(report["passed"])

    def test_run_is_idempotent_for_state_history_and_evidence(self):
        project = copy.deepcopy(data_layer.State._project_config)
        state = copy.deepcopy(data_layer.State._default)
        state["project_id"] = project["project_id"]
        state["project_config"] = project
        state["thresholds"] = {}
        state["iteration_history"] = []
        data_layer.State.save(state)
        battery = complete_battery(failed=("l2_pass",))
        record = self._record(
            "C0001", "ACDEFGHI", "needs_optimization", battery=battery
        )
        handoff = self._handoff(
            [("needs_optimization", "C0001", record)],
            project_id=project["project_id"],
        )
        rows = self._rows(("C0001", "ACDEFGHI", "route_A"))
        output = self.root / "critic_report.json"
        first = run(
            handoff_path=handoff,
            output_path=output,
            state=state,
            candidate_rows=rows,
            config=CriticConfig(min_cohort_for_distribution=1),
        )
        second = run(
            handoff_path=handoff,
            output_path=output,
            state=data_layer.State.load(),
            candidate_rows=rows,
            config=CriticConfig(min_cohort_for_distribution=1),
        )
        self.assertEqual(first["report_sha256"], second["report_sha256"])
        saved = data_layer.State.load()
        self.assertEqual(saved["phase"], "critic")
        history = [
            item for item in saved["iteration_history"] if item.get("agent") == "critic"
        ]
        self.assertEqual(len(history), 1)
        events = data_layer.EvidenceLogger.filter(event_type="critic_review")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["report_id"], first["report"]["report_id"])


    def test_review_injected_project_config_overrides_state_mismatch(self):
        record = self._record(
            "C0001", "ACDEFGHI", "finalized", battery=complete_battery()
        )
        handoff = self._handoff(
            [("finalized", "C0001", record)], project_id="planner_test"
        )
        with self.assertRaises(CriticContractError) as captured:
            review(
                handoff_path=handoff,
                state={"project_id": "planner_test", "thresholds": {}},
                candidate_rows=self._rows(("C0001", "ACDEFGHI", "route_A")),
                project_config={"project_id": "keap1", "targets": [{"id": "KEAP1"}]},
            )
        self.assertEqual(captured.exception.code, "critic_project_mismatch")

    def test_review_injected_project_config_can_make_handoff_pass(self):
        inputs = (
            ("C0001", "ACDEFGHI", "route_A"),
            ("C0002", "KLMNPQRS", "route_B"),
            ("C0003", "TVWYACDE", "route_C"),
        )
        entries = []
        for candidate_id, sequence, _ in inputs:
            path = self._record(
                candidate_id, sequence, "finalized", battery=complete_battery()
            )
            entries.append(("finalized", candidate_id, path))
        # State has no project identity; injection supplies keap1, so the
        # keap1 handoff passes the mismatch check.
        report = review(
            handoff_path=self._handoff(entries, project_id="keap1"),
            state={"thresholds": {}},
            candidate_rows=self._rows(*inputs),
            project_config={"project_id": "keap1", "targets": [{"id": "KEAP1"}]},
        )
        self.assertEqual(report["verdict"], "clear")
        self.assertTrue(report["passed"])


if __name__ == "__main__":
    unittest.main()
