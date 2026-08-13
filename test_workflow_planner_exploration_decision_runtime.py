"""Formal E3-C runtime integration tests for Planner Decision handoff."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import data_layer
import experience
from agents.planner import PlannerContractError, run as run_planner_service
from contracts.exploration_decision import ExplorationDecision
from contracts.trace import TraceContext
from core.context import ProjectContext, ProjectPaths
from data_layer import EvidenceLogger, State
from exploration import exploration_shortlist
from exploration_decision import (
    build_exploration_decision,
    record_exploration_decision,
)
from prediction_pipeline.contracts import object_sha256
from test_exploration_decision import (
    PREDICTION_RUN_ID,
    PROJECT,
    PROJECT_ID,
    PROTOCOL,
    RUN_ID,
    TARGETS,
    THRESHOLDS,
    THRESHOLD_DIGEST,
    WORKFLOW_ID,
    decision_battery,
    evidence_batch,
)
from target_bootstrap import config_digest
from workflow.adapters import DefaultWorkflowRuntime
from workflow.exploration_decision_handoff import ExplorationDecisionHandoffError
from workflow.runtime_context import bind_project_context


POLICY_CONSTRAINTS = [
    "do_not_change_thresholds_automatically",
    "do_not_delete_candidates_automatically",
    "do_not_start_gpu_jobs_without_planner_budget_and_execution_approval",
    "reuse_complete_prediction_evidence",
]


class WorkflowPlannerExplorationDecisionRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="workflow-e3c-runtime-")
        self.root = Path(self.temp.name)
        self.config = deepcopy(PROJECT)
        self.context = ProjectContext(
            project_id=PROJECT_ID,
            config=self.config,
            paths=ProjectPaths(
                data_dir=self.root / "data",
                evidence_dir=self.root / "evidence",
                output_dir=self.root / "outputs",
                database_path=self.root / "data" / "store.db",
            ),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _state(self) -> dict:
        return {
            "project_id": PROJECT_ID,
            "round": 1,
            "phase": "critic",
            "design_budget": {"route_A": 12},
            "project_config": deepcopy(self.config),
            "iteration_history": [],
        }

    def _runtime(self) -> DefaultWorkflowRuntime:
        return DefaultWorkflowRuntime(
            self.context,
            "launcher_000000000000400080000000000000c3",
        )

    def _publish_decision(
        self,
        source_rows: list[dict],
        *,
        project_config: dict | None = None,
        target_ids: tuple[str, ...] = TARGETS,
    ) -> ExplorationDecision:
        project_config = self.config if project_config is None else project_config
        trace = TraceContext(PROJECT_ID, WORKFLOW_ID, RUN_ID)
        batteries = []
        trace_fields = {
            "event_id",
            "event_type",
            "agent",
            "phase",
            "project_id",
            "workflow_id",
            "run_id",
            "targets",
        }
        for source in source_rows:
            event_id = EvidenceLogger.log(
                "prediction",
                "battery_evaluated",
                {
                    key: deepcopy(value)
                    for key, value in source.items()
                    if key not in trace_fields
                },
                targets=list(target_ids),
                phase="evaluate",
                trace_context=trace,
            )
            batteries.append(next(
                row
                for row in EvidenceLogger.get_all()
                if row["event_id"] == event_id
            ))

        shortlist_id = EvidenceLogger.log(
            "critic",
            "exploration_shortlist",
            exploration_shortlist(
                batteries,
                targets=list(target_ids),
                thresholds=THRESHOLDS,
            ),
            targets=list(target_ids),
            phase="critic",
            round_num=1,
            trace_context=trace,
        )
        shortlist = next(
            row
            for row in EvidenceLogger.get_all()
            if row["event_id"] == shortlist_id
        )
        handoff_id = EvidenceLogger.log(
            "prediction",
            "prediction_handoff_ready",
            {
                "prediction_run_id": PREDICTION_RUN_ID,
                "candidate_ids": sorted(row["candidate_id"] for row in batteries),
                "protocol_identity": dict(PROTOCOL),
                "thresholds_digest": THRESHOLD_DIGEST,
                "handoff_artifact_id": "artifact-handoff-e3c-runtime",
            },
            targets=list(target_ids),
            phase="evaluate",
            trace_context=trace,
        )
        handoff = next(
            row
            for row in EvidenceLogger.get_all()
            if row["event_id"] == handoff_id
        )
        decision = build_exploration_decision(
            battery_events=batteries,
            shortlist_event=shortlist,
            prediction_handoff_event=handoff,
            project_config=project_config,
            thresholds=THRESHOLDS,
            project_id=PROJECT_ID,
            workflow_id=WORKFLOW_ID,
            run_id=RUN_ID,
            target_ids=target_ids,
            source_round=1,
        )
        record_exploration_decision(decision)
        return decision

    def _publish_prediction_identity(self) -> None:
        EvidenceLogger.log(
            "prediction",
            "prediction_handoff_ready",
            {
                "prediction_run_id": PREDICTION_RUN_ID,
                "candidate_ids": ["C800"],
                "protocol_identity": dict(PROTOCOL),
                "thresholds_digest": THRESHOLD_DIGEST,
                "handoff_artifact_id": "artifact-handoff-e3c-missing-decision",
            },
            targets=list(TARGETS),
            phase="evaluate",
            trace_context=TraceContext(PROJECT_ID, WORKFLOW_ID, RUN_ID),
        )

    def _critic_report(
        self,
        *,
        record_count: int,
        project_id: str = PROJECT_ID,
        prediction_run_id: str = PREDICTION_RUN_ID,
        required_targets: tuple[str, ...] = TARGETS,
    ) -> Path:
        issue = {
            "code": "l2_interface_confidence_low",
            "severity": "high",
            "category": "scientific_metric",
            "message": "formal runtime fixture",
            "candidate_ids": ["C800"],
            "evidence": [],
            "recommended_action": "iterate_interface_design",
            "owner_hint": "design",
            "blocks_finalization": True,
            "approval_required": False,
            "priority": "P1",
        }
        digest = object_sha256({
            "fixture": "formal-workflow-planner-e3c",
            "record_count": record_count,
            "project_id": project_id,
            "prediction_run_id": prediction_run_id,
            "required_targets": list(required_targets),
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
                "prediction_run_id": prediction_run_id,
                "prediction_pipeline_version": "1.5.0",
                "project_id": project_id,
                "required_targets": list(required_targets),
                "record_count": record_count,
            },
            "verdict": "iterate",
            "passed": False,
            "summary": "formal runtime fixture",
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
        path = self.root / f"{report_id}.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return path

    @staticmethod
    def _design_jobs(plan: dict) -> list[dict]:
        return next(
            task["parameters"]["design_jobs"]
            for task in plan["tasks"]
            if task["action"] == "iterate_design"
        )

    def test_formal_round2_decision_reaches_planner_service(self) -> None:
        source_rows = evidence_batch()
        with bind_project_context(self.context):
            State.save(self._state())
            decision = self._publish_decision(source_rows)
            report_path = self._critic_report(record_count=len(source_rows))
            baseline_state = self._state()
            baseline_state["workflow_id"] = WORKFLOW_ID
            with patch.object(
                experience,
                "consume_experience_preference",
                side_effect=AssertionError("Planner consulted ambient experience"),
            ) as ambient_preference:
                baseline = run_planner_service(
                    critic_report_path=report_path,
                    output_path=self.root / "legacy-baseline-plan.json",
                    state=baseline_state,
                    project_config=deepcopy(self.config),
                )
                runtime = self._runtime()
                result = runtime.run_planner(report_path)
                replay = runtime.run_planner(report_path)
            persisted_state = State.load()

        plan = result["plan"]
        canonical = decision.to_dict()
        self.assertEqual(plan["workflow_id"], WORKFLOW_ID)
        self.assertEqual(
            plan["source"]["exploration_decision_id"], decision.decision_id
        )
        self.assertEqual(
            plan["source"]["exploration_decision_sha256"],
            object_sha256(canonical),
        )
        self.assertEqual(
            plan["source"]["exploration_decision_input_digest"],
            decision.decision_input_digest,
        )
        self.assertEqual(
            [job["lengths"] for job in self._design_jobs(plan)],
            [[12], [12]],
        )
        self.assertEqual(result["plan"], replay["plan"])
        self.assertEqual(result["plan_sha256"], replay["plan_sha256"])
        baseline_jobs = self._design_jobs(baseline["plan"])
        decision_jobs = self._design_jobs(plan)
        self.assertEqual(len(baseline_jobs), len(decision_jobs))
        for baseline_job, decision_job in zip(baseline_jobs, decision_jobs):
            baseline_policy = deepcopy(baseline_job)
            decision_policy = deepcopy(decision_job)
            baseline_policy.pop("lengths")
            decision_policy.pop("lengths")
            self.assertEqual(baseline_policy, decision_policy)
        self.assertEqual(
            [task["resource_request"] for task in baseline["plan"]["tasks"]],
            [task["resource_request"] for task in plan["tasks"]],
        )
        self.assertEqual(
            baseline["plan"]["approval_request"], plan["approval_request"]
        )
        ambient_preference.assert_not_called()
        self.assertNotIn("workflow_id", persisted_state)

    def test_formal_no_adjustment_decision_preserves_approved_lengths_10_12(
        self,
    ) -> None:
        source_rows = [
            *[
                decision_battery(f"C10{index}", 10, False, f"battery-10-{index}")
                for index in range(4)
            ],
            *[
                decision_battery(f"C12{index}", 12, False, f"battery-12-{index}")
                for index in range(4)
            ],
        ]
        config = deepcopy(self.config)
        for target in config["targets"]:
            target["design"]["lengths"] = [10, 12]
        config["review"]["approved_digest"] = config_digest(config)
        config["review"]["content_digest"] = config_digest(config)
        self.config = config
        self.context = ProjectContext(
            project_id=PROJECT_ID,
            config=config,
            paths=self.context.paths,
        )

        with bind_project_context(self.context):
            State.save(self._state())
            decision = self._publish_decision(source_rows)
            result = self._runtime().run_planner(
                self._critic_report(record_count=len(source_rows))
            )

        self.assertEqual(decision.decision_status, "no_adjustment")
        self.assertEqual(list(decision.adjustment["preferred_lengths"]), [])
        self.assertEqual(
            [
                item["length"]
                for item in decision.adjustment["proposed_policy_weights"]
            ],
            [10, 12],
        )
        self.assertEqual(
            [job["lengths"] for job in self._design_jobs(result["plan"])],
            [[10, 12], [10, 12]],
        )

    def test_formal_round2_missing_decision_fails_before_plan_persistence(self) -> None:
        with bind_project_context(self.context):
            State.save(self._state())
            self._publish_prediction_identity()
            report_path = self._critic_report(record_count=1)

            with self.assertRaises(ExplorationDecisionHandoffError) as captured:
                self._runtime().run_planner(report_path)

            self.assertEqual(captured.exception.code, "exploration_decision_required")
            self.assertFalse(
                data_layer.get_storage_backend().query(
                    project_id=PROJECT_ID,
                    agent="planner",
                    event_type="planner_plan",
                )
            )
            self.assertFalse((report_path.parent / "planner").exists())

    def test_formal_runtime_scope_mismatches_fail_closed(self) -> None:
        cases = (
            ("project", self._state(), {"project_id": "other_project"}),
            ("prediction_run", self._state(), {"prediction_run_id": "prediction_other"}),
            ("source_round", {**self._state(), "round": 2}, {}),
        )
        for label, state, report_overrides in cases:
            with self.subTest(label=label):
                case_root = self.root / label
                context = ProjectContext(
                    project_id=PROJECT_ID,
                    config=self.config,
                    paths=ProjectPaths(
                        data_dir=case_root / "data",
                        evidence_dir=case_root / "evidence",
                        output_dir=case_root / "outputs",
                        database_path=case_root / "data" / "store.db",
                    ),
                )
                original_context = self.context
                self.context = context
                try:
                    with bind_project_context(context):
                        State.save(state)
                        source_rows = evidence_batch()
                        self._publish_decision(source_rows)
                        report_path = self._critic_report(
                            record_count=len(source_rows),
                            **report_overrides,
                        )
                        with self.assertRaises(ExplorationDecisionHandoffError):
                            self._runtime().run_planner(report_path)
                        self.assertFalse((report_path.parent / "planner").exists())
                finally:
                    self.context = original_context

    def test_contract_valid_target_mismatch_reaches_e3a_and_fails_closed(self) -> None:
        target_ids = ("MDM2",)
        source_rows = evidence_batch()
        for row in source_rows:
            row["targets"] = list(target_ids)
            row["target_pass"] = {"MDM2": row["passed"]}
            row["layer_values"] = {
                key: value
                for key, value in row["layer_values"].items()
                if key.endswith("_mdm2")
            }
        decision_config = deepcopy(self.config)
        decision_config["targets"] = [decision_config["targets"][0]]
        decision_config["review"]["approved_digest"] = config_digest(decision_config)
        decision_config["review"]["content_digest"] = config_digest(decision_config)

        with bind_project_context(self.context):
            State.save(self._state())
            self._publish_decision(
                source_rows,
                project_config=decision_config,
                target_ids=target_ids,
            )
            report_path = self._critic_report(record_count=len(source_rows))

            with self.assertRaises(PlannerContractError) as captured:
                self._runtime().run_planner(report_path)

            self.assertEqual(captured.exception.code, "exploration_decision_binding_mismatch")
            self.assertFalse((report_path.parent / "planner").exists())


if __name__ == "__main__":
    unittest.main()
