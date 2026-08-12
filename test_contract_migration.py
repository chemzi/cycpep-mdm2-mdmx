"""PR2 contract, trace propagation and Evidence Ledger migration tests."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import data_layer
from agents import planner
from agents.orchestrator import claim, initialize, retry, status
from agents.planner import record_approval, run as planner_run
from contracts import (
    ActionType,
    ArtifactRef,
    Approval,
    ErrorInfo,
    EvidenceEvent,
    ExecutionTask,
    TraceContext,
    get_action_spec,
)
from contracts.event import VALID_AGENTS, VALID_EVENT_TYPES
from contracts.plan import PlanContractError, validate_plan_for_approval
from execution.action_registry import ACTION_REGISTRY, handler_for, validate_registry
from execution.config import ExecutionConfig
from execution.contracts import ExecutionContractError, validate_dispatch_packet
from execution.worker import execute_task
from prediction_pipeline.contracts import object_sha256


POLICY_CONSTRAINTS = [
    "do_not_change_thresholds_automatically",
    "do_not_delete_candidates_automatically",
    "do_not_start_gpu_jobs_without_planner_budget_and_execution_approval",
    "reuse_complete_prediction_evidence",
]


class ContractMigrationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="contract-migration-test-"))
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
            "project_id": "contract_migration_test",
            "round": 1,
            "phase": "critic",
            "design_budget": {"route_A": 20},
            "thresholds": {"L2_ipsae": {"value": None}},
            "project_config": {
                "project_id": "contract_migration_test",
                "targets": [{"id": "MDM2", "required": True}],
                "review": {
                    "status": "approved",
                    "approved_digest": "a" * 64,
                    "content_digest": "a" * 64,
                },
            },
            "iteration_history": [],
        })

    def tearDown(self):
        (
            data_layer.DATA_DIR,
            data_layer.EVIDENCE_DIR,
            data_layer.STATE_PATH,
            data_layer.LOG_PATH,
            data_layer.INDEX_PATH,
        ) = self.original_paths

    def _report(self, *, marker: str = "flow") -> Path:
        issue = {
            "code": "threshold_calibration_pending",
            "severity": "medium",
            "category": "calibration",
            "message": "fixture",
            "candidate_ids": ["C0001"],
            "evidence": [{"threshold_keys": ["L2_ipsae:MDM2"]}],
            "recommended_action": "calibrate_thresholds",
            "owner_hint": "research",
            "blocks_finalization": True,
        }
        digest = object_sha256({"marker": marker, "root": str(self.root)})
        report_id = f"critic_{digest[:12]}"
        report = {
            "schema_version": 1,
            "critic_version": "1.1.1",
            "report_id": report_id,
            "input_digest": digest,
            "source": {
                "prediction_handoff": str(self.root / "prediction.json"),
                "prediction_handoff_sha256": "b" * 64,
                "prediction_run_id": f"prediction_{marker}",
                "prediction_pipeline_version": "1.5.1",
                "project_id": "contract_migration_test",
                "required_targets": ["MDM2"],
                "record_count": 1,
            },
            "verdict": "review",
            "passed": False,
            "summary": "fixture",
            "issue_counts": {},
            "issues": [issue],
            "metrics_snapshot": {},
            "recommendations": [{
                "action": "calibrate_thresholds",
                "owner_hint": "research",
                "priority": "P2",
                "reason_codes": [issue["code"]],
                "approval_required": True,
            }],
            "planner_handoff": {
                "critic_report_id": report_id,
                "issue_codes": [issue["code"]],
                "recommended_actions": ["calibrate_thresholds"],
                "policy_constraints": POLICY_CONSTRAINTS,
            },
        }
        path = self.root / f"{report_id}.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return path

    def _plan_and_run(self, marker: str = "flow") -> tuple[dict, dict, dict]:
        plan_result = planner_run(critic_report_path=self._report(marker=marker))
        task_ids = plan_result["plan"]["approval_request"]["required_task_ids"]
        approval = record_approval(
            plan_path=plan_result["plan_path"],
            task_ids=task_ids,
            approver="PI-contract-test",
            justification="reviewed CPU calibration proposal",
        )
        initialized = initialize(
            plan_path=plan_result["plan_path"],
            approval_paths=[approval["approval_path"]],
        )
        return plan_result, approval, initialized

    def _config(self) -> ExecutionConfig:
        return ExecutionConfig(
            repo_root=Path(__file__).resolve().parent,
            execution_root=self.root / "execution",
            core_python=Path(sys.executable),
            design_python=Path(sys.executable),
            prediction_python=Path(sys.executable),
            prediction_artifacts_root=self.root / "artifacts",
            prediction_runs_root=self.root / "prediction_runs",
            colabdesign_dir=self.root,
            colabdesign_params=self.root,
            cuda_data_dir=self.root,
            boltz_executable=None,
            boltz_cache=None,
            boltz_checkpoint=None,
            prodigy_executable=None,
            pyrosetta_python=None,
            control_data_path=None,
        )

    def test_action_registry_completeness_and_single_truth(self):
        validate_registry()
        self.assertFalse(hasattr(planner, "ACTION_SPECS"))
        self.assertEqual(set(ACTION_REGISTRY), set(ActionType))
        for action in ActionType:
            spec = get_action_spec(action)
            self.assertEqual(ACTION_REGISTRY[action], spec)
            if spec.executable:
                self.assertTrue(callable(handler_for(action)))

    def test_action_resource_class_is_derived_and_cross_checked(self):
        self.assertTrue(planner.RECOMMENDATION_MAPPINGS)
        self.assertTrue(all(
            not hasattr(mapping, "resource_class")
            for mapping in planner.RECOMMENDATION_MAPPINGS.values()
        ))
        plan_result, _, _ = self._plan_and_run(marker="resource")
        task = plan_result["plan"]["tasks"][0]
        spec = get_action_spec(task["action"])
        self.assertEqual(task["resource_request"]["class"], spec.resource_class)
        tampered = copy.deepcopy(plan_result["plan"])
        tampered["tasks"][0]["resource_request"]["class"] = "cpu"
        with self.assertRaises(PlanContractError) as raised:
            validate_plan_for_approval(tampered, Path(plan_result["plan_path"]))
        self.assertEqual(raised.exception.code, "execution_resource_class_mismatch")

    def test_v2_schemas_require_workflow_and_legacy_plan_adapts(self):
        plan_result = planner_run(critic_report_path=self._report(marker="legacy"))
        self.assertEqual(plan_result["plan"]["schema_version"], 2)
        plan_schema = json.loads(
            (Path(__file__).resolve().parent / "agents" / "planner_plan.schema.json").read_text()
        )
        run_schema = json.loads(
            (Path(__file__).resolve().parent / "agents" / "orchestrator_run.schema.json").read_text()
        )
        self.assertEqual(plan_schema["properties"]["schema_version"]["const"], 2)
        self.assertIn("workflow_id", plan_schema["required"])
        source_variants = plan_schema["properties"]["source"]["oneOf"]
        critic_ref = next(
            item["$ref"] for item in source_variants
            if item["$ref"].endswith("/critic_source")
        )
        critic_source = plan_schema["$defs"][critic_ref.rsplit("/", 1)[-1]]
        self.assertIn("workflow_id", critic_source["required"])
        self.assertEqual(run_schema["properties"]["schema_version"]["const"], 2)
        self.assertIn("workflow_id", run_schema["required"])
        self.assertIn("workflow_id", run_schema["properties"]["plan"]["required"])

        legacy = copy.deepcopy(plan_result["plan"])
        legacy["schema_version"] = 1
        legacy.pop("workflow_id")
        legacy["source"].pop("workflow_id")
        legacy_path = self.root / "legacy_plan.json"
        legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
        approval = record_approval(
            plan_path=legacy_path,
            task_ids=legacy["approval_request"]["required_task_ids"],
            approver="PI-contract-test",
            justification="approve legacy adapter fixture",
        )
        initialized = initialize(
            plan_path=legacy_path, approval_paths=[approval["approval_path"]]
        )
        self.assertEqual(initialized["run"]["schema_version"], 2)
        self.assertTrue(initialized["run"]["workflow_id"])
        self.assertEqual(
            initialized["run"]["workflow_id"],
            initialized["run"]["plan"]["workflow_id"],
        )

    def test_core_contract_round_trips_through_json(self):
        context = TraceContext(
            project_id="contract_migration_test",
            workflow_id="workflow_fixture",
            plan_id="planner_fixture",
            run_id="orchestrator_fixture",
            task_id="T001",
            attempt_id="T001-A01",
        )
        task = ExecutionTask(
            task_id="T001",
            action=ActionType.PROPOSE_THRESHOLD_CALIBRATION,
            phase="research",
            depends_on=("T000",),
            resource_request={"class": "network_cpu", "proposal_count": 0},
            approval={"required": True},
            parameters={"threshold_keys": ["L2_ipsae:MDM2"]},
            trace_context=context,
            execution_gate={"status": "proposed", "block_reasons": []},
            candidate_scope={"candidate_ids": [], "from_task_id": None},
            outputs=("threshold_calibration_proposal.json",),
            agent="research",
            priority=2,
            disposition="required",
            reason_codes=("threshold_calibration_pending",),
            constraints=("produce_calibration_proposal_only",),
        )
        restored = ExecutionTask.from_dict(json.loads(json.dumps(task.to_dict())))
        self.assertEqual(restored, task)
        event = EvidenceEvent(
            timestamp="2026-08-04T00:00:00+00:00",
            event_id="event_fixture",
            agent="planner",
            event_type="planner_plan",
            payload={"plan_sha256": "a" * 64},
            trace_context=context,
        )
        self.assertEqual(
            EvidenceEvent.from_dict(json.loads(json.dumps(event.to_dict()))), event
        )
        artifact = ArtifactRef(
            artifact_id="artifact_fixture",
            artifact_type="calibration_proposal",
            path=str(self.root / "proposal.json"),
            sha256="b" * 64,
            producer_task_id="T001",
            producer_attempt_id="T001-A01",
            schema_version=1,
            input_artifact_ids=("artifact_input",),
        )
        self.assertEqual(
            ArtifactRef.from_dict(json.loads(json.dumps(artifact.to_dict()))), artifact
        )
        approval = Approval(
            plan_id="planner_fixture",
            plan_path=str(self.root / "execution_plan.json"),
            plan_sha256="c" * 64,
            project_id="contract_migration_test",
            approved_task_ids=("T001",),
            approver="PI-test",
            justification="fixture",
            budget_limits={"max_gpu_job_slots": None},
        )
        self.assertEqual(
            Approval.from_dict(json.loads(json.dumps(approval.to_dict()))), approval
        )
        error = ErrorInfo(
            code="fixture_error", message="transient", component="test", retryable=True
        )
        self.assertEqual(
            ErrorInfo.from_dict(json.loads(json.dumps(error.to_dict()))), error
        )
        self.assertTrue(
            ErrorInfo.from_exception(TimeoutError("transient"), component="test").retryable
        )
        self.assertFalse(
            ErrorInfo.from_exception(ValueError("invalid"), component="test").retryable
        )

    def test_invalid_event_rejected_and_legacy_candidate_trace_kept(self):
        with self.assertRaises(ValueError):
            data_layer.EvidenceLogger.log("unknown", "test", {})
        with self.assertRaises(ValueError):
            data_layer.EvidenceLogger.log("planner", "test", {}, trace_context={
                "project_id": "contract_migration_test",
                "workflow_id": "contains whitespace",
            })
        with self.assertRaises(ValueError):
            data_layer.EvidenceLogger.log(
                "planner", "test", {"workflow_id": "contains whitespace"}
            )
        data_layer.EvidenceLogger.candidate_registered({
            "candidate_id": "C0001", "sequence": "GFEWALAAK"
        })
        self.assertTrue(data_layer.EvidenceLogger.trace_candidate("C0001"))

    def test_planner_orchestrator_execution_trace_propagation(self):
        plan_result, _, initialized = self._plan_and_run()
        plan = plan_result["plan"]
        task_id = plan["tasks"][0]["task_id"]
        self.assertTrue(plan["workflow_id"])
        self.assertEqual(initialized["run"]["workflow_id"], plan["workflow_id"])
        receipt = execute_task(
            run_path=initialized["run_path"],
            task_id=task_id,
            worker_id="fake-worker",
            config=self._config(),
        )
        self.assertEqual(receipt["workflow_id"], plan["workflow_id"])
        events = data_layer.EvidenceLogger.trace_workflow(plan["workflow_id"])
        claimed_event = next(
            event for event in events
            if event["event_type"] == "orchestrator_task_claimed"
        )
        packet = json.loads(
            Path(claimed_event["dispatch_packet_path"]).read_text()
        )
        self.assertEqual(packet["workflow_id"], plan["workflow_id"])
        self.assertEqual(packet["trace_context"]["workflow_id"], plan["workflow_id"])
        event_types = {event["event_type"] for event in events}
        self.assertTrue({
            "planner_plan", "orchestrator_run_initialized",
            "orchestrator_task_claimed", "execution_task_started",
            "execution_task_completed",
        }.issubset(event_types))
        for event in events:
            if event["event_type"] in {
                "orchestrator_task_claimed", "execution_task_started",
                "execution_task_completed",
            }:
                self.assertEqual(event["workflow_id"], plan["workflow_id"])
        self.assertTrue(data_layer.EvidenceLogger.trace_run(initialized["run"]["run_id"]))
        self.assertTrue(data_layer.EvidenceLogger.trace_task(task_id))

    def test_trace_task_rejects_ambiguous_workflows(self):
        for workflow_id in ("workflow_one", "workflow_two"):
            data_layer.EvidenceLogger.log(
                "execution",
                "execution_task_started",
                {"worker": "fixture"},
                trace_context=TraceContext(
                    project_id="contract_migration_test",
                    workflow_id=workflow_id,
                    task_id="T001",
                ),
            )
        with self.assertRaises(data_layer.EvidenceTraceQueryError) as raised:
            data_layer.EvidenceLogger.trace_task("T001")
        self.assertEqual(raised.exception.code, "ambiguous_trace_query")
        scoped = data_layer.EvidenceLogger.trace_task(
            "T001", workflow_id="workflow_one"
        )
        self.assertEqual({entry["workflow_id"] for entry in scoped}, {"workflow_one"})

    def test_dispatch_trace_bindings_fail_closed(self):
        plan_result, _, initialized = self._plan_and_run(marker="dispatch")
        task_id = plan_result["plan"]["tasks"][0]["task_id"]
        claimed = claim(
            run_path=initialized["run_path"], task_id=task_id, worker="binding-worker"
        )
        packet = json.loads(Path(claimed["dispatch_packet_path"]).read_text())
        validate_dispatch_packet(packet)
        replacements = {
            "workflow_id": "other_workflow",
            "run_id": "orchestrator_other",
            "plan_id": "planner_deadbeef0000",
            "attempt_id": "T001-A99",
        }
        for field, replacement in replacements.items():
            tampered = copy.deepcopy(packet)
            tampered[field] = replacement
            with self.assertRaises(ExecutionContractError) as raised:
                validate_dispatch_packet(tampered)
            self.assertEqual(raised.exception.code, "dispatch_trace_invalid")
        tampered = copy.deepcopy(packet)
        tampered["task"]["task_id"] = "T002"
        with self.assertRaises(ExecutionContractError) as raised:
            validate_dispatch_packet(tampered)
        self.assertEqual(raised.exception.code, "dispatch_trace_invalid")

    def test_retry_keeps_workflow_task_and_distinguishes_attempts(self):
        plan_result, _, initialized = self._plan_and_run(marker="retry")
        plan = plan_result["plan"]
        task_id = plan["tasks"][0]["task_id"]

        class TransientHandlerFailure(RuntimeError):
            retryable = True

        def fail_handler(_context):
            raise TransientHandlerFailure("temporary worker failure")

        with patch("execution.worker.handler_for", return_value=fail_handler):
            with self.assertRaises(TransientHandlerFailure):
                execute_task(
                    run_path=initialized["run_path"],
                    task_id=task_id,
                    worker_id="retry-worker",
                    config=self._config(),
                )
        failed = status(run_path=initialized["run_path"])["run"]["tasks"][task_id]
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["last_error"]["retryable"])
        self.assertEqual(failed["last_error"]["code"], "TransientHandlerFailure")
        self.assertNotIn("error_code", failed["last_error"])
        retry(
            run_path=initialized["run_path"],
            task_id=task_id,
            operator="PI-contract-test",
            reason="retry transient fixture failure",
        )
        receipt = execute_task(
            run_path=initialized["run_path"],
            task_id=task_id,
            worker_id="retry-worker",
            config=self._config(),
        )
        self.assertEqual(receipt["attempt_id"], "T001-A02")
        events = data_layer.EvidenceLogger.trace_task(
            task_id, workflow_id=plan["workflow_id"]
        )
        attempts = {
            event.get("attempt_id")
            for event in events
            if event["event_type"] in {
                "orchestrator_task_failed", "orchestrator_task_completed"
            }
        }
        self.assertEqual(attempts, {"T001-A01", "T001-A02"})
        self.assertEqual(
            {event["workflow_id"] for event in events if event.get("workflow_id")},
            {plan["workflow_id"]},
        )
        first_attempt_events = data_layer.EvidenceLogger.trace_task(
            task_id,
            workflow_id=plan["workflow_id"],
            attempt_id="T001-A01",
        )
        self.assertEqual(
            {event.get("attempt_id") for event in first_attempt_events}, {"T001-A01"}
        )
        failure_event = next(
            event for event in events if event["event_type"] == "execution_task_failed"
        )
        self.assertEqual(failure_event["code"], "TransientHandlerFailure")
        self.assertNotIn("error_code", failure_event)


if __name__ == "__main__":
    unittest.main()
