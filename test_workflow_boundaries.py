"""Characterization and authority tests for formal Launcher inspection."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from prediction_pipeline.contracts import file_sha256
from workflow.boundaries import FormalBoundaryInspector


class _Store:
    def __init__(self, events=(), transactions=(), artifacts=None):
        self.events = list(events)
        self.transactions = list(transactions)
        self.artifacts = dict(artifacts or {})

    def query(self, **filters):
        return [
            event
            for event in self.events
            if all(
                key == "project_id" or event.get(key) == value
                for key, value in filters.items()
            )
        ]

    def get_artifact(self, artifact_id):
        return self.artifacts.get(artifact_id)

    def list_transactions(self):
        return list(self.transactions)


def _inspector(store, *, research=None, design=None, prediction=None, status=None):
    return FormalBoundaryInspector(
        store=store,
        research_validator=research or (lambda *_args, **_kwargs: SimpleNamespace(status="not_started")),
        design_validator=design or (lambda *_args, **_kwargs: SimpleNamespace(status="not_started")),
        prediction_validator=prediction or (lambda *_args, **_kwargs: SimpleNamespace(status="not_started")),
        orchestrator_status=status or (lambda **_kwargs: {}),
    )


class FormalBoundaryInspectorTests(unittest.TestCase):
    def test_owner_validators_are_authority_for_complete_and_partial_receipts(self):
        completed = SimpleNamespace(
            status="completed",
            start_event_id="start-1",
            completion_event_id="completion-1",
            research_evidence_ids=("research-1",),
        )
        partial = SimpleNamespace(
            status="started_without_completion",
            start_event_id="start-2",
            blocker_code="design_recovery_ambiguous",
        )
        inspector = _inspector(
            _Store(),
            research=lambda *_args, **_kwargs: completed,
            design=lambda *_args, **_kwargs: partial,
        )

        research = inspector.research(object())
        design = inspector.design(object())

        self.assertEqual(research.status, "completed")
        self.assertEqual(research.references["completion_event_id"], "completion-1")
        self.assertEqual(design.status, "blocked")
        self.assertEqual(design.blocker_code, "design_recovery_ambiguous")

    def test_prediction_validator_receives_expected_inputs_and_store(self):
        calls = []
        store = _Store()

        def validate(correlation, *, store, expected_inputs):
            calls.append((correlation, store, expected_inputs))
            return SimpleNamespace(
                status="completed",
                prediction_invocation_id="prediction_invocation_1",
                prediction_run_id="prediction_1",
                handoff_path=Path("C:/formal/handoff.json"),
            )

        result = _inspector(store, prediction=validate).prediction(
            "correlation", expected_inputs="inputs"
        )

        self.assertEqual(calls, [("correlation", store, "inputs")])
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.references["prediction_run_id"], "prediction_1")

    def test_critic_requires_one_digest_bound_report_for_prediction_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "critic.json"
            report = {
                "report_id": "critic-1",
                "source": {"prediction_run_id": "prediction-1"},
            }
            path.write_text(json.dumps(report), encoding="utf-8")
            event = {
                "event_id": "event-1",
                "agent": "critic",
                "event_type": "critic_review",
                "report_id": "critic-1",
                "report_path": str(path),
                "report_sha256": file_sha256(path),
            }
            inspector = _inspector(_Store([event]))

            result = inspector.critic(project_id="project-1", prediction_run_id="prediction-1")

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.references["report_id"], "critic-1")
            event["report_sha256"] = "wrong"
            blocked = inspector.critic(project_id="project-1", prediction_run_id="prediction-1")
            self.assertEqual(blocked.blocker_code, "critic_recovery_ambiguous")

    def test_duplicate_critic_completion_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            for suffix in ("a", "b"):
                path = Path(tmp) / f"critic-{suffix}.json"
                report = {
                    "report_id": f"critic-{suffix}",
                    "source": {"prediction_run_id": "prediction-1"},
                }
                path.write_text(json.dumps(report), encoding="utf-8")
                events.append({
                    "event_id": f"event-{suffix}",
                    "agent": "critic",
                    "event_type": "critic_review",
                    "report_id": report["report_id"],
                    "report_path": str(path),
                    "report_sha256": file_sha256(path),
                })

            result = _inspector(_Store(events)).critic(
                project_id="project-1", prediction_run_id="prediction-1"
            )

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.blocker_code, "critic_recovery_ambiguous")

    def test_unresolved_transaction_is_a_blocker_without_recovery_mutation(self):
        store = _Store(transactions=[{
            "transaction_id": "tx-1",
            "status": "COMMITTING",
            "context": {"run_id": "run-1"},
        }])

        result = _inspector(store).transactions(run_id="run-1")

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.blocker_code, "transaction_recovery_unresolved")
        self.assertEqual(result.references["transaction_ids"], ("tx-1",))

    def test_orchestrator_status_is_revalidated_by_public_seam(self):
        calls = []

        def status(*, run_path):
            calls.append(run_path)
            return {
                "run": {
                    "run_id": "run-1",
                    "workflow_id": "workflow-1",
                    "plan": {"plan_id": "plan-1"},
                    "status": "completed",
                },
                "run_path": str(run_path),
                "summary": {"succeeded": 2},
            }

        result = _inspector(_Store(), status=status).orchestrator(run_path="run.json")

        self.assertEqual(calls, ["run.json"])
        self.assertEqual(result.references["formal_status"], "completed")
        self.assertEqual(result.references["run_id"], "run-1")

    def test_worker_failure_trace_comes_from_formal_evidence(self):
        store = _Store([{
            "event_id": "failure-evidence",
            "agent": "execution",
            "event_type": "execution_task_failed",
            "workflow_id": "workflow-1",
            "run_id": "run-1",
            "plan_id": "plan-1",
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "transaction_id": "transaction-1",
        }])

        result = _inspector(store).execution_failure(run_id="run-1")

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.references["task_id"], "task-1")
        self.assertEqual(result.references["transaction_id"], "transaction-1")


if __name__ == "__main__":
    unittest.main()
