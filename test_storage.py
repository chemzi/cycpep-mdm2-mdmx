"""SQLite storage foundation tests; legacy files remain untouched."""

import json
import tempfile
import unittest
from pathlib import Path

from storage import SQLiteStore, migrate_json_to_sqlite


class SQLiteStorageTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cycpep-storage-test-"))
        self.db = self.root / "project.db"
        self.store = SQLiteStore(self.db, project_id="p1")

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
        events = self.store.query(workflow_id="w1")
        self.assertEqual([event["event_id"] for event in events], ["e1", "e2"])
        self.assertEqual(len(self.store.query(candidate_id="C1")), 2)
        self.assertFalse(hasattr(self.store, "update_evidence"))
        self.assertFalse(hasattr(self.store, "delete_evidence"))

    def test_contract_like_records_are_accepted(self):
        class ExecutionTask:
            task_id = "task-1"
            workflow_id = "workflow-1"
            action = "noop"
            status = "blocked_unimplemented"

        task = ExecutionTask()
        event_id = self.store.append({"task_id": task.task_id, "workflow_id": task.workflow_id, "event_type": task.status})
        self.assertTrue(event_id)

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
