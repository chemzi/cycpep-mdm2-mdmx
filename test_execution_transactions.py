≠rá^—f•ñÿ¶{O,y 'v√Æ∂õ≠"""Transaction boundary regression tests."""

import tempfile
import unittest
from pathlib import Path

from contracts.transaction import TransactionStatus
from execution import ExecutionFailure, ExecutionResult, ExecutionWorker, StagingArea
from storage import SQLiteStore


class ExecutionTransactionTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix="cycpep-tx-test-"))
        self.store = SQLiteStore(root / "project.db", project_id="p1")
        self.worker = ExecutionWorker(self.store, str(root / "staging"), str(root / "committed"))

    def context(self, attempt="attempt-1"):
        from contracts.transaction import TransactionContext
        return TransactionContext.create(workflow_id="w1", run_id="r1", task_id="t1", attempt_id=attempt)

    def test_success_commits_candidate_artifact_and_completed_after_started(self):
        context = self.context()

        def handler(ctx, staging):
            source = Path(staging.path) / "output.txt"
            source.write_text("result", encoding="utf-8")
            artifact = staging.stage_artifact(source, artifact_id="a1", artifact_type="result")
            return ExecutionResult([{"candidate_id": "C1", "sequence": "GFEW", "status": "ready"}], {}, [artifact])

        self.worker.run(context, handler)
        self.assertEqual(context.status, TransactionStatus.COMMITTED)
        self.assertEqual(self.store.get("C1")["status"], "ready")
        events = self.store.trace_task("t1")
        self.assertEqual([event["event_type"] for event in events], ["execution_started", "execution_completed"])

    def test_failure_rolls_back_formal_data_and_preserves_failure_trace(self):
        context = self.context()

        def handler(ctx, staging):
            source = Path(staging.path) / "output.txt"
            source.write_text("uncommitted", encoding="utf-8")
            raise RuntimeError("boom")

        with self.assertRaises(ExecutionFailure):
            self.worker.run(context, handler)
        self.assertIsNone(self.store.get("C1"))
        task = self.store._connect().execute("SELECT status FROM tasks WHERE task_id = 't1'").fetchone()
        self.assertEqual(task["status"], "FAILED_FINAL")
        self.assertEqual(self.store.trace_task("t1")[-1]["event_type"], "execution_failed")

    def test_retry_uses_same_task_and_new_attempt(self):
        first = self.context("attempt-1")
        with self.assertRaises(ExecutionFailure):
            self.worker.run(first, lambda *_: (_ for _ in ()).throw(TimeoutError("temporary")))
        second = self.context("attempt-2")
        second.task_id = first.task_id
        self.worker.run(second, lambda *_: ExecutionResult([], {}, []))
        events = self.store.trace_task(first.task_id)
        self.assertEqual(len(events), 4)
        self.assertEqual({event["attempt_id"] for event in events}, {"attempt-1", "attempt-2"})


if __name__ == "__main__":
    unittest.main()
