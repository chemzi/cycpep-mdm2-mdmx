"""Planner-owned initial Prediction bootstrap contract tests."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from agents.planner import (
    PlannerConfig,
    PlannerContractError,
    build_initial_prediction_bootstrap_plan,
    retry_initial_prediction_bootstrap,
    run_initial_prediction_bootstrap,
)
from contracts.plan import validate_plan_for_approval
from prediction_pipeline.execution_identity import build_prediction_execution_identity


def _source(candidate_ids=("C0001", "C0002")):
    return {
        "project_id": "project-1",
        "approved_content_binding": "approved-content",
        "launcher_run_id": "launcher_0123456789abcdef0123456789abcdef",
        "research_completion_event_id": "research-complete",
        "design_invocation_id": "design_initial_0123456789abcdef0123456789abcdef",
        "design_completion_event_id": "design-complete",
        "design_transaction_id": "tx-design",
        "candidate_ids": list(candidate_ids),
        "execution_identity": build_prediction_execution_identity(),
    }


class PlannerBootstrapPredictionTests(unittest.TestCase):
    def test_planner_config_preserves_legacy_positional_constructor_order(self):
        config = PlannerConfig(12, 3, 48, 48, 3, 120, None,
                               "graceful_stop_return_current_best", 5.0, 0.25, 0.02)

        self.assertEqual(config.gpu_cost_per_minute_usd, 0.02)
        self.assertEqual(config.prediction_gpu_slot_minutes_per_candidate, 15)

    def test_n2_prediction_uses_benchmark_backed_gpu_slot_minutes(self):
        plan = build_initial_prediction_bootstrap_plan(source=_source())

        resource_request = plan["tasks"][0]["resource_request"]
        self.assertEqual(resource_request["estimated_gpu_minutes"], 30)
        self.assertEqual(resource_request["estimate_status"], "estimated")
        self.assertEqual(plan["decision_metadata"]["total_estimated_gpu_minutes"], 30)

    def test_prediction_estimator_configuration_is_bound_to_plan_identity(self):
        default = build_initial_prediction_bootstrap_plan(source=_source())
        calibrated = build_initial_prediction_bootstrap_plan(
            source=_source(),
            config=PlannerConfig(prediction_gpu_slot_minutes_per_candidate=14),
        )

        self.assertEqual(
            calibrated["tasks"][0]["resource_request"]["estimated_gpu_minutes"],
            28,
        )
        self.assertNotEqual(calibrated["input_digest"], default["input_digest"])
        self.assertNotEqual(calibrated["plan_id"], default["plan_id"])

    def test_prediction_estimator_configuration_requires_a_positive_integer(self):
        for invalid in (0, -1, 1.5, True, "11"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    PlannerContractError,
                    "prediction_gpu_slot_minutes_per_candidate must be a positive integer",
                ):
                    PlannerConfig(
                        prediction_gpu_slot_minutes_per_candidate=invalid
                    )

    def test_builds_one_exact_scoped_registered_prediction_task(self):
        plan = build_initial_prediction_bootstrap_plan(source=_source())

        self.assertEqual(plan["source"]["kind"], "initial_prediction_bootstrap")
        self.assertEqual(plan["status"], "awaiting_approval")
        self.assertEqual(len(plan["tasks"]), 1)
        task = plan["tasks"][0]
        self.assertEqual(task["action"], "evaluate_new_design_candidates")
        self.assertEqual(task["candidate_scope"], {
            "candidate_ids": ["C0001", "C0002"], "from_task_id": None,
        })
        self.assertEqual(task["resource_request"]["candidate_limit"], 2)
        self.assertEqual(
            task["parameters"]["execution_identity"],
            plan["source"]["execution_identity"],
        )
        self.assertEqual(plan["approval_request"]["required_task_ids"], ["T001"])

    def test_is_deterministic_and_rejects_scope_truncation(self):
        self.assertEqual(
            build_initial_prediction_bootstrap_plan(source=_source()),
            build_initial_prediction_bootstrap_plan(source=_source()),
        )
        with self.assertRaisesRegex(PlannerContractError, "exact candidate set"):
            build_initial_prediction_bootstrap_plan(
                source=_source(), config=PlannerConfig(max_prediction_candidates_per_task=1)
            )

    def test_tagged_source_validation_rejects_critic_or_scope_drift(self):
        plan = build_initial_prediction_bootstrap_plan(source=_source())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            path.write_text("{}", encoding="utf-8")
            validate_plan_for_approval(plan, path)

            for mutate in ("critic", "scope", "extra_task"):
                with self.subTest(mutate=mutate):
                    changed = copy.deepcopy(plan)
                    if mutate == "critic":
                        changed["source"]["critic_report_id"] = "critic_000000000000"
                    elif mutate == "scope":
                        changed["tasks"][0]["candidate_scope"]["candidate_ids"] = ["C0001"]
                    else:
                        changed["tasks"].append(copy.deepcopy(changed["tasks"][0]))
                        changed["tasks"][1]["task_id"] = "T002"
                    with self.assertRaises(ValueError):
                        validate_plan_for_approval(changed, path)

    def test_formal_source_is_persisted_idempotently_and_retry_is_new(self):
        source = _source()

        class Store:
            def __init__(self):
                self.published = []
                self.base = [
                    {
                        "event_id": "research-complete", "agent": "research",
                        "event_type": "research_completion_receipt",
                        "project_id": source["project_id"],
                        "launcher_run_id": source["launcher_run_id"],
                        "approved_content_binding": source["approved_content_binding"],
                    },
                    {
                        "event_id": "design-complete", "agent": "design",
                        "project_id": source["project_id"],
                        "event_type": "design_initial_completion",
                        "design_invocation_id": source["design_invocation_id"],
                        "launcher_run_id": source["launcher_run_id"],
                        "approved_content_binding": source["approved_content_binding"],
                        "transaction_id": source["design_transaction_id"],
                        "candidate_ids": source["candidate_ids"],
                    },
                    *[
                        {
                            "event_id": f"registered-{candidate_id}",
                            "agent": "design", "event_type": "candidate_registered",
                            "project_id": source["project_id"],
                            "transaction_id": source["design_transaction_id"],
                            "candidate": {"candidate_id": candidate_id},
                        }
                        for candidate_id in source["candidate_ids"]
                    ],
                    {
                        "event_id": "failure-1", "agent": "execution",
                        "event_type": "execution_task_failed",
                        "project_id": source["project_id"],
                        "plan_id": "placeholder",
                        "run_id": "orchestrator-failed", "task_id": "T001",
                        "attempt_id": "T001-A01", "transaction_id": "tx-failed",
                        "workflow_id": "workflow-placeholder",
                        "action": "evaluate_new_design_candidates",
                        "retryable": True,
                    },
                ]

            def query(self, **filters):
                return [
                    event for event in [*self.base, *self.published]
                    if all(event.get(key) == value for key, value in filters.items())
                ]

            def get_transaction_status(self, transaction_id):
                return "COMMITTED" if transaction_id == source["design_transaction_id"] else None

            def get_transaction(self, transaction_id):
                if transaction_id != "tx-failed":
                    return None
                return {
                    "transaction_id": transaction_id,
                    "project_id": source["project_id"],
                    "workflow_id": next(
                        event["workflow_id"] for event in self.base
                        if event.get("event_id") == "failure-1"
                    ),
                    "run_id": "orchestrator-failed", "task_id": "T001",
                    "attempt_id": "T001-A01",
                    "action": "evaluate_new_design_candidates",
                    "status": "FAILED", "error": {"retryable": True},
                    "metadata": {
                        "project_id": source["project_id"],
                        "plan_id": next(
                            event["plan_id"] for event in self.base
                            if event.get("event_id") == "failure-1"
                        ),
                    },
                }

        store = Store()

        def publish(agent, event_type, payload, **_kwargs):
            store.published.append({
                "event_id": f"plan-event-{len(store.published) + 1}",
                "agent": agent, "event_type": event_type,
                "project_id": source["project_id"], **payload,
            })

        with tempfile.TemporaryDirectory() as tmp:
            first = run_initial_prediction_bootstrap(
                source=source, output_root=tmp, store=store, publisher=publish
            )
            second = run_initial_prediction_bootstrap(
                source=source, output_root=tmp, store=store, publisher=publish
            )
            self.assertEqual(first, second)
            self.assertEqual(len(store.published), 1)
            next(
                event for event in store.base if event.get("event_id") == "failure-1"
            ).update({
                "plan_id": first["plan"]["plan_id"],
                "workflow_id": first["plan"]["workflow_id"],
            })

            for key, value in (
                ("approved_content_binding", "changed-approval"),
                ("design_transaction_id", "tx-other"),
                ("candidate_ids", ["C0001"]),
            ):
                with self.subTest(formal_source_drift=key):
                    changed = copy.deepcopy(source)
                    changed[key] = value
                    with self.assertRaises(PlannerContractError):
                        run_initial_prediction_bootstrap(
                            source=changed,
                            output_root=tmp,
                            store=store,
                            publisher=publish,
                        )
            self.assertEqual(len(store.published), 1)

            retried = retry_initial_prediction_bootstrap(
                failed_plan=first["plan"],
                failure={
                    "plan_id": first["plan"]["plan_id"],
                    "workflow_id": first["plan"]["workflow_id"],
                    "run_id": "orchestrator-failed", "task_id": "T001",
                    "attempt_id": "T001-A01", "transaction_id": "tx-failed",
                    "evidence_id": "failure-1",
                },
                output_root=tmp, store=store,
                config=PlannerConfig(),
                publisher=publish,
            )
            self.assertNotEqual(retried["plan"]["plan_id"], first["plan"]["plan_id"])
            self.assertEqual(
                retried["plan"]["source"]["candidate_ids"],
                first["plan"]["source"]["candidate_ids"],
            )
            self.assertEqual(retried["plan"]["source"]["retry"]["prior_plan_id"], first["plan"]["plan_id"])

    def test_retry_rejects_non_retryable_or_unbound_transaction_states(self):
        plan = build_initial_prediction_bootstrap_plan(source=_source(("C0001",)))
        failure = {
            "plan_id": plan["plan_id"], "workflow_id": plan["workflow_id"],
            "run_id": "orchestrator-failed", "task_id": "T001",
            "attempt_id": "T001-A01", "transaction_id": "tx-failed",
            "evidence_id": "failure-1",
        }
        event = {
            **failure, "event_id": "failure-1", "project_id": "project-1",
            "agent": "execution", "event_type": "execution_task_failed",
            "action": "evaluate_new_design_candidates", "retryable": True,
        }
        event.pop("evidence_id")

        class Store:
            transaction = None

            def query(self, **filters):
                return [event] if all(event.get(k) == v for k, v in filters.items()) else []

            def get_transaction(self, _transaction_id):
                return self.transaction

        base_transaction = {
            "transaction_id": "tx-failed", "project_id": "project-1",
            "workflow_id": plan["workflow_id"], "run_id": "orchestrator-failed",
            "task_id": "T001", "attempt_id": "T001-A01",
            "action": "evaluate_new_design_candidates",
            "status": "FAILED", "error": {"retryable": True},
            "metadata": {"project_id": "project-1", "plan_id": plan["plan_id"]},
        }
        store = Store()
        cases = (
            ("missing", None),
            ("active", {**base_transaction, "status": "STAGING"}),
            ("committing", {**base_transaction, "status": "COMMITTING"}),
            ("committed", {**base_transaction, "status": "COMMITTED"}),
            ("conflict", {**base_transaction, "status": "COMPENSATION_CONFLICT"}),
            ("unknown", {**base_transaction, "status": "UNKNOWN"}),
            ("not-retryable", {**base_transaction, "error": {"retryable": False}}),
            ("wrong-action", {**base_transaction, "action": "iterate_design"}),
            ("wrong-plan", {
                **base_transaction,
                "metadata": {"project_id": "project-1", "plan_id": "plan-other"},
            }),
        )
        with tempfile.TemporaryDirectory() as tmp:
            for label, transaction in cases:
                with self.subTest(label=label):
                    store.transaction = transaction
                    with self.assertRaisesRegex(
                        PlannerContractError, "retry failure proof"
                    ):
                        retry_initial_prediction_bootstrap(
                            failed_plan=plan, failure=failure,
                            output_root=tmp, store=store,
                        )


if __name__ == "__main__":
    unittest.main()
