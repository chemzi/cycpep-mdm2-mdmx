"""Non-authoritative diagnostic observation assembly for Launcher service."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from .boundaries import FormalBoundary
from .models import (
    CallObservation,
    DiagnosticReport,
    FormalTrace,
    OpaqueReference,
    PredictionRunLocator,
)


def observe(
    report: DiagnosticReport, boundary: str, formal: FormalBoundary
) -> DiagnosticReport:
    references = formal.references
    evidence_ids = _merge_ids(
        report.evidence_ids,
        _reference_ids(references, "evidence_id", "evidence_ids", "completion_event_id"),
    )
    artifact_ids = _merge_ids(
        report.artifact_ids, _reference_ids(references, "artifact_id", "artifact_ids")
    )
    trace = _trace_from_references(report.formal_trace, references)
    output_refs = tuple(
        OpaqueReference(kind=key.removesuffix("_id"), id=value)
        for key, value in references.items()
        if key.endswith("_id") and isinstance(value, str) and value
    )
    call = CallObservation(
        boundary=boundary,
        component=boundary,
        started_at=_now(),
        completed_at=_now(),
        output_refs=output_refs,
        formal_trace=trace,
        observed_formal_status=references.get("formal_status"),
    )
    calls = report.calls
    if not any(
        existing.boundary == boundary and existing.output_refs == output_refs
        for existing in calls
    ):
        calls = (*calls, call)
    return report.with_observation(
        current_boundary=_next_boundary(boundary),
        last_completed_boundary=boundary,
        calls=calls,
        formal_trace=trace,
        evidence_ids=evidence_ids,
        artifact_ids=artifact_ids,
        last_known_formal_status=references.get("formal_status")
        or report.last_known_formal_status,
    )


def mirror_prediction_identity(
    report: DiagnosticReport, runtime: Any, formal: FormalBoundary
) -> DiagnosticReport:
    invocation_id = (
        formal.references.get("prediction_invocation_id")
        or runtime.prediction_invocation_id
    )
    run_id = formal.references.get("prediction_run_id") or runtime.prediction_run_id
    root = formal.references.get("run_root")
    locator = (
        PredictionRunLocator(root=str(root), run_id=run_id)
        if root is not None
        else report.prediction_run_locator
    )
    return report.with_observation(
        prediction_invocation_id=invocation_id,
        prediction_run_id=run_id,
        prediction_run_locator=locator,
    )


def with_plan_trace(
    report: DiagnosticReport, plan: Mapping[str, Any]
) -> DiagnosticReport:
    return report.with_observation(formal_trace=FormalTrace(
        workflow_id=plan.get("workflow_id"),
        plan_id=plan.get("plan_id"),
        run_id=report.formal_trace.run_id,
    ))


def _reference_ids(references: Mapping[str, Any], *keys: str) -> tuple[str, ...]:
    values: list[str] = []
    for key in keys:
        value = references.get(key)
        if isinstance(value, str) and value:
            values.append(value)
        elif isinstance(value, (list, tuple)):
            values.extend(item for item in value if isinstance(item, str) and item)
    return tuple(values)


def _merge_ids(existing: tuple[str, ...], added: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*existing, *added)))


def _trace_from_references(
    trace: FormalTrace, references: Mapping[str, Any]
) -> FormalTrace:
    return FormalTrace(
        workflow_id=references.get("workflow_id") or trace.workflow_id,
        run_id=references.get("run_id") or trace.run_id,
        plan_id=references.get("plan_id") or trace.plan_id,
        task_id=references.get("task_id") or trace.task_id,
        attempt_id=references.get("attempt_id") or trace.attempt_id,
        transaction_id=references.get("transaction_id") or trace.transaction_id,
    )


def _next_boundary(boundary: str) -> str:
    order = (
        "project_approval", "research", "design", "prediction", "critic",
        "planner", "approval", "orchestrator", "execution",
    )
    try:
        return order[order.index(boundary) + 1]
    except (ValueError, IndexError):
        return boundary


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["mirror_prediction_identity", "observe", "with_plan_trace"]
