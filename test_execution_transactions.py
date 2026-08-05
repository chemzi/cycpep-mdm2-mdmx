≠rá^—f•ñÿ¶{Oly 'v√Æ∂õ≠"""Transaction boundary regression tests."""

import tempfile
import unittest
import json
from pathlib import Path

from contracts.transaction import TransactionContext, TransactionStatus
from execution import CommitManager, ExecutionActionResult, ExecutionFailure, ExecutionResult, ExecutionWorker
from storage import SQLiteStore


class ExecutionTransactionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cycpep-tx-test-"))
        self.store = SQLiteStore(self.root / "project.db", project_id="p1")
        self.worker = ExecutionWorker(self.store, str(self.root / "staging"), str(self.root / "committed"))

    def context(self, attempt="attempt-1", task_id="t1"):
        return TransactionContext.create(workflow_id="w1", run_id="r1", task_id=task_id, attempt_id=attempt)

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
        self.assertIsNotNone(self.store.get_artifact("a1"))
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
        second = self.context("attempt-2", first.task_id)
        self.worker.run(second, lambda *_: ExecutionResult([], {}, []))
        events = self.store.trace_task(first.task_id)
        self.assertEqual(len(events), 4)
        self.assertEqual({event["attempt_id"] for event in events}, {"attempt-1", "attempt-2"})

    def test_transaction_state_machine_rejects_invalid_transition_and_id_mutation(self):
        context = self.context()
        with self.assertRaises(ValueError):
            context.transition(TransactionStatus.COMMITTED)
        with self.assertRaises((AttributeError, TypeError)):
            context.task_id = "other"

    def test_mapping_result_is_rejected_and_error_is_traceable(self):
        context = self.context()
        context.metadata.update({"action_name": "demo", "agent_name": "design", "input_hash": "abc"})
        with self.assertRaises(ExecutionFailure) as raised:
            self.worker.run(context, lambda *_: {"candidate_updates": []})
        error = raised.exception.error
        self.assertEqual(error.action_name, "demo")
        self.assertEqual(error.agent_name, "design")
        self.assertEqual(error.input_hash, "abc")
        self.assertTrue(error.stack_trace)

    def test_sqlite_commit_failure_removes_atomic_artifact(self):
        class FailingStore:
            def __init__(self, delegate):
                self.delegate = delegate

            def append(self, event):
                return self.delegate.append(event)

            def record_task_failure(self, **kwargs):
                return self.delegate.record_task_failure(**kwargs)

            def commit_transaction(self, **kwargs):
                raise RuntimeError("database unavailable")

        worker = ExecutionWorker(FailingStore(self.store), str(self.root / "staging-fail"), str(self.root / "committed-fail"))
        context = self.context()

        def handler(ctx, staging):
            source = Path(staging.path) / "output.txt"
            source.write_text("result", encoding="utf-8")
            return ExecutionResult([], {}, [staging.stage_artifact(source, artifact_id="a-fail", artifact_type="result")])

        with self.assertRaises(ExecutionFailure):
            worker.run(context, handler)
        self.assertFalse(list((self.root / "committed-fail").rglob("output.txt")))

    def test_recovery_removes_unregistered_files_from_pending_marker(self):
        manager = CommitManager(self.store, self.root / "committed-recovery")
        transaction_dir = self.root / "staging-recovery" / "tx-crashed"
        transaction_dir.mkdir(parents=True)
        committed = self.root / "committed-recovery" / "w1" / "t1" / "a-crash" / "output.txt"
        committed.parent.mkdir(parents=True)
        committed.write_text("orphan", encoding="utf-8")
        temporary = committed.with_name(".output.txt.tx-crashed.tmp")
        temporary.write_text("temporary", encoding="utf-8")
        (transaction_dir / "commit.json").write_text(json.dumps({
            "transaction_id": "tx-crashed", "status": "PREPARED",
            "artifacts": [{"artifact_id": "a-crash", "path": str(committed), "temporary": str(temporary)}],
        }), encoding="utf-8")

        self.assertEqual(manager.recover_pending(self.root / "staging-recovery"), ["tx-crashed"])
        self.assertFalse(committed.exists())
        self.assertFalse(temporary.exists())


if __name__ == "__main__":
    unittest.main()
