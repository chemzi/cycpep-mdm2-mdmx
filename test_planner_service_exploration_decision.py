"""Focused E3-C tests for the public Planner service Decision handoff."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

import data_layer
from agents.planner import PlannerContractError, run
from contracts.exploration_decision import ExplorationDecision
from prediction_pipeline.contracts import object_sha256
from test_exploration_decision import (
    PREDICTION_RUN_ID,
    PROJECT_ID,
    TARGETS,
    WORKFLOW_ID,
    build_decision,
    evidence_batch,
)


POLICY_CONSTRAINTS = [
    "do_not_change_thresholds_automatically",
    "do_not_delete_candidates_automatically",
    "do_not_start_gpu_jobs_without_planner_budget_and_execution_approval",
    "reuse_complete_prediction_evidence",
]


class PlannerServiceExplorationDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="planner-e3c-service-")
        self.root = Path(self.temp.name)
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
        self.decision = build_decision(evidence_batch()).to_dict()
        self.state = {
            "project_id": PROJECT_ID,
            "workflow_id": WORKFLOW_ID,
            "round": 1,
            "design_budget": {"route_A": 12},
            "project_config": {
                "project_id": PROJECT_ID,
                "targets": [
                    {"id": target, "design": {"lengths": [8, 10, 12]}}
                    for target in TARGETS
                ],
                "review": {
                    "status": "approved",
                    "approved_digest": self.decision["evidence_support"][
                        "approval_digest"
                    ],
                    "content_digest": self.decision["evidence_support"][
                        "approval_digest"
                    ],
                },
            },
            "iteration_history": [],
        }

    def tearDown(self) -> None:
        (
            data_layer.DATA_DIR,
            data_layer.EVIDENCE_DIR,
            data_layer.STATE_PATH,
            data_layer.LOG_PATH,
            data_layer.INDEX_PATH,
        ) = self.original_paths
        self.temp.cleanup()

    def _critic_report(self) -> Path:
        issue = {
            "code": "l2_interface_confidence_low",
            "severity": "high",
            "category": "scientific_metric",
            "message": "fixture",
            "candidate_ids": ["C800"],
            "evidence": [],
            "recommended_action": "iterate_interface_design",
            "owner_hint": "design",
            "blocks_finalization": True,
            "approval_required": False,
            "priority": "P1",
        }
        input_digest = object_sha256({"fixture": "planner-service-e3c"})
        report_id = f"critic_{input_digest[:12]}"
        report = {
            "schema_version": 1,
            "critic_version": "1.0.0",
            "report_id": report_id,
            "input_digest": input_digest,
            "source": {
                "prediction_handoff": str(self.root / "prediction_handoff.json"),
                "prediction_handoff_sha256": "b" * 64,
                "prediction_run_id": PREDICTION_RUN_ID,
                "prediction_pipeline_version": "1.5.0",
                "project_id": PROJECT_ID,
                "required_targets": list(TARGETS),
                "record_count": len(self.decision["candidate_ids"]),
            },
            "verdict": "iterate",
            "passed": False,
            "summary": "fixture",
            "issue_counts": {"high": 1},
            "issues": [issue],
            "metrics_snapshot": {},
            "recommendations": [{
                "action": "iterate_interface_design",
                "owner_hint": "design",
                "priority": "P1",
                "reason_codes": [issue["code"]],
                "approval_required": False,
            }],
            "planner_handoff": {
                "critic_report_id": report_id,
                "issue_codes": [issue["code"]],
                "recommended_actions": [issue["recommended_action"]],
                "policy_constraints": POLICY_CONSTRAINTS,
            },
        }
        path = self.root / "critic_report.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return path

    def test_required_decision_missing_fails_before_planning(self) -> None:
        with self.assertRaises(PlannerContractError) as captured:
            run(
                critic_report_path="unused-critic-report.json",
                state={},
                exploration_decision_required=True,
            )

        self.assertEqual(captured.exception.code, "exploration_decision_required")

    def test_explicit_decision_reaches_binding_and_length_materialization(self) -> None:
        data_layer.State.save(deepcopy(self.state))

        result = run(
            critic_report_path=self._critic_report(),
            output_path=self.root / "execution_plan.json",
            state=deepcopy(self.state),
            exploration_decision=deepcopy(self.decision),
            exploration_decision_required=True,
        )

        plan = result["plan"]
        canonical = ExplorationDecision.from_dict(self.decision).to_dict()
        self.assertEqual(
            plan["source"]["exploration_decision_id"],
            self.decision["decision_id"],
        )
        self.assertEqual(
            plan["source"]["exploration_decision_sha256"],
            object_sha256(canonical),
        )
        self.assertEqual(
            plan["source"]["exploration_decision_input_digest"],
            self.decision["decision_input_digest"],
        )
        design = next(
            task for task in plan["tasks"] if task["action"] == "iterate_design"
        )
        self.assertEqual(
            [job["lengths"] for job in design["parameters"]["design_jobs"]],
            [[12], [12]],
        )

    def test_omitted_decision_preserves_direct_legacy_contract(self) -> None:
        data_layer.State.save(deepcopy(self.state))

        result = run(
            critic_report_path=self._critic_report(),
            output_path=self.root / "legacy_execution_plan.json",
            state=deepcopy(self.state),
        )

        plan = result["plan"]
        self.assertNotIn("exploration_decision_id", plan["source"])
        self.assertNotIn("exploration_decision_sha256", plan["source"])
        self.assertNotIn("exploration_decision_input_digest", plan["source"])
        design = next(
            task for task in plan["tasks"] if task["action"] == "iterate_design"
        )
        self.assertEqual(
            [job["lengths"] for job in design["parameters"]["design_jobs"]],
            [[8, 10, 12], [8, 10, 12]],
        )


if __name__ == "__main__":
    unittest.main()
