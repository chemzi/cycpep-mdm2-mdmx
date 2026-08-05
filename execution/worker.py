­r‡^Ñf¥–Ø¦{O,yÊ'vÃ®¶›­"""Controlled execution lifecycle around a handler supplied by the caller."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from contracts.transaction import ErrorInfo, TransactionContext, TransactionStatus
from .commit_manager import CommitManager
from .staging import StagedArtifact, StagingArea


class ExecutionFailure(RuntimeError):
    def __init__(self, error: ErrorInfo):
        super().__init__(error.message)
        self.error = error


@dataclass
class ExecutionResult:
    candidate_updates: list[Mapping[str, Any]]
    state_updates: dict[str, Any]
    artifacts: list[StagedArtifact]


class ExecutionWorker:
    def __init__(self, store: Any, staging_root: str, artifact_root: str):
        self.store = store
        self.staging_root = staging_root
        self.commit_manager = CommitManager(store, artifact_root)

    def run(
        self,
        context: TransactionContext,
        handler: Callable[[TransactionContext, StagingArea], ExecutionResult | Mapping[str, Any]],
        *,
        validator: Callable[[ExecutionResult], None] | None = None,
    ) -> ExecutionResult:
        staging = StagingArea(self.staging_root, context.transaction_id).create()
        context.transition(TransactionStatus.STAGING)
        self.store.append(self._event(context, "execution_started"))
        try:
            result = handler(context, staging)
            if not isinstance(result, ExecutionResult):
                result = ExecutionResult(
                    candidate_updates=list(result.get("candidate_updates", [])),
                    state_updates=dict(result.get("state_updates", {})),
                    artifacts=list(result.get("artifacts", [])),
                )
            context.transition(TransactionStatus.VALIDATING)
            if validator:
                validator(result)
            self.commit_manager.commit(
                context,
                candidate_updates=result.candidate_updates,
                state_updates=result.state_updates,
                artifacts=result.artifacts,
            )
            staging.discard()
            return result
        except Exception as exc:
            context.transition(TransactionStatus.FAILED)
            retryable = isinstance(exc, (TimeoutError, ConnectionError))
            error = ErrorInfo(
                error_code="execution_failed", component="execution.worker",
                task_id=context.task_id, transaction_id=context.transaction_id,
                retryable=retryable, message=str(exc),
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
