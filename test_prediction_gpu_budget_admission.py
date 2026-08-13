"""Prediction GPU-minute approval admission contract tests; no GPU used."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import data_layer
from agents.orchestrator import OrchestratorContractError, authorize, claim, initialize
from agents.planner import (
    PlannerContractError,
    build_initial_prediction_bootstrap_plan,
    record_approval,
)
from contracts.plan import PlanContractError, validate_approval_gpu_minutes
from prediction_pipeline.contracts import object_sha256
from prediction_pipeline.execution_identity import build_prediction_execution_identity


class ApprovalGpuMinuteContractTests(unittest.TestCase):
    @staticmethod
    def _plan() -> dict:
        return {
            "tasks": [
                {
                    "task_id": "T001",
                    "resource_request": {
                        "class": "gpu",
                        "estimated_gpu_minutes": 11,
                        "estimate_status": "estimated",
                    },
                },
                {
                    "task_id": "T002",
                    "resource_request": {
                        "class": "gpu",
                        "estimated_gpu_minutes": 11,
                        "estimate_status": "estimated",
                    },
                },
                {
                    "task_id": "T003",
                    "resource_request": {
                        "class": "cpu",
                        "estimated_gpu_minutes": None,
                        "estimate_status": "not_applicable",
                    },
                },
            ]
        }

    def test_selected_gpu_estimates_must_be_usable_and_fit_the_ceiling(self):
        plan = self._plan()
        self.assertEqual(
            validate_approval_gpu_minutes(plan, ["T001", "T002", "T003"], 22),
            22.0,
        )

        with self.assertRaises(PlanContractError) as insufficient:
            validate_approval_gpu_minutes(plan, ["T001", "T002"], 2.5)
        self.assertEqual(insufficient.exception.code, "approval_gpu_minutes_insufficient")

        invalid_estimates = (
            ("missing", None, "estimated"),
            ("non-numeric", "11", "estimated"),
            ("boolean", True, "estimated"),
            ("non-positive", 0, "estimated"),
            ("unestimated", 11, "unestimated"),
        )
        for label, estimate, status in invalid_estimates:
            with self.subTest(label=label):
                changed = copy.deepcopy(plan)
                resource = changed["tasks"][0]["resource_request"]
                if label == "missing":
                    resource.pop("estimated_gpu_minutes")
                else:
                    resource["estimated_gpu_minutes"] = estimate
                resource["estimate_status"] = status
                with self.assertRaises(PlanContractError) as invalid:
                    validate_approval_gpu_minutes(changed, ["T001"], 22)
                self.assertEqual(invalid.exception.code, "approval_gpu_estimate_invalid")


class PredictionGpuBudgetAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="gpu-budget-admission-"))
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
        data_layer.State.save({
            "project_id": "project-1",
            "round": 1,
            "phase": "design",
            "design_budget": {},
            "project_config": {
                "project_id": "project-1",
                "targets": [{"id": "MDM2"}, {"id": "MDMX"}],
                "review": {
                    "status": "approved",
                    "approved_digest": "a" * 64,
                    "content_digest": "a" * 64,
                },
            },
            "iteration_history": [],
        })
        source = {
            "project_id": "project-1",
            "approved_content_binding": "approved-content",
            "launcher_run_id": "launcher_0123456789abcdef0123456789abcdef",
            "research_completion_event_id": "research-complete",
            "design_invocation_id": "design_initial_0123456789abcdef0123456789abcdef",
            "design_completion_event_id": "design-complete",
            "design_transaction_id": "tx-design",
            "candidate_ids": ["C0001", "C0002"],
            "execution_identity": build_prediction_execution_identity(),
        }
        self.plan = build_initial_prediction_bootstrap_plan(source=source)
        self.plan_path = self.root / "plan.json"
        self.plan_path.write_text(json.dumps(self.plan), encoding="utf-8")

    def tearDown(self):
        (
            data_layer.DATA_DIR,
            data_layer.EVIDENCE_DIR,
            data_layer.STATE_PATH,
            data_layer.LOG_PATH,
            data_layer.INDEX_PATH,
        ) = self.original_paths

    def _record_approval(self, max_gpu_minutes: float) -> dict:
        return record_approval(
            plan_path=self.plan_path,
            task_ids=["T001"],
            approver="PI-test",
            justification="approved Prediction budget test",
            max_gpu_job_slots=1,
            max_gpu_minutes=max_gpu_minutes,
            max_design_proposals=0,
            max_prediction_candidates=2,
        )

    def _manual_low_budget_approval(self) -> Path:
        covering = self._record_approval(30)
        approval = copy.deepcopy(covering["approval"])
        approval["budget_limits"]["max_gpu_minutes"] = 2.5
        semantic_keys = (
            "schema_version",
            "plan_id",
            "plan_path",
            "plan_sha256",
            "project_id",
            "approved_task_ids",
            "approver",
            "justification",
            "budget_limits",
        )
        semantic = {key: approval[key] for key in semantic_keys}
        approval["approval_id"] = f"approval_{object_sha256(semantic)[:12]}"
        path = self.root / "manual-low-budget-approval.json"
        path.write_text(json.dumps(approval), encoding="utf-8")
        return path

    def test_planner_rejects_below_estimate_and_accepts_30(self):
        with self.assertRaises(PlannerContractError) as insufficient:
            self._record_approval(22)
        self.assertEqual(insufficient.exception.code, "approval_gpu_minutes_insufficient")

        covering = self._record_approval(30)
        self.assertEqual(covering["approval"]["budget_limits"]["max_gpu_minutes"], 30)

    def test_initialize_rejects_low_budget_without_creating_run(self):
        approval_path = self._manual_low_budget_approval()

        with self.assertRaises(OrchestratorContractError) as insufficient:
            initialize(plan_path=self.plan_path, approval_paths=[approval_path])
        self.assertEqual(insufficient.exception.code, "approval_gpu_minutes_insufficient")
        self.assertEqual(list(self.root.rglob("orchestrator_run.json")), [])

    def test_authorize_rejects_low_budget_and_preserves_awaiting_run(self):
        initialized = initialize(plan_path=self.plan_path)
        approval_path = self._manual_low_budget_approval()

        with self.assertRaises(OrchestratorContractError) as insufficient:
            authorize(run_path=initialized["run_path"], approval_path=approval_path)
        self.assertEqual(insufficient.exception.code, "approval_gpu_minutes_insufficient")

        persisted = json.loads(Path(initialized["run_path"]).read_text(encoding="utf-8"))
        self.assertEqual(persisted["status"], "awaiting_approval")
        self.assertEqual(persisted["tasks"]["T001"]["status"], "awaiting_approval")
        self.assertEqual(persisted["approvals"], [])
        with self.assertRaisesRegex(OrchestratorContractError, "awaiting_approval"):
            claim(
                run_path=initialized["run_path"],
                task_id="T001",
                worker="prediction-agent",
            )

    def test_initialize_accepts_covering_budget_and_makes_task_ready(self):
        covering = self._record_approval(30)

        initialized = initialize(
            plan_path=self.plan_path,
            approval_paths=[covering["approval_path"]],
        )
        self.assertEqual(initialized["run"]["status"], "ready")
        self.assertEqual(initialized["run"]["tasks"]["T001"]["status"], "ready")


if __name__ == "__main__":
    unittest.main()
