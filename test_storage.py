"""SQLite storage foundation tests; legacy files remain untouched."""

import json
import tempfile
import unittest
from pathlib import Path

from storage import SQLiteStore, migrate_json_to_sqlite
from storage.base import Store


class SQLiteStorageTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cycpep-storage-test-"))
        self.db = self.root / "project.db"
        self.store = SQLiteStore(self.db, project_id="p1")

    @staticmethod
    def _commit(
        store: SQLiteStore,
        transaction_id: str,
        *,
        workflow_id: str,
        run_id: str,
        task_id: str,
        attempt_id: str,
        created_at: str | None = None,
        metadata: dict | None = None,
        artifacts=(),
    ) -> None:
        context = {
            "transaction_id": transaction_id,
            "workflow_id": workflow_id,
            "run_id": run_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "action": "review_prediction_handoff",
            "status": "COMMITTING",
            "metadata": {"project_id": store.project_id, **(metadata or {})},
        }
        if created_at is not None:
            context["created_at"] = created_at
        store.commit_transaction(
            context=context,
            candidate_updates=(),
            state_updates={},
            state_appends=(),
            artifacts=artifacts,
        )

    def test_initialization_and_schema(self):
        tables = {row[0] for row in self.store._connect().execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertTrue(self.db.exists())
        self.assertTrue({"projects", "states", "candidates", "evidence_events", "artifacts", "workflow_runs", "tasks"} <= tables)

    def test_state_and_candidate_crud(self):
        self.assertEqual(self.store.get_state("p1"), {})
        state = self.store.update_state("p1", {"phase": "design", "round": 2})
        self.assertEqual(state["phase"], "design")
        self.assertEqual(self.store.get_state("p1")["round"], 2)
        self.store.upsert({"candidate_id": "C1", "sequence": "GFEW", "status": "pending", "metrics": {"score": 0.2}})
        self.store.upsert({"candidate_id": "C1", "sequence": "GFEW", "status": "finalized", "metrics": {"score": 0.9}})
        candidate = self.store.get("C1")
        self.assertEqual(candidate["status"], "finalized")
        self.assertEqual(self.store.list(status="finalized")[0]["candidate_id"], "C1")

    def test_evidence_is_append_only_and_traceable(self):
        self.store.append({"event_id": "e1", "workflow_id": "w1", "task_id": "t1", "candidate_id": "C1", "agent": "design", "event_type": "candidate_registered", "payload": {"ok": True}})
        self.store.append({"event_id": "e2", "workflow_id": "w1", "task_id": "t2", "candidate_id": "C1", "agent": "prediction", "event_type": "candidate_scored", "payload": {"score": 0.9}})
        self.store.append({"event_id": "e3", "project_id": "p1", "workflow_id": "w1", "event_type": "scoped"})
        SQLiteStore(self.db, project_id="p2").append({
            "event_id": "e4",
            "project_id": "p2",
            "workflow_id": "w1",
            "event_type": "scoped",
        })
        events = self.store.query(workflow_id="w1")
        self.assertEqual([event["event_id"] for event in events], ["e1", "e2", "e3", "e4"])
        self.assertEqual(
            [event["event_id"] for event in self.store.query(project_id="p1")],
            ["e3"],
        )
        self.assertEqual(len(self.store.query(candidate_id="C1")), 2)
        self.assertFalse(hasattr(self.store, "update_evidence"))
        self.assertFalse(hasattr(self.store, "delete_evidence"))

    def test_project_evidence_query_follows_formal_transaction_ownership(self):
        self._commit(
            self.store,
            "tx-p1-evidence",
            workflow_id="workflow-1",
            run_id="run-1",
            task_id="task-1",
            attempt_id="attempt-1",
        )
        other_store = SQLiteStore(self.db, project_id="p2")
        self._commit(
            other_store,
            "tx-p2-evidence",
            workflow_id="workflow-2",
            run_id="run-2",
            task_id="task-2",
            attempt_id="attempt-2",
        )

        events = self.store.query(project_id="p1")

        transaction_ids = {item.get("transaction_id") for item in events}
        self.assertIn("tx-p1-evidence", transaction_ids)
        self.assertNotIn("tx-p2-evidence", transaction_ids)
        self.assertEqual(self.store.query(project_id="p2"), [])

    def test_new_read_methods_do_not_break_existing_store_implementations(self):
        self.assertNotIn("list_artifacts", Store.__abstractmethods__)
        self.assertNotIn("get_transaction", Store.__abstractmethods__)
        self.assertNotIn("list_transactions", Store.__abstractmethods__)

    def test_contract_like_records_are_accepted(self):
        class ExecutionTask:
            task_id = "task-1"
            workflow_id = "workflow-1"
            action = "noop"
            status = "blocked_unimplemented"

        task = ExecutionTask()
        event_id = self.store.append({"task_id": task.task_id, "workflow_id": task.workflow_id, "event_type": task.status})
        self.assertTrue(event_id)

    def test_store_transaction_reads_are_project_scoped_filterable_and_domain_shaped(self):
        self._commit(
            self.store,
            "tx-later",
            workflow_id="workflow-2",
            run_id="run-2",
            task_id="task-2",
            attempt_id="attempt-2",
            created_at="2026-08-09T02:00:00+00:00",
        )
        self._commit(
            self.store,
            "tx-earlier",
            workflow_id="workflow-1",
            run_id="run-1",
            task_id="task-1",
            attempt_id="attempt-1",
            created_at="2026-08-09T01:00:00+00:00",
        )
        other_store = SQLiteStore(self.db, project_id="p2")
        self._commit(
            other_store,
            "tx-other-project",
            workflow_id="workflow-1",
            run_id="run-1",
            task_id="task-1",
            attempt_id="attempt-1",
            created_at="2026-08-09T00:00:00+00:00",
        )

        self.assertEqual(
            [item["transaction_id"] for item in self.store.list_transactions()],
            ["tx-earlier", "tx-later"],
        )
        transaction = self.store.get_transaction("tx-earlier")
        self.assertEqual(transaction["status"], "COMMITTED")
        self.assertEqual(transaction["project_id"], "p1")
        self.assertEqual(transaction["workflow_id"], "workflow-1")
        self.assertEqual(transaction["run_id"], "run-1")
        self.assertEqual(transaction["task_id"], "task-1")
        self.assertEqual(transaction["attempt_id"], "attempt-1")
        self.assertIn("updated_at", transaction)
        self.assertIsNone(self.store.get_transaction("tx-other-project"))
        self.assertEqual(
            [item["transaction_id"] for item in self.store.list_transactions(
                workflow_id="workflow-2",
                run_id="run-2",
                task_id="task-2",
                attempt_id="attempt-2",
            )],
            ["tx-later"],
        )

        self.assertEqual(
            [item["transaction_id"] for item in self.store.list_transactions(
                workflow_id="workflow-2"
            )],
            ["tx-later"],
        )
        self.assertEqual(
            [item["transaction_id"] for item in self.store.list_transactions(
                run_id="run-2"
            )],
            ["tx-later"],
        )
        self.assertEqual(
            [item["transaction_id"] for item in self.store.list_transactions(
                task_id="task-2"
            )],
            ["tx-later"],
        )
        self.assertEqual(
            [item["transaction_id"] for item in self.store.list_transactions(
                attempt_id="attempt-2"
            )],
            ["tx-later"],
        )

    def test_store_transaction_reads_preserve_failure_and_compensation_status(self):
        failure_context = {
            "transaction_id": "tx-failed",
            "workflow_id": "workflow-1",
            "run_id": "run-1",
            "task_id": "T099",
            "attempt_id": "T099-A01",
            "action": "review_prediction_handoff",
            "status": "STAGING",
            "metadata": {"project_id": "p1", "protocol_version": "v2"},
        }
        self.store.record_task_failure(
            context=failure_context,
            error={
                "code": "adapter_failed",
                "message": "adapter stopped",
                "component": "test",
                "retryable": False,
            },
        )
        failed = self.store.get_transaction("tx-failed")
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["error"]["code"], "adapter_failed")
        self.assertEqual(failed["metadata"]["protocol_version"], "v2")

        committing = {
            "transaction_id": "tx-compensate",
            "workflow_id": "workflow-1",
            "run_id": "run-1",
            "task_id": "T098",
            "attempt_id": "T098-A01",
            "action": "review_prediction_handoff",
            "status": "COMMITTING",
            "metadata": {"project_id": "p1"},
        }
        self.store.commit_transaction(
            context=committing,
            candidate_updates=(),
            state_updates={"owned": "transaction"},
            state_appends=(),
            artifacts=(),
        )
        self.store.update_state("p1", {"owned": "later writer"})
        self.store.rollback_transaction("tx-compensate")
        self.assertEqual(
            self.store.get_transaction("tx-compensate")["status"],
            "COMPENSATION_CONFLICT",
        )

        rolled_back = dict(
            committing,
            transaction_id="tx-rolled-back",
            task_id="T097",
            attempt_id="T097-A01",
        )
        self.store.commit_transaction(
            context=rolled_back,
            candidate_updates=(),
            state_updates={"temporary": True},
            state_appends=(),
            artifacts=(),
        )
        self.store.rollback_transaction("tx-rolled-back")
        self.assertEqual(
            self.store.get_transaction("tx-rolled-back")["status"], "ROLLED_BACK"
        )

    def test_store_artifact_reads_follow_formal_project_transaction_linkage(self):
        self.store.register_artifact({
            "artifact_id": "artifact-unlinked",
            "artifact_type": "legacy",
            "path": "legacy/unlinked.json",
        })
        self._commit(
            self.store,
            "tx-p1",
            workflow_id="workflow-1",
            run_id="run-1",
            task_id="task-1",
            attempt_id="attempt-1",
            metadata={"protocol_version": "v1"},
            artifacts=(
                {
                    "artifact_id": "artifact-b",
                    "artifact_type": "report",
                    "path": "artifacts/b.json",
                    "size_bytes": 20,
                    "sha256": "digest-b",
                },
                {
                    "artifact_id": "artifact-a",
                    "artifact_type": "report",
                    "path": "artifacts/a.json",
                    "size_bytes": 10,
                    "sha256": "digest-a",
                },
            ),
        )
        other_store = SQLiteStore(self.db, project_id="p2")
        self._commit(
            other_store,
            "tx-p2",
            workflow_id="workflow-2",
            run_id="run-2",
            task_id="task-2",
            attempt_id="attempt-2",
            artifacts=({
                "artifact_id": "artifact-other-project",
                "artifact_type": "report",
                "path": "artifacts/other.json",
            },),
        )

        projection = self.root / "artifacts.json"
        projection.write_text('[{"artifact_id":"projection-only"}]', encoding="utf-8")
        artifacts = self.store.list_artifacts()

        self.assertEqual(
            [item["artifact_id"] for item in artifacts],
            ["artifact-a", "artifact-b"],
        )
        self.assertEqual(artifacts[0]["project_id"], "p1")
        self.assertEqual(artifacts[0]["transaction_id"], "tx-p1")
        self.assertEqual(artifacts[0]["workflow_id"], "workflow-1")
        self.assertEqual(artifacts[0]["run_id"], "run-1")
        self.assertEqual(artifacts[0]["task_id"], "task-1")
        self.assertEqual(artifacts[0]["attempt_id"], "attempt-1")
        self.assertEqual(artifacts[0]["metadata"]["protocol_version"], "v1")
        self.assertNotIn("artifact-unlinked", {item["artifact_id"] for item in artifacts})
        self.assertNotIn(
            "artifact-other-project", {item["artifact_id"] for item in artifacts}
        )
        self.assertEqual(
            projection.read_text(encoding="utf-8"),
            '[{"artifact_id":"projection-only"}]',
        )

    def test_fake_workflow_is_traceable(self):
        workflow_id = "workflow-fake-1"
        self.store.append({"event_id": "plan-1", "workflow_id": workflow_id, "task_id": "task-1", "agent": "planner", "event_type": "planned"})
        self.store.append({"event_id": "done-1", "workflow_id": workflow_id, "task_id": "task-1", "agent": "execution", "event_type": "completed"})
        self.assertEqual(len(self.store.trace_workflow(workflow_id)), 2)
        self.assertEqual(self.store.trace_task("task-1")[1]["event_type"], "completed")

    def test_legacy_migration_is_idempotent_and_preserves_sources(self):
        state = self.root / "state.json"
        candidates = self.root / "candidate_index.csv"
        evidence = self.root / "evidence_log.jsonl"
        state.write_text(json.dumps({"phase": "research", "round": 1}), encoding="utf-8")
        candidates.write_text("candidate_id,sequence,final_status\nC1,GFEW,pending\n", encoding="utf-8")
        evidence.write_text(json.dumps({"event_id": "e1", "event_type": "test"}) + "\n", encoding="utf-8")
        first = migrate_json_to_sqlite(db_path=self.db, state_path=state, candidate_path=candidates, evidence_path=evidence, project_id="p1")
        second = migrate_json_to_sqlite(db_path=self.db, state_path=state, candidate_path=candidates, evidence_path=evidence, project_id="p1")
        self.assertEqual(first["states"], 1)
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(len(self.store.query()), 1)
        self.assertEqual(second["candidates"], 1)
        self.assertTrue(state.exists() and candidates.exists() and evidence.exists())


if __name__ == "__main__":
    unittest.main()
