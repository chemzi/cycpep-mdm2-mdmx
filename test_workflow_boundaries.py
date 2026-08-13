"""Characterization and authority tests for formal Launcher inspection."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agents.planner import build_initial_prediction_bootstrap_plan
from agents.prediction_contract import CRITIC_READY_STATUSES
from prediction_pipeline.contracts import file_sha256
from prediction_pipeline.execution_identity import build_prediction_execution_identity
from threshold_contract import canonical_threshold_digest
from workflow.boundaries import FormalBoundary, FormalBoundaryInspector


class _Store:
    def __init__(self, events=(), transactions=(), artifacts=None, statuses=None):
        self.events = list(events)
        self.transactions = list(transactions)
        self.artifacts = dict(artifacts or {})
        self.statuses = dict(statuses or {})

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

    def get_transaction_status(self, transaction_id):
        return self.statuses.get(transaction_id)

    def get_transaction(self, transaction_id):
        return next(
            (item for item in self.transactions if item.get("transaction_id") == transaction_id),
            None,
        )

    def list_artifacts(self):
        return list(self.artifacts.values())


def _inspector(store, *, research=None, design=None, prediction=None, status=None):
    return FormalBoundaryInspector(
        store=store,
        research_validator=research or (lambda *_args, **_kwargs: SimpleNamespace(status="not_started")),
        design_validator=design or (lambda *_args, **_kwargs: SimpleNamespace(status="not_started")),
        prediction_validator=prediction or (lambda *_args, **_kwargs: SimpleNamespace(status="not_started")),
        orchestrator_status=status or (lambda **_kwargs: {}),
    )


def _bootstrap_prediction_plan(identity):
    return {
        "plan_id": "planner_0123456789ab", "workflow_id": "workflow-1",
        "source": {
            "kind": "initial_prediction_bootstrap", "project_id": "project-1",
            "candidate_ids": ["C0001"], "execution_identity": identity,
        },
        "tasks": [{
            "task_id": "T001", "action": "evaluate_new_design_candidates",
            "candidate_scope": {"candidate_ids": ["C0001"]},
            "parameters": {"execution_identity": identity},
        }],
    }


def _retry_plan_recovery_fixture(root):
    identity = build_prediction_execution_identity()
    source = {
        "project_id": "project-1", "approved_content_binding": "approved-content",
        "launcher_run_id": "launcher_0123456789abcdef0123456789abcdef",
        "research_completion_event_id": "research-complete",
        "design_invocation_id": "design_initial_0123456789abcdef0123456789abcdef",
        "design_completion_event_id": "design-complete",
        "design_transaction_id": "tx-design", "candidate_ids": ["C0001"],
        "execution_identity": identity,
    }
    initial = build_initial_prediction_bootstrap_plan(source=source)
    retry_binding = {
        "retry_index": 1, "prior_plan_id": initial["plan_id"],
        "prior_run_id": "run-failed", "prior_task_id": "T001",
        "prior_attempt_id": "T001-A01", "prior_transaction_id": "tx-failed",
        "failure_event_id": "failure-1", "failure_status": "failed",
    }
    retry = build_initial_prediction_bootstrap_plan(
        source={**source, "retry": retry_binding}
    )
    events = []
    for plan in (initial, retry):
        path = root / f"{plan['plan_id']}.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        plan_source = plan["source"]
        events.append({
            "event_id": f"event-{plan['plan_id']}", "agent": "planner",
            "event_type": "planner_plan", "source_kind": "initial_prediction_bootstrap",
            "project_id": "project-1", "launcher_run_id": source["launcher_run_id"],
            "design_completion_event_id": "design-complete",
            "design_transaction_id": "tx-design", "candidate_ids": ["C0001"],
            "execution_identity": identity, "retry": plan_source.get("retry"),
            "plan_id": plan["plan_id"], "plan_path": str(path),
            "plan_sha256": file_sha256(path),
        })
    events.append({
        "event_id": "failure-1", "agent": "execution",
        "event_type": "execution_task_failed", "project_id": "project-1",
        "workflow_id": initial["workflow_id"], "run_id": "run-failed",
        "plan_id": initial["plan_id"], "task_id": "T001",
        "attempt_id": "T001-A01", "transaction_id": "tx-failed",
        "action": "evaluate_new_design_candidates", "retryable": True,
    })
    transaction = {
        "transaction_id": "tx-failed", "project_id": "project-1",
        "workflow_id": initial["workflow_id"], "run_id": "run-failed",
        "task_id": "T001", "attempt_id": "T001-A01",
        "action": "evaluate_new_design_candidates", "status": "FAILED",
        "error": {"retryable": True},
        "metadata": {"project_id": "project-1", "plan_id": initial["plan_id"]},
    }
    return source, events, transaction


def _assert_prediction_proof_tampering_blocks(
    case, inspector, store, plan, orchestrator, record_artifact_id,
    prediction_events, record_path,
):
    removed_artifact = store.artifacts.pop(record_artifact_id)
    case.assertEqual(inspector.prediction_execution(
        project_id="project-1", plan=plan, orchestrator=orchestrator
    ).status, "blocked")
    store.artifacts[record_artifact_id] = removed_artifact
    removed_event = store.events.pop(store.events.index(prediction_events[0]))
    case.assertEqual(inspector.prediction_execution(
        project_id="project-1", plan=plan, orchestrator=orchestrator
    ).status, "blocked")
    store.events.append(removed_event)
    extra_event = {
        **prediction_events[0],
        "event_id": "recorded-C9999",
        "candidate_id": "C9999",
        "record_artifact_id": "tx-prediction-prediction-record-C9999",
    }
    store.events.append(extra_event)
    case.assertEqual(inspector.prediction_execution(
        project_id="project-1", plan=plan, orchestrator=orchestrator
    ).status, "blocked")
    store.events.remove(extra_event)
    handoff_event = prediction_events[1]
    original_handoff_event = dict(handoff_event)
    handoff_event.update({
        "candidate_id": "C0001",
        "record_artifact_id": record_artifact_id,
    })
    handoff_event.pop("handoff_artifact_id")
    case.assertEqual(inspector.prediction_execution(
        project_id="project-1", plan=plan, orchestrator=orchestrator
    ).status, "blocked")
    handoff_event.clear()
    handoff_event.update(original_handoff_event)
    battery_event = prediction_events[2]
    store.events.remove(battery_event)
    case.assertEqual(inspector.prediction_execution(
        project_id="project-1", plan=plan, orchestrator=orchestrator
    ).status, "blocked")
    store.events.append(battery_event)
    duplicate_battery = {**battery_event, "event_id": "battery-duplicate"}
    store.events.append(duplicate_battery)
    case.assertEqual(inspector.prediction_execution(
        project_id="project-1", plan=plan, orchestrator=orchestrator
    ).status, "blocked")
    store.events.remove(duplicate_battery)
    original_identity = battery_event["execution_identity"]
    battery_event["execution_identity"] = {"wrong": True}
    case.assertEqual(inspector.prediction_execution(
        project_id="project-1", plan=plan, orchestrator=orchestrator
    ).status, "blocked")
    battery_event["execution_identity"] = original_identity
    record_event = prediction_events[0]
    original_prediction_run_id = record_event.pop("prediction_run_id")
    missing_run = inspector.prediction_execution(
        project_id="project-1", plan=plan, orchestrator=orchestrator
    )
    case.assertEqual(
        missing_run.blocker_code, "prediction_execution_correlation_invalid"
    )
    record_event["prediction_run_id"] = "prediction-wrong"
    mismatched_run = inspector.prediction_execution(
        project_id="project-1", plan=plan, orchestrator=orchestrator
    )
    case.assertEqual(
        mismatched_run.blocker_code, "prediction_execution_correlation_invalid"
    )
    record_event["prediction_run_id"] = original_prediction_run_id
    original_record = record_path.read_text(encoding="utf-8")
    record_path.write_text("{}", encoding="utf-8")
    case.assertEqual(inspector.prediction_execution(
        project_id="project-1", plan=plan, orchestrator=orchestrator
    ).status, "blocked")
    record_path.write_text(original_record, encoding="utf-8")


def _threshold_artifact_fixture(root):
    path = root / "committed-thresholds.json"
    path.write_text("{}", encoding="utf-8")
    artifact_id = "tx-prediction-prediction-thresholds"
    return artifact_id, path, canonical_threshold_digest({}), {
        "artifact_id": artifact_id,
        "artifact_type": "prediction_thresholds",
        "path": str(path),
        "sha256": file_sha256(path),
        "producer_task_id": "T001",
    }


def _prediction_common(plan, identity, record_artifact_id, threshold_artifact_id):
    artifact_ids = [
        "tx-prediction-prediction_handoff",
        record_artifact_id,
        threshold_artifact_id,
    ]
    return artifact_ids, {
        "project_id": "project-1", "workflow_id": "workflow-1",
        "run_id": "run-1", "plan_id": plan["plan_id"],
        "task_id": "T001", "attempt_id": "T001-A01",
        "transaction_id": "tx-prediction",
        "prediction_run_id": "prediction-domain",
        "execution_identity": identity,
        "artifact_ids": artifact_ids,
    }


def _bind_threshold_artifact_trace(artifact, common, plan):
    artifact.update({
        **{key: common[key] for key in (
            "project_id", "workflow_id", "run_id", "task_id",
            "attempt_id", "transaction_id",
        )},
        "metadata": {"project_id": "project-1", "plan_id": plan["plan_id"]},
    })


def _assert_threshold_boundary(case, inspector, store, plan, orchestrator, artifact_id, path):
    completed = inspector.prediction_execution(
        project_id="project-1", plan=plan, orchestrator=orchestrator
    )
    case.assertEqual(completed.status, "completed")
    case.assertEqual(completed.references["thresholds_artifact_id"], artifact_id)
    case.assertEqual(completed.references["thresholds_path"], str(path.resolve()))
    removed = store.artifacts.pop(artifact_id)
    missing = inspector.prediction_execution(
        project_id="project-1", plan=plan, orchestrator=orchestrator
    )
    case.assertEqual(missing.blocker_code, "prediction_execution_correlation_invalid")
    store.artifacts[artifact_id] = removed
    original = path.read_text(encoding="utf-8")
    path.write_text('{"changed": true}', encoding="utf-8")
    changed = inspector.prediction_execution(
        project_id="project-1", plan=plan, orchestrator=orchestrator
    )
    case.assertEqual(changed.blocker_code, "prediction_execution_correlation_invalid")
    path.write_text(original, encoding="utf-8")
    artifact = store.artifacts[artifact_id]
    original_transaction_id = artifact["transaction_id"]
    artifact["transaction_id"] = "tx-wrong"
    wrong_trace = inspector.prediction_execution(
        project_id="project-1", plan=plan, orchestrator=orchestrator
    )
    case.assertEqual(
        wrong_trace.blocker_code, "prediction_execution_correlation_invalid"
    )
    artifact["transaction_id"] = original_transaction_id
    return completed


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
                "source": {
                    "project_id": "project-1",
                    "prediction_run_id": "prediction-1",
                },
            }
            path.write_text(json.dumps(report), encoding="utf-8")
            event = {
                "event_id": "event-1",
                "agent": "critic",
                "event_type": "critic_review",
                "project_id": "project-1",
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
                    "source": {
                        "project_id": "project-1",
                        "prediction_run_id": "prediction-1",
                    },
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

    def test_retry_failure_recovery_requires_shared_retryable_terminal_proof(self):
        identity = build_prediction_execution_identity()
        plan = _bootstrap_prediction_plan(identity)
        event = {
            "event_id": "failure-evidence", "agent": "execution",
            "event_type": "execution_task_failed", "project_id": "project-1",
            "workflow_id": "workflow-1", "run_id": "run-1",
            "plan_id": plan["plan_id"], "task_id": "T001",
            "attempt_id": "T001-A01", "transaction_id": "transaction-1",
            "action": "evaluate_new_design_candidates", "retryable": True,
        }
        transaction = {
            **{key: event[key] for key in (
                "project_id", "workflow_id", "run_id", "task_id",
                "attempt_id", "transaction_id", "action",
            )},
            "status": "FAILED", "error": {"retryable": True},
            "metadata": {"project_id": "project-1", "plan_id": plan["plan_id"]},
        }
        store = _Store([event], transactions=[transaction])
        inspector = _inspector(store)
        self.assertEqual(inspector.execution_failure(
            run_id="run-1", failed_plan=plan
        ).status, "completed")
        for status in (
            "STAGING", "COMMITTING", "COMMITTED", "COMPENSATION_CONFLICT", "UNKNOWN"
        ):
            with self.subTest(status=status):
                transaction["status"] = status
                self.assertEqual(inspector.execution_failure(
                    run_id="run-1", failed_plan=plan
                ).status, "blocked")
        store.transactions.clear()
        self.assertEqual(inspector.execution_failure(
            run_id="run-1", failed_plan=plan
        ).status, "blocked")

    def test_bootstrap_prediction_rejects_project_and_orchestrator_plan_drift(self):
        plan = {
            "plan_id": "planner_0123456789ab",
            "workflow_id": "workflow-1",
            "source": {
                "kind": "initial_prediction_bootstrap",
                "project_id": "project-1",
                "candidate_ids": ["C0001"],
                "execution_identity": {"identity": "expected"},
            },
            "tasks": [{
                "task_id": "T001",
                "action": "evaluate_new_design_candidates",
                "candidate_scope": {"candidate_ids": ["C0001"]},
                "parameters": {"execution_identity": {"identity": "expected"}},
            }],
        }
        run = {
            "workflow_id": "workflow-other",
            "plan": {"plan_id": plan["plan_id"]},
            "tasks": {"T001": {"status": "succeeded", "attempts": 1}},
        }
        orchestrator = FormalBoundary.completed(
            "orchestrator",
            plan_id=plan["plan_id"],
            workflow_id="workflow-other",
            run_id="run-1",
            run_document=run,
        )
        inspector = _inspector(_Store())

        wrong_project = inspector.prediction_execution(
            project_id="project-other", plan=plan, orchestrator=orchestrator
        )
        wrong_run = inspector.prediction_execution(
            project_id="project-1", plan=plan, orchestrator=orchestrator
        )

        self.assertEqual(wrong_project.blocker_code, "prediction_execution_plan_invalid")
        self.assertEqual(wrong_run.blocker_code, "prediction_execution_correlation_invalid")

    def test_duplicate_bootstrap_plan_publications_fail_closed(self):
        launcher_id = "launcher_0123456789abcdef0123456789abcdef"
        source = {
            "project_id": "project-1",
            "approved_content_binding": "approved-content",
            "launcher_run_id": launcher_id,
            "research_completion_event_id": "research-complete",
            "design_invocation_id": "design_initial_0123456789abcdef0123456789abcdef",
            "design_completion_event_id": "design-complete",
            "design_transaction_id": "tx-design",
            "candidate_ids": ["C0001"],
            "execution_identity": build_prediction_execution_identity(),
        }
        plan = build_initial_prediction_bootstrap_plan(source=source)
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            for suffix in ("a", "b"):
                path = Path(tmp) / f"plan-{suffix}.json"
                path.write_text(json.dumps(plan), encoding="utf-8")
                events.append({
                    "event_id": f"plan-event-{suffix}",
                    "agent": "planner",
                    "event_type": "planner_plan",
                    "source_kind": "initial_prediction_bootstrap",
                    "launcher_run_id": launcher_id,
                    "design_completion_event_id": "design-complete",
                    "design_transaction_id": "tx-design",
                    "candidate_ids": ["C0001"],
                    "execution_identity": source["execution_identity"],
                    "plan_id": plan["plan_id"],
                    "plan_path": str(path),
                    "plan_sha256": file_sha256(path),
                })

            result = _inspector(_Store(events)).bootstrap_prediction_plan(
                project_id="project-1",
                approved_content_binding="approved-content",
                launcher_run_id=launcher_id,
                design_invocation_id=source["design_invocation_id"],
                design_completion_event_id="design-complete",
                design_transaction_id="tx-design",
                candidate_ids=("C0001",),
            )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.blocker_code, "bootstrap_plan_recovery_ambiguous")

    def test_retry_plan_recovery_requires_shared_retryable_terminal_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, events, transaction = _retry_plan_recovery_fixture(Path(tmp))
            store = _Store(events, transactions=[transaction])
            inspector = _inspector(store)
            arguments = {
                "project_id": "project-1",
                "approved_content_binding": "approved-content",
                "launcher_run_id": source["launcher_run_id"],
                "design_invocation_id": source["design_invocation_id"],
                "design_completion_event_id": "design-complete",
                "design_transaction_id": "tx-design",
                "candidate_ids": ("C0001",),
            }
            self.assertEqual(
                inspector.bootstrap_prediction_plan(**arguments).status, "completed"
            )
            for status in (
                "STAGING", "COMMITTING", "COMMITTED", "COMPENSATION_CONFLICT", "UNKNOWN"
            ):
                with self.subTest(status=status):
                    transaction["status"] = status
                    self.assertEqual(
                        inspector.bootstrap_prediction_plan(**arguments).status, "blocked"
                    )
            store.transactions.clear()
            self.assertEqual(
                inspector.bootstrap_prediction_plan(**arguments).status, "blocked"
            )

    def test_bootstrap_prediction_completion_requires_one_bound_committed_output(self):
        identity = build_prediction_execution_identity()
        plan = _bootstrap_prediction_plan(identity)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record_path = root / "C0001.json"
            record = {
                "candidate": {"candidate_id": "C0001", "sequence": "ACDEFGHI"},
                "run_id": "prediction-domain",
                "pipeline_version": "test",
                "status": "needs_optimization",
                "battery": {
                    "competition_clearance": False,
                    "metric_clearance": False,
                    "triage_status": "valid",
                    "missing_evidence": [],
                    "missing_thresholds": [],
                },
                "protocol_identity": identity["prediction_protocol"],
                "execution_identity": identity,
            }
            record_path.write_text(json.dumps(record), encoding="utf-8")
            handoff_path = root / "prediction_handoff.json"
            (threshold_artifact_id, thresholds_path, thresholds_digest,
             threshold_artifact) = _threshold_artifact_fixture(root)
            handoff = {
                "project_id": "project-1",
                "run_id": "prediction-domain",
                "pipeline_version": "test",
                "protocol_identity": identity["prediction_protocol"],
                "execution_identity": identity,
                "thresholds_digest": thresholds_digest,
                "downstream": {
                    "critic_input_statuses": list(CRITIC_READY_STATUSES),
                    "authoritative_record_field": "record_path",
                },
                "categories": {"needs_optimization": [{
                    "candidate_id": "C0001",
                    "record_path": str(record_path),
                    "record_sha256": file_sha256(record_path),
                }]},
            }
            handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
            run = {
                "workflow_id": "workflow-1",
                "plan": {"plan_id": plan["plan_id"]},
                "tasks": {"T001": {
                    "status": "succeeded",
                    "attempts": 1,
                    "outputs": [{
                        "role": "prediction_handoff",
                        "path": str(handoff_path),
                        "sha256": file_sha256(handoff_path),
                    }],
                }},
            }
            orchestrator = FormalBoundary.completed(
                "orchestrator",
                plan_id=plan["plan_id"], workflow_id="workflow-1",
                run_id="run-1", run_document=run,
            )
            completion = {
                "event_id": "execution-complete",
                "project_id": "project-1",
                "agent": "execution",
                "event_type": "execution_task_completed",
                "workflow_id": "workflow-1",
                "run_id": "run-1",
                "plan_id": plan["plan_id"],
                "task_id": "T001",
                "attempt_id": "T001-A01",
                "transaction_id": "tx-prediction",
                "expected_execution_identity": identity,
                "observed_execution_identity": identity,
            }
            record_artifact_id = "tx-prediction-prediction-record-C0001"
            artifact_ids, common = _prediction_common(
                plan, identity, record_artifact_id, threshold_artifact_id
            )
            _bind_threshold_artifact_trace(threshold_artifact, common, plan)
            handoff["categories"]["needs_optimization"][0]["record_artifact_id"] = (
                record_artifact_id
            )
            handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
            run["tasks"]["T001"]["outputs"][0]["sha256"] = file_sha256(handoff_path)
            prediction_events = [{
                **common, "event_id": "recorded-C0001", "agent": "prediction",
                "event_type": "prediction_recorded", "candidate_id": "C0001",
                "record_artifact_id": record_artifact_id,
            }, {
                **common, "event_id": "handoff-ready", "agent": "prediction",
                "event_type": "prediction_handoff_ready",
                "handoff_artifact_id": "tx-prediction-prediction_handoff",
                "thresholds_artifact_id": threshold_artifact_id,
                "thresholds_digest": thresholds_digest,
            }, {
                **common, "event_id": "battery-C0001", "agent": "prediction",
                "event_type": "battery_evaluated", "candidate_id": "C0001",
            }]
            transaction = {
                **common, "status": "COMMITTED",
                "action": "evaluate_new_design_candidates",
                "metadata": {"project_id": "project-1", "plan_id": plan["plan_id"]},
                "artifact_ids": artifact_ids,
            }
            store = _Store(
                [completion, *prediction_events],
                transactions=[transaction],
                artifacts={"tx-prediction-prediction_handoff": {
                    "artifact_id": "tx-prediction-prediction_handoff",
                    "path": str(handoff_path),
                    "sha256": file_sha256(handoff_path),
                    "producer_task_id": "T001",
                    **{key: common[key] for key in (
                        "project_id", "workflow_id", "run_id", "task_id",
                        "attempt_id", "transaction_id",
                    )},
                }, threshold_artifact_id: threshold_artifact, record_artifact_id: {
                    "artifact_id": record_artifact_id,
                    "artifact_type": "prediction_record",
                    "path": str(record_path),
                    "sha256": file_sha256(record_path),
                    "producer_task_id": "T001",
                    **{key: common[key] for key in (
                        "project_id", "workflow_id", "run_id", "task_id",
                        "attempt_id", "transaction_id",
                    )},
                    "metadata": {"project_id": "project-1", "plan_id": plan["plan_id"]},
                }},
                statuses={"tx-prediction": "COMMITTED"},
            )
            inspector = _inspector(store)
            completed = _assert_threshold_boundary(
                self, inspector, store, plan, orchestrator,
                threshold_artifact_id, thresholds_path,
            )
            self.assertEqual(completed.references["prediction_run_id"], "prediction-domain")
            self.assertIn("battery-C0001", completed.references["evidence_ids"])
            _assert_prediction_proof_tampering_blocks(
                self, inspector, store, plan, orchestrator,
                record_artifact_id, prediction_events, record_path,
            )

            completion["observed_execution_identity"] = {"wrong": True}
            blocked = inspector.prediction_execution(
                project_id="project-1", plan=plan, orchestrator=orchestrator
            )
            self.assertEqual(
                blocked.blocker_code, "prediction_execution_correlation_invalid"
            )


if __name__ == "__main__":
    unittest.main()
