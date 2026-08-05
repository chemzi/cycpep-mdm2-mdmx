"""Execution Worker: claim, run and close reviewed Planner task handlers.

The Worker accepts an Orchestrator run path and task ID.  It never accepts a
shell command.  The claimed task's semantic ``action`` is resolved through the
fixed registry in :mod:`execution.handlers`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback as traceback_module
from typing import Any, Callable
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.orchestrator import (  # noqa: E402
    OrchestratorContractError,
    claim,
    complete,
    fail,
    status,
)
from data_layer import EvidenceLogger  # noqa: E402
from prediction_pipeline.contracts import file_sha256  # noqa: E402
from contracts.errors import ErrorInfo  # noqa: E402
from contracts.task import TaskStatus  # noqa: E402
from contracts.trace import TraceContext  # noqa: E402

from execution.config import ExecutionConfig  # noqa: E402
from execution.contracts import (  # noqa: E402
    EXECUTION_WORKER_VERSION,
    ExecutionContractError,
    assert_action_executable,
    validate_dispatch_packet,
)
from execution.action_registry import handler_for  # noqa: E402
from execution.handlers import HandlerContext  # noqa: E402
from execution.supervisor import atomic_json  # noqa: E402

from contracts.transaction import ErrorInfo as TransactionErrorInfo  # noqa: E402
from contracts.transaction import ErrorType, TransactionContext, TransactionStatus  # noqa: E402
from execution.commit_manager import CommitManager  # noqa: E402
from execution.results import ExecutionActionResult  # noqa: E402
from execution.staging import StagingArea  # noqa: E402
from storage.base import Store  # noqa: E402


class ExecutionFailure(RuntimeError):
    """Raised after a transaction has been failed and recorded."""

    def __init__(self, error: TransactionErrorInfo):
        super().__init__(error.message)
        self.error = error


ExecutionResult = ExecutionActionResult


class ExecutionWorker:
    """Run one typed action through staging, validation and atomic commit."""

    def __init__(self, store: Store, staging_root: str | Path, artifact_root: str | Path):
        self.store = store
        self.staging_root = Path(staging_root)
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
                raise TypeError(
                    "execution handler must return ExecutionActionResult, "
                    f"got {type(result).__name__}"
                )
            context.transition(TransactionStatus.VALIDATING)
            if validator is not None:
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
            error = self._error_info(context, exc)
            if context.status not in {TransactionStatus.FAILED, TransactionStatus.COMMITTED}:
                if context.status == TransactionStatus.ROLLED_BACK:
                    context.transition(TransactionStatus.FAILED)
                else:
                    context.transition(TransactionStatus.FAILED)
            staging.write_manifest("error.json", error.to_dict())
            staging.write_manifest("transaction.json", context.to_dict())
            self.store.record_task_failure(context=context.to_dict(), error=error.to_dict())
            self.store.append(self._event(context, "execution_failed", **error.to_dict()))
            raise ExecutionFailure(error) from exc

    @staticmethod
    def _error_info(context: TransactionContext, exc: Exception) -> TransactionErrorInfo:
        if isinstance(exc, TypeError):
            error_type = ErrorType.CONTRACT_ERROR
        elif isinstance(exc, ValueError):
            error_type = ErrorType.VALIDATION_ERROR
        elif isinstance(exc, OSError):
            error_type = ErrorType.IO_ERROR
        else:
            error_type = ErrorType.SYSTEM_ERROR
        error_code = exc.code if isinstance(exc, ExecutionContractError) else exc.__class__.__name__
        metadata = context.metadata
        return TransactionErrorInfo(
            error_code=error_code,
            error_type=error_type,
            message=str(exc) or exc.__class__.__name__,
            traceback=traceback_module.format_exc(limit=30),
            task_id=context.task_id,
            transaction_id=context.transaction_id,
            workflow_id=context.workflow_id,
            run_id=context.run_id,
            attempt_id=context.attempt_id,
            component="execution.worker",
            retryable=isinstance(exc, (TimeoutError, ConnectionError)),
            action_name=str(metadata.get("action_name", "")),
            agent_name=str(metadata.get("agent_name", "execution")),
            input_hash=str(metadata.get("input_hash", "")),
        )

    @staticmethod
    def _event(context: TransactionContext, event_type: str, **payload: Any) -> dict[str, Any]:
        return {
            "workflow_id": context.workflow_id,
            "run_id": context.run_id,
            "task_id": context.task_id,
            "agent": "execution",
            "event_type": event_type,
            "transaction_id": context.transaction_id,
            "attempt_id": context.attempt_id,
            **payload,
        }


def _read_packet(path: Path, expected_sha256: str) -> dict:
    if not path.is_file() or file_sha256(path) != expected_sha256:
        raise ExecutionContractError(
            "dispatch_file_hash_mismatch", "dispatch packet file is missing or changed"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ExecutionContractError(
            "dispatch_packet_malformed", f"invalid dispatch JSON: {path}"
        ) from exc
    return validate_dispatch_packet(value)


def execute_task(
    *,
    run_path: str | Path,
    task_id: str,
    worker_id: str,
    config: ExecutionConfig | None = None,
) -> dict:
    """Claim and execute exactly one ready task."""
    config = config or ExecutionConfig.from_environment()
    started = time.monotonic()
    claimed = claim(run_path=run_path, task_id=task_id, worker=worker_id)
    token = claimed["claim_token"]
    run_id = claimed["run"]["run_id"]
    attempt = int(claimed["run"]["tasks"][task_id]["attempts"])
    task_dir = config.task_dir(run_id, task_id, attempt)
    packet = None
    task = None
    action = "unknown"
    trace_context = TraceContext(
        project_id=str((claimed["run"].get("plan") or {}).get("project_id") or "unknown_project"),
        workflow_id=str(
            claimed["run"].get("workflow_id")
            or (claimed["run"].get("plan") or {}).get("workflow_id")
            or "legacy_" + run_id,
        ),
        run_id=run_id,
        plan_id=(claimed["run"].get("plan") or {}).get("plan_id"),
        task_id=task_id,
        attempt_id=TraceContext.attempt_id_for(task_id, attempt),
    )
    try:
        packet = _read_packet(
            Path(claimed["dispatch_packet_path"]),
            claimed["dispatch_packet_sha256"],
        )
        if packet.get("trace_context") is not None:
            trace_context = TraceContext.from_dict(packet["trace_context"])
        task = packet["task"]
        parameters = assert_action_executable(task)
        action = task["action"]
        handler = handler_for(action)
        if handler is None:
            raise ExecutionContractError(
                "execution_handler_missing", f"no handler registered for {action}"
            )
        task_dir.mkdir(parents=True, exist_ok=False)
        atomic_json(task_dir / "dispatch_snapshot.json", packet)
        atomic_json(task_dir / "execution_started.json", {
            "execution_worker_version": EXECUTION_WORKER_VERSION,
            "worker_id": worker_id,
            "run_id": packet["run_id"],
            "task_id": task_id,
            "action": action,
            "workflow_id": trace_context.workflow_id,
            "plan_id": trace_context.plan_id,
            "attempt_id": trace_context.attempt_id,
            "normalized_parameters": parameters,
            "started_monotonic_recorded": True,
        })
        EvidenceLogger.log("execution", "execution_task_started", {
            "run_id": packet["run_id"],
            "task_id": task_id,
            "action": action,
            "worker": worker_id,
            "task_dir": str(task_dir),
            "attempt": attempt,
        }, phase=task["phase"], trace_context=trace_context)
        outcome = handler(HandlerContext(packet=packet, config=config, task_dir=task_dir))
        if not isinstance(outcome, ExecutionActionResult):
            raise ExecutionContractError(
                "handler_result_contract",
                "execution handler must return ExecutionActionResult, "
                f"got {type(outcome).__name__}",
            )
        elapsed_seconds = max(0.0, time.monotonic() - started)
        output_values = [f"{role}={path}" for role, path in outcome.outputs]
        gpu_minutes = (
            elapsed_seconds / 60.0
            if task["resource_request"]["class"] == "gpu" else None
        )
        result = complete(
            run_path=run_path,
            task_id=task_id,
            claim_token=token,
            output_paths=output_values,
            gpu_minutes=gpu_minutes,
        )
        receipt = {
            "execution_worker_version": EXECUTION_WORKER_VERSION,
            "status": TaskStatus.SUCCEEDED.value,
            "run_id": packet["run_id"],
            "task_id": task_id,
            "action": action,
            "workflow_id": trace_context.workflow_id,
            "plan_id": trace_context.plan_id,
            "attempt": attempt,
            "attempt_id": trace_context.attempt_id,
            "elapsed_seconds": elapsed_seconds,
            "gpu_minutes": gpu_minutes,
            "outputs": result["run"]["tasks"][task_id].get("outputs") or [
                {"role": role, "path": str(path), "sha256": file_sha256(path)}
                for role, path in outcome.outputs
            ],
            "processes": list(outcome.processes),
            "orchestrator_status": result["run"]["status"],
        }
        atomic_json(task_dir / "execution_receipt.json", receipt)
        EvidenceLogger.log(
            "execution", "execution_task_completed", receipt,
            phase=task["phase"], trace_context=trace_context,
        )
        return receipt
    except BaseException as exc:
        elapsed_seconds = max(0.0, time.monotonic() - started)
        has_gpu_lease = bool(
            (claimed["run"].get("resources") or {}).get("gpu_lease")
        )
        gpu_minutes = (
            elapsed_seconds / 60.0
            if (task and task["resource_request"]["class"] == "gpu") or has_gpu_lease
            else None
        )
        task_dir.mkdir(parents=True, exist_ok=True)
        error_info = ErrorInfo.from_exception(
            exc,
            component="execution.worker",
            code=str(getattr(exc, "code", exc.__class__.__name__)),
        )
        failure = {
            "execution_worker_version": EXECUTION_WORKER_VERSION,
            "status": TaskStatus.FAILED.value,
            "run_id": run_id,
            "task_id": task_id,
            "action": action,
            "workflow_id": trace_context.workflow_id,
            "plan_id": trace_context.plan_id,
            "attempt": attempt,
            "attempt_id": trace_context.attempt_id,
            **error_info.to_dict(),
            "elapsed_seconds": elapsed_seconds,
            "gpu_minutes": gpu_minutes,
        }
        atomic_json(task_dir / "execution_failure.json", failure)
        try:
            fail(
                run_path=run_path,
                task_id=task_id,
                claim_token=token,
                reason=f"{failure['code']}: {failure['message']}",
                error_info=error_info,
                gpu_minutes=gpu_minutes,
            )
        except Exception as close_exc:
            failure["orchestrator_close_error"] = {
                "code": getattr(close_exc, "code", close_exc.__class__.__name__),
                "message": str(close_exc),
            }
            atomic_json(task_dir / "execution_failure.json", failure)
        EvidenceLogger.log(
            "execution", "execution_task_failed", failure,
            phase=task["phase"] if task else "iterate",
            trace_context=trace_context,
        )
        raise


def drain_run(
    *,
    run_path: str | Path,
    worker_id: str,
    config: ExecutionConfig | None = None,
) -> dict:
    """Execute ready core tasks in task-ID order until no ready task remains."""
    config = config or ExecutionConfig.from_environment()
    receipts = []
    while True:
        snapshot = status(run_path=run_path)["run"]
        ready = [
            task_id for task_id, value in sorted(snapshot["tasks"].items())
            if value["status"] == TaskStatus.READY.value
        ]
        if not ready:
            return {
                "execution_worker_version": EXECUTION_WORKER_VERSION,
                "run_id": snapshot["run_id"],
                "status": snapshot["status"],
                "receipts": receipts,
                "task_statuses": {
                    task_id: value["status"]
                    for task_id, value in sorted(snapshot["tasks"].items())
                },
            }
        task_id = ready[0]
        receipts.append(execute_task(
            run_path=run_path,
            task_id=task_id,
            worker_id=worker_id,
            config=config,
        ))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run-task", help="claim and execute one ready task")
    run.add_argument("--run", required=True)
    run.add_argument("--task", required=True)
    run.add_argument("--worker", default="execution-worker-01")
    drain = commands.add_parser("drain", help="execute ready tasks until the run waits/stops")
    drain.add_argument("--run", required=True)
    drain.add_argument("--worker", default="execution-worker-01")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "run-task":
            result = execute_task(
                run_path=args.run, task_id=args.task, worker_id=args.worker
            )
        elif args.command == "drain":
            result = drain_run(run_path=args.run, worker_id=args.worker)
        else:
            raise AssertionError(args.command)
    except (ExecutionContractError, OrchestratorContractError, OSError) as exc:
        print(json.dumps({
            "status": "error",
            "code": getattr(exc, "code", exc.__class__.__name__),
            "message": str(exc),
        }, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
