"""Thin, approval-aware Workflow Launcher application service."""

from __future__ import annotations

import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from core.context import ProjectContext
from target_bootstrap import assert_project_approved

from .boundaries import FormalBoundary, FormalBoundaryInspector
from .diagnostics import DiagnosticStore, resolve_diagnostics_root
from .errors import DiagnosticContractError, normalize_error
from .models import (
    BrowserResult,
    DiagnosticReport,
    LauncherCommandResult,
    StructuredError,
)
from .observations import (
    mirror_prediction_identity as _mirror_prediction_identity,
    observe as _observe,
    with_plan_trace as _with_plan_trace,
)
from .runtime_context import bind_project_context


@dataclass(frozen=True)
class LauncherServiceDependencies:
    diagnostics: DiagnosticStore
    load_context: Callable[[str | Path], ProjectContext]
    validate_project: Callable[[dict], None]
    bind_context: Callable[[ProjectContext], AbstractContextManager]
    runtime_factory: Callable[[ProjectContext, str], Any]
    launcher_id: Callable[[], str]


def launch_project(
    *, project_path: str | Path, dependencies: LauncherServiceDependencies | None = None
) -> LauncherCommandResult:
    deps = dependencies or _default_dependencies()
    try:
        context = deps.load_context(project_path)
        deps.validate_project(dict(context.config))
        binding = _approved_binding(context)
        launcher_run_id = deps.launcher_id()
        report = DiagnosticReport.initial(
            launcher_run_id=launcher_run_id,
            project_id=context.project_id,
            approved_content_binding=binding,
            project_locator=str(Path(project_path).expanduser().resolve()),
        )
        # This durable create is deliberately before Data Layer binding and
        # before constructing an Agent runtime that could perform side effects.
        deps.diagnostics.create(report)
        return _coordinate_locked(deps, launcher_run_id, (), execute=True)
    except Exception as error:
        return _unbound_failure(error)


def status_launcher_run(
    *, launcher_run_id: str, dependencies: LauncherServiceDependencies | None = None
) -> LauncherCommandResult:
    deps = dependencies or _default_dependencies()
    try:
        return _coordinate_locked(deps, launcher_run_id, (), execute=False)
    except Exception as error:
        return _unbound_failure(error, launcher_run_id=launcher_run_id)


def resume_launcher_run(
    *,
    launcher_run_id: str,
    approval_paths: Iterable[str | Path] = (),
    dependencies: LauncherServiceDependencies | None = None,
) -> LauncherCommandResult:
    deps = dependencies or _default_dependencies()
    try:
        return _coordinate_locked(
            deps, launcher_run_id, tuple(approval_paths), execute=True
        )
    except Exception as error:
        return _unbound_failure(error, launcher_run_id=launcher_run_id)


def _coordinate_locked(
    deps: LauncherServiceDependencies,
    launcher_run_id: str,
    approval_paths: tuple[str | Path, ...],
    *,
    execute: bool,
) -> LauncherCommandResult:
    with deps.diagnostics.locked(launcher_run_id) as session:
        report = session.read()
        try:
            context = deps.load_context(report.project_locator)
            deps.validate_project(dict(context.config))
            _validate_resume_binding(report, context)
            with deps.bind_context(context):
                runtime = deps.runtime_factory(context, report.launcher_run_id)
                return _advance(
                    runtime,
                    report,
                    session,
                    approval_paths=approval_paths,
                    execute=execute,
                )
        except Exception as error:
            return _record_failure(session, report, error, report.current_boundary or "launcher")


def _advance(
    runtime: Any,
    report: DiagnosticReport,
    session: Any,
    *,
    approval_paths: tuple[str | Path, ...],
    execute: bool,
) -> LauncherCommandResult:
    report, planner, outcome = _advance_to_plan(
        runtime, report, session, execute=execute
    )
    if outcome is not None:
        return outcome
    return _continue_approved_plan(
        runtime,
        report,
        session,
        planner,
        approval_paths=approval_paths,
        execute=execute,
    )


def _advance_to_plan(
    runtime: Any,
    report: DiagnosticReport,
    session: Any,
    *,
    execute: bool,
) -> tuple[DiagnosticReport, FormalBoundary | None, LauncherCommandResult | None]:
    prediction = runtime.inspect_prediction()
    report = _mirror_prediction_identity(report, runtime, prediction)
    critic = runtime.inspect_critic(prediction)
    if critic.status == "completed":
        planner = runtime.inspect_planner(critic)
        if planner.status == "completed":
            return _accept_existing_plan(report, planner, session, execute)
        if planner.status == "blocked":
            return report, None, _block(session, report, planner)
        return _resolve_planner(runtime, report, session, critic, execute)
    if critic.status == "blocked":
        return report, None, _block(session, report, critic)

    if prediction.status != "completed":
        report, outcome = _advance_to_prediction(
            runtime, report, session, prediction, execute=execute
        )
        if outcome is not None:
            return report, None, outcome
        prediction = runtime.inspect_prediction()
    return _resolve_critic_and_planner(
        runtime, report, session, prediction, execute=execute
    )


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
        lambda: runtime.inspect_critic(runtime.inspect_prediction()),
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
        lambda: runtime.inspect_planner(runtime.inspect_critic(runtime.inspect_prediction())),
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

    orchestrator = runtime.inspect_orchestrator(plan)
    if orchestrator.status == "blocked":
        return _block(session, report, orchestrator)
    if orchestrator.status == "not_started":
        approvals = runtime.inspect_approvals(planner)
        if approvals.status == "blocked":
            return _block(session, report, approvals)
        supplied = approval_paths or tuple(approvals.references.get("approval_paths") or ())
        approval_request = plan.get("approval_request") or {}
        approval_required = bool(approval_request.get("required_task_ids") or ())
        if approval_required and not supplied:
            report = report.with_observation(
                current_boundary="approval",
                last_completed_boundary="planner",
                last_known_formal_status="awaiting_approval",
            )
            if execute:
                session.write(report)
            return _result(
                report,
                "awaiting_approval",
                0,
                required_task_ids=tuple(approval_request.get("required_task_ids") or ()),
            )
        if not execute:
            return _result(report, "ready", 0)
        runtime.initialize_orchestrator(planner.references["plan_path"], supplied)
        orchestrator = runtime.inspect_orchestrator(plan)
        if orchestrator.status != "completed":
            return _block_or_invalid(session, report, orchestrator, "orchestrator")
        report = _observe(report, "orchestrator", orchestrator)
        session.write(report)

    formal_status = str(orchestrator.references.get("formal_status") or "pending")
    if formal_status == "ready" and execute:
        try:
            runtime.recover_transactions()
            runtime.drain(orchestrator.references["run_path"])
        except Exception as error:
            if getattr(error, "code", None) == "transaction_recovery_unresolved":
                blocker = FormalBoundary.blocked(
                    "transaction",
                    "transaction_recovery_unresolved",
                    "formal transaction recovery requires operator action",
                )
                return _block(session, report, blocker)
            return _worker_failure(
                runtime, session, report, orchestrator, plan, error
            )
        orchestrator = runtime.inspect_orchestrator(plan)
        if orchestrator.status != "completed":
            return _block_or_invalid(session, report, orchestrator, "orchestrator")
        formal_status = str(orchestrator.references.get("formal_status") or "pending")
        report = _observe(report, "execution", orchestrator)
        session.write(report)
    return _formal_outcome(report, formal_status, orchestrator, plan)


def _worker_failure(runtime, session, report, orchestrator, plan, error):
    refreshed = runtime.inspect_orchestrator(plan)
    if refreshed.status == "completed":
        report = _observe(report, "orchestrator", refreshed)
        orchestrator = refreshed
    formal_failure = runtime.inspect_execution_failure(orchestrator)
    if formal_failure.status == "completed":
        report = _observe(report, "execution", formal_failure)
    failed = report.with_failure(
        boundary="execution",
        error=normalize_error(error, component="execution"),
        formal_status=orchestrator.references.get("formal_status"),
    )
    try:
        session.write(failed)
    except Exception as write_error:
        failed = report.with_failure(
            boundary="execution",
            error=normalize_error(write_error, component="launcher"),
            formal_status=orchestrator.references.get("formal_status"),
        )
    return _result(failed, "failed", 2)


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
        return report, LauncherCommandResult(failed.browser_projection(status="failed"), 2)
    return report, None


def _block_or_invalid(
    session: Any, report: DiagnosticReport, formal: FormalBoundary, boundary: str
) -> LauncherCommandResult:
    if formal.status == "blocked":
        return _block(session, report, formal)
    error = DiagnosticContractError(
        f"{boundary}_completion_unproven",
        f"{boundary} returned without uniquely proven formal completion",
    )
    return _record_failure(session, report, error, boundary, exit_code=3)


def _block(
    session: Any, report: DiagnosticReport, formal: FormalBoundary
) -> LauncherCommandResult:
    error = StructuredError(
        code=formal.blocker_code or f"{formal.boundary}_recovery_blocked",
        component=formal.boundary,
        message=formal.message or "Formal recovery requires operator action.",
    )
    failed = report.with_failure(boundary=formal.boundary, error=error)
    session.write(failed)
    return _result(failed, "blocked", 3)


def _record_failure(
    session: Any,
    report: DiagnosticReport,
    error: BaseException,
    boundary: str,
    *,
    exit_code: int = 2,
) -> LauncherCommandResult:
    failed = report.with_failure(
        boundary=boundary, error=normalize_error(error, component=boundary)
    )
    try:
        session.write(failed)
    except Exception as write_error:
        failed = report.with_failure(
            boundary=boundary,
            error=normalize_error(write_error, component="launcher"),
        )
    return _result(failed, "failed" if exit_code == 2 else "blocked", exit_code)


def _formal_outcome(
    report: DiagnosticReport,
    status: str,
    orchestrator: FormalBoundary,
    plan: Mapping[str, Any],
) -> LauncherCommandResult:
    summary = orchestrator.references.get("summary") or {}
    task_status_counts = summary.get("task_status_counts") or {}
    required_task_ids = ()
    if status == "awaiting_approval":
        required_task_ids = tuple(
            (plan.get("approval_request") or {}).get("required_task_ids", ())
        )
    if status == "blocked":
        return _result(
            report,
            status,
            3,
            required_task_ids=required_task_ids,
            task_status_counts=task_status_counts,
        )
    if status == "failed":
        return _result(
            report,
            status,
            2,
            required_task_ids=required_task_ids,
            task_status_counts=task_status_counts,
        )
    return _result(
        report,
        status,
        0,
        required_task_ids=required_task_ids,
        task_status_counts=task_status_counts,
    )


def _result(
    report: DiagnosticReport,
    status: str,
    exit_code: int,
    *,
    required_task_ids: tuple[str, ...] = (),
    task_status_counts: Mapping[str, int] | None = None,
) -> LauncherCommandResult:
    return LauncherCommandResult(
        report.browser_projection(
            status=status,
            required_task_ids=required_task_ids,
            task_status_counts=task_status_counts,
        ),
        exit_code,
    )


def _unbound_failure(
    error: BaseException, *, launcher_run_id: str | None = None
) -> LauncherCommandResult:
    return LauncherCommandResult(
        BrowserResult(
            status="failed",
            launcher_run_id=launcher_run_id,
            error=normalize_error(error, component="launcher"),
        ),
        2,
    )


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


def _default_dependencies() -> LauncherServiceDependencies:
    from .adapters import DefaultWorkflowRuntime

    diagnostics = DiagnosticStore(resolve_diagnostics_root())

    return LauncherServiceDependencies(
        diagnostics=diagnostics,
        load_context=lambda path: ProjectContext.load(path=path),
        validate_project=assert_project_approved,
        bind_context=bind_project_context,
        runtime_factory=lambda context, launcher_run_id: DefaultWorkflowRuntime(
            context, launcher_run_id
        ),
        launcher_id=lambda: f"launcher_{uuid.uuid4().hex}",
    )


__all__ = [
    "LauncherServiceDependencies",
    "launch_project",
    "resume_launcher_run",
    "status_launcher_run",
]
