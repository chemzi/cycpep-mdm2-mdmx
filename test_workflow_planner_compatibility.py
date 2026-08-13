"""Compatibility coverage for Launcher and the current immutable Planner plan."""

from __future__ import annotations

from copy import deepcopy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import data_layer
from agents.planner import record_approval, validate_plan_for_approval
from contracts.trace import TraceContext
from core.context import ProjectContext, ProjectPaths
from exploration import exploration_shortlist
from exploration_decision import build_exploration_decision, record_exploration_decision
from prediction_pipeline.contracts import file_sha256, object_sha256
from target_bootstrap import config_digest
from test_exploration_decision import (
    PROTOCOL,
    THRESHOLDS,
    THRESHOLD_DIGEST,
    evidence_batch,
)
from workflow.adapters import DefaultWorkflowRuntime
from workflow.boundaries import FormalBoundaryInspector
from workflow.runtime_context import bind_project_context


POLICY_CONSTRAINTS = [
    "do_not_change_thresholds_automatically",
    "do_not_delete_candidates_automatically",
    "do_not_start_gpu_jobs_without_planner_budget_and_execution_approval",
    "reuse_complete_prediction_evidence",
]


class WorkflowPlannerCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="launcher-planner-compat-")
        self.root = Path(self.temporary.name)
        self.project_id = "launcher_planner_compatibility"
        self.config = {
            "project_id": self.project_id,
            "targets": [
                {"id": "MDM2", "design": {"lengths": [8, 10, 12]}},
                {"id": "MDMX", "design": {"lengths": [8, 10, 12]}},
            ],
            "review": {
                "status": "approved",
                "approved_digest": "a" * 64,
                "content_digest": "a" * 64,
            },
        }
        self.config["review"]["approved_digest"] = config_digest(self.config)
        self.config["review"]["content_digest"] = config_digest(self.config)
        self.context = ProjectContext(
            project_id=self.project_id,
            config=self.config,
            paths=ProjectPaths(
                data_dir=self.root / "data",
                evidence_dir=self.root / "evidence",
                output_dir=self.root / "outputs",
                database_path=self.root / "data" / "store.db",
            ),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_current_plan_survives_launcher_approval_and_orchestrator_chain(self) -> None:
        with bind_project_context(self.context):
            data_layer.State.save(self._state())
            report_path, report_id = self._critic_report()
            self._publish_formal_decision()
            runtime = self._runtime()

            planner_result = runtime.run_planner(report_path)
            plan_path = Path(planner_result["plan_path"])
            original_bytes = plan_path.read_bytes()
            original_plan = json.loads(original_bytes)

            self._assert_current_planner_metadata(original_plan)
            planner_boundary = runtime.inspect_planner(
                SimpleNamespace(references={"report_id": report_id})
            )
            self.assertEqual(planner_boundary.status, "completed")
            self.assertEqual(planner_boundary.references["plan_document"], original_plan)
            self.assertEqual(
                planner_boundary.references["plan_id"], original_plan["plan_id"]
            )
            self.assertEqual(
                planner_boundary.references["plan_sha256"], file_sha256(plan_path)
            )

            inspected_plan = planner_boundary.references["plan_document"]
            validated_plan = validate_plan_for_approval(inspected_plan, plan_path)
            self.assertIs(validated_plan, inspected_plan)
            self.assertEqual(validated_plan, original_plan)

            approval = record_approval(
                plan_path=plan_path,
                task_ids=original_plan["approval_request"]["required_task_ids"],
                approver="planner-compatibility-reviewer",
                justification="verify current Launcher Planner compatibility",
                max_gpu_job_slots=10,
                max_gpu_minutes=10_000,
                max_design_proposals=10_000,
                max_prediction_candidates=10_000,
            )
            self.assertEqual(approval["approval"]["plan_id"], original_plan["plan_id"])
            self.assertEqual(
                approval["approval"]["plan_sha256"], file_sha256(plan_path)
            )
            approval_boundary = runtime.inspect_approvals(planner_boundary)
            self.assertEqual(approval_boundary.status, "completed")
            self.assertIn(
                approval["approval"]["approval_id"],
                approval_boundary.references["approval_ids"],
            )

            initialized = runtime.initialize_orchestrator(
                plan_path, [approval["approval_path"]]
            )
            run = initialized["run"]
            self.assertEqual(run["plan"]["plan_id"], original_plan["plan_id"])
            self.assertEqual(run["plan"]["plan_sha256"], file_sha256(plan_path))
            self.assertEqual(run["plan"]["workflow_id"], original_plan["workflow_id"])
            self.assertEqual(plan_path.read_bytes(), original_bytes)
            self.assertEqual(json.loads(plan_path.read_bytes()), original_plan)

    def _runtime(self) -> DefaultWorkflowRuntime:
        runtime = object.__new__(DefaultWorkflowRuntime)
        runtime.context = self.context
        runtime.store = data_layer.get_storage_backend()
        runtime.inspector = FormalBoundaryInspector(
            store=runtime.store,
            research_validator=lambda *_args, **_kwargs: None,
            design_validator=lambda *_args, **_kwargs: None,
            prediction_validator=lambda *_args, **_kwargs: None,
            orchestrator_status=lambda **_kwargs: {},
        )
        return runtime

    def _publish_formal_decision(self) -> None:
        workflow_id = "workflow_planner_compatibility"
        run_id = "run_planner_compatibility"
        prediction_run_id = "prediction_planner_compatibility"
        targets = ("MDM2", "MDMX")
        trace = TraceContext(self.project_id, workflow_id, run_id)
        batteries = []
        excluded = {
            "event_id",
            "event_type",
            "agent",
            "phase",
            "project_id",
            "workflow_id",
            "run_id",
            "targets",
        }
        for source in evidence_batch():
            payload = {
                key: deepcopy(value)
                for key, value in source.items()
                if key not in excluded
            }
            payload["prediction_run_id"] = prediction_run_id
            event_id = data_layer.EvidenceLogger.log(
                "prediction",
                "battery_evaluated",
                payload,
                targets=list(targets),
                phase="evaluate",
                trace_context=trace,
            )
            batteries.append(next(
                row
                for row in data_layer.EvidenceLogger.get_all()
                if row["event_id"] == event_id
            ))
        shortlist_id = data_layer.EvidenceLogger.log(
            "critic",
            "exploration_shortlist",
            exploration_shortlist(
                batteries,
                targets=list(targets),
                thresholds=THRESHOLDS,
            ),
            targets=list(targets),
            phase="critic",
            round_num=2,
            trace_context=trace,
        )
        shortlist = next(
            row
            for row in data_layer.EvidenceLogger.get_all()
            if row["event_id"] == shortlist_id
        )
        handoff_id = data_layer.EvidenceLogger.log(
            "prediction",
            "prediction_handoff_ready",
            {
                "prediction_run_id": prediction_run_id,
                "candidate_ids": sorted(row["candidate_id"] for row in batteries),
                "protocol_identity": dict(PROTOCOL),
                "thresholds_digest": THRESHOLD_DIGEST,
                "handoff_artifact_id": "artifact-planner-compatibility",
            },
            targets=list(targets),
            phase="evaluate",
            trace_context=trace,
        )
        handoff = next(
            row
            for row in data_layer.EvidenceLogger.get_all()
            if row["event_id"] == handoff_id
        )
        decision = build_exploration_decision(
            battery_events=batteries,
            shortlist_event=shortlist,
            prediction_handoff_event=handoff,
            project_config=self.config,
            thresholds=THRESHOLDS,
            project_id=self.project_id,
            workflow_id=workflow_id,
            run_id=run_id,
            target_ids=targets,
            source_round=2,
        )
        record_exploration_decision(decision)

    def _state(self) -> dict:
        return {
            "project_id": self.project_id,
            "round": 2,
            "phase": "critic",
            "design_budget": {"route_A": 20, "route_B": 10},
            "compute_budget": {"global_budget_minutes": 240.0},
            "project_config": self.config,
            "iteration_history": [],
        }

    def _critic_report(self) -> tuple[Path, str]:
        issue = {
            "code": "l2_interface_confidence_low",
            "severity": "high",
            "category": "scientific_metric",
            "message": "fixture",
            "candidate_ids": ["C0514"],
            "evidence": [],
            "recommended_action": "iterate_interface_design",
            "owner_hint": "design",
            "blocks_finalization": True,
        }
        digest = object_sha256({"fixture": "launcher-planner-compatibility"})
        report_id = f"critic_{digest[:12]}"
        report = {
            "schema_version": 1,
            "critic_version": "1.0.0",
            "report_id": report_id,
            "input_digest": digest,
            "source": {
                "prediction_handoff": str(self.root / "prediction_handoff.json"),
                "prediction_handoff_sha256": "b" * 64,
                "prediction_run_id": "prediction_planner_compatibility",
                "prediction_pipeline_version": "1.5.0",
                "project_id": self.project_id,
                "required_targets": ["MDM2", "MDMX"],
                "record_count": 1,
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
        return path, report_id

    def _assert_current_planner_metadata(self, plan: dict) -> None:
        self.assertEqual(plan["schema_version"], 2)
        self.assertEqual(plan["decision_metadata"]["global_budget_minutes"], 240.0)
        self.assertEqual(plan["decision_metadata"]["budget_status"], "within_budget")
        self.assertGreater(
            plan["decision_metadata"]["total_estimated_gpu_minutes"], 0.0
        )
        self.assertEqual(
            plan["budget_request"]["configured_design_budget_snapshot"],
            {"route_A": 20, "route_B": 10},
        )
        gpu_tasks = [
            task for task in plan["tasks"]
            if task["resource_request"]["class"] == "gpu"
        ]
        self.assertTrue(gpu_tasks)
        self.assertTrue(
            all(task["resource_request"]["estimate_status"] == "estimated" for task in gpu_tasks)
        )
        self.assertTrue(
            all(task["resource_request"]["estimated_gpu_minutes"] > 0 for task in gpu_tasks)
        )


if __name__ == "__main__":
    unittest.main()
