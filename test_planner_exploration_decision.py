"""Focused E3-A tests for Planner's frozen ExplorationDecision input."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
from itertools import combinations
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import data_layer
import experience
from agents.planner import (
    PLANNER_VERSION,
    PlannerConfig,
    PlannerContractError,
    build_plan,
)
from contracts.exploration_decision import (
    ExplorationDecision,
    ExplorationDecisionContractError,
)
from jsonschema import Draft202012Validator, ValidationError
from prediction_pipeline.contracts import object_sha256
from test_exploration_decision import (
    PROJECT_ID,
    PREDICTION_RUN_ID,
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


class PlannerExplorationDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="planner-e3a-")
        self.root = Path(self.temp.name)
        self.decision = build_decision(evidence_batch()).to_dict()
        self.other_decision = build_decision(evidence_batch(sufficient=False)).to_dict()
        self.state = {
            "project_id": PROJECT_ID,
            "workflow_id": WORKFLOW_ID,
            "round": 1,
            "design_budget": {"route_A": 12},
            "project_config": {
                "project_id": PROJECT_ID,
                "targets": [
                    {"id": "MDM2", "design": {"lengths": [8, 10, 12]}},
                    {"id": "MDMX", "design": {"lengths": [8, 10, 12]}},
                ],
                "review": {
                    "status": "approved",
                    "approved_digest": "a" * 64,
                    "content_digest": "a" * 64,
                },
            },
            "iteration_history": [{"event_type": "exploration_decision", "newer": True}],
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _report(
        self,
        *,
        project_id: str = PROJECT_ID,
        prediction_run_id: str = PREDICTION_RUN_ID,
        required_targets: list[str] | None = None,
    ) -> Path:
        targets = list(TARGETS) if required_targets is None else required_targets
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
        digest = object_sha256({
            "fixture": len(list(self.root.glob("critic_*.json"))),
            "project_id": project_id,
            "prediction_run_id": prediction_run_id,
            "required_targets": targets,
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
                "required_targets": targets,
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
        path = self.root / f"{report_id}.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return path

    @staticmethod
    def _task_surface(plan: dict) -> dict:
        return {
            "tasks": plan["tasks"],
            "budget_request": plan["budget_request"],
            "approval_request": plan["approval_request"],
            "execution": plan["execution"],
            "proposal_counts": [
                task["resource_request"]["proposal_count"] for task in plan["tasks"]
            ],
            "design_jobs": [
                deepcopy(task.get("parameters", {}).get("design_jobs", []))
                for task in plan["tasks"]
            ],
        }

    def _build(self, report: Path, decision: dict | None = None, **kwargs) -> dict:
        call = {
            "critic_report_path": report,
            "state": deepcopy(self.state),
            **kwargs,
        }
        if decision is not None:
            call["exploration_decision"] = deepcopy(decision)
        return build_plan(**call)

    def test_valid_binding_is_canonical_and_injected_only_into_local_state(self):
        report = self._report()
        caller_state = deepcopy(self.state)
        import agents.planner.plan_builder as builder

        with patch.object(
            builder,
            "_design_iteration_tasks",
            wraps=builder._design_iteration_tasks,
        ) as task_builder:
            plan = build_plan(
                critic_report_path=report,
                state=caller_state,
                exploration_decision=deepcopy(self.decision),
            )

        canonical = ExplorationDecision.from_dict(self.decision).to_dict()
        decision_sha = object_sha256(canonical)
        self.assertEqual(
            task_builder.call_args.args[1]["_frozen_exploration_decision"], canonical
        )
        self.assertNotIn("_frozen_exploration_decision", caller_state)
        self.assertEqual(plan["source"]["exploration_decision_id"], canonical["decision_id"])
        self.assertEqual(plan["source"]["exploration_decision_sha256"], decision_sha)
        self.assertEqual(
            plan["source"]["exploration_decision_input_digest"],
            canonical["decision_input_digest"],
        )

    def test_replay_is_deterministic_and_different_valid_decision_changes_identity(self):
        report = self._report()
        first = self._build(report, self.decision)
        replay = self._build(report, self.decision)
        changed = self._build(report, self.other_decision)
        self.assertEqual(
            (first["source"], first["input_digest"], first["plan_id"]),
            (replay["source"], replay["input_digest"], replay["plan_id"]),
        )
        self.assertNotEqual(first["input_digest"], changed["input_digest"])
        self.assertNotEqual(first["plan_id"], changed["plan_id"])

    def test_all_handoff_mismatch_classes_fail_closed(self):
        cases = {
            "project": (
                self._report(project_id="other_project"),
                {**deepcopy(self.state), "project_id": "other_project"},
            ),
            "workflow": (
                self._report(),
                {**deepcopy(self.state), "workflow_id": "workflow_other"},
            ),
            "source_round": (
                self._report(),
                {**deepcopy(self.state), "round": 2},
            ),
            "prediction_run": (
                self._report(prediction_run_id="prediction_other"),
                deepcopy(self.state),
            ),
            "target_scope": (
                self._report(required_targets=["MDM2"]),
                deepcopy(self.state),
            ),
        }
        for label, (report, state) in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(PlannerContractError):
                    build_plan(
                        critic_report_path=report,
                        state=state,
                        exploration_decision=deepcopy(self.decision),
                    )

        tampered = ExplorationDecision.from_dict(self.decision)
        object.__setattr__(tampered, "applies_to_round", tampered.applies_to_round + 1)
        import agents.planner.plan_builder as builder

        with patch.object(ExplorationDecision, "from_dict", return_value=tampered):
            with self.assertRaises(PlannerContractError):
                builder.build_plan(
                    critic_report_path=self._report(),
                    state=deepcopy(self.state),
                    exploration_decision=deepcopy(self.decision),
                )

    def test_invalid_contract_and_ambiguous_critic_target_scopes_fail_closed(self):
        invalid = deepcopy(self.decision)
        invalid["decision_id"] = "exploration_decision_" + "0" * 64
        with self.assertRaises(ExplorationDecisionContractError):
            self._build(self._report(), invalid)
        for scope in ([], ["MDM2", "MDM2"], ["MDM2", ""]):
            with self.subTest(scope=scope):
                with self.assertRaises(PlannerContractError):
                    self._build(self._report(required_targets=scope), self.decision)

    def test_reordered_equivalent_targets_are_accepted_without_reordering_tasks(self):
        reversed_targets = list(reversed(TARGETS))
        plan = self._build(
            self._report(required_targets=reversed_targets), self.decision
        )
        design = next(task for task in plan["tasks"] if task["action"] == "iterate_design")
        self.assertEqual(design["parameters"]["required_targets"], reversed_targets)
        self.assertEqual(
            [job["target_id"] for job in design["parameters"]["design_jobs"]],
            reversed_targets,
        )

    def test_absence_preserves_frozen_legacy_source_identity_and_tasks(self):
        report = self._report()
        baseline = self._build(report)
        explicit_absence = build_plan(
            critic_report_path=report,
            state=deepcopy(self.state),
            exploration_decision=None,
        )
        expected_source = {
            "critic_report": str(report.resolve()),
            "critic_report_sha256": baseline["source"]["critic_report_sha256"],
            "critic_report_id": baseline["source"]["critic_report_id"],
            "critic_verdict": "iterate",
            "prediction_run_id": PREDICTION_RUN_ID,
            "project_id": PROJECT_ID,
            "workflow_id": WORKFLOW_ID,
        }
        report_sha = hashlib.sha256(report.read_bytes()).hexdigest()
        expected_digest = object_sha256({
            "critic_report_path": str(report.resolve()),
            "critic_report_sha256": report_sha,
            "workflow_id": WORKFLOW_ID,
            "state": {
                "project_id": PROJECT_ID,
                "round": 1,
                "design_budget": {"route_A": 12},
                "project_config_digest": object_sha256(self.state["project_config"]),
                "critic_report_id": None,
            },
            "config": asdict(PlannerConfig()),
            "planner_version": PLANNER_VERSION,
        })
        self.assertEqual(baseline["source"], expected_source)
        self.assertEqual(baseline["input_digest"], expected_digest)
        self.assertEqual(baseline["plan_id"], f"planner_{expected_digest[:12]}")
        self.assertEqual(explicit_absence["source"], baseline["source"])
        self.assertEqual(explicit_absence["input_digest"], baseline["input_digest"])
        self.assertEqual(explicit_absence["plan_id"], baseline["plan_id"])
        self.assertEqual(self._task_surface(explicit_absence), self._task_surface(baseline))

    def test_critic_source_schema_accepts_complete_decision_group_only(self):
        plan = self._build(self._report(), self.decision)
        schema_path = Path(__file__).parent / "agents" / "planner_plan.schema.json"
        root_schema = json.loads(schema_path.read_text(encoding="utf-8"))
        critic_source_schema = deepcopy(root_schema["$defs"]["critic_source"])
        critic_source_schema["$schema"] = root_schema["$schema"]
        critic_source_schema["$defs"] = {
            "sha256": deepcopy(root_schema["$defs"]["sha256"])
        }
        validator = Draft202012Validator(critic_source_schema)
        validator.validate(plan["source"])

        provenance_fields = (
            "exploration_decision_id",
            "exploration_decision_sha256",
            "exploration_decision_input_digest",
        )
        for field_count in (1, 2):
            for kept_fields in combinations(provenance_fields, field_count):
                partial = {
                    key: value
                    for key, value in plan["source"].items()
                    if key not in provenance_fields or key in kept_fields
                }
                with self.subTest(kept_fields=kept_fields):
                    with self.assertRaises(ValidationError):
                        validator.validate(partial)

    def test_explicit_decision_uses_no_ambient_lookup_or_formal_persistence(self):
        report = self._report()
        with (
            patch.object(data_layer.State, "load", side_effect=AssertionError("State.load")) as load,
            patch.object(data_layer.State, "update", side_effect=AssertionError("State.update")) as update,
            patch.object(data_layer.State, "append_history", side_effect=AssertionError("history")) as history,
            patch.object(data_layer.EvidenceLogger, "get_all", side_effect=AssertionError("Evidence lookup")) as get_all,
            patch.object(data_layer.EvidenceLogger, "log", side_effect=AssertionError("Evidence write")) as log,
            patch.object(experience, "consume_experience_preference", side_effect=AssertionError("experience lookup")) as consume,
            patch.object(experience, "record_applied_preference", side_effect=AssertionError("experience write")) as record,
        ):
            self._build(report, self.decision)
        for mocked in (load, update, history, get_all, log, consume, record):
            mocked.assert_not_called()

    def test_decision_materializes_only_length_policy_on_task_surface(self):
        report = self._report()
        first = self._build(report, self.decision)
        changed = self._build(report, self.other_decision)
        first_surface = self._task_surface(first)
        changed_surface = self._task_surface(changed)
        self.assertEqual(first_surface["budget_request"], changed_surface["budget_request"])
        self.assertEqual(first_surface["approval_request"], changed_surface["approval_request"])
        self.assertEqual(first_surface["execution"], changed_surface["execution"])
        self.assertEqual(first_surface["proposal_counts"], changed_surface["proposal_counts"])
        self.assertEqual(
            [job["lengths"] for job in first_surface["design_jobs"][0]],
            [[12], [12]],
        )
        self.assertEqual(
            [job["lengths"] for job in changed_surface["design_jobs"][0]],
            [[8, 10, 12], [8, 10, 12]],
        )

    def test_explicit_adjustment_missing_lengths_uses_decision_nonambient_policy(self):
        state = deepcopy(self.state)
        for target in state["project_config"]["targets"]:
            target["design"].pop("lengths")
        with (
            patch.object(
                experience,
                "consume_experience_preference",
                side_effect=AssertionError("explicit Decision consulted experience"),
            ) as consume,
            patch.object(
                experience,
                "record_applied_preference",
                side_effect=AssertionError("explicit Decision recorded experience"),
            ) as record,
        ):
            plan = build_plan(
                critic_report_path=self._report(),
                state=state,
                exploration_decision=deepcopy(self.decision),
            )
        design = next(task for task in plan["tasks"] if task["action"] == "iterate_design")
        self.assertTrue(design["parameters"]["design_jobs"])
        for job in design["parameters"]["design_jobs"]:
            self.assertEqual(job["lengths"], [12])
        consume.assert_not_called()
        record.assert_not_called()

    def test_absent_decision_missing_lengths_uses_static_nonambient_default(self):
        state = deepcopy(self.state)
        for target in state["project_config"]["targets"]:
            target["design"].pop("lengths")
        hint = {"lengths": [10], "reason": "legacy fixture"}
        with (
            patch.object(
                experience,
                "consume_experience_preference",
                return_value=([10], hint),
            ) as consume,
            patch.object(experience, "record_applied_preference") as record,
        ):
            plan = build_plan(critic_report_path=self._report(), state=state)
        design = next(task for task in plan["tasks"] if task["action"] == "iterate_design")
        self.assertEqual(
            [job["lengths"] for job in design["parameters"]["design_jobs"]],
            [[8, 10, 12], [8, 10, 12]],
        )
        consume.assert_not_called()
        record.assert_not_called()

    def test_caller_state_marker_is_removed_before_static_nonambient_default(self):
        state = deepcopy(self.state)
        state["_frozen_exploration_decision"] = deepcopy(self.decision)
        for target in state["project_config"]["targets"]:
            target["design"].pop("lengths")
        hint = {"lengths": [12], "reason": "legacy caller marker fixture"}
        with (
            patch.object(
                experience,
                "consume_experience_preference",
                return_value=([12], hint),
            ) as consume,
            patch.object(experience, "record_applied_preference") as record,
        ):
            plan = build_plan(critic_report_path=self._report(), state=state)
        design = next(task for task in plan["tasks"] if task["action"] == "iterate_design")
        self.assertEqual(
            [job["lengths"] for job in design["parameters"]["design_jobs"]],
            [[8, 10, 12], [8, 10, 12]],
        )
        consume.assert_not_called()
        record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
