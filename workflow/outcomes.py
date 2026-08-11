"""Failure recording and browser-safe outcome projection for Launcher commands."""

from __future__ import annotations

import logging
from typing import Any, Mapping

from .boundaries import FormalBoundary
from .errors import DiagnosticContractError, normalize_error
from .models import (
    BrowserResult,
    DiagnosticReport,
    LauncherCommandResult,
    StructuredError,
)
from .observations import observe


# Preserve the established operational channel while moving its implementation.
_LOGGER = logging.getLogger("workflow.service")


def _worker_failure(runtime, session, report, orchestrator, plan, error):
    """Project a Worker failure after refreshing its formal owner records."""

    refreshed = runtime.inspect_orchestrator(plan)
    if refreshed.status == "completed":
        report = observe(report, "orchestrator", refreshed)
        orchestrator = refreshed
    formal_failure = runtime.inspect_execution_failure(orchestrator)
    if formal_failure.status == "completed":
        report = observe(report, "execution", formal_failure)
    failed = report.with_failure(
        boundary="execution",
        error=normalize_error(error, component="execution"),
        formal_status=orchestrator.references.get("formal_status"),
    )
    try:
        session.write(failed)
    except Exception as write_error:
        _log_operational_failure(write_error, component="launcher")
        failed = report.with_failure(
            boundary="execution",
            error=normalize_error(write_error, component="launcher"),
            formal_status=orchestrator.references.get("formal_status"),
        )
    return _result(failed, "failed", 2)


def _clear_resolved_failure(
    report: DiagnosticReport, boundary: str, formal: FormalBoundary
) -> DiagnosticReport:
    if report.failed_boundary == boundary and formal.status == "completed":
        return report.clear_failure()
    return report


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
    try:
        session.write(failed)
    except Exception as write_error:
        _log_operational_failure(write_error, component="launcher")
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
        _log_operational_failure(write_error, component="launcher")
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
    exit_code = 3 if status == "blocked" else 2 if status == "failed" else 0
    return _result(
        report,
        status,
        exit_code,
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
    normalized = normalize_error(error, component="launcher")
    _log_normalized_failure(normalized)
    return LauncherCommandResult(
        BrowserResult(
            status="failed",
            launcher_run_id=launcher_run_id,
            error=normalized,
        ),
        2,
    )


def _log_operational_failure(error: BaseException, *, component: str) -> None:
    _log_normalized_failure(normalize_error(error, component=component))


def _log_normalized_failure(error: StructuredError) -> None:
    _LOGGER.error(
        "launcher command failed: code=%s component=%s message=%s",
        error.code,
        error.component,
        error.message,
    )
