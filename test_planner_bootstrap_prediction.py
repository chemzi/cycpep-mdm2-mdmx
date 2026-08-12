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
                    },
                ]

            def query(self, **filters):
                return [
                    event for event in [*self.base, *self.published]
                    if all(event.get(key) == value for key, value in filters.items())
                ]

            def get_transaction_status(self, transaction_id):
                return "COMMITTED" if transaction_id == source["design_transaction_id"] else None

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
            )["plan_id"] = first["plan"]["plan_id"]

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


if __name__ == "__main__":
    unittest.main()
