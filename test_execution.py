"""Execution Worker registry, process and Orchestrator integration tests."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import data_layer
from agents.orchestrator import initialize, status
from agents.planner import record_approval, run as planner_run
from execution.config import ExecutionConfig
from execution.contracts import (
    V2_RESERVED_ACTIONS,
    ExecutionContractError,
    assert_action_executable,
    validate_task_parameters,
    _validate_research_result,
)
from execution.supervisor import run_process
from execution.autopilot import _research_state_is_current
from execution.handlers import _binding_residue_numbers
from execution.worker import execute_task
from prediction_pipeline.contracts import object_sha256


POLICY_CONSTRAINTS = [
    "do_not_change_thresholds_automatically",
    "do_not_delete_candidates_automatically",
    "do_not_start_gpu_jobs_without_planner_budget_and_execution_approval",
    "reuse_complete_prediction_evidence",
]


class ExecutionTests(unittest.TestCase):
    def test_autopilot_requires_thresholds_bound_to_current_approval(self):
        config = {"review": {"approved_digest": "a" * 64}}
        self.assertTrue(_research_state_is_current(
            {"approved_digest": "a" * 64, "thresholds": {"L1_plddt": {}}},
            config,
        ))
        self.assertFalse(_research_state_is_current(
            {"approved_digest": "b" * 64, "thresholds": {"L1_plddt": {}}},
            config,
        ))
        self.assertFalse(_research_state_is_current(
            {"approved_digest": "a" * 64, "thresholds": {}}, config,
        ))

    def test_reviewed_residue_labels_are_normalized_for_design(self):
        self.assertEqual(
            _binding_residue_numbers(["Trp23", "A:54LEU", 61, "Met62"]),
            [23, 54, 61, 62],
        )
        with self.assertRaisesRegex(ExecutionContractError, "invalid binding-site"):
            _binding_residue_numbers(["unknown"])

    def test_research_receipt_is_not_validated_as_calibration(self):
        task = {"action": "run_research", "task_id": "T001"}
        _validate_research_result({
            "schema_version": 1,
            "execution_worker_version": "1.0.1",
            "action": "run_research",
            "task_id": "T001",
            "project_id": "project",
            "run_status": "complete",
            "stage_status": {"rcsb_search": "complete"},
            "state_sha256": "a" * 64,
            "completed_at": "2026-08-04T00:00:00+00:00",
        }, task)

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="execution-test-"))
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
        data_layer.State.save(self._state())

    def tearDown(self):
        (
            data_layer.DATA_DIR,
            data_layer.EVIDENCE_DIR,
            data_layer.STATE_PATH,
            data_layer.LOG_PATH,
            data_layer.INDEX_PATH,
        ) = self.original_paths

    @staticmethod
    def _state():
        return {
            "project_id": "execution_test",
            "round": 2,
            "phase": "critic",
            "design_budget": {"route_A_mdm2": 20},
            "thresholds": {"L2_ipsae": {"value": None}},
            "project_config": {
                "project_id": "execution_test",
                "modality": "head_to_tail_cyclic_peptide",
                "targets": [{
                    "id": "MDM2",
                    "required": True,
                    "design": {"lengths": [8, 10, 12]},
                }],
                "review": {
                    "status": "approved",
                    "approved_digest": "a" * 64,
                    "content_digest": "a" * 64,
                },
            },
            "iteration_history": [],
        }

    def _report(self, *, calibration=False):
        action = "calibrate_thresholds" if calibration else "iterate_interface_design"
        code = "threshold_calibration_pending" if calibration else "l2_interface_confidence_low"
        issue = {
            "code": code,
            "severity": "medium" if calibration else "high",
            "category": "calibration" if calibration else "scientific_metric",
            "message": code,
            "candidate_ids": ["C0001"],
            "evidence": [{"threshold_keys": ["L2_ipsae:MDM2"]}] if calibration else [],
            "recommended_action": action,
            "owner_hint": "research" if calibration else "design",
            "blocks_finalization": True,
        }
        digest = object_sha256({"action": action, "root": str(self.root)})
        report_id = f"critic_{digest[:12]}"
        report = {
            "schema_version": 1,
            "critic_version": "1.1.1",
            "report_id": report_id,
            "input_digest": digest,
            "source": {
                "prediction_handoff": str(self.root / "prediction.json"),
                "prediction_handoff_sha256": "b" * 64,
                "prediction_run_id": "prediction_execution_test",
                "prediction_pipeline_version": "1.5.1",
                "project_id": "execution_test",
                "required_targets": ["MDM2"],
                "record_count": 1,
            },
            "verdict": "review" if calibration else "iterate",
            "passed": False,
            "summary": "fixture",
            "issue_counts": {},
            "issues": [issue],
            "metrics_snapshot": {},
            "recommendations": [{
                "action": action,
                "owner_hint": issue["owner_hint"],
                "priority": "P2" if calibration else "P1",
                "reason_codes": [code],
                "approval_required": calibration,
            }],
            "planner_handoff": {
                "critic_report_id": report_id,
                "issue_codes": [code],
                "recommended_actions": [action],
                "policy_constraints": POLICY_CONSTRAINTS,
            },
        }
        path = self.root / f"{report_id}.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return path

    def _config(self, *, design_python: Path | None = None):
        repo = Path(__file__).resolve().parent
        missing = self.root / "missing"
        return ExecutionConfig(
            repo_root=repo,
            execution_root=self.root / "execution",
            core_python=Path("/usr/bin/python3"),
            design_python=design_python or missing,
            prediction_python=missing,
            prediction_artifacts_root=self.root / "artifacts",
            prediction_runs_root=self.root / "prediction_runs",
            colabdesign_dir=missing,
            colabdesign_params=missing,
            cuda_data_dir=missing,
            boltz_executable=None,
            boltz_cache=None,
            boltz_checkpoint=None,
            prodigy_executable=None,
            pyrosetta_python=None,
            control_data_path=None,
            project_config_path=None,
            target_structures_root=self.root / "target_structures",
        )

    def test_planner_materializes_a_typed_design_job(self):
        plan = planner_run(critic_report_path=self._report())["plan"]
        task = plan["tasks"][0]
        parameters = validate_task_parameters(task)
        self.assertEqual(task["action"], "iterate_design")
        self.assertEqual(parameters["design_jobs"][0]["target_id"], "MDM2")
        self.assertEqual(parameters["design_jobs"][0]["route"], "A")
        self.assertEqual(parameters["design_jobs"][0]["lengths"], [8, 10, 12])
        self.assertEqual(
            sum(job["proposal_count"] for job in parameters["design_jobs"]),
            task["resource_request"]["proposal_count"],
        )

    def test_reserved_v2_actions_have_no_v1_handler(self):
        for action in V2_RESERVED_ACTIONS:
            task = {
                "action": action,
                "parameters": {},
                "candidate_scope": {"candidate_ids": [], "from_task_id": None},
                "resource_request": {"proposal_count": 0, "candidate_limit": 0},
                "outputs": [],
            }
            with self.assertRaisesRegex(ExecutionContractError, "reserved for v2"):
                assert_action_executable(task)

    def test_process_arguments_are_not_interpreted_by_a_shell(self):
        marker = self.root / "must_not_exist"
        result = run_process(
            ["/bin/echo", f"safe; touch {marker}"],
            cwd=self.root,
            logs_dir=self.root / "logs",
            timeout_seconds=10,
            label="shell_injection_regression",
        )
        self.assertEqual(result["returncode"], 0)
        self.assertFalse(marker.exists())
        self.assertIn("safe; touch", Path(result["stdout"]).read_text())

    def test_process_failure_reports_stdout_when_stderr_is_empty(self):
        with self.assertRaisesRegex(
            ExecutionContractError, "structured failure on stdout"
        ):
            run_process(
                [
                    sys.executable,
                    "-c",
                    "print('structured failure on stdout'); raise SystemExit(2)",
                ],
                cwd=self.root,
                logs_dir=self.root / "stdout_failure_logs",
                timeout_seconds=10,
                label="stdout_failure",
            )
    def test_calibration_handler_completes_without_mutating_thresholds(self):
        plan_result = planner_run(critic_report_path=self._report(calibration=True))
        task_id = plan_result["plan"]["tasks"][0]["task_id"]
        approval = record_approval(
            plan_path=plan_result["plan_path"],
            task_ids=[task_id],
            approver="PI-test",
            justification="review threshold proposal only",
        )
        initialized = initialize(
            plan_path=plan_result["plan_path"],
            approval_paths=[approval["approval_path"]],
        )
        before = data_layer.State.load()["thresholds"]
        receipt = execute_task(
            run_path=initialized["run_path"],
            task_id=task_id,
            worker_id="execution-test",
            config=self._config(),
        )
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(data_layer.State.load()["thresholds"], before)
        proposal = json.loads(Path(receipt["outputs"][0]["path"]).read_text())
        self.assertFalse(proposal["applied_to_state"])
        self.assertEqual(proposal["status"], "pending_controls")

    def test_failed_gpu_handler_closes_claim_and_releases_lease(self):
        plan_result = planner_run(critic_report_path=self._report())
        task_id = plan_result["plan"]["tasks"][0]["task_id"]
        approval = record_approval(
            plan_path=plan_result["plan_path"],
            task_ids=plan_result["plan"]["approval_request"]["required_task_ids"],
            approver="PI-test",
            justification="bounded failure regression",
            max_gpu_job_slots=1,
            max_gpu_minutes=1,
            max_design_proposals=12,
            max_prediction_candidates=12,
        )
        initialized = initialize(
            plan_path=plan_result["plan_path"],
            approval_paths=[approval["approval_path"]],
        )
        with self.assertRaisesRegex(ExecutionContractError, "executable not found"):
            execute_task(
                run_path=initialized["run_path"],
                task_id=task_id,
                worker_id="execution-test",
                config=self._config(),
            )
        snapshot = status(run_path=initialized["run_path"])["run"]
        self.assertEqual(snapshot["tasks"][task_id]["status"], "failed")
        self.assertIsNone(snapshot["resources"]["gpu_lease"])
        self.assertFalse((data_layer.DATA_DIR / "orchestrator" / "gpu_lease.json").exists())


if __name__ == "__main__":
    unittest.main()
