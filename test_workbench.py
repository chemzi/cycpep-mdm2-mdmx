"""Frontend V2 workbench read-model contract tests."""

from __future__ import annotations

import unittest

from agents.orchestrator import OrchestratorContractError
from web_api.workbench import WorkbenchReader


class FakeStore:
    project_id = "project-1"

    def __init__(self, *, state=None, candidates=(), evidence=(), artifacts=(), transactions=()):
        self.state = dict(state or {})
        self.candidates = list(candidates)
        self.evidence = list(evidence)
        self.artifacts = list(artifacts)
        self.transactions = list(transactions)
        self.calls = []

    def get_state(self, project_id):
        self.calls.append(("get_state", project_id))
        return dict(self.state)

    def list(self):
        self.calls.append(("list",))
        return list(self.candidates)

    def query(self, **filters):
        self.calls.append(("query", filters))
        return [
            item for item in self.evidence
            if all(item.get(key) == value for key, value in filters.items())
        ]

    def list_artifacts(self):
        self.calls.append(("list_artifacts",))
        return list(self.artifacts)

    def list_transactions(self, **filters):
        self.calls.append(("list_transactions", filters))
        return [
            item for item in self.transactions
            if all(item.get(key) == value for key, value in filters.items() if value is not None)
        ]


def _task(task_id, action, depends_on=(), *, gate="proposed", approval=False):
    return {
        "task_id": task_id,
        "action": action,
        "agent": "design",
        "kind": "scientific",
        "phase": "iterate",
        "disposition": "required",
        "depends_on": list(depends_on),
        "resource_request": {"class": "cpu"},
        "approval": {"required": approval, "types": []},
        "execution_gate": {"status": gate, "block_reasons": ["manual_review"] if gate == "blocked" else []},
    }


class WorkbenchReaderTests(unittest.TestCase):
    def test_no_run_returns_project_scoped_history_with_explicit_collection_counts(self):
        store = FakeStore(
            state={"project_id": "project-1", "project": "Demo"},
            candidates=[
                {"candidate_id": "C1", "sequence": "AAAA", "project_id": "project-1", "run_id": "old-run"},
                {"candidate_id": "C2", "sequence": "BBBB", "project_id": "project-1"},
            ],
            evidence=[{"event_id": "e1", "event_type": "test", "project_id": "project-1", "run_id": "old-run"}],
        )

        result = WorkbenchReader(store).read(limit=1)

        self.assertEqual(result["project"], {"project_id": "project-1", "name": "Demo", "targets": []})
        self.assertIsNone(result["workflow"])
        self.assertIsNone(result["run"])
        self.assertEqual(result["tasks"], {"scope": "current_run", "total": 0, "returned": 0, "truncated": False, "items": []})
        self.assertEqual(result["candidates"]["scope"], "project")
        self.assertEqual((result["candidates"]["total"], result["candidates"]["returned"], result["candidates"]["truncated"]), (2, 1, True))
        self.assertEqual(result["candidates"]["items"][0]["run_relation"], "historical_run")
        self.assertEqual(result["blockers"]["items"][0]["code"], "no_current_run")

    def test_non_linear_tasks_preserve_graph_and_canonical_action_availability(self):
        plan = {
            "plan_id": "plan-1",
            "workflow_id": "workflow-1",
            "tasks": [
                _task("T001", "iterate_design"),
                _task("T002", "dock_shortlisted_candidates", ("T001",)),
                _task("T003", "review_prediction_handoff", ("T001",)),
            ],
        }
        run = {
            "run_id": "run-1",
            "workflow_id": "workflow-1",
            "status": "ready",
            "plan": {"plan_id": "plan-1", "project_id": "project-1", "plan_path": "internal"},
            "tasks": {
                "T001": {"status": "ready", "attempts": 0, "attempt_history": []},
                "T002": {"status": "blocked", "attempts": 0, "attempt_history": []},
                "T003": {"status": "pending_dependency", "attempts": 0, "attempt_history": []},
            },
        }
        store = FakeStore(state={"project_id": "project-1", "orchestrator": {"run_path": "internal"}})
        reader = WorkbenchReader(
            store,
            status_reader=lambda **_: {"run": run, "summary": {"status": "ready"}},
            plan_reader=lambda *_: plan,
        )

        result = reader.read()

        tasks = {item["task_id"]: item for item in result["tasks"]["items"]}
        self.assertEqual(tasks["T002"]["depends_on"], ["T001"])
        self.assertEqual(tasks["T003"]["depends_on"], ["T001"])
        self.assertFalse(tasks["T002"]["action"]["executable"])
        self.assertIn("action_not_executable", tasks["T002"]["availability"]["reason_codes"])
        self.assertTrue(tasks["T001"]["action"]["handler_available"])

    def test_transactions_keep_formal_status_and_trace_linkage(self):
        transactions = [
            {"transaction_id": f"tx-{status.lower()}", "project_id": "project-1", "workflow_id": "workflow-1", "run_id": "run-1", "task_id": "T001", "attempt_id": "T001-A01", "status": status}
            for status in ("COMMITTED", "FAILED", "ROLLED_BACK", "COMPENSATION_CONFLICT")
        ]
        run = {"run_id": "run-1", "workflow_id": "workflow-1", "status": "running", "plan": {"plan_id": "plan-1", "project_id": "project-1", "plan_path": "internal"}, "tasks": {}}
        reader = WorkbenchReader(
            FakeStore(state={"project_id": "project-1", "orchestrator": {"run_path": "internal"}}, transactions=transactions),
            status_reader=lambda **_: {"run": run, "summary": {"status": "running"}},
            plan_reader=lambda *_: {"plan_id": "plan-1", "workflow_id": "workflow-1", "tasks": []},
        )

        items = reader.read()["transactions"]["items"]

        self.assertEqual({item["status"] for item in items}, {"COMMITTED", "FAILED", "ROLLED_BACK", "COMPENSATION_CONFLICT"})
        self.assertTrue(all(item["run_id"] == "run-1" for item in items))

    def test_execution_states_cover_approval_claim_without_transaction_and_failure(self):
        plan = {
            "plan_id": "plan-1",
            "workflow_id": "workflow-1",
            "tasks": [
                _task("T001", "review_prediction_handoff", approval=True),
                _task("T002", "review_prediction_handoff", ("T001",)),
                _task("T003", "review_prediction_handoff", ("T001",)),
            ],
        }
        run = {
            "run_id": "run-1",
            "workflow_id": "workflow-1",
            "status": "running",
            "plan": {"plan_id": "plan-1", "project_id": "project-1", "plan_path": "internal"},
            "tasks": {
                "T001": {"status": "awaiting_approval", "attempts": 0},
                "T002": {"status": "claimed", "attempts": 1, "claim": {"attempt_id": "T002-A01", "worker": "worker-1"}},
                "T003": {"status": "failed", "attempts": 1, "last_error": {"code": "scientific_input_invalid", "message": "Input was rejected", "component": "execution", "retryable": False}},
            },
        }
        reader = WorkbenchReader(
            FakeStore(state={"project_id": "project-1", "orchestrator": {"run_path": "internal"}}),
            status_reader=lambda **_: {"run": run},
            plan_reader=lambda *_: plan,
        )

        result = reader.read()

        tasks = {item["task_id"]: item for item in result["tasks"]["items"]}
        executions = {item["task_id"]: item for item in result["executions"]["items"]}
        self.assertIn("approval_required", tasks["T001"]["availability"]["reason_codes"])
        self.assertEqual(executions["T002"]["transaction_visibility"], "not_yet_recorded")
        self.assertEqual(executions["T003"]["error"]["code"], "scientific_input_invalid")
        self.assertIn("scientific_input_invalid", {item["code"] for item in result["blockers"]["items"]})

    def test_current_attempt_does_not_inherit_an_earlier_transaction(self):
        plan = {"plan_id": "plan-1", "workflow_id": "workflow-1", "tasks": [_task("T001", "review_prediction_handoff")]}
        run = {
            "run_id": "run-1", "workflow_id": "workflow-1", "status": "running",
            "plan": {"plan_id": "plan-1", "project_id": "project-1", "plan_path": "internal"},
            "tasks": {"T001": {"status": "claimed", "attempts": 2, "claim": {"worker": "worker-1"}}},
        }
        old_transaction = {
            "transaction_id": "tx-a01", "project_id": "project-1", "workflow_id": "workflow-1",
            "run_id": "run-1", "task_id": "T001", "attempt_id": "T001-A01", "status": "FAILED",
        }
        reader = WorkbenchReader(
            FakeStore(
                state={"project_id": "project-1", "orchestrator": {"run_path": "internal"}},
                transactions=[old_transaction],
            ),
            status_reader=lambda **_: {"run": run},
            plan_reader=lambda *_: plan,
        )

        execution = reader.read()["executions"]["items"][0]

        self.assertEqual(execution["attempt_id"], "T001-A02")
        self.assertEqual(execution["transaction_visibility"], "not_yet_recorded")

    def test_unresolved_recovery_evidence_is_a_structured_blocker(self):
        plan = {"plan_id": "plan-1", "workflow_id": "workflow-1", "tasks": []}
        run = {
            "run_id": "run-1", "workflow_id": "workflow-1", "status": "failed",
            "plan": {"plan_id": "plan-1", "project_id": "project-1", "plan_path": "internal"},
            "tasks": {},
        }
        transaction = {
            "transaction_id": "tx-1", "project_id": "project-1", "workflow_id": "workflow-1",
            "run_id": "run-1", "task_id": "T001", "attempt_id": "T001-A01", "status": "COMMITTED",
        }
        store = FakeStore(
            state={"project_id": "project-1", "orchestrator": {"run_path": "internal"}},
            transactions=[transaction],
            evidence=[{
                "event_id": "e1", "event_type": "execution_transaction_post_commit_failure",
                "code": "transaction_recovery_unresolved", "project_id": "project-1",
                "workflow_id": "workflow-1", "run_id": "run-1", "transaction_id": "tx-1",
            }],
        )

        blockers = WorkbenchReader(
            store, status_reader=lambda **_: {"run": run}, plan_reader=lambda *_: plan
        ).read()["blockers"]["items"]

        self.assertIn(
            ("transaction_compensation_unresolved", "tx-1"),
            {(item["code"], item.get("transaction_id")) for item in blockers},
        )

    def test_duplicate_recovery_signals_produce_one_blocker(self):
        plan = {"plan_id": "plan-1", "workflow_id": "workflow-1", "tasks": []}
        run = {
            "run_id": "run-1", "workflow_id": "workflow-1", "status": "failed",
            "plan": {"plan_id": "plan-1", "project_id": "project-1", "plan_path": "internal"},
            "tasks": {},
        }
        signal = {
            "event_id": "e1", "event_type": "execution_transaction_compensation_unresolved",
            "project_id": "project-1", "workflow_id": "workflow-1", "run_id": "run-1",
            "transaction_id": "tx-1",
        }
        transaction = {
            "transaction_id": "tx-1", "project_id": "project-1", "workflow_id": "workflow-1",
            "run_id": "run-1", "task_id": "T001", "attempt_id": "T001-A01",
            "status": "COMPENSATION_CONFLICT",
        }
        reader = WorkbenchReader(
            FakeStore(
                state={"project_id": "project-1", "orchestrator": {"run_path": "internal"}},
                evidence=[signal], transactions=[transaction],
            ),
            status_reader=lambda **_: {"run": run}, plan_reader=lambda *_: plan,
        )

        blockers = reader.read()["blockers"]["items"]

        matching = [item for item in blockers if item["code"] == "transaction_compensation_unresolved"]
        self.assertEqual(len(matching), 1)

    def test_resolved_transaction_suppresses_stale_recovery_evidence(self):
        plan = {"plan_id": "plan-1", "workflow_id": "workflow-1", "tasks": []}
        run = {
            "run_id": "run-1", "workflow_id": "workflow-1", "status": "failed",
            "plan": {"plan_id": "plan-1", "project_id": "project-1", "plan_path": "internal"},
            "tasks": {},
        }
        transaction = {
            "transaction_id": "tx-1", "project_id": "project-1", "workflow_id": "workflow-1",
            "run_id": "run-1", "task_id": "T001", "attempt_id": "T001-A01", "status": "ROLLED_BACK",
        }
        stale_signal = {
            "event_id": "e1", "event_type": "execution_transaction_compensation_conflict",
            "project_id": "project-1", "workflow_id": "workflow-1", "run_id": "run-1",
            "transaction_id": "tx-1",
        }
        reader = WorkbenchReader(
            FakeStore(
                state={"project_id": "project-1", "orchestrator": {"run_path": "internal"}},
                evidence=[stale_signal], transactions=[transaction],
            ),
            status_reader=lambda **_: {"run": run}, plan_reader=lambda *_: plan,
        )

        blockers = reader.read()["blockers"]["items"]

        self.assertNotIn("transaction_compensation_unresolved", {item["code"] for item in blockers})

    def test_invalid_binding_returns_only_trustworthy_partial_data(self):
        store = FakeStore(
            state={"project_id": "project-1", "project": "Demo", "orchestrator": {"run_path": "secret/run.json"}},
            candidates=[{"candidate_id": "C1", "sequence": "AAAA", "project_id": "project-1", "run_id": "old-run"}],
        )

        def invalid_status(**_):
            raise OrchestratorContractError("run_plan_hash_mismatch", "secret/run.json")

        result = WorkbenchReader(store, status_reader=invalid_status).read()

        self.assertIsNone(result["workflow"])
        self.assertIsNone(result["run"])
        self.assertEqual(result["tasks"]["items"], [])
        self.assertEqual(result["executions"]["items"], [])
        self.assertEqual(result["transactions"]["items"], [])
        self.assertEqual(result["candidates"]["items"][0]["candidate_id"], "C1")
        blocker = result["blockers"]["items"][0]
        self.assertEqual((blocker["code"], blocker["scope"]), ("workflow_binding_invalid", "workflow"))
        self.assertNotIn("secret", str(result))

    def test_read_uses_only_read_methods_and_never_relabels_protocol_or_exposes_paths(self):
        store = FakeStore(
            state={"project_id": "project-1"},
            candidates=[{"candidate_id": "C1", "sequence": "AAAA", "design_pdb_path": "C:/secret/candidate.pdb"}],
            evidence=[{"event_id": "e1", "event_type": "test", "project_id": "project-1", "report_path": "C:/secret/report.json"}],
            artifacts=[{
                "artifact_id": "artifact-1",
                "artifact_type": "prediction",
                "path": "C:/secret/output.json",
                "project_id": "project-1",
                "run_id": "old-run",
                "protocol_name": "prediction",
                "protocol_version": "1.4",
                "protocol_sha256": "existing-integrity-identity",
            }],
        )

        result = WorkbenchReader(store).read()

        self.assertEqual([call[0] for call in store.calls], ["get_state", "list", "query", "list_artifacts"])
        self.assertEqual(store.calls[2][1], {"project_id": "project-1"})
        artifact = result["artifacts"]["items"][0]
        self.assertNotIn("path", artifact)
        self.assertEqual(artifact["protocol"]["version"], "1.4")
        self.assertEqual(artifact["protocol"]["integrity_identity"], "existing-integrity-identity")
        self.assertNotIn("C:/secret", str(result))

    def test_path_shaped_messages_and_unsupported_content_links_are_redacted(self):
        store = FakeStore(
            state={"project_id": "project-1"},
            evidence=[{
                "event_id": "e1", "event_type": "test", "project_id": "project-1",
                "message": "failed at C:/server/private/report.json",
            }],
            artifacts=[{
                "artifact_id": "artifact-1", "artifact_type": "report", "project_id": "project-1",
                "content_link": "file:///server/private/report.json",
            }],
        )

        result = WorkbenchReader(store).read()

        self.assertNotIn("C:/server", str(result))
        self.assertNotIn("file://", str(result))
        self.assertNotIn("content_link", result["artifacts"]["items"][0])


if __name__ == "__main__":
    unittest.main()
