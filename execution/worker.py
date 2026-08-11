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
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping


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
from contracts.event import (  # noqa: E402
    EvidenceEvent,
    VALID_AGENTS,
    VALID_EVENT_TYPES,
    VALID_PHASES,
)
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
from execution.recovery import CLOSED, UNKNOWN, probe_orchestrator_state  # noqa: E402
from execution.results import ExecutionActionResult  # noqa: E402
from execution.staging import StagingArea  # noqa: E402
from storage.base import Store  # noqa: E402
from execution.supervisor import atomic_json  # noqa: E402


class ExecutionFailure(RuntimeError):
    pass


class RecoveryError(ExecutionFailure):
    """Recovery left the store in an unresolved state; refuse to start new work."""

    def __init__(self, message: str, *, unresolved=(), marker_errors=()):
        super().__init__(message)
        self.code = "transaction_recovery_unresolved"
        self.retryable = False
        self.unresolved = tuple(unresolved)
        self.marker_errors = tuple(marker_errors)


class OrchestratorClosureUnresolved(RecoveryError):
    """The completion call failed and its durable Orchestrator outcome is unknown."""

    orchestrator_outcome_unknown = True


def _assert_recovery_clean(recovery) -> None:
    """Fail closed when a recovery pass left anything unresolved."""
    if not recovery.clean:
        raise RecoveryError(
            "unresolved transaction recovery state; refusing to start a new "
            f"transaction (unresolved={list(recovery.unresolved)}, "
            f"marker_errors={list(recovery.marker_errors)})",
            unresolved=recovery.unresolved,
            marker_errors=recovery.marker_errors,
        )


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
        trace_context: TraceContext | None = None,
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
            formal_events = self._formal_events(context, result, trace_context)
            commit_result = self.commit_manager.commit(
                context,
                candidate_updates=result.candidate_updates,
                candidate_patches=result.candidate_patches,
                state_updates=result.state_updates,
                state_appends=result.state_appends,
                artifacts=result.artifacts,
                evidence_events=formal_events,
                staging_path=staging.path,
            )
            committed_by_role = {
                str(item.get("artifact_type")): Path(str(item["path"]))
                for item in commit_result.artifacts
            }
            return replace(result, outputs=tuple(
                (role, committed_by_role.get(role, path))
                for role, path in result.outputs
            ))
        except BaseException as exc:
            ambiguous_commit = context.status == TransactionStatus.COMMITTING
            preserve_recovery_marker = context.status in {
                TransactionStatus.COMMITTING,
                TransactionStatus.COMMITTED,
                TransactionStatus.COMPENSATION_CONFLICT,
            }
            if context.status in {TransactionStatus.STAGING, TransactionStatus.VALIDATING}:
                context.transition(TransactionStatus.FAILED)
            if not ambiguous_commit:
                self.store.record_task_failure(
                    context=context.to_dict(),
                    error=ErrorInfo.from_exception(
                        exc, component="execution.worker"
                    ).to_dict(),
                )
            if not preserve_recovery_marker:
                staging.discard()
                self._staging.pop(context.transaction_id, None)
            if ambiguous_commit:
                raise RecoveryError(
                    "transaction commit outcome is unresolved; recovery must "
                    f"reconcile {context.transaction_id} before retry",
                    unresolved=(context.transaction_id,),
                ) from exc
            raise

    @staticmethod
    def _formal_events(
        context: TransactionContext,
        result: ExecutionActionResult,
        trace_context: TraceContext | None,
    ) -> tuple[dict, ...]:
        if not result.evidence_events:
            return ()
        trace = trace_context or TraceContext(
            project_id=str((context.metadata or {}).get("project_id") or "unknown_project"),
            workflow_id=context.workflow_id,
            run_id=context.run_id,
            plan_id=(context.metadata or {}).get("plan_id"),
            task_id=context.task_id,
            attempt_id=context.attempt_id,
        )
        return _formalize_evidence_events(tuple(result.evidence_events), trace)

    def rollback(self, context: TransactionContext) -> None:
        staging = self._staging[context.transaction_id]
        self.commit_manager.rollback_committed(context, staging.path)
        refresh_projections()

    def finalize(self, context: TransactionContext) -> None:
        staging = self._staging.pop(context.transaction_id, None)
        if staging is not None:
            staging.discard()


def _validate_action_result(result: ExecutionActionResult) -> None:
    patch_ids = [mutation.candidate_id for mutation in result.candidate_patches]
    if any(not candidate_id for candidate_id in patch_ids):
        raise ExecutionContractError(
            "candidate_patch_invalid", "candidate patch requires candidate_id"
        )
    if len(patch_ids) != len(set(patch_ids)):
        raise ExecutionContractError(
            "candidate_patch_invalid", "candidate patches must target unique candidates"
        )
    roles = [role for role, _ in result.outputs]
    if len(roles) != len(set(roles)):
        raise ExecutionContractError("task_output_contract_invalid", "output roles must be unique")
    for role, path in result.outputs:
        if not role or not Path(path).is_file():
            raise ExecutionContractError(
                "task_output_contract_invalid", f"output is missing: {role}={path}"
            )
    for event in result.evidence_events:
        if event.get("agent") not in VALID_AGENTS:
            raise ExecutionContractError(
                "task_evidence_contract_invalid", "evidence event has an unknown agent"
            )
        if event.get("event_type") not in VALID_EVENT_TYPES:
            raise ExecutionContractError(
                "task_evidence_contract_invalid", "evidence event has an unknown type"
            )
        if event.get("phase") not in VALID_PHASES:
            raise ExecutionContractError(
                "task_evidence_contract_invalid", "evidence event has an unknown phase"
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


def _orchestrator_state_for_transaction(context: Mapping[str, object]) -> str:
    """Three-state closure probe: CLOSED / OPEN / UNKNOWN.

    UNKNOWN (unreadable run snapshot, parse error, missing run path) must never
    be treated as "not closed" -- an unreadable snapshot is not evidence that
    the owner is dead, so recovery must refuse to compensate on it.
    """
    return probe_orchestrator_state(context)


def _formalize_evidence_events(
    events: tuple[Mapping[str, object], ...],
    trace_context: TraceContext,
) -> tuple[dict, ...]:
    """Promote transactional evidence dicts to the full EvidenceEvent contract.

    The transaction path used to bypass ``EvidenceEvent`` validation entirely
    (only agent/event_type/phase were checked).  Every committed event now goes
    through the same contract as ``EvidenceLogger`` so the ledger has a single
    write standard.
    """
    formalized = []
    for event in events:
        value = dict(event)
        value.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        value.setdefault("event_id", uuid.uuid4().hex[:12])
        if "phase" not in value or value.get("phase") is None:
            value.pop("phase", None)
        merged = {**value, **trace_context.to_dict()}
        # Raises ValueError on any contract violation; commit must not proceed.
        EvidenceEvent.from_dict(merged)
        formalized.append(merged)
    return tuple(formalized)


def _post_commit_finalize(
    *,
    task_dir: Path,
    receipt: dict,
    task: Mapping[str, object],
    trace_context: TraceContext,
    transaction_worker: "ExecutionWorker",
    transaction_context: TransactionContext,
) -> None:
    """Post-commit bookkeeping: receipt, completion evidence, staging cleanup.

    Runs strictly AFTER the irreversible success boundary (Orchestrator
    ``complete()``).  Any failure here is recorded on the receipt as a
    ``post_commit_warning`` and never raised, so an already-committed task is
    never reported to the caller as a retryable execution failure.
    """
    warnings: list[dict[str, str]] = []

    def _note(step: str, exc: BaseException) -> None:
        warnings.append({
            "step": step,
            "code": getattr(exc, "code", exc.__class__.__name__),
            "message": str(exc),
        })

    try:
        atomic_json(task_dir / "execution_receipt.json", receipt)
    except BaseException as exc:  # receipt is diagnostic; never fatal post-commit
        _note("execution_receipt", exc)
    try:
        EvidenceLogger.log(
            "execution", "execution_task_completed", receipt,
            phase=task["phase"], trace_context=trace_context,
        )
    except BaseException as exc:
        _note("completion_evidence", exc)
    try:
        transaction_worker.finalize(transaction_context)
    except BaseException as exc:
        _note("staging_cleanup", exc)
    if warnings:
        receipt["post_commit_warnings"] = warnings
        try:
            atomic_json(task_dir / "execution_receipt.json", receipt)
        except BaseException as exc:
            EvidenceLogger.error(
                "execution", "receipt_persist_failed", str(exc),
                recovery="post-commit warnings were not persisted",
            )


def _finalize_failure(
    *,
    exc: BaseException,
    started: float,
    claimed: Mapping[str, object],
    run_path: str | Path,
    task_id: str,
    task: Mapping[str, object] | None,
    action: str,
    task_dir: Path,
    trace_context: TraceContext,
    transaction_context: TransactionContext | None,
    transaction_worker: "ExecutionWorker | None",
    orchestrator_closed: bool,
) -> None:
    """Failure teardown: compensate (only if still possible), close, record.

    Compensation runs ONLY when the transaction committed but the Orchestrator
    was never closed -- after ``complete()`` the task is a formal success and
    this path is unreachable by construction.
    """
    run_id = claimed["run"]["run_id"]
    attempt = int(claimed["run"]["tasks"][task_id]["attempts"])
    token = claimed["claim_token"]
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
    closure_outcome_unknown = bool(
        getattr(exc, "orchestrator_outcome_unknown", False)
    )
    if closure_outcome_unknown:
        failure["status"] = "transaction_recovery_unresolved"
        failure["integrity_unresolved"] = True
    orchestrator_error = error_info
    if (
        transaction_worker is not None
        and transaction_context is not None
        and transaction_context.status == TransactionStatus.COMMITTED
        and not orchestrator_closed
        and not closure_outcome_unknown
    ):
        try:
            transaction_worker.rollback(transaction_context)
        except BaseException as rollback_exc:
            failure["original_error"] = error_info.to_dict()
            failure["compensation_error"] = {
                "code": getattr(
                    rollback_exc, "code", rollback_exc.__class__.__name__
                ),
                "message": str(rollback_exc),
            }
            orchestrator_error = ErrorInfo(
                code="transaction_recovery_unresolved",
                message=(
                    "transaction compensation did not resolve cleanly; "
                    "automatic retry is blocked"
                ),
                component="execution.transaction_recovery",
                retryable=False,
            )
            failure.update(orchestrator_error.to_dict())
            failure["integrity_unresolved"] = True
    atomic_json(task_dir / "execution_failure.json", failure)
    if closure_outcome_unknown and transaction_worker and transaction_context:
        try:
            transaction_worker.store.record_task_failure(
                context=transaction_context.to_dict(),
                error=orchestrator_error.to_dict(),
            )
        except Exception as diagnostic_exc:
            failure["transaction_diagnostic_error"] = {
                "code": getattr(
                    diagnostic_exc, "code", diagnostic_exc.__class__.__name__
                ),
                "message": str(diagnostic_exc),
            }
            atomic_json(task_dir / "execution_failure.json", failure)
    if not orchestrator_closed and not closure_outcome_unknown:
        try:
            fail(
                run_path=run_path,
                task_id=task_id,
                claim_token=token,
                reason=f"{failure['code']}: {failure['message']}",
                error_info=orchestrator_error,
                gpu_minutes=gpu_minutes,
            )
        except Exception as close_exc:
            failure["orchestrator_close_error"] = {
                "code": getattr(close_exc, "code", close_exc.__class__.__name__),
                "message": str(close_exc),
            }
            atomic_json(task_dir / "execution_failure.json", failure)
    if not closure_outcome_unknown:
        try:
            EvidenceLogger.log(
                "execution", "execution_task_failed", failure,
                phase=task["phase"] if task else "iterate",
                trace_context=trace_context,
            )
        except BaseException as evidence_exc:
            failure["failure_evidence_error"] = {
                "code": getattr(evidence_exc, "code", evidence_exc.__class__.__name__),
                "message": str(evidence_exc),
            }
            atomic_json(task_dir / "execution_failure.json", failure)


@dataclass
class _TaskExecution:
    """Mutable per-task execution state shared across phase helpers."""

    packet: dict | None = None
    task: dict | None = None
    action: str = "unknown"
    transaction_context: TransactionContext | None = None
    transaction_worker: "ExecutionWorker | None" = None
    orchestrator_closed: bool = False
    trace_context: TraceContext | None = None


def ensure_transaction_recovery_clean(
    *, config: ExecutionConfig | None = None
):
    """Run the formal recovery owner before any Orchestrator task claim.

    This public gate may reconcile interrupted transactions, but it never
    claims or executes a task.  Callers must stop when it raises
    :class:`RecoveryError`.
    """
    config = config or ExecutionConfig.from_environment()
    transaction_worker = ExecutionWorker(
        get_storage_backend(),
        config.execution_root / ".staging",
        config.execution_root / "artifacts",
    )
    recovery = transaction_worker.commit_manager.recover_pending(
        config.execution_root / ".staging",
        orchestrator_state=_orchestrator_state_for_transaction,
    )
    _assert_recovery_clean(recovery)
    return recovery


def inspect_transaction_recovery(
    *,
    config: ExecutionConfig | None = None,
    run_id: str | None = None,
    store=None,
):
    """Inspect one run's formal recovery state without mutating it."""

    config = config or ExecutionConfig.from_environment()
    transaction_worker = ExecutionWorker(
        store or get_storage_backend(read_only=True),
        config.execution_root / ".staging",
        config.execution_root / "artifacts",
    )
    return transaction_worker.commit_manager.recovery.inspect_pending(
        config.execution_root / ".staging",
        orchestrator_state=_orchestrator_state_for_transaction,
        run_id=run_id,
    )


def execute_task(
    *,
    run_path: str | Path,
    task_id: str,
    worker_id: str,
    config: ExecutionConfig | None = None,
) -> dict:
    """Claim and execute exactly one ready task."""
    config = config or ExecutionConfig.from_environment()
    ensure_transaction_recovery_clean(config=config)
    started = time.monotonic()
    claimed = claim(run_path=run_path, task_id=task_id, worker=worker_id)
    run_id = claimed["run"]["run_id"]
    attempt = int(claimed["run"]["tasks"][task_id]["attempts"])
    task_dir = config.task_dir(run_id, task_id, attempt)
    execution = _TaskExecution(trace_context=TraceContext(
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
    ))
    try:
        return _run_claimed_task(
            execution,
            claimed=claimed,
            run_path=run_path,
            task_id=task_id,
            worker_id=worker_id,
            config=config,
            task_dir=task_dir,
            started=started,
        )
    except BaseException as exc:
        _finalize_failure(
            exc=exc,
            started=started,
            claimed=claimed,
            run_path=run_path,
            task_id=task_id,
            task=execution.task,
            action=execution.action,
            task_dir=task_dir,
            trace_context=execution.trace_context,
            transaction_context=execution.transaction_context,
            transaction_worker=execution.transaction_worker,
            orchestrator_closed=execution.orchestrator_closed,
        )
        raise


def _run_claimed_task(
    execution: _TaskExecution,
    *,
    claimed: Mapping[str, object],
    run_path: str | Path,
    task_id: str,
    worker_id: str,
    config: ExecutionConfig,
    task_dir: Path,
    started: float,
) -> dict:
    """Prepare, recover, execute+commit, and close one claimed task."""
    packet = _read_packet(
        Path(claimed["dispatch_packet_path"]),
        claimed["dispatch_packet_sha256"],
    )
    execution.packet = packet
    if packet.get("trace_context") is not None:
        execution.trace_context = TraceContext.from_dict(packet["trace_context"])
    trace_context = execution.trace_context
    task = packet["task"]
    execution.task = task
    parameters = assert_action_executable(task)
    action = task["action"]
    execution.action = action
    handler = handler_for(action)
    if handler is None:
        raise ExecutionContractError(
            "execution_handler_missing", f"no handler registered for {action}"
        )
    task_dir.mkdir(parents=True, exist_ok=False)
    transaction_context = TransactionContext.create(
        workflow_id=trace_context.workflow_id,
        run_id=packet["run_id"],
        task_id=task_id,
        attempt_id=trace_context.attempt_id,
        action=action,
        metadata={
            "orchestrator_run_path": str(Path(run_path).resolve()),
            "worker_id": worker_id,
            "project_id": trace_context.project_id,
            "plan_id": trace_context.plan_id,
        },
    )
    execution.transaction_context = transaction_context
    transaction_worker = ExecutionWorker(
        get_storage_backend(),
        config.execution_root / ".staging",
        config.execution_root / "artifacts",
    )
    execution.transaction_worker = transaction_worker
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
        "attempt": int(claimed["run"]["tasks"][task_id]["attempts"]),
    }, phase=task["phase"], trace_context=trace_context)
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
        trace_context=trace_context,
    )
    refresh_projections()
    receipt = _close_orchestrator(
        execution,
        outcome,
        claimed=claimed,
        run_path=run_path,
        task_id=task_id,
        started=started,
    )
    _post_commit_finalize(
        task_dir=task_dir,
        receipt=receipt,
        task=task,
        trace_context=trace_context,
        transaction_worker=transaction_worker,
        transaction_context=transaction_context,
    )
    return receipt


def _close_orchestrator(
    execution: _TaskExecution,
    outcome: ExecutionActionResult,
    *,
    claimed: Mapping[str, object],
    run_path: str | Path,
    task_id: str,
    started: float,
) -> dict:
    """Close the Orchestrator task and build the success receipt.

    ``complete()`` is the irreversible success boundary; once it returns, the
    task must never again be reported as an execution failure.
    """
    packet = execution.packet
    task = execution.task
    trace_context = execution.trace_context
    elapsed_seconds = max(0.0, time.monotonic() - started)
    output_values = [f"{role}={path}" for role, path in outcome.outputs]
    gpu_minutes = (
        elapsed_seconds / 60.0
        if task["resource_request"]["class"] == "gpu" else None
    )
    completion_warnings: list[dict[str, str]] = []
    try:
        result = complete(
            run_path=run_path,
            task_id=task_id,
            claim_token=claimed["claim_token"],
            output_paths=output_values,
            gpu_minutes=gpu_minutes,
        )
    except BaseException as exc:
        verdict = probe_orchestrator_state(execution.transaction_context.to_dict())
        if verdict == UNKNOWN:
            raise OrchestratorClosureUnresolved(
                "Orchestrator completion outcome is unknown; recovery must "
                f"reconcile {execution.transaction_context.transaction_id} before retry",
                unresolved=(execution.transaction_context.transaction_id,),
            ) from exc
        if verdict != CLOSED:
            raise
        result = {"run": status(run_path=run_path)["run"]}
        completion_warnings.append({
            "step": "completion_outcome_probe",
            "code": getattr(exc, "code", exc.__class__.__name__),
            "message": str(exc),
        })
    execution.orchestrator_closed = True
    completion_warnings.extend(result.get("post_completion_warnings") or [])
    receipt = {
        "execution_worker_version": EXECUTION_WORKER_VERSION,
        "status": TaskStatus.SUCCEEDED.value,
        "run_id": packet["run_id"],
        "task_id": task_id,
        "action": execution.action,
        "workflow_id": trace_context.workflow_id,
        "plan_id": trace_context.plan_id,
        "attempt": int(claimed["run"]["tasks"][task_id]["attempts"]),
        "attempt_id": trace_context.attempt_id,
        "transaction_id": execution.transaction_context.transaction_id,
        "elapsed_seconds": elapsed_seconds,
        "gpu_minutes": gpu_minutes,
        "outputs": result["run"]["tasks"][task_id].get("outputs") or [
            {"role": role, "path": str(path), "sha256": file_sha256(path)}
            for role, path in outcome.outputs
        ],
        "processes": list(outcome.processes),
        "orchestrator_status": result["run"]["status"],
    }
    if completion_warnings:
        receipt["orchestrator_completion_warnings"] = completion_warnings
    return receipt


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
    except (ExecutionContractError, OrchestratorContractError, ExecutionFailure, OSError) as exc:
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
