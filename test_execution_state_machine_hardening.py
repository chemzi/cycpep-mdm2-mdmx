"""Cross-layer invariants for terminal transaction and Orchestrator states."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from contracts.trace import TraceContext
from contracts.transaction import TransactionContext, TransactionStatus
from agents.orchestrator.completion import _post_completion_steps
from execution.results import ExecutionActionResult
from execution.worker import (
    ExecutionWorker,
    OrchestratorClosureUnresolved,
    RecoveryError,
    _TaskExecution,
    _close_orchestrator,
    _finalize_failure,
    _validate_action_result,
)
from storage import SQLiteStore


class ExecutionStateMachineHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="execution-state-hardening-"))

    @staticmethod
    def _committed_context() -> TransactionContext:
        context = TransactionContext.create(
            workflow_id="workflow-state",
            run_id="run-state",
            task_id="T001",
            attempt_id="T001-A01",
            action="propose_threshold_calibration",
            metadata={"orchestrator_run_path": "run.json", "project_id": "p1"},
        )
        for status in (
            TransactionStatus.STAGING,
            TransactionStatus.VALIDATING,
            TransactionStatus.COMMITTING,
            TransactionStatus.COMMITTED,
        ):
            context.transition(status)
        return context

    def test_rolled_back_and_conflicted_transactions_cannot_be_failed(self):
        rolled_back = self._committed_context()
        rolled_back.transition(TransactionStatus.ROLLED_BACK)
        with self.assertRaisesRegex(ValueError, "ROLLED_BACK -> FAILED"):
            rolled_back.transition(TransactionStatus.FAILED)

        conflicted = self._committed_context()
        conflicted.transition(TransactionStatus.COMPENSATION_CONFLICT)
        with self.assertRaisesRegex(ValueError, "COMPENSATION_CONFLICT -> FAILED"):
            conflicted.transition(TransactionStatus.FAILED)
        conflicted.transition(TransactionStatus.ROLLED_BACK)

    def test_ambiguous_commit_preserves_marker_and_blocks_retry(self):
        store = SQLiteStore(self.root / "ambiguous.db", project_id="p1")
        worker = ExecutionWorker(
            store, self.root / "staging", self.root / "artifacts"
        )
        context = TransactionContext.create(
            workflow_id="workflow-state",
            run_id="run-state",
            task_id="T001",
            attempt_id="T001-A01",
            action="propose_threshold_calibration",
        )
        with (
            patch.object(store, "commit_transaction", side_effect=OSError("db reset")),
            patch.object(
                store, "get_transaction_status", side_effect=OSError("probe reset")
            ),
            patch.object(store, "record_task_failure") as record_failure,
        ):
            with self.assertRaises(RecoveryError) as raised:
                worker.run(
                    context,
                    lambda *_: ExecutionActionResult(state_updates={"formal": True}),
                    validator=_validate_action_result,
                )

        marker = (
            self.root / "staging" / context.transaction_id / "metadata" / "commit.json"
        )
        self.assertTrue(marker.is_file())
        self.assertEqual(
            json.loads(marker.read_text(encoding="utf-8"))["status"],
            "RECOVERY_UNRESOLVED",
        )
        self.assertEqual(context.status, TransactionStatus.COMMITTING)
        self.assertFalse(raised.exception.retryable)
        record_failure.assert_not_called()

    def test_confirmed_orchestrator_success_wins_over_ambiguous_complete_error(self):
        context = self._committed_context()
        trace = TraceContext(
            project_id="p1",
            workflow_id="workflow-state",
            run_id="run-state",
            task_id="T001",
            attempt_id="T001-A01",
        )
        execution = _TaskExecution(
            packet={"run_id": "run-state"},
            task={"resource_request": {"class": "cpu"}},
            action="propose_threshold_calibration",
            transaction_context=context,
            trace_context=trace,
        )
        claimed = {
            "claim_token": "claim-token",
            "run": {"tasks": {"T001": {"attempts": 1}}},
        }
        succeeded = {
            "status": "succeeded",
            "tasks": {"T001": {"status": "succeeded", "outputs": []}},
        }
        with (
            patch("execution.worker.complete", side_effect=OSError("reply lost")),
            patch("execution.worker.probe_orchestrator_state", return_value="closed"),
            patch("execution.worker.status", return_value={"run": succeeded}),
        ):
            receipt = _close_orchestrator(
                execution,
                ExecutionActionResult(),
                claimed=claimed,
                run_path=self.root / "run.json",
                task_id="T001",
                started=0.0,
            )

        self.assertTrue(execution.orchestrator_closed)
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(
            receipt["orchestrator_completion_warnings"][0]["step"],
            "completion_outcome_probe",
        )

    def test_unknown_orchestrator_outcome_never_compensates_committed_store(self):
        context = self._committed_context()
        trace = TraceContext(
            project_id="p1",
            workflow_id="workflow-state",
            run_id="run-state",
            task_id="T001",
            attempt_id="T001-A01",
        )
        execution = _TaskExecution(
            packet={"run_id": "run-state"},
            task={"phase": "evaluate", "resource_request": {"class": "cpu"}},
            action="propose_threshold_calibration",
            transaction_context=context,
            trace_context=trace,
        )
        claimed = {
            "claim_token": "claim-token",
            "run": {
                "run_id": "run-state",
                "resources": {},
                "tasks": {"T001": {"attempts": 1}},
            },
        }
        with (
            patch("execution.worker.complete", side_effect=OSError("reply lost")),
            patch("execution.worker.probe_orchestrator_state", return_value="unknown"),
        ):
            with self.assertRaises(OrchestratorClosureUnresolved) as raised:
                _close_orchestrator(
                    execution,
                    ExecutionActionResult(),
                    claimed=claimed,
                    run_path=self.root / "run.json",
                    task_id="T001",
                    started=0.0,
                )

        worker = MagicMock()
        with (
            patch("execution.worker.fail") as fail_task,
            patch("execution.worker.EvidenceLogger.log") as log_evidence,
        ):
            _finalize_failure(
                exc=raised.exception,
                started=0.0,
                claimed=claimed,
                run_path=self.root / "run.json",
                task_id="T001",
                task=execution.task,
                action=execution.action,
                task_dir=self.root / "task",
                trace_context=trace,
                transaction_context=context,
                transaction_worker=worker,
                orchestrator_closed=False,
            )

        worker.rollback.assert_not_called()
        fail_task.assert_not_called()
        log_evidence.assert_not_called()
        worker.store.record_task_failure.assert_called_once()
        failure = json.loads(
            (self.root / "task" / "execution_failure.json").read_text(encoding="utf-8")
        )
        self.assertEqual(failure["status"], "transaction_recovery_unresolved")
        self.assertTrue(failure["integrity_unresolved"])

    def test_post_success_projection_failures_are_warnings(self):
        trace = TraceContext(
            project_id="p1",
            workflow_id="workflow-state",
            run_id="run-state",
            task_id="T001",
            attempt_id="T001-A01",
        )
        with (
            patch(
                "agents.orchestrator.completion._sync_state",
                side_effect=OSError("projection unavailable"),
            ),
            patch(
                "agents.orchestrator.completion.EvidenceLogger.log",
                side_effect=OSError("evidence unavailable"),
            ),
            patch("agents.orchestrator.completion._trace_for_run", return_value=trace),
        ):
            warnings = _post_completion_steps(
                release_global_gpu=False,
                claim_token="claim-token",
                run={"run_id": "run-state", "status": "succeeded"},
                plan={},
                task={"phase": "iterate"},
                task_id="T001",
                inventory=[],
                usage={},
                state={"attempts": 1},
            )

        self.assertEqual(
            [warning["step"] for warning in warnings],
            ["sync_state", "completion_evidence"],
        )


if __name__ == "__main__":
    unittest.main()
