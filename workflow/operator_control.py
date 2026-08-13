"""Explicit operator control for the pre-Orchestrator Launcher approval gate."""

from __future__ import annotations

import math
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from agents.planner import record_approval
from core.context import ProjectContext

from .control_models import (
    ApprovalBudgetProjection,
    ManualApprovalRequest,
    PreOrchestratorApprovalProjection,
    TaskResourceProjection,
)
from .diagnostics import LockedDiagnosticSession
from .errors import DiagnosticContractError
from .models import DiagnosticReport, LauncherCommandResult, RuntimeLocatorBinding
from .runtime_locator import require_runtime_locator, restore_project_context
from .service import (
    LauncherServiceDependencies,
    continue_locked_launcher_run,
    default_launcher_dependencies,
)


ApprovalRecorder = Callable[..., Mapping[str, Any]]


@dataclass(frozen=True)
class BoundLauncherContext:
    """Validated public project binding held under one Launcher run lock."""

    report: DiagnosticReport
    context: ProjectContext


@contextmanager
def bound_launcher_context(
    *,
    launcher_run_id: str,
    dependencies: LauncherServiceDependencies | None = None,
) -> Iterator[BoundLauncherContext]:
    """Bind the exact durable project and formal Store for one Launcher run."""

    deps = dependencies or default_launcher_dependencies()
    with deps.diagnostics.locked(launcher_run_id) as session:
        report = session.read()
        binding = require_runtime_locator(report)
        context = _restore_context(deps, binding)
        deps.validate_project(dict(context.config))
        _validate_report_binding(report, context)
        if deps.validate_formal_store is None:
            raise DiagnosticContractError(
                "control_binding_invalid",
                "The formal project Store cannot be validated.",
            )
        deps.validate_formal_store(binding, context)
        with deps.bind_context(context):
            yield BoundLauncherContext(report=report, context=context)


@contextmanager
def bound_launcher_project(
    launcher_run_id: str,
    dependencies: LauncherServiceDependencies | None = None,
) -> Iterator[ProjectContext]:
    """Yield the exact validated ProjectContext while its run lock is held."""

    with bound_launcher_context(
        launcher_run_id=launcher_run_id, dependencies=dependencies
    ) as bound:
        yield bound.context


def inspect_pre_orchestrator_approval(
    *,
    launcher_run_id: str,
    dependencies: LauncherServiceDependencies | None = None,
) -> PreOrchestratorApprovalProjection:
    """Project the exact formal plan currently waiting before Orchestrator."""

    deps = dependencies or default_launcher_dependencies()
    with deps.diagnostics.locked(launcher_run_id) as session:
        report = session.read()
        with _bound_read_only_runtime(deps, report) as runtime:
            planner = _inspect_awaiting_plan(runtime)
            return _project_plan(report, planner)


def inspect_first_gate_auto_approval(
    *,
    launcher_run_id: str,
    dependencies: LauncherServiceDependencies | None = None,
) -> PreOrchestratorApprovalProjection:
    """Inspect only the initial, non-retry bootstrap plan eligible for auto policy."""

    deps = dependencies or default_launcher_dependencies()
    with deps.diagnostics.locked(launcher_run_id) as session:
        report = session.read()
        with _bound_read_only_runtime(deps, report) as runtime:
            planner = _inspect_awaiting_plan(runtime)
            source = planner.references["plan_document"].get("source") or {}
            if (
                source.get("kind") != "initial_prediction_bootstrap"
                or source.get("retry") is not None
            ):
                raise DiagnosticContractError(
                    "approval_plan_stale",
                    "Automatic approval is limited to the initial bootstrap plan.",
                )
            return _project_plan(report, planner)


inspect_first_bootstrap_auto_approval = inspect_first_gate_auto_approval


def approve_and_resume(
    *,
    request: ManualApprovalRequest,
    dependencies: LauncherServiceDependencies | None = None,
    approval_recorder: ApprovalRecorder = record_approval,
) -> LauncherCommandResult:
    """Record one exact Planner approval and continue without releasing the run lock."""

    if not isinstance(request, ManualApprovalRequest):
        raise TypeError("request must be ManualApprovalRequest")
    deps = dependencies or default_launcher_dependencies()
    with deps.diagnostics.locked(request.launcher_run_id) as session:
        report = session.read()
        with _bound_read_only_runtime(deps, report) as runtime:
            planner = _inspect_awaiting_plan(runtime)
            projection = _project_plan(report, planner)
            _validate_exact_request(request, projection)
            _validate_gpu_minute_ceiling(request, projection)
            approval = approval_recorder(
                plan_path=planner.references["plan_path"],
                task_ids=list(request.required_task_ids),
                approver=request.approver,
                justification=request.justification,
                **request.ceilings.to_dict(),
            )
        approval_path = _approval_path(approval)
        return continue_locked_launcher_run(
            dependencies=deps,
            session=session,
            report=report,
            approval_paths=(approval_path,),
        )


@contextmanager
def _bound_read_only_runtime(
    deps: LauncherServiceDependencies, report: DiagnosticReport
) -> Iterator[Any]:
    binding = require_runtime_locator(report)
    context = _restore_context(deps, binding)
    deps.validate_project(dict(context.config))
    _validate_report_binding(report, context)
    if deps.validate_formal_store is None:
        raise DiagnosticContractError(
            "control_binding_invalid",
            "The formal project Store cannot be validated.",
        )
    deps.validate_formal_store(binding, context)
    with deps.bind_context(context):
        yield _read_only_runtime(deps, context, report, binding)


def _restore_context(
    deps: LauncherServiceDependencies, binding: RuntimeLocatorBinding
) -> ProjectContext:
    try:
        if deps.restore_context is not None:
            return deps.restore_context(binding)
        return restore_project_context(binding, loader=deps.load_context)
    except DiagnosticContractError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise DiagnosticContractError(
            "launcher_runtime_locator_unavailable",
            "The original Launcher runtime locator cannot be restored.",
        ) from error


def _read_only_runtime(
    deps: LauncherServiceDependencies,
    context: ProjectContext,
    report: DiagnosticReport,
    binding: RuntimeLocatorBinding,
) -> Any:
    if deps.runtime_factory_with_locator is not None:
        return deps.runtime_factory_with_locator(
            context, report.launcher_run_id, binding, True
        )
    if deps.read_only_runtime_factory is not None:
        return deps.read_only_runtime_factory(context, report.launcher_run_id)
    raise DiagnosticContractError(
        "control_binding_invalid",
        "A read-only Launcher runtime is unavailable.",
    )


def _validate_report_binding(
    report: DiagnosticReport, context: ProjectContext
) -> None:
    approved_binding = (context.config.get("review") or {}).get("approved_digest")
    if (
        context.project_id != report.project_id
        or approved_binding != report.approved_content_binding
    ):
        raise DiagnosticContractError(
            "control_binding_conflict",
            "The restored project does not match this Launcher run.",
        )


def _inspect_awaiting_plan(runtime: Any) -> Any:
    design = runtime.inspect_design()
    if design.status != "completed":
        raise DiagnosticContractError(
            "control_binding_invalid",
            "Initial Design completion cannot be validated for this Launcher run.",
        )
    planner = runtime.inspect_bootstrap_planner(design)
    if planner.status != "completed":
        raise DiagnosticContractError(
            planner.blocker_code or "control_binding_invalid",
            "The formal pre-Orchestrator plan cannot be validated.",
        )
    plan = planner.references.get("plan_document")
    if not isinstance(plan, Mapping):
        raise DiagnosticContractError(
            "control_binding_invalid", "The formal Planner document is unavailable."
        )
    orchestrator = runtime.inspect_orchestrator(plan)
    approvals = runtime.inspect_approvals(planner)
    if orchestrator.status != "not_started" or approvals.status != "not_started":
        raise DiagnosticContractError(
            "approval_plan_stale",
            "The plan is no longer awaiting pre-Orchestrator approval.",
        )
    return planner


def _project_plan(
    report: DiagnosticReport, planner: Any
) -> PreOrchestratorApprovalProjection:
    plan = planner.references["plan_document"]
    source = plan.get("source") or {}
    if (
        source.get("project_id") != report.project_id
        or source.get("approved_content_binding") != report.approved_content_binding
        or source.get("launcher_run_id") != report.launcher_run_id
    ):
        raise DiagnosticContractError(
            "control_binding_conflict",
            "The formal plan does not match this Launcher run.",
        )
    required = tuple((plan.get("approval_request") or {}).get("required_task_ids") or ())
    tasks_by_id = {
        task.get("task_id"): task
        for task in plan.get("tasks") or ()
        if isinstance(task, Mapping)
    }
    if any(task_id not in tasks_by_id for task_id in required):
        raise DiagnosticContractError(
            "control_binding_invalid", "The formal approval task scope is incomplete."
        )
    metadata = plan.get("decision_metadata") or {}
    return PreOrchestratorApprovalProjection(
        launcher_run_id=report.launcher_run_id,
        project_id=report.project_id,
        approved_content_binding=report.approved_content_binding,
        plan_id=planner.references["plan_id"],
        plan_sha256=planner.references["plan_sha256"],
        source_kind=source.get("kind"),
        required_task_ids=required,
        tasks=tuple(_project_task(tasks_by_id[task_id], metadata) for task_id in required),
        budget=_project_budget(plan.get("budget_request") or {}, metadata),
    )


def _project_task(
    task: Mapping[str, Any], metadata: Mapping[str, Any]
) -> TaskResourceProjection:
    resource = task.get("resource_request") or {}
    estimate_status = resource.get("estimate_status") or "unavailable"
    return TaskResourceProjection(
        task_id=task.get("task_id"),
        action=task.get("action"),
        resource_class=resource.get("class"),
        gpu_job_slots=resource.get("gpu_job_slots", 0),
        proposal_count=resource.get("proposal_count", 0),
        candidate_limit=resource.get("candidate_limit", 0),
        estimated_gpu_minutes=resource.get("estimated_gpu_minutes"),
        estimate_status=estimate_status,
        estimator_version=(
            resource.get("estimator_version")
            or (metadata.get("estimator_version") if estimate_status == "estimated" else None)
        ),
        calibration_status=(
            resource.get("calibration_status")
            or metadata.get("estimate_calibration_status")
            or ("not_applicable" if estimate_status == "not_applicable" else "unavailable")
        ),
    )


def _project_budget(
    budget: Mapping[str, Any], metadata: Mapping[str, Any]
) -> ApprovalBudgetProjection:
    status = budget.get("gpu_minutes_status") or "unavailable"
    return ApprovalBudgetProjection(
        gpu_minutes=budget.get("gpu_minutes"),
        gpu_minutes_status=status,
        estimator_version=(
            budget.get("gpu_minutes_estimator_version")
            or (metadata.get("estimator_version") if status == "estimated" else None)
        ),
        calibration_status=(
            budget.get("gpu_minutes_calibration_status")
            or metadata.get("estimate_calibration_status")
            or "unavailable"
        ),
    )


def _validate_exact_request(
    request: ManualApprovalRequest,
    projection: PreOrchestratorApprovalProjection,
) -> None:
    expected = (
        projection.launcher_run_id,
        projection.project_id,
        projection.approved_content_binding,
        projection.plan_id,
        projection.plan_sha256,
        projection.required_task_ids,
    )
    actual = (
        request.launcher_run_id,
        request.project_id,
        request.approved_content_binding,
        request.plan_id,
        request.plan_sha256,
        request.required_task_ids,
    )
    if actual != expected:
        raise DiagnosticContractError(
            "approval_plan_stale",
            "The displayed plan no longer matches the plan awaiting approval.",
        )


def _validate_gpu_minute_ceiling(
    request: ManualApprovalRequest,
    projection: PreOrchestratorApprovalProjection,
) -> None:
    minutes = projection.budget.gpu_minutes
    if (
        projection.budget.gpu_minutes_status != "estimated"
        or isinstance(minutes, bool)
        or not isinstance(minutes, (int, float))
        or not math.isfinite(float(minutes))
    ):
        raise DiagnosticContractError(
            "approval_estimate_unavailable",
            "A finite Planner GPU-minute estimate is required for approval.",
        )
    ceiling = request.ceilings.max_gpu_minutes
    if ceiling is None or float(minutes) > ceiling:
        raise DiagnosticContractError(
            "approval_ceiling_exceeded",
            "The current GPU-minute estimate exceeds the submitted ceiling.",
        )


def _approval_path(result: Mapping[str, Any]) -> Path:
    value = result.get("approval_path")
    if not isinstance(value, str) or not value:
        raise DiagnosticContractError(
            "launcher_operation_failed",
            "Planner approval did not return a usable approval artifact.",
        )
    return Path(value)


__all__ = [
    "BoundLauncherContext",
    "approve_and_resume",
    "bound_launcher_context",
    "bound_launcher_project",
    "inspect_first_gate_auto_approval",
    "inspect_first_bootstrap_auto_approval",
    "inspect_pre_orchestrator_approval",
]
