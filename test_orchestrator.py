"""Orchestrator approval, DAG, lease, artifact, and recovery tests; no GPU used."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import data_layer
from agents.orchestrator import (
    OrchestratorContractError,
    authorize,
    claim,
    complete,
    fail,
    initialize,
    recover,
    skip,
    status,
)
from agents.planner import record_approval, run as planner_run
from prediction_pipeline.contracts import file_sha256, object_sha256


POLICY_CONSTRAINTS = [
    "do_not_change_thresholds_automatically",
    "do_not_delete_candidates_automatically",
    "do_not_start_gpu_jobs_without_planner_budget_and_execution_approval",
    "reuse_complete_prediction_evidence",
]


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="orchestrator-test-"))
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
            "project_id": "orchestrator_test",
            "round": 2,
            "phase": "critic",
            "design_budget": {"route_A": 20, "route_B": 10},
            "project_config": {
                "project_id": "orchestrator_test",
                "targets": [{"id": "MDM2"}, {"id": "MDMX"}],
                "review": {
                    "status": "approved",
                    "approved_digest": "a" * 64,
                    "content_digest": "a" * 64,
                },
            },
            "iteration_history": [],
        }

    @staticmethod
    def _issue(
        code,
        action,
        *,
        severity="high",
        priority="P1",
        approval_required=False,
    ):
        return {
            "code": code,
            "severity": severity,
            "category": "scientific_metric",
            "message": code,
            "candidate_ids": ["C0514"],
            "evidence": (
                [{"threshold_keys": ["L2_ipsae:MDM2"]}]
                if action == "calibrate_thresholds" else []
            ),
            "recommended_action": action,
            "owner_hint": "research" if action == "calibrate_thresholds" else "design",
            "blocks_finalization": severity != "info",
            "_priority": priority,
            "_approval_required": approval_required,
        }

    def _critic_report(self, issues, *, verdict="iterate", marker="default"):
        clean_issues = []
        recommendations = []
        action_index = {}
        for source in issues:
            issue = {
                key: value for key, value in source.items() if not key.startswith("_")
            }
            clean_issues.append(issue)
            action = issue["recommended_action"]
            if action not in action_index:
                action_index[action] = len(recommendations)
                recommendations.append({
                    "action": action,
                    "owner_hint": issue["owner_hint"],
                    "priority": source.get("_priority", "P1"),
                    "reason_codes": [],
                    "approval_required": source.get("_approval_required", False),
                })
            recommendations[action_index[action]]["reason_codes"].append(issue["code"])
        digest = object_sha256({"marker": marker, "issues": clean_issues, "verdict": verdict})
        report_id = f"critic_{digest[:12]}"
        report = {
            "schema_version": 1,
            "critic_version": "1.0.0",
            "report_id": report_id,
            "input_digest": digest,
            "source": {
                "prediction_handoff": str(self.root / f"prediction_{marker}.json"),
                "prediction_handoff_sha256": "b" * 64,
                "prediction_run_id": f"prediction_{marker}",
                "prediction_pipeline_version": "1.5.0",
                "project_id": "orchestrator_test",
                "required_targets": ["MDM2", "MDMX"],
                "record_count": 1,
            },
            "verdict": verdict,
            "passed": verdict == "clear",
            "summary": marker,
            "issue_counts": {},
            "issues": clean_issues,
            "metrics_snapshot": {},
            "recommendations": recommendations,
            "planner_handoff": {
                "critic_report_id": report_id,
                "issue_codes": [item["code"] for item in clean_issues],
                "recommended_actions": [item["action"] for item in recommendations],
                "policy_constraints": POLICY_CONSTRAINTS,
            },
        }
        path = self.root / f"{report_id}.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return path

    def _required_plan(self, marker="default"):
        report = self._critic_report([
            self._issue("l2_interface_confidence_low", "iterate_interface_design"),
            self._issue(
                "threshold_calibration_pending",
                "calibrate_thresholds",
                severity="medium",
                priority="P2",
                approval_required=True,
            ),
        ], marker=marker)
        return planner_run(critic_report_path=report)

    def _approval(self, plan_result, *, max_minutes=30.0):
        task_ids = plan_result["plan"]["approval_request"]["required_task_ids"]
        return record_approval(
            plan_path=plan_result["plan_path"],
            task_ids=task_ids,
            approver="PI-test",
            justification="approved isolated orchestrator test",
            max_gpu_job_slots=1,
            max_gpu_minutes=max_minutes,
            max_design_proposals=12,
            max_prediction_candidates=12,
        )

    def _output(self, name, content="ok"):
        path = self.root / "outputs" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _json_output(self, name, value):
        return self._output(name, json.dumps(value))

    def _design_output(self, name="design.json"):
        stem = Path(name).stem
        before = self._json_output(f"{stem}_candidate_index_before.json", [])
        after = self._json_output(f"{stem}_candidate_index_after.json", [])
        digest = object_sha256([])
        return self._json_output(name, {
            "schema_version": 1,
            "execution_worker_version": "1.0.0",
            "action": "iterate_design",
            "task_id": "T001",
            "project_id": "orchestrator_test",
            "project_config_digest": "a" * 64,
            "jobs": [],
            "candidate_index_before_sha256": digest,
            "candidate_index_after_sha256": digest,
            "candidate_index_before_snapshot": {
                "path": str(before),
                "sha256": file_sha256(before),
            },
            "candidate_index_after_snapshot": {
                "path": str(after),
                "sha256": file_sha256(after),
            },
            "new_candidate_ids": [],
            "candidates": [],
            "existing_rows_unchanged": True,
            "completed_at": "2026-08-03T00:00:00+00:00",
        })

    def _prediction_output(self, name="prediction.json"):
        return self._json_output(name, {
            "schema_version": 1,
            "pipeline_version": "1.5.1",
            "run_id": "prediction_orchestrator_test",
            "project_id": "orchestrator_test",
            "required_targets": ["MDM2", "MDMX"],
            "categories": {},
            "downstream": {},
        })

    def _critic_output(self, name="critic.json", prediction_path=None):
        digest = "c" * 64
        prediction_path = prediction_path or self.root / "outputs" / "prediction.json"
        return self._json_output(name, {
            "schema_version": 1,
            "critic_version": "1.1.1",
            "report_id": f"critic_{digest[:12]}",
            "input_digest": digest,
            "source": {
                "prediction_handoff": str(prediction_path),
                "prediction_handoff_sha256": file_sha256(prediction_path),
            },
            "verdict": "iterate",
            "passed": False,
            "issues": [],
            "recommendations": [],
            "planner_handoff": {},
        })

    def _calibration_output(self, name="thresholds.json"):
        return self._json_output(name, {
            "schema_version": 1,
            "execution_worker_version": "1.0.0",
            "action": "propose_threshold_calibration",
            "task_id": "T004",
            "project_id": "orchestrator_test",
            "status": "pending_controls",
            "requested_threshold_keys": ["L2_ipsae:MDM2"],
            "current_thresholds": {},
            "control_data": {"available": False, "path": None, "sha256": None},
            "control_requirements": {},
            "applied_to_state": False,
            "created_at": "2026-08-03T00:00:00+00:00",
        })

    def test_init_without_approval_waits_and_is_idempotent(self):
        plan_result = self._required_plan()
        first = initialize(plan_path=plan_result["plan_path"])
        second = initialize(plan_path=plan_result["plan_path"])
        self.assertEqual(first["run"]["run_id"], second["run"]["run_id"])
        self.assertEqual(first["run_sha256"], second["run_sha256"])
        self.assertEqual(first["run"]["status"], "awaiting_approval")
        self.assertEqual(first["run"]["tasks"]["T001"]["status"], "awaiting_approval")
        with self.assertRaisesRegex(OrchestratorContractError, "awaiting_approval"):
            claim(
                run_path=first["run_path"], task_id="T001", worker="design-agent"
            )
        events = data_layer.EvidenceLogger.filter(
            event_type="orchestrator_run_initialized"
        )
        self.assertEqual(len(events), 1)

    def test_authorize_unlocks_entry_tasks_and_is_idempotent(self):
        plan_result = self._required_plan()
        approval = self._approval(plan_result)
        initialized = initialize(plan_path=plan_result["plan_path"])
        first = authorize(
            run_path=initialized["run_path"], approval_path=approval["approval_path"]
        )
        second = authorize(
            run_path=initialized["run_path"], approval_path=approval["approval_path"]
        )
        self.assertTrue(first["approval_added"])
        self.assertFalse(second["approval_added"])
        self.assertEqual(first["run"]["status"], "ready")
        self.assertEqual(first["run"]["tasks"]["T001"]["status"], "ready")
        self.assertEqual(first["run"]["tasks"]["T002"]["status"], "pending_dependency")
        self.assertEqual(first["run"]["tasks"]["T004"]["status"], "ready")
        events = data_layer.EvidenceLogger.filter(
            event_type="orchestrator_approval_loaded"
        )
        self.assertEqual(len(events), 1)

    def test_full_dag_completion_advances_round_after_required_tasks(self):
        plan_result = self._required_plan()
        approval = self._approval(plan_result, max_minutes=30)
        initialized = initialize(
            plan_path=plan_result["plan_path"],
            approval_paths=[approval["approval_path"]],
        )
        run_path = initialized["run_path"]

        t1 = claim(run_path=run_path, task_id="T001", worker="design-agent")
        self.assertEqual(t1["run"]["status"], "running")
        complete(
            run_path=run_path,
            task_id="T001",
            claim_token=t1["claim_token"],
            output_paths=["design_result=" + str(self._design_output())],
            gpu_minutes=10,
        )
        self.assertEqual(status(run_path=run_path)["run"]["tasks"]["T002"]["status"], "ready")

        t2 = claim(run_path=run_path, task_id="T002", worker="prediction-agent")
        complete(
            run_path=run_path,
            task_id="T002",
            claim_token=t2["claim_token"],
            output_paths=["prediction_handoff=" + str(self._prediction_output())],
            gpu_minutes=8,
        )
        t3 = claim(run_path=run_path, task_id="T003", worker="critic-agent")
        complete(
            run_path=run_path,
            task_id="T003",
            claim_token=t3["claim_token"],
            output_paths=["critic_report=" + str(self._critic_output())],
        )
        t4 = claim(run_path=run_path, task_id="T004", worker="research-agent")
        final = complete(
            run_path=run_path,
            task_id="T004",
            claim_token=t4["claim_token"],
            output_paths=["calibration_proposal=" + str(self._calibration_output())],
        )
        self.assertEqual(final["run"]["status"], "completed")
        self.assertEqual(final["run"]["resources"]["gpu_minutes_consumed"], 18.0)
        self.assertFalse((data_layer.DATA_DIR / "orchestrator" / "gpu_lease.json").exists())
        state = data_layer.State.load()
        self.assertEqual(state["round"], 3)
        self.assertEqual(state["phase"], "critic")
        completion_history = [
            item for item in state["iteration_history"]
            if item.get("agent") == "orchestrator"
            and (item.get("summary") or {}).get("history_status") == "completed"
        ]
        self.assertEqual(len(completion_history), 1)

    def test_dependency_output_hash_drift_blocks_downstream_claim(self):
        plan_result = self._required_plan()
        approval = self._approval(plan_result)
        initialized = initialize(
            plan_path=plan_result["plan_path"],
            approval_paths=[approval["approval_path"]],
        )
        output = self._design_output("mutable_design.json")
        t1 = claim(
            run_path=initialized["run_path"], task_id="T001", worker="design-agent"
        )
        complete(
            run_path=initialized["run_path"],
            task_id="T001",
            claim_token=t1["claim_token"],
            output_paths=["design_result=" + str(output)],
            gpu_minutes=1,
        )
        output.write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(OrchestratorContractError, "changed after completion"):
            claim(
                run_path=initialized["run_path"],
                task_id="T002",
                worker="prediction-agent",
            )

    def test_prediction_cannot_report_candidates_absent_from_design_output(self):
        plan_result = self._required_plan()
        approval = self._approval(plan_result)
        initialized = initialize(
            plan_path=plan_result["plan_path"],
            approval_paths=[approval["approval_path"]],
        )
        run_path = initialized["run_path"]
        t1 = claim(run_path=run_path, task_id="T001", worker="design-agent")
        complete(
            run_path=run_path,
            task_id="T001",
            claim_token=t1["claim_token"],
            output_paths=["design_result=" + str(self._design_output())],
            gpu_minutes=1,
        )
        t2 = claim(run_path=run_path, task_id="T002", worker="prediction-agent")
        output = self._prediction_output("wrong_scope.json")
        value = json.loads(output.read_text(encoding="utf-8"))
        value["categories"] = {
            "needs_optimization": [{"candidate_id": "C9999"}]
        }
        output.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(OrchestratorContractError, "differ from task scope"):
            complete(
                run_path=run_path,
                task_id="T002",
                claim_token=t2["claim_token"],
                output_paths=["prediction_handoff=" + str(output)],
                gpu_minutes=1,
            )

    def test_critic_must_bind_the_actual_upstream_prediction_hash(self):
        plan_result = self._required_plan()
        approval = self._approval(plan_result)
        initialized = initialize(
            plan_path=plan_result["plan_path"],
            approval_paths=[approval["approval_path"]],
        )
        run_path = initialized["run_path"]
        t1 = claim(run_path=run_path, task_id="T001", worker="design-agent")
        complete(
            run_path=run_path,
            task_id="T001",
            claim_token=t1["claim_token"],
            output_paths=["design_result=" + str(self._design_output())],
            gpu_minutes=1,
        )
        t2 = claim(run_path=run_path, task_id="T002", worker="prediction-agent")
        prediction = self._prediction_output()
        complete(
            run_path=run_path,
            task_id="T002",
            claim_token=t2["claim_token"],
            output_paths=["prediction_handoff=" + str(prediction)],
            gpu_minutes=1,
        )
        t3 = claim(run_path=run_path, task_id="T003", worker="critic-agent")
        critic = self._critic_output(prediction_path=prediction)
        value = json.loads(critic.read_text(encoding="utf-8"))
        value["source"]["prediction_handoff_sha256"] = "0" * 64
        critic.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(OrchestratorContractError, "differs from upstream"):
            complete(
                run_path=run_path,
                task_id="T003",
                claim_token=t3["claim_token"],
                output_paths=["critic_report=" + str(critic)],
            )

    def test_gpu_minutes_ceiling_is_enforced_and_recovery_releases_lease(self):
        plan_result = self._required_plan()
        approval = self._approval(plan_result, max_minutes=5)
        initialized = initialize(
            plan_path=plan_result["plan_path"],
            approval_paths=[approval["approval_path"]],
        )
        claimed = claim(
            run_path=initialized["run_path"], task_id="T001", worker="design-agent"
        )
        with self.assertRaisesRegex(OrchestratorContractError, "GPU ceiling"):
            complete(
                run_path=initialized["run_path"],
                task_id="T001",
                claim_token=claimed["claim_token"],
                output_paths=["design_result=" + str(self._design_output("too_expensive.json"))],
                gpu_minutes=6,
            )
        with self.assertRaisesRegex(OrchestratorContractError, "confirmation"):
            recover(
                run_path=initialized["run_path"],
                task_id="T001",
                claim_token=claimed["claim_token"],
                operator="ops",
                reason="worker stopped",
                process_stopped_confirmed=False,
                gpu_minutes=0,
            )
        recovered = recover(
            run_path=initialized["run_path"],
            task_id="T001",
            claim_token=claimed["claim_token"],
            operator="ops",
            reason="verified process absent",
            process_stopped_confirmed=True,
            gpu_minutes=0,
        )
        self.assertEqual(recovered["run"]["tasks"]["T001"]["status"], "failed")
        self.assertFalse((data_layer.DATA_DIR / "orchestrator" / "gpu_lease.json").exists())

    def test_failure_is_terminal_and_blocks_dependency_without_retry(self):
        plan_result = self._required_plan()
        approval = self._approval(plan_result)
        initialized = initialize(
            plan_path=plan_result["plan_path"],
            approval_paths=[approval["approval_path"]],
        )
        claimed = claim(
            run_path=initialized["run_path"], task_id="T001", worker="design-agent"
        )
        result = fail(
            run_path=initialized["run_path"],
            task_id="T001",
            claim_token=claimed["claim_token"],
            reason="model process failed",
            retryable=True,
            gpu_minutes=2,
        )
        self.assertEqual(result["run"]["status"], "failed")
        self.assertEqual(result["run"]["tasks"]["T002"]["status"], "blocked_dependency")
        self.assertFalse(
            result["run"]["tasks"]["T001"]["last_error"]["automatic_retry_scheduled"]
        )
        with self.assertRaisesRegex(OrchestratorContractError, "status is failed"):
            claim(
                run_path=initialized["run_path"], task_id="T001", worker="design-agent"
            )

    def test_blocked_plan_projects_blocked_run_status(self):
        report = self._critic_report([
            self._issue(
                "candidate_index_sequence_mismatch",
                "repair_candidate_index",
                severity="blocker",
                priority="P0",
            )
        ], verdict="blocked", marker="blocked-run")
        plan_result = planner_run(critic_report_path=report)
        initialized = initialize(plan_path=plan_result["plan_path"])
        self.assertEqual(initialized["run"]["status"], "blocked")

    def test_active_plan_conflict_and_global_gpu_lease_artifact(self):
        first_plan = self._required_plan(marker="first")
        first_approval = self._approval(first_plan)
        first_run = initialize(
            plan_path=first_plan["plan_path"],
            approval_paths=[first_approval["approval_path"]],
        )
        second_plan = self._required_plan(marker="second")
        second_approval = self._approval(second_plan)
        first_claim = claim(
            run_path=first_run["run_path"], task_id="T001", worker="worker-1"
        )
        lease_path = data_layer.DATA_DIR / "orchestrator" / "gpu_lease.json"
        self.assertTrue(lease_path.is_file())
        lease = json.loads(lease_path.read_text())
        self.assertEqual(lease["claim_token"], first_claim["claim_token"])
        with self.assertRaisesRegex(OrchestratorContractError, "must finish"):
            initialize(
                plan_path=second_plan["plan_path"],
                approval_paths=[second_approval["approval_path"]],
            )
        recover(
            run_path=first_run["run_path"],
            task_id="T001",
            claim_token=first_claim["claim_token"],
            operator="ops",
            reason="test cleanup",
            process_stopped_confirmed=True,
            gpu_minutes=0,
        )

    def test_tampered_approval_is_rejected(self):
        plan_result = self._required_plan()
        approval = self._approval(plan_result)
        path = Path(approval["approval_path"])
        value = json.loads(path.read_text())
        value["justification"] = "tampered"
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(OrchestratorContractError, "not bound"):
            initialize(
                plan_path=plan_result["plan_path"], approval_paths=[path]
            )

    def test_optional_task_can_be_skipped_but_required_task_cannot(self):
        report = self._critic_report([
            self._issue(
                "cohort_too_small",
                "generate_review_cohort",
                severity="info",
                priority="P2",
            )
        ], verdict="clear", marker="optional")
        plan_result = planner_run(critic_report_path=report)
        initialized = initialize(plan_path=plan_result["plan_path"])
        result = skip(
            run_path=initialized["run_path"],
            task_id="T001",
            reason="current clear cohort is sufficient for reporting",
        )
        self.assertEqual(result["run"]["tasks"]["T001"]["status"], "skipped")
        reporter = next(
            task["task_id"] for task in plan_result["plan"]["tasks"]
            if task["agent"] == "reporter"
        )
        with self.assertRaisesRegex(OrchestratorContractError, "not optional"):
            skip(
                run_path=initialized["run_path"],
                task_id=reporter,
                reason="should fail",
            )


if __name__ == "__main__":
    unittest.main()
