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
from pathlib import Path
from typing import Callable


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
from data_layer import (  # noqa: E402
    EvidenceLogger,
    get_storage_backend,
    refresh_projections,
)
from prediction_pipeline.contracts import file_sha256  # noqa: E402
from contracts.errors import ErrorInfo  # noqa: E402
from contracts.task import TaskStatus  # noqa: E402
from contracts.trace import TraceContext  # noqa: E402
from contracts.transaction import TransactionContext, TransactionStatus  # noqa: E402

from execution.config import ExecutionConfig  # noqa: E402
from execution.contracts import (  # noqa: E402
    EXECUTION_WORKER_VERSION,
    ExecutionContractError,
    assert_action_executable,
    validate_dispatch_packet,
)
from execution.action_registry import handler_for  # noqa: E402
from execution.adapters import adapter_for  # noqa: E402
from execution.commit_manager import CommitManager  # noqa: E402
from execution.results import ExecutionActionResult  # noqa: E402
from execution.staging import StagingArea  # noqa: E402
from storage.base import Store  # noqa: E402
from execution.supervisor import atomic_json  # noqa: E402


class ExecutionFailure(RuntimeError):
    pass


class ExecutionWorker:
    """Run one action through isolated staging and atomic formal commit."""

    def __init__(self, store: Store, staging_root: Path, artifact_root: Path):
        self.store = store
        self.staging_root = staging_root
        self.commit_manager = CommitManager(store, artifact_root)
        self._staging: dict[str, StagingArea] = {}

    def run(
        self,
        context: TransactionContext,
        handler: Callable[[TransactionContext, StagingArea], ExecutionActionResult],
        *,
        validator: Callable[[ExecutionActionResult], None],
    ) -> ExecutionActionResult:
        staging = StagingArea(self.staging_root, context.transaction_id).create()
        self._staging[context.transaction_id] = staging
        context.transition(TransactionStatus.STAGING)
        try:
            result = handler(context, staging)
            if not isinstance(result, ExecutionActionResult):
                raise TypeError(
                    f"execution handler returned {type(result).__name__}, expected ExecutionActionResult"
                )
            context.transition(TransactionStatus.VALIDATING)
            validator(result)
            self.commit_manager.commit(
                context,
                candidate_updates=result.candidate_updates,
                state_updates=result.state_updates,
                artifacts=result.artifacts,
                staging_path=staging.path,
            )
            return result
        except BaseException as exc:
            if context.status == TransactionStatus.ROLLED_BACK:
                context.transition(TransactionStatus.FAILED)
            elif context.status in {TransactionStatus.STAGING, TransactionStatus.VALIDATING}:
                context.transition(TransactionStatus.FAILED)
            self.store.record_task_failure(
                context=context.to_dict(),
                error=ErrorInfo.from_exception(exc, component="execution.worker").to_dict(),
            )
            staging.discard()
            self._staging.pop(context.transaction_id, None)
            raise

    def rollback(self, context: TransactionContext) -> None:
        staging = self._staging[context.transaction_id]
        self.commit_manager.rollback_committed(context, staging.path)
        refresh_projections()

    def finalize(self, context: TransactionContext) -> None:
        staging = self._staging.pop(context.transaction_id, None)
        if staging is not None:
            staging.discard()


def _validate_action_result(result: ExecutionActionResult) -> None:
    roles = [role for role, _ in result.outputs]
    if len(roles) != len(set(roles)):
        raise ExecutionContractError("task_output_contract_invalid", "output roles must be unique")
    for role, path in result.outputs:
        if not role or not Path(path).is_file():
            raise ExecutionContractError(
                "task_output_contract_invalid", f"output is missing: {role}={path}"
            )


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
    transaction_context = None
    transaction_worker = None
    orchestrator_closed = False
    trace_context = _build_trace_context(claimed, run_id, task_id, attempt)
    try:
        packet, task, parameters, action, handler, trace_context = _load_packet(
            claimed, trace_context
        )
        transaction_context, transaction_worker = _open_transaction(
            trace_context, packet, task_id, action, config, task_dir
        )
        _record_task_start(
            task_dir, packet, task, worker_id, task_id, action,
            trace_context, transaction_context, parameters, attempt,
        )
        adapter = adapter_for(
            action,
            handler,
            packet,
            config,
            task_dir,
            None,
        )
        outcome = transaction_worker.run(
            transaction_context,
            adapter,
            validator=_validate_action_result,
        )
        refresh_projections()
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
        orchestrator_closed = True
        receipt = _build_success_receipt(
            packet, task, task_id, attempt, trace_context,
            transaction_context, elapsed_seconds, gpu_minutes, result, outcome,
        )
        atomic_json(task_dir / "execution_receipt.json", receipt)
        EvidenceLogger.log(
            "execution", "execution_task_completed", receipt,
            phase=task["phase"], trace_context=trace_context,
        )
        transaction_worker.finalize(transaction_context)
        return receipt
    except BaseException as exc:
        _record_execution_failure(
            exc,
            claimed=claimed,
            token=token,
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            task_dir=task_dir,
            task=task,
            action=action,
            transaction_context=transaction_context,
            transaction_worker=transaction_worker,
            trace_context=trace_context,
            started=started,
            orchestrator_closed=orchestrator_closed,
            run_path=run_path,
        )
        raise


def _build_trace_context(claimed: dict, run_id: str, task_id: str, attempt: int) -> TraceContext:
    return TraceContext(
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


def _load_packet(
    claimed: dict, trace_context: TraceContext
) -> tuple[dict, dict, dict, str, object, TraceContext]:
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
    return packet, task, parameters, action, handler, trace_context


def _open_transaction(
    trace_context: TraceContext,
    packet: dict,
    task_id: str,
    action: str,
    config: ExecutionConfig,
    task_dir: Path,
) -> tuple[TransactionContext, ExecutionWorker]:
    task_dir.mkdir(parents=True, exist_ok=False)
    transaction_context = TransactionContext.create(
        workflow_id=trace_context.workflow_id,
        run_id=packet["run_id"],
        task_id=task_id,
        attempt_id=trace_context.attempt_id,
        action=action,
    )
    transaction_worker = ExecutionWorker(
        get_storage_backend(),
        config.execution_root / ".staging",
        config.execution_root / "artifacts",
    )
    transaction_worker.commit_manager.recover_pending(
        config.execution_root / ".staging"
    )
    return transaction_context, transaction_worker


def _record_task_start(
    task_dir: Path,
    packet: dict,
    task: dict,
    worker_id: str,
    task_id: str,
    action: str,
    trace_context: TraceContext,
    transaction_context: TransactionContext,
    parameters: dict,
    attempt: int,
) -> None:
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
        "transaction_id": transaction_context.transaction_id,
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


def _build_success_receipt(
    packet: dict,
    task: dict,
    task_id: str,
    attempt: int,
    trace_context: TraceContext,
    transaction_context: TransactionContext,
    elapsed_seconds: float,
    gpu_minutes: float | None,
    result: dict,
    outcome,
) -> dict:
    return {
        "execution_worker_version": EXECUTION_WORKER_VERSION,
        "status": TaskStatus.SUCCEEDED.value,
        "run_id": packet["run_id"],
        "task_id": task_id,
        "action": task["action"],
        "workflow_id": trace_context.workflow_id,
        "plan_id": trace_context.plan_id,
        "attempt": attempt,
        "attempt_id": trace_context.attempt_id,
        "transaction_id": transaction_context.transaction_id,
        "elapsed_seconds": elapsed_seconds,
        "gpu_minutes": gpu_minutes,
        "outputs": result["run"]["tasks"][task_id].get("outputs") or [
            {"role": role, "path": str(path), "sha256": file_sha256(path)}
            for role, path in outcome.outputs
        ],
        "processes": list(outcome.processes),
        "orchestrator_status": result["run"]["status"],
    }


def _record_execution_failure(
    exc: BaseException,
    *,
    claimed: dict,
    token: str,
    run_id: str,
    task_id: str,
    attempt: int,
    task_dir: Path,
    task: dict | None,
    action: str,
    transaction_context,
    transaction_worker,
    trace_context: TraceContext,
    started: float,
    orchestrator_closed: bool,
    run_path: str | Path,
) -> None:
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
        "transaction_id": (
            transaction_context.transaction_id if transaction_context else ""
        ),
        **error_info.to_dict(),
        "elapsed_seconds": elapsed_seconds,
        "gpu_minutes": gpu_minutes,
    }
    if (
        transaction_worker is not None
        and transaction_context is not None
        and transaction_context.status == TransactionStatus.COMMITTED
        and not orchestrator_closed
    ):
        transaction_worker.rollback(transaction_context)
    atomic_json(task_dir / "execution_failure.json", failure)
    if not orchestrator_closed:
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
