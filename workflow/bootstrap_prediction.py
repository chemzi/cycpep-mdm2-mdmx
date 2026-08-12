"""Approval-gated coordination for the pre-Critic Prediction phase."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .boundaries import FormalBoundary
from .models import DiagnosticReport, LauncherCommandResult
from .observations import observe, with_plan_trace
from .outcomes import _block, _block_or_invalid, _clear_resolved_failure, _result


def advance_bootstrap_prediction(
    runtime: Any,
    report: DiagnosticReport,
    session: Any,
    *,
    approval_paths: tuple[str | Path, ...],
    retry_requested: bool,
    execute: bool,
    resolve_boundary: Callable[..., tuple[DiagnosticReport, LauncherCommandResult | None]],
    continue_plan: Callable[..., LauncherCommandResult],
) -> tuple[DiagnosticReport, FormalBoundary, LauncherCommandResult | None]:
    """Drive the approved pre-Critic Prediction plan through existing owners."""
    report, outcome = resolve_boundary(
        session, report, runtime.inspect_research, "research", runtime.run_research, execute
    )
    if outcome is not None:
        return report, FormalBoundary.not_started("prediction"), outcome
    research = runtime.inspect_research()
    report, outcome = resolve_boundary(
        session, report, runtime.inspect_design, "design", runtime.run_design, execute
    )
    if outcome is not None:
        return report, FormalBoundary.not_started("prediction"), outcome
    design = runtime.inspect_design()

    inspect_plan = lambda: runtime.inspect_bootstrap_planner(design)
    create_plan = lambda: runtime.run_bootstrap_planner(research, design)
    report, outcome = resolve_boundary(
        session, report, inspect_plan, "planner", create_plan, execute
    )
    if outcome is not None:
        return report, FormalBoundary.not_started("prediction"), outcome
    planner = inspect_plan()
    report = _clear_resolved_failure(report, "planner", planner)
    report = with_plan_trace(report, planner.references["plan_document"])
    if execute:
        session.write(report)

    if retry_requested:
        return _retry_failed_execution(runtime, report, session, planner, design, execute)

    execution_outcome = continue_plan(
        runtime,
        report,
        session,
        planner,
        approval_paths=approval_paths,
        execute=execute,
    )
    if execution_outcome.payload.status not in {"completed", "completed_required"}:
        return report, FormalBoundary.not_started("prediction"), execution_outcome
    orchestrator = runtime.inspect_orchestrator(planner.references["plan_document"])
    prediction = runtime.inspect_bootstrap_prediction(
        planner.references["plan_document"], orchestrator
    )
    if prediction.status == "blocked":
        return report, prediction, _block(session, report, prediction)
    if prediction.status != "completed":
        return report, prediction, _block_or_invalid(
            session, report, prediction, "prediction"
        )
    report = observe(report, "prediction", prediction)
    if execute:
        session.write(report)
    return report, prediction, None


def _retry_failed_execution(runtime, report, session, planner, design, execute):
    plan = planner.references["plan_document"]
    orchestrator = runtime.inspect_orchestrator(plan)
    source = plan.get("source") or {}
    if source.get("retry") is not None and orchestrator.status == "not_started":
        return _awaiting_retry_approval(report, planner)
    if (
        orchestrator.status != "completed"
        or orchestrator.references.get("formal_status") != "failed"
    ):
        blocker = FormalBoundary.blocked(
            "prediction",
            "bootstrap_retry_not_terminal_failed",
            "bootstrap retry requires one formally terminal failed execution",
        )
        return report, FormalBoundary.not_started("prediction"), _block(
            session, report, blocker
        )
    recovery = runtime.inspect_transaction_recovery(orchestrator)
    if recovery.status != "completed":
        return report, FormalBoundary.not_started("prediction"), _block_or_invalid(
            session, report, recovery, "transaction"
        )
    failure = runtime.inspect_execution_failure(orchestrator, plan)
    if failure.status != "completed":
        return report, FormalBoundary.not_started("prediction"), _block_or_invalid(
            session, report, failure, "execution"
        )
    runtime.retry_bootstrap_prediction(plan, failure)
    retry_plan = runtime.inspect_bootstrap_planner(design)
    if retry_plan.status != "completed":
        return report, FormalBoundary.not_started("prediction"), _block_or_invalid(
            session, report, retry_plan, "planner"
        )
    report = with_plan_trace(report, retry_plan.references["plan_document"])
    if execute:
        session.write(report)
    return _awaiting_retry_approval(report, retry_plan)


def _awaiting_retry_approval(report, planner):
    required = tuple(
        planner.references["plan_document"]["approval_request"]["required_task_ids"]
    )
    return report, FormalBoundary.not_started("prediction"), _result(
        report, "awaiting_approval", 0, required_task_ids=required
    )


__all__ = ["advance_bootstrap_prediction"]
