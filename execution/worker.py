­r‡^Ñf¥–Ø¦{O,yÊ'vÃ®¶›­"""Controlled execution lifecycle around a handler supplied by the caller."""

from __future__ import annotations

from dataclasses import dataclass
import traceback
from typing import Any, Callable, Mapping

from contracts.transaction import ErrorInfo, TransactionContext, TransactionStatus
from .commit_manager import CommitManager
from .staging import StagedArtifact, StagingArea


class ExecutionFailure(RuntimeError):
    def __init__(self, error: ErrorInfo):
        super().__init__(error.message)
        self.error = error


@dataclass
class ExecutionActionResult:
    candidate_updates: list[Mapping[str, Any]]
    state_updates: dict[str, Any]
    artifacts: list[StagedArtifact]


ExecutionResult = ExecutionActionResult


class ExecutionWorker:
    def __init__(self, store: Any, staging_root: str, artifact_root: str):
        self.store = store
        self.staging_root = staging_root
        self.commit_manager = CommitManager(store, artifact_root)

    def run(
        self,
        context: TransactionContext,
        handler: Callable[[TransactionContext, StagingArea], ExecutionActionResult],
        *,
        validator: Callable[[ExecutionActionResult], None] | None = None,
    ) -> ExecutionActionResult:
        staging = StagingArea(self.staging_root, context.transaction_id).create()
        context.transition(TransactionStatus.STAGING)
        self.store.append(self._event(context, "execution_started"))
        try:
            result = handler(context, staging)
            if not isinstance(result, ExecutionActionResult):
                raise TypeError("execution handler must return ExecutionActionResult")
            context.transition(TransactionStatus.VALIDATING)
            if validator:
                validator(result)
            self.commit_manager.commit(
                context,
                candidate_updates=result.candidate_updates,
                state_updates=result.state_updates,
                artifacts=result.artifacts,
                staging_path=staging.path,
            )
            staging.discard()
            return result
        except Exception as exc:
            if context.status not in {TransactionStatus.ROLLED_BACK, TransactionStatus.FAILED}:
                context.transition(TransactionStatus.FAILED)
            retryable = isinstance(exc, (TimeoutError, ConnectionError))
            error = ErrorInfo(
                error_code="execution_failed", component="execution.worker",
                task_id=context.task_id, transaction_id=context.transaction_id,
                retryable=retryable, message=str(exc),
                workflow_id=context.workflow_id, attempt_id=context.attempt_id,
                action_name=str(context.metadata.get("action_name", "")),
                agent_name=str(context.metadata.get("agent_name", "execution")),
                stack_trace=traceback.format_exc(limit=20),
                input_hash=str(context.metadata.get("input_hash", "")),
            )
            staging.write_manifest("error.json", error.to_dict())
            staging.write_manifest("transaction.json", context.to_dict())
            record_failure = getattr(self.store, "record_task_failure", None)
            if record_failure:
                record_failure(context=context.to_dict(), error=error.to_dict())
            self.store.append(self._event(context, "execution_failed", **error.to_dict()))
            raise ExecutionFailure(error) from exc

    @staticmethod
    def _event(context: TransactionContext, event_type: str, **payload: Any) -> dict[str, Any]:
        return {"workflow_id": context.workflow_id, "run_id": context.run_id,
                "task_id": context.task_id, "agent": "execution",
                "event_type": event_type, "transaction_id": context.transaction_id,
                "attempt_id": context.attempt_id, **payload}
