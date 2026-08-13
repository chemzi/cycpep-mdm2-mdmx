"""Thin, approval-aware Workflow Launcher application service."""

from __future__ import annotations

import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from core.context import ProjectContext
from target_bootstrap import assert_project_approved

from .bootstrap_prediction import advance_bootstrap_prediction
from .boundaries import FormalBoundary, FormalBoundaryInspector
from .diagnostics import (
    DiagnosticStore,
    resolve_diagnostics_root,
    validate_launcher_run_id,
)
from .errors import DiagnosticContractError, normalize_error
from .models import (
    DiagnosticReport,
    LauncherCommandResult,
    RuntimeLocatorBinding,
)
from .observations import (
    mirror_prediction_identity as _mirror_prediction_identity,
    observe as _observe,
    with_plan_trace as _with_plan_trace,
)
from .outcomes import (
    _block,
    _block_or_invalid,
    _clear_resolved_failure,
    _formal_outcome,
    _log_operational_failure,
    _record_failure,
    _result,
    _unbound_failure,
    _worker_failure,
)
from .runtime_context import bind_project_context
from .runtime_locator import (
    ContextRestorer,
    require_formal_store,
    require_runtime_locator,
    resolve_execution_root,
    restore_project_context,
)

@dataclass(frozen=True)
class LauncherServiceDependencies:
    diagnostics: DiagnosticStore
    load_context: Callable[[str | Path], ProjectContext]
    validate_project: Callable[[dict], None]
    bind_context: Callable[[ProjectContext], AbstractContextManager]
    runtime_factory: Callable[[ProjectContext, str], Any]
    launcher_id: Callable[[], str]
    restore_context: ContextRestorer | None = None
    read_only_runtime_factory: Callable[[ProjectContext, str], Any] | None = None
    validate_formal_store: (
        Callable[[RuntimeLocatorBinding, ProjectContext], None] | None
    ) = None
    runtime_factory_with_locator: (
        Callable[[ProjectContext, str, RuntimeLocatorBinding, bool], Any] | None
    ) = None
    execution_root_resolver: Callable[[], Path] | None = None


def launch_project(
    *,
    project_path: str | Path,
    launcher_run_id: str | None = None,
    dependencies: LauncherServiceDependencies | None = None,
) -> LauncherCommandResult:
    deps = dependencies or default_launcher_dependencies()
    resolved_launcher_run_id = None
    try:
        if launcher_run_id is not None:
            resolved_launcher_run_id = validate_launcher_run_id(launcher_run_id)
        context = deps.load_context(project_path)
        deps.validate_project(dict(context.config))
        binding = _approved_binding(context)
        if resolved_launcher_run_id is None:
            resolved_launcher_run_id = deps.launcher_id()
        else:
            recovered = _recover_existing_launch(
                deps,
                resolved_launcher_run_id,
                project_id=context.project_id,
                approved_content_binding=binding,
            )
            if recovered is not None:
                return recovered
        project_locator = str(Path(project_path).expanduser().resolve())
        execution_root = (
            deps.execution_root_resolver()
            if deps.execution_root_resolver is not None
            else resolve_execution_root()
        )
        runtime_locator = RuntimeLocatorBinding.from_context(
            context, project_locator, execution_root=execution_root
        )
        report = DiagnosticReport.initial(
            launcher_run_id=resolved_launcher_run_id,
            project_id=context.project_id,
            approved_content_binding=binding,
            project_locator=project_locator,
            runtime_locator_binding=runtime_locator,
        )
        # This durable create is deliberately before Data Layer binding and
        # before constructing an Agent runtime that could perform side effects.
        try:
            deps.diagnostics.create(report)
        except DiagnosticContractError as error:
            if (
                launcher_run_id is None
                or error.code != "launcher_diagnostic_already_exists"
            ):
                raise
            return _coordinate_locked(
                deps,
                resolved_launcher_run_id,
                (),
                execute=True,
                expected_project_id=context.project_id,
                expected_approved_content_binding=binding,
            )
        return _coordinate_locked(
            deps,
            resolved_launcher_run_id,
            (),
            execute=True,
            allow_missing_store=True,
        )
    except Exception as error:
        return _unbound_failure(error, launcher_run_id=resolved_launcher_run_id)


def _recover_existing_launch(
    deps: LauncherServiceDependencies,
    launcher_run_id: str,
    *,
    project_id: str,
    approved_content_binding: str,
) -> LauncherCommandResult | None:
    with deps.diagnostics.locked(launcher_run_id) as session:
        try:
            report = session.read()
        except DiagnosticContractError as error:
            if error.code == "launcher_diagnostic_not_found":
                return None
            raise
        return _coordinate_session(
            deps,
            session,
            report,
            (),
            execute=True,
            expected_project_id=project_id,
            expected_approved_content_binding=approved_content_binding,
        )


def status_launcher_run(
    *, launcher_run_id: str, dependencies: LauncherServiceDependencies | None = None
) -> LauncherCommandResult:
    deps = dependencies or default_launcher_dependencies()
    try:
        return _coordinate_locked(deps, launcher_run_id, (), execute=False)
    except Exception as error:
        return _unbound_failure(error, launcher_run_id=launcher_run_id)


def resume_launcher_run(
    *,
    launcher_run_id: str,
    approval_paths: Iterable[str | Path] = (),
    retry_bootstrap_prediction: bool = False,
    dependencies: LauncherServiceDependencies | None = None,
) -> LauncherCommandResult:
    deps = dependencies or default_launcher_dependencies()
    try:
        return _coordinate_locked(
            deps, launcher_run_id, tuple(approval_paths), execute=True,
            retry_bootstrap_prediction=retry_bootstrap_prediction,
        )
    except Exception as error:
        return _unbound_failure(error, launcher_run_id=launcher_run_id)


def continue_locked_launcher_run(
    *,
    dependencies: LauncherServiceDependencies,
    session: Any,
    report: DiagnosticReport,
    approval_paths: Iterable[str | Path],
) -> LauncherCommandResult:
    """Continue one run while its caller already holds the diagnostic lock."""

    if session.launcher_run_id != report.launcher_run_id:
        raise DiagnosticContractError(
            "launcher_diagnostic_binding_mismatch",
            "Launcher diagnostic binding is invalid.",
        )
    return _coordinate_session(
        dependencies,
        session,
        report,
        tuple(approval_paths),
        execute=True,
    )


def _coordinate_locked(
    deps: LauncherServiceDependencies,
    launcher_run_id: str,
    approval_paths: tuple[str | Path, ...],
    *,
    execute: bool,
    allow_missing_store: bool = False,
    retry_bootstrap_prediction: bool = False,
    expected_project_id: str | None = None,
    expected_approved_content_binding: str | None = None,
) -> LauncherCommandResult:
    with deps.diagnostics.locked(launcher_run_id) as session:
        report = session.read()
        return _coordinate_session(
            deps,
            session,
            report,
            approval_paths,
            execute=execute,
            allow_missing_store=allow_missing_store,
            retry_bootstrap_prediction=retry_bootstrap_prediction,
            expected_project_id=expected_project_id,
            expected_approved_content_binding=expected_approved_content_binding,
        )


def _coordinate_session(
    deps: LauncherServiceDependencies,
    session: Any,
    report: DiagnosticReport,
    approval_paths: tuple[str | Path, ...],
    *,
    execute: bool,
    allow_missing_store: bool = False,
    retry_bootstrap_prediction: bool = False,
    expected_project_id: str | None = None,
    expected_approved_content_binding: str | None = None,
) -> LauncherCommandResult:
    try:
        _validate_launch_binding(
            report,
            expected_project_id=expected_project_id,
            expected_approved_content_binding=expected_approved_content_binding,
        )
    except DiagnosticContractError as error:
        # Conflicting retry input must not be recorded on the existing run.
        return _unbound_failure(error, launcher_run_id=report.launcher_run_id)
    try:
        binding = require_runtime_locator(report)
        context = _restore_bound_context(deps, binding)
        deps.validate_project(dict(context.config))
        _validate_resume_binding(report, context)
        if not allow_missing_store and deps.validate_formal_store is not None:
            deps.validate_formal_store(binding, context)
        with deps.bind_context(context):
            if deps.runtime_factory_with_locator is not None:
                runtime = deps.runtime_factory_with_locator(
                    context,
                    report.launcher_run_id,
                    binding,
                    not execute,
                )
            else:
                runtime_factory = (
                    deps.read_only_runtime_factory
                    if not execute and deps.read_only_runtime_factory is not None
                    else deps.runtime_factory
                )
                runtime = runtime_factory(context, report.launcher_run_id)
            return _advance(
                runtime,
                report,
                session,
                approval_paths=approval_paths,
                retry_bootstrap_prediction=retry_bootstrap_prediction,
                execute=execute,
            )
    except Exception as error:
        exit_code = (
            3
            if getattr(error, "code", None) == "launcher_runtime_locator_unavailable"
            else 2
        )
        return _record_failure(
            session,
            report,
            error,
            report.current_boundary or "launcher",
            exit_code=exit_code,
        )


def _advance(
    runtime: Any,
    report: DiagnosticReport,
    session: Any,
    *,
    approval_paths: tuple[str | Path, ...],
    retry_bootstrap_prediction: bool = False,
    execute: bool,
) -> LauncherCommandResult:
    report, planner, outcome = _advance_to_plan(
        runtime,
        report,
        session,
        approval_paths=approval_paths,
        retry_bootstrap_prediction=retry_bootstrap_prediction,
        execute=execute,
    )
    if outcome is not None:
        return outcome
    return _continue_approved_plan(
        runtime,
        report,
        session,
        planner,
        approval_paths=(
            () if planner.references.get("requires_new_approval") else approval_paths
        ),
        execute=execute,
    )


def _advance_to_plan(
    runtime: Any,
    report: DiagnosticReport,
    session: Any,
    *,
    approval_paths: tuple[str | Path, ...],
    retry_bootstrap_prediction: bool = False,
    execute: bool,
) -> tuple[DiagnosticReport, FormalBoundary | None, LauncherCommandResult | None]:
    bootstrap_advanced = False
    direct_prediction = runtime.inspect_prediction()
    prediction = direct_prediction
    report = _mirror_prediction_identity(report, runtime, prediction)
    if prediction.status == "blocked":
        return report, None, _block(session, report, prediction)
    if prediction.status == "completed" and hasattr(
        runtime, "inspect_bootstrap_planner"
    ):
        design = runtime.inspect_design()
        if design.status == "completed":
            bootstrap = runtime.inspect_bootstrap_planner(design)
            if bootstrap.status != "not_started":
                conflict = FormalBoundary.blocked(
                    "prediction",
                    "prediction_authority_conflict",
                    "direct Prediction completion and a bootstrap Prediction plan both exist",
                )
                return report, None, _block(session, report, conflict)
    if prediction.status == "not_started" and hasattr(
        runtime, "inspect_bootstrap_planner"
    ):
        report, prediction, outcome = advance_bootstrap_prediction(
            runtime,
            report,
            session,
            approval_paths=approval_paths,
            retry_requested=retry_bootstrap_prediction,
            execute=execute,
            resolve_boundary=_resolve_boundary,
            continue_plan=_continue_approved_plan,
        )
        if outcome is not None:
            return report, None, outcome
        bootstrap_advanced = prediction.status == "completed"
    if prediction.status == "blocked":
        return report, None, _block(session, report, prediction)
    if prediction.status != "completed":
        report, outcome = _advance_to_prediction(
            runtime, report, session, prediction, execute=execute
        )
        if outcome is not None:
            return report, None, outcome
        prediction = runtime.inspect_prediction()
        if prediction.status != "completed":
            return report, None, _block_or_invalid(
                session, report, prediction, "prediction"
            )

    critic = runtime.inspect_critic(prediction)
    if critic.status == "completed":
        planner = runtime.inspect_planner(critic)
        if planner.status == "completed":
            return _accept_existing_plan(
                report, _mark_new_approval(planner, bootstrap_advanced), session, execute
            )
        if planner.status == "blocked":
            return report, None, _block(session, report, planner)
        return _mark_resolved_new_approval(
            _resolve_planner(runtime, report, session, critic, execute),
            bootstrap_advanced,
        )
    if critic.status == "blocked":
        return report, None, _block(session, report, critic)
    return _mark_resolved_new_approval(
        _resolve_critic_and_planner(
            runtime, report, session, prediction, execute=execute
        ),
        bootstrap_advanced,
    )


def _mark_new_approval(
    planner: FormalBoundary | None, required: bool
) -> FormalBoundary | None:
    if not required or planner is None:
        return planner
    return FormalBoundary(
        status=planner.status,
        boundary=planner.boundary,
        blocker_code=planner.blocker_code,
        message=planner.message,
        references={**planner.references, "requires_new_approval": True},
    )


def _mark_resolved_new_approval(resolved, required):
    report, planner, outcome = resolved
    return report, _mark_new_approval(planner, required), outcome


def _advance_to_prediction(
    runtime: Any,
    report: DiagnosticReport,
    session: Any,
    prediction: FormalBoundary,
    *,
    execute: bool,
) -> tuple[DiagnosticReport, LauncherCommandResult | None]:
    if prediction.status == "blocked":
        return report, _block(session, report, prediction)
    report, outcome = _resolve_boundary(
        session, report, runtime.inspect_research, "research", runtime.run_research, execute
    )
    if outcome is not None:
        return report, outcome

    report, outcome = _resolve_boundary(
        session, report, runtime.inspect_design, "design", runtime.run_design, execute
    )
    if outcome is not None:
        return report, outcome
    design = runtime.inspect_design()

    prediction_call = lambda: runtime.run_prediction(
        tuple(design.references.get("candidate_ids") or ())
    )
    report, outcome = _resolve_boundary(
        session, report, runtime.inspect_prediction, "prediction", prediction_call, execute
    )
    if outcome is not None:
        return report, outcome
    prediction = runtime.inspect_prediction()
    report = _mirror_prediction_identity(report, runtime, prediction)
    return report, None


def _resolve_critic_and_planner(
    runtime: Any,
    report: DiagnosticReport,
    session: Any,
    prediction: FormalBoundary,
    *,
    execute: bool,
) -> tuple[DiagnosticReport, FormalBoundary | None, LauncherCommandResult | None]:
    critic_call = lambda: runtime.run_critic(prediction.references["handoff_path"])
    report, outcome = _resolve_boundary(
        session,
        report,
        lambda: runtime.inspect_critic(prediction),
        "critic",
        critic_call,
        execute,
    )
    if outcome is not None:
        return report, None, outcome
    critic = runtime.inspect_critic(prediction)
    return _resolve_planner(runtime, report, session, critic, execute)


def _resolve_planner(
    runtime: Any,
    report: DiagnosticReport,
    session: Any,
    critic: FormalBoundary,
    execute: bool,
) -> tuple[DiagnosticReport, FormalBoundary | None, LauncherCommandResult | None]:
    planner_call = lambda: runtime.run_planner(critic.references["report_path"])
    report, outcome = _resolve_boundary(
        session,
        report,
        lambda: runtime.inspect_planner(critic),
        "planner",
        planner_call,
        execute,
    )
    if outcome is not None:
        return report, None, outcome
    planner = runtime.inspect_planner(critic)
    return _accept_existing_plan(report, planner, session, execute)


def _accept_existing_plan(report, planner, session, execute):
    plan = planner.references["plan_document"]
    report = _clear_resolved_failure(report, "planner", planner)
    report = _with_plan_trace(report, plan)
    if execute:
        session.write(report)
    return report, planner, None


def _continue_approved_plan(
    runtime: Any,
    report: DiagnosticReport,
    session: Any,
    planner: FormalBoundary,
    *,
    approval_paths: tuple[str | Path, ...],
    execute: bool,
) -> LauncherCommandResult:
    plan = planner.references["plan_document"]
    report, orchestrator, outcome = _resolve_orchestrator(
        runtime,
        report,
        session,
        planner,
        plan,
        approval_paths=approval_paths,
        execute=execute,
    )
    if outcome is not None:
        return outcome
    return _drive_orchestrator(
        runtime, report, session, orchestrator, plan, execute=execute
    )


def _resolve_orchestrator(
    runtime: Any,
    report: DiagnosticReport,
    session: Any,
    planner: FormalBoundary,
    plan: Mapping[str, Any],
    *,
    approval_paths: tuple[str | Path, ...],
    execute: bool,
) -> tuple[DiagnosticReport, FormalBoundary | None, LauncherCommandResult | None]:
    orchestrator = runtime.inspect_orchestrator(plan)
    if orchestrator.status == "blocked":
        return report, None, _block(session, report, orchestrator)
    if orchestrator.status != "not_started":
        return report, orchestrator, None

    approvals = runtime.inspect_approvals(planner)
    if approvals.status == "blocked":
        return report, None, _block(session, report, approvals)
    supplied = approval_paths or tuple(approvals.references.get("approval_paths") or ())
    required = tuple(
        (plan.get("approval_request") or {}).get("required_task_ids") or ()
    )
    if required and not supplied:
        report = report.with_observation(
            current_boundary="approval",
            last_completed_boundary="planner",
            last_known_formal_status="awaiting_approval",
        )
        if execute:
            session.write(report)
        return report, None, _result(
            report, "awaiting_approval", 0, required_task_ids=required
        )
    if not execute:
        return report, None, _result(report, "ready", 0)
    runtime.initialize_orchestrator(planner.references["plan_path"], supplied)
    orchestrator = runtime.inspect_orchestrator(plan)
    if orchestrator.status != "completed":
        return report, None, _block_or_invalid(
            session, report, orchestrator, "orchestrator"
        )
    report = _observe(report, "orchestrator", orchestrator)
    session.write(report)
    return report, orchestrator, None


def _drive_orchestrator(
    runtime: Any,
    report: DiagnosticReport,
    session: Any,
    orchestrator: FormalBoundary,
    plan: Mapping[str, Any],
    *,
    execute: bool,
) -> LauncherCommandResult:
    formal_status = str(orchestrator.references.get("formal_status") or "pending")
    if formal_status in {"ready", "running", "pending"}:
        report, orchestrator, outcome = _gate_transaction_recovery(
            runtime, report, session, orchestrator, plan, execute=execute
        )
        if outcome is not None:
            return outcome
        formal_status = str(orchestrator.references.get("formal_status") or "pending")
    drained = False
    if formal_status == "ready" and execute:
        report, orchestrator, outcome = _drain_ready_run(
            runtime, report, session, orchestrator, plan
        )
        if outcome is not None:
            return outcome
        formal_status = str(orchestrator.references.get("formal_status") or "pending")
        drained = True
    if not drained:
        report = _observe(report, "orchestrator", orchestrator)
        if formal_status in {
            "completed",
            "completed_required",
            "blocked",
            "failed",
            "awaiting_approval",
        }:
            report = _observe(report, "execution", orchestrator)
        if execute:
            session.write(report)
    return _formal_outcome(report, formal_status, orchestrator, plan)


def _gate_transaction_recovery(
    runtime, report, session, orchestrator, plan, *, execute
):
    report = _observe(report, "orchestrator", orchestrator)
    transaction = runtime.inspect_transaction_recovery(orchestrator)
    if transaction.references.get("live_owner") is True:
        if transaction.status == "blocked":
            report = _merge_transaction_trace(report, transaction)
            return report, orchestrator, _block(session, report, transaction)
        report = _merge_transaction_trace(report, transaction).with_observation(
            current_boundary="transaction",
            last_known_formal_status="running",
        )
        if execute:
            session.write(report)
        return report, orchestrator, _formal_outcome(
            report, "running", orchestrator, plan
        )
    if transaction.status == "completed":
        report = _clear_transaction_recovery_failure(report)
        if not execute:
            return report, orchestrator, None
        orchestrator = runtime.inspect_orchestrator(plan)
        if orchestrator.status != "completed":
            return report, orchestrator, _block_or_invalid(
                session, report, orchestrator, "orchestrator"
            )
        report = _observe(report, "orchestrator", orchestrator)
        return report, orchestrator, None
    if transaction.status != "blocked":
        return report, orchestrator, _block_or_invalid(
            session, report, transaction, "transaction"
        )
    report = _merge_transaction_trace(report, transaction)
    if not execute:
        return report, orchestrator, _block(session, report, transaction)
    try:
        runtime.recover_transactions()
    except Exception as error:
        if getattr(error, "code", None) == "transaction_recovery_unresolved":
            blocker = _transaction_recovery_blocker(error)
            report = _merge_transaction_trace(report, blocker)
            return report, orchestrator, _block(session, report, blocker)
        return report, orchestrator, _record_failure(
            session, report, error, "transaction"
        )

    orchestrator = runtime.inspect_orchestrator(plan)
    if orchestrator.status != "completed":
        return report, orchestrator, _block_or_invalid(
            session, report, orchestrator, "orchestrator"
        )
    report = _observe(report, "orchestrator", orchestrator)
    transaction = runtime.inspect_transaction_recovery(orchestrator)
    if transaction.status == "blocked":
        report = _merge_transaction_trace(report, transaction)
        return report, orchestrator, _block(session, report, transaction)
    if transaction.status != "completed":
        return report, orchestrator, _block_or_invalid(
            session, report, transaction, "transaction"
        )
    return _clear_transaction_recovery_failure(report), orchestrator, None


def _drain_ready_run(runtime, report, session, orchestrator, plan):
    try:
        runtime.drain(orchestrator.references["run_path"])
    except Exception as error:
        if getattr(error, "code", None) != "transaction_recovery_unresolved":
            return report, orchestrator, _worker_failure(
                runtime, session, report, orchestrator, plan, error
            )
        blocker = _transaction_recovery_blocker(error)
        report = _merge_transaction_trace(report, blocker)
        return report, orchestrator, _block(session, report, blocker)
    orchestrator = runtime.inspect_orchestrator(plan)
    if orchestrator.status != "completed":
        return report, orchestrator, _block_or_invalid(
            session, report, orchestrator, "orchestrator"
        )
    report = _observe(report, "execution", orchestrator)
    session.write(report)
    return report, orchestrator, None


def _transaction_recovery_blocker(error):
    unresolved = tuple(
        str(value)
        for value in (getattr(error, "unresolved", ()) or ())
        if value
    )
    return FormalBoundary.blocked(
        "transaction",
        "transaction_recovery_unresolved",
        "formal transaction recovery requires operator action",
        transaction_id=unresolved[0] if unresolved else None,
        transaction_ids=unresolved,
    )


def _merge_transaction_trace(report, transaction):
    transaction_id = transaction.references.get("transaction_id")
    if not transaction_id:
        return report
    return report.with_observation(formal_trace=replace(
        report.formal_trace, transaction_id=str(transaction_id)
    ))


def _clear_transaction_recovery_failure(report):
    failure = report.failure
    if (
        report.failed_boundary == "transaction"
        and failure is not None
        and failure.code == "transaction_recovery_unresolved"
    ):
        return report.clear_failure()
    return report


def _resolve_boundary(
    session: Any,
    report: DiagnosticReport,
    inspect: Callable[[], FormalBoundary],
    boundary: str,
    invoke: Callable[[], Any],
    execute: bool,
) -> tuple[DiagnosticReport, LauncherCommandResult | None]:
    formal = inspect()
    if formal.status == "blocked":
        return report, _block(session, report, formal)
    if formal.status == "not_started":
        if not execute:
            pending = report.with_observation(current_boundary=boundary)
            return pending, _result(pending, "pending", 0)
        try:
            invoke()
        except Exception as error:
            return report, _record_failure(session, report, error, boundary)
        formal = inspect()
        if formal.status != "completed":
            return report, _block_or_invalid(session, report, formal, boundary)
    report = _clear_resolved_failure(report, boundary, formal)
    report = _observe(report, boundary, formal)
    if not execute:
        return report, None
    try:
        session.write(report)
    except Exception as error:
        # Formal work is deliberately not rolled back.  A later resume will
        # rediscover it through the owner validator and repair this journal.
        failed = report.with_failure(
            boundary=boundary, error=normalize_error(error, component="launcher")
        )
        _log_operational_failure(error, component="launcher")
        return report, LauncherCommandResult(failed.browser_projection(status="failed"), 2)
    return report, None


def _approved_binding(context: ProjectContext) -> str:
    value = (context.config.get("review") or {}).get("approved_digest")
    if not isinstance(value, str) or not value:
        raise ValueError("approved project has no approved-content binding")
    return value


def _validate_resume_binding(report: DiagnosticReport, context: ProjectContext) -> None:
    if context.project_id != report.project_id:
        raise DiagnosticContractError(
            "launcher_project_binding_changed",
            "The project identifier no longer matches this launcher run.",
        )
    if _approved_binding(context) != report.approved_content_binding:
        raise DiagnosticContractError(
            "launcher_approved_content_changed",
            "The approved project content no longer matches this launcher run.",
        )


def _validate_launch_binding(
    report: DiagnosticReport,
    *,
    expected_project_id: str | None,
    expected_approved_content_binding: str | None,
) -> None:
    if expected_project_id is None and expected_approved_content_binding is None:
        return
    if (
        report.project_id != expected_project_id
        or report.approved_content_binding != expected_approved_content_binding
    ):
        raise DiagnosticContractError(
            "launcher_launch_binding_conflict",
            "The supplied launcher run identifier is bound to another approved project.",
        )


def _restore_bound_context(
    deps: LauncherServiceDependencies, binding: RuntimeLocatorBinding
) -> ProjectContext:
    if deps.restore_context is not None:
        try:
            return deps.restore_context(binding)
        except DiagnosticContractError:
            raise
        except (OSError, TypeError, ValueError) as error:
            raise DiagnosticContractError(
                "launcher_runtime_locator_unavailable",
                "The original Launcher runtime locator cannot be restored.",
            ) from error
    # Compatibility for dependency-injected callers predating the durable
    # locator seam.  Formal paths still come exclusively from the binding.
    return restore_project_context(binding, loader=deps.load_context)


def default_launcher_dependencies() -> LauncherServiceDependencies:
    from .adapters import DefaultWorkflowRuntime

    diagnostics = DiagnosticStore(resolve_diagnostics_root())

    return LauncherServiceDependencies(
        diagnostics=diagnostics,
        load_context=lambda path: ProjectContext.from_runtime(path=path),
        validate_project=assert_project_approved,
        bind_context=bind_project_context,
        runtime_factory=lambda context, launcher_run_id: DefaultWorkflowRuntime(
            context, launcher_run_id
        ),
        launcher_id=lambda: f"launcher_{uuid.uuid4().hex}",
        restore_context=restore_project_context,
        read_only_runtime_factory=lambda context, launcher_run_id: DefaultWorkflowRuntime(
            context, launcher_run_id, read_only=True
        ),
        validate_formal_store=require_formal_store,
        runtime_factory_with_locator=lambda context, launcher_run_id, binding, read_only: DefaultWorkflowRuntime(
            context,
            launcher_run_id,
            read_only=read_only,
            execution_root=Path(binding.execution_root),
        ),
        execution_root_resolver=resolve_execution_root,
    )


__all__ = [
    "LauncherServiceDependencies",
    "continue_locked_launcher_run",
    "default_launcher_dependencies",
    "launch_project",
    "resume_launcher_run",
    "status_launcher_run",
]
