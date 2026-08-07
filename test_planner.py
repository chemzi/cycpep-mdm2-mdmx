"""Planner contract, safety, approval, and idempotency tests; no GPU required."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import data_layer
from agents.planner import (
    PlannerContractError,
    build_plan,
    plan,
    record_approval,
    run,
)
from prediction_pipeline.contracts import object_sha256
from prediction_pipeline.protocol import (
    PREDICTOR_PROTOCOL,
    ProtocolError,
    protocol_binding,
    validate_execution_compatibility,
)
from execution.contracts import validate_task_parameters


POLICY_CONSTRAINTS = [
    "do_not_change_thresholds_automatically",
    "do_not_delete_candidates_automatically",
    "do_not_start_gpu_jobs_without_planner_budget_and_execution_approval",
    "reuse_complete_prediction_evidence",
]


class _PlannerFixtures:

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="planner-test-"))
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

    @staticmethod
    def _state(*, budget=True):
        return {
            "project_id": "planner_test",
            "round": 2,
            "design_budget": {"route_A": 20, "route_B": 10} if budget else {},
            "project_config": {
                "project_id": "planner_test",
                "targets": [{"id": "MDM2"}, {"id": "MDMX"}],
                "review": {
                    "status": "approved",
                    "approved_digest": "a" * 64,
                    "content_digest": "a" * 64,
                },
            },
            "iteration_history": [],
        }

    def _report(self, issues, *, verdict="iterate", passed=None):
        passed = verdict == "clear" if passed is None else passed
        recommendations = []
        actions = []
        for issue in issues:
            action = issue["recommended_action"]
            if action in actions:
                next(item for item in recommendations if item["action"] == action)[
                    "reason_codes"
                ].append(issue["code"])
                continue
            actions.append(action)
            recommendations.append({
                "action": action,
                "owner_hint": issue.get("owner_hint", "design"),
                "priority": issue.get("priority", "P1"),
                "reason_codes": [issue["code"]],
                "approval_required": issue.get("approval_required", False),
            })
        digest = object_sha256({
            "fixture": len(list(self.root.glob("critic_*.json"))),
            "issues": issues,
            "verdict": verdict,
        })
        report_id = f"critic_{digest[:12]}"
        report = {
            "schema_version": 1,
            "critic_version": "1.0.0",
            "report_id": report_id,
            "input_digest": digest,
            "source": {
                "prediction_handoff": str(self.root / "prediction_handoff.json"),
                "prediction_handoff_sha256": "b" * 64,
                "prediction_run_id": "prediction_fixture",
                "prediction_pipeline_version": "1.5.0",
                "project_id": "planner_test",
                "required_targets": ["MDM2", "MDMX"],
                "record_count": 1,
            },
            "verdict": verdict,
            "passed": passed,
            "summary": "fixture",
            "issue_counts": {},
            "issues": issues,
            "metrics_snapshot": {},
            "recommendations": recommendations,
            "planner_handoff": {
                "critic_report_id": report_id,
                "issue_codes": [issue["code"] for issue in issues],
                "recommended_actions": actions,
                "policy_constraints": POLICY_CONSTRAINTS,
            },
        }
        path = self.root / f"{report_id}.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return path

    @staticmethod
    def _issue(
        code,
        action,
        *,
        severity="high",
        candidate_ids=None,
        approval_required=False,
        priority="P1",
        evidence=None,
    ):
        return {
            "code": code,
            "severity": severity,
            "category": "scientific_metric",
            "message": code,
            "candidate_ids": list(candidate_ids or ["C0514"]),
            "evidence": list(evidence or []),
            "recommended_action": action,
            "owner_hint": "research" if action == "calibrate_thresholds" else "design",
            "blocks_finalization": severity != "info",
            "approval_required": approval_required,
            "priority": priority,
        }


class PlannerTests(_PlannerFixtures, unittest.TestCase):
    def test_c0514_plan_groups_design_then_prediction_then_critic(self):
        report_path = self._report([
            self._issue("l2_interface_confidence_low", "iterate_interface_design"),
            self._issue("l3_interface_physics_low", "iterate_interface_physics"),
            self._issue(
                "threshold_calibration_pending",
                "calibrate_thresholds",
                severity="medium",
                approval_required=True,
                priority="P2",
                evidence=[{"threshold_keys": ["L2_ipsae:MDM2", "L2_ipsae:MDMX"]}],
            ),
            self._issue(
                "cohort_too_small",
                "generate_review_cohort",
                severity="info",
                priority="P2",
            ),
        ])
        result = build_plan(critic_report_path=report_path, state=self._state())
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertEqual(result["cycle"], {
            "source_round": 2,
            "target_round": 3,
            "round_advancement_deferred_to_orchestrator": True,
        })
        actions = [task["action"] for task in result["tasks"]]
        self.assertEqual(actions[:3], [
            "iterate_design",
            "evaluate_new_design_candidates",
            "review_prediction_handoff",
        ])
        design = result["tasks"][0]
        self.assertEqual(design["resource_request"]["proposal_count"], 12)
        self.assertEqual(design["candidate_scope"]["candidate_ids"], ["C0514"])
        self.assertEqual(set(design["parameters"]["strategy_directives"]), {
            "iterate_interface_design",
            "iterate_interface_physics",
            "generate_review_cohort",
        })
        self.assertEqual(result["tasks"][1]["depends_on"], [design["task_id"]])
        research = next(
            task for task in result["tasks"]
            if task["action"] == "propose_threshold_calibration"
        )
        self.assertEqual(research["approval"]["types"], ["scientific_policy"])
        self.assertIn(
            "do_not_apply_thresholds_without_human_approval",
            research["constraints"],
        )
        self.assertEqual(
            research["parameters"]["threshold_keys"],
            ["L2_ipsae:MDM2", "L2_ipsae:MDMX"],
        )

    def test_missing_design_budget_blocks_gpu_iteration(self):
        report_path = self._report([
            self._issue("l2_interface_confidence_low", "iterate_interface_design")
        ])
        result = build_plan(
            critic_report_path=report_path,
            state=self._state(budget=False),
        )
        self.assertEqual(result["status"], "blocked")
        design = result["tasks"][0]
        self.assertEqual(design["resource_request"]["proposal_count"], 0)
        self.assertEqual(
            design["execution_gate"]["block_reasons"],
            ["design_budget_missing_or_exhausted"],
        )

    def test_l7_reference_and_complete_l6_failures_enter_design_iteration(self):
        report_path = self._report([
            self._issue(
                "design_reference_missing",
                "regenerate_design_reference",
                candidate_ids=["C1250"],
                priority="P0",
            ),
            self._issue(
                "l6_ensemble_convergence_low",
                "improve_pose_robustness",
                candidate_ids=["C1255", "C1256"],
            ),
        ])
        result = build_plan(critic_report_path=report_path, state=self._state())
        actions = [task["action"] for task in result["tasks"]]
        self.assertEqual(actions, [
            "iterate_design",
            "evaluate_new_design_candidates",
            "review_prediction_handoff",
        ])
        design = result["tasks"][0]
        self.assertEqual(
            set(design["parameters"]["strategy_directives"]),
            {"regenerate_design_reference", "improve_pose_robustness"},
        )
        self.assertEqual(
            design["candidate_scope"]["candidate_ids"],
            ["C1250", "C1255", "C1256"],
        )
        self.assertNotIn("complete_prediction_evidence", actions)
        self.assertNotIn("diagnose_and_improve_pose_robustness", actions)

    def test_missing_prediction_evidence_maps_to_executable_prediction_handler(self):
        report_path = self._report([
            self._issue(
                "prediction_evidence_missing",
                "complete_prediction_evidence",
                candidate_ids=["C1255", "C1256"],
                priority="P0",
            )
        ])
        result = build_plan(critic_report_path=report_path, state=self._state())
        prediction, critic = result["tasks"]
        self.assertEqual(prediction["action"], "evaluate_new_design_candidates")
        self.assertEqual(
            prediction["candidate_scope"]["candidate_ids"], ["C1255", "C1256"]
        )
        self.assertEqual(prediction["parameters"], {
            "reuse_complete_evidence": True,
            "evidence_mode": "reuse_or_generate_full",
            "predictor_protocol": dict(PREDICTOR_PROTOCOL),
        })
        self.assertEqual(critic["action"], "review_prediction_handoff")
        self.assertEqual(critic["depends_on"], [prediction["task_id"]])

    def test_clear_report_blocks_unimplemented_report_action(self):
        report_path = self._report([], verdict="clear")
        result = build_plan(critic_report_path=report_path, state=self._state())
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(len(result["tasks"]), 1)
        self.assertEqual(result["tasks"][0]["action"], "prepare_final_candidate_report")
        self.assertEqual(result["tasks"][0]["execution_gate"]["status"], "blocked")
        self.assertIn("blocked_unimplemented", result["tasks"][0]["execution_gate"]["block_reasons"])
        self.assertEqual(result["budget_request"]["requested_gpu_job_slots"], 0)
        self.assertFalse(result["execution"]["automatic_dispatch_allowed"])

    def test_unknown_critic_action_fails_closed(self):
        report_path = self._report([
            self._issue("new_issue", "launch_unreviewed_tool")
        ])
        with self.assertRaisesRegex(PlannerContractError, "no safe mapping"):
            build_plan(critic_report_path=report_path, state=self._state())

    def test_tampered_report_id_and_project_mismatch_fail(self):
        report_path = self._report([
            self._issue("l2_interface_confidence_low", "iterate_interface_design")
        ])
        report = json.loads(report_path.read_text())
        report["report_id"] = "critic_000000000000"
        report["planner_handoff"]["critic_report_id"] = report["report_id"]
        report_path.write_text(json.dumps(report))
        with self.assertRaisesRegex(PlannerContractError, "not bound"):
            build_plan(critic_report_path=report_path, state=self._state())

        report_path = self._report([
            self._issue("l2_interface_confidence_low", "iterate_interface_design")
        ])
        state = self._state()
        state["project_id"] = "other_project"
        with self.assertRaisesRegex(PlannerContractError, "project IDs differ"):
            build_plan(critic_report_path=report_path, state=state)

    def test_blocked_report_allows_recovery_only(self):
        report_path = self._report([
            self._issue(
                "candidate_index_sequence_mismatch",
                "repair_candidate_index",
                severity="blocker",
                priority="P0",
            )
        ], verdict="blocked")
        result = build_plan(critic_report_path=report_path, state=self._state())
        self.assertEqual(result["status"], "recovery_only")
        repair = result["tasks"][0]
        self.assertEqual(repair["disposition"], "recovery")
        self.assertEqual(repair["approval"]["types"], ["data_integrity"])
        self.assertEqual(result["tasks"][1]["action"], "review_prediction_handoff")

    def test_blocker_freezes_unrelated_scientific_branch(self):
        report_path = self._report([
            self._issue(
                "candidate_index_sequence_mismatch",
                "repair_candidate_index",
                severity="blocker",
                priority="P0",
            ),
            self._issue(
                "l2_interface_confidence_low",
                "iterate_interface_design",
                severity="high",
                priority="P1",
            ),
        ], verdict="blocked")
        result = build_plan(critic_report_path=report_path, state=self._state())
        design = next(task for task in result["tasks"] if task["action"] == "iterate_design")
        repair = next(
            task for task in result["tasks"] if task["action"] == "repair_candidate_index"
        )
        self.assertEqual(design["execution_gate"]["status"], "blocked")
        self.assertIn(
            "critic_blocker_requires_recovery",
            design["execution_gate"]["block_reasons"],
        )
        self.assertEqual(repair["execution_gate"]["status"], "blocked")
        self.assertIn("blocked_unimplemented", repair["execution_gate"]["block_reasons"])
        self.assertNotIn(design["task_id"], result["approval_request"]["required_task_ids"])
        self.assertEqual(result["execution"]["entry_task_ids"], [])

    def test_run_is_idempotent_for_state_history_and_evidence(self):
        report_path = self._report([
            self._issue("l2_interface_confidence_low", "iterate_interface_design")
        ])
        state = self._state()
        data_layer.State.save(state)
        first = run(critic_report_path=report_path)
        second = run(critic_report_path=report_path)
        self.assertEqual(first["plan"], second["plan"])
        self.assertEqual(first["plan_sha256"], second["plan_sha256"])
        final_state = data_layer.State.load()
        histories = [
            entry for entry in final_state["iteration_history"]
            if entry.get("agent") == "planner"
        ]
        events = data_layer.EvidenceLogger.filter(event_type="planner_plan")
        self.assertEqual(len(histories), 1)
        self.assertEqual(len(events), 1)

    def test_approval_is_digest_bound_budget_limited_and_idempotent(self):
        report_path = self._report([
            self._issue("l2_interface_confidence_low", "iterate_interface_design")
        ])
        data_layer.State.save(self._state())
        result = run(critic_report_path=report_path)
        plan_path = result["plan_path"]
        gpu_tasks = [
            task for task in result["plan"]["tasks"]
            if task["resource_request"]["class"] == "gpu"
        ]
        task_ids = [task["task_id"] for task in gpu_tasks]
        with self.assertRaisesRegex(PlannerContractError, "max_gpu_job_slots"):
            record_approval(
                plan_path=plan_path,
                task_ids=task_ids,
                approver="PI",
                justification="approved test iteration",
                max_gpu_job_slots=0,
                max_gpu_minutes=120,
                max_design_proposals=12,
                max_prediction_candidates=12,
            )
        with self.assertRaisesRegex(PlannerContractError, "max_gpu_minutes"):
            record_approval(
                plan_path=plan_path,
                task_ids=task_ids,
                approver="PI",
                justification="missing time ceiling",
                max_gpu_job_slots=1,
                max_design_proposals=12,
                max_prediction_candidates=12,
            )
        first = record_approval(
            plan_path=plan_path,
            task_ids=task_ids,
            approver="PI",
            justification="approved test iteration",
            max_gpu_job_slots=1,
            max_gpu_minutes=120,
            max_design_proposals=12,
            max_prediction_candidates=12,
        )
        second = record_approval(
            plan_path=plan_path,
            task_ids=task_ids,
            approver="PI",
            justification="approved test iteration",
            max_gpu_job_slots=1,
            max_gpu_minutes=120,
            max_design_proposals=12,
            max_prediction_candidates=12,
        )
        self.assertEqual(first["approval_sha256"], second["approval_sha256"])
        self.assertEqual(first["approval"]["plan_sha256"], result["plan_sha256"])
        events = data_layer.EvidenceLogger.filter(
            event_type="planner_approval_recorded"
        )
        self.assertEqual(len(events), 1)

    def test_approval_rejects_tampered_gpu_policy(self):
        report_path = self._report([
            self._issue("l2_interface_confidence_low", "iterate_interface_design")
        ])
        data_layer.State.save(self._state())
        result = run(critic_report_path=report_path)
        plan_path = Path(result["plan_path"])
        value = json.loads(plan_path.read_text())
        gpu_task = next(
            task for task in value["tasks"]
            if task["resource_request"]["class"] == "gpu"
        )
        gpu_task["approval"] = {
            "required": False,
            "types": [],
            "status": "not_required",
        }
        plan_path.write_text(json.dumps(value))
        with self.assertRaisesRegex(PlannerContractError, "lacks execution-budget"):
            record_approval(
                plan_path=plan_path,
                task_ids=[gpu_task["task_id"]],
                approver="PI",
                justification="should fail",
                max_gpu_job_slots=1,
                max_gpu_minutes=120,
                max_design_proposals=12,
                max_prediction_candidates=12,
            )

    def test_bootstrap_planner_respects_phase_gates(self):
        state = self._state()
        state["project_config"]["review"]["content_digest"] = "b" * 64
        self.assertEqual(
            plan(state=state, candidate_rows=[])[0]["action"],
            "review_and_approve_project_config",
        )

        state = self._state()
        self.assertEqual(plan(state=state, candidate_rows=[])[0]["action"], "run")
        state["pocket_differences"] = {"ready": True}
        self.assertEqual(
            plan(state=state, candidate_rows=[])[0]["action"],
            "generate_candidates",
        )
        rows = [{"candidate_id": "C0001", "sequence": "ACDEFGHI"}]
        self.assertEqual(plan(state=state, candidate_rows=rows)[0]["agent"], "prediction")
        state["prediction"] = {"handoff_path": "/tmp/prediction_handoff.json"}
        self.assertEqual(plan(state=state, candidate_rows=rows)[0]["agent"], "critic")
        state["critic"] = {"report_path": "/tmp/critic_report.json"}
        self.assertEqual(plan(state=state, candidate_rows=rows)[0]["agent"], "planner")


    def test_build_plan_injects_project_config(self):
        report_path = self._report([
            self._issue("l2_interface_confidence_low", "iterate_interface_design")
        ])
        # Injected snapshot wins over state's project_config; the project IDs
        # must agree (Planner enforces the same project).
        injected = {
            "project_id": "planner_test",
            "targets": [{"id": "KEAP1"}],
        }
        result = build_plan(
            critic_report_path=report_path,
            state=self._state(),
            project_config=injected,
        )
        design = next(
            task for task in result["tasks"]
            if task["action"] == "iterate_design"
        )
        self.assertEqual(
            design["parameters"]["project_config_digest"],
            object_sha256(injected),
        )
        self.assertNotEqual(
            design["parameters"]["project_config_digest"],
            object_sha256(self._state()["project_config"]),
        )

    def test_build_plan_rejects_mismatched_project_config(self):
        report_path = self._report([
            self._issue("l2_interface_confidence_low", "iterate_interface_design")
        ])
        with self.assertRaises(PlannerContractError) as captured:
            build_plan(
                critic_report_path=report_path,
                state=self._state(),
                project_config={"project_id": "keap1", "targets": [{"id": "KEAP1"}]},
            )
        self.assertEqual(captured.exception.code, "planner_project_mismatch")

    def test_plan_injects_project_config(self):
        state = self._state()  # approved planner_test config
        state["project_id"] = "keap1"  # must agree with the injected config
        keap1_unapproved = {"project_id": "keap1", "targets": [{"id": "KEAP1"}]}
        result = plan(
            state=state,
            candidate_rows=[],
            project_config=keap1_unapproved,
        )
        self.assertEqual(result[0]["action"], "review_and_approve_project_config")

        keap1_approved = {
            "project_id": "keap1",
            "targets": [{"id": "KEAP1"}],
            "review": {
                "status": "approved",
                "approved_digest": "a" * 64,
                "content_digest": "a" * 64,
            },
        }
        result = plan(
            state=state,
            candidate_rows=[],
            project_config=keap1_approved,
        )
        self.assertEqual(result[0]["action"], "run")


class ProtocolWorkflowTests(_PlannerFixtures, unittest.TestCase):
    """PR40 review round 4, must-fix 3: the full protocol identity loop.

    Planner emits a prediction task carrying a protocol identity object ->
    Execution contract validation accepts it -> Prediction records the same
    identity in the artifact bundle -> the execution gate reuses that evidence
    only when the identity matches the active protocol exactly.
    """

    def _prediction_task(self):
        report_path = self._report([
            self._issue("l2_interface_confidence_low", "iterate_interface_design")
        ])
        plan = build_plan(critic_report_path=report_path, state=self._state())
        return next(
            task for task in plan["tasks"]
            if task["action"] == "evaluate_new_design_candidates"
        )

    def test_planner_prediction_task_carries_active_protocol_identity(self):
        task = self._prediction_task()
        identity = task["parameters"]["predictor_protocol"]
        self.assertEqual(identity, protocol_binding())
        self.assertEqual(set(identity), {"name", "version", "sha256"})
        self.assertIsInstance(identity["sha256"], str)
        self.assertEqual(len(identity["sha256"]), 64)

    def test_planner_task_passes_execution_contract_validation(self):
        task = self._prediction_task()
        normalized = validate_task_parameters(task)
        self.assertEqual(
            normalized["predictor_protocol"],
            protocol_binding(),
        )

    def test_bundle_reuse_gate_full_loop(self):
        """Prediction records the planner identity; reuse passes; tampering
        is refused before stale evidence can be mixed into a run."""
        task = self._prediction_task()
        identity = dict(task["parameters"]["predictor_protocol"])

        # Prediction writes the bundle with exactly the identity it received.
        bundle = {"protocol": identity}
        validate_execution_compatibility(bundle)

        # Resume/reuse under the same identity stays green.
        validate_execution_compatibility({"protocol": dict(bundle["protocol"])})

        # Any parameter drift changes the identity SHA: refuse, never reuse.
        tampered = dict(identity)
        tampered["sha256"] = "f" * 64
        self.assertNotEqual(tampered["sha256"], identity["sha256"])
        with self.assertRaises(ProtocolError):
            validate_execution_compatibility({"protocol": tampered})


if __name__ == "__main__":
    unittest.main()
