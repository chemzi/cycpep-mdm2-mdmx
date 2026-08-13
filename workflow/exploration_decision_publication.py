"""Publish the current formal E2 Decision before closed-loop planning."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from contracts.exploration_decision import ExplorationDecision
from contracts.trace import TraceContext
from prediction_pipeline.contracts import file_sha256
from exploration import (
    build_exploration_shortlist_event,
    exploration_shortlist,
    record_exploration_shortlist,
)
from exploration_decision import build_exploration_decision, record_exploration_decision

from .exploration_decision_handoff import requires_exploration_decision


class ExplorationDecisionPublicationError(ValueError):
    """Raised when current formal evidence cannot publish one Decision."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExplorationDecisionPublicationError(
            f"{label}_invalid", f"{label} is missing or invalid"
        ) from exc
    if not isinstance(value, dict):
        raise ExplorationDecisionPublicationError(
            f"{label}_invalid", f"{label} must be an object"
        )
    return value


def _same_payload(event: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    return all(event.get(key) == value for key, value in payload.items())


@dataclass(frozen=True)
class _PublicationSources:
    project_id: str
    handoff: Mapping[str, Any]
    targets: tuple[str, ...]
    batteries: tuple[Mapping[str, Any], ...]
    thresholds: Mapping[str, Any]
    prediction_run_id: str


def _resolve_sources(*, store, project_config, report, prediction) -> _PublicationSources:
    project_id = str(project_config.get("project_id") or "")
    if not project_id or getattr(store, "project_id", project_id) != project_id:
        raise ExplorationDecisionPublicationError(
            "exploration_decision_store_mismatch",
            "publication Store does not match the approved project",
        )
    if getattr(prediction, "status", None) != "completed":
        raise ExplorationDecisionPublicationError(
            "exploration_decision_prediction_incomplete",
            "Decision publication requires completed Prediction authority",
        )
    references = getattr(prediction, "references", {})
    prediction_run_id = str(references.get("prediction_run_id") or "")
    handoff_path = Path(str(references.get("handoff_path") or ""))
    transaction_id = str(references.get("transaction_id") or "")
    if transaction_id:
        threshold_path = Path(str(references.get("thresholds_path") or ""))
        threshold_sha = str(references.get("thresholds_sha256") or "")
        threshold_artifact_id = str(
            references.get("thresholds_artifact_id") or ""
        )
        handoff_artifact_id = str(references.get("handoff_artifact_id") or "")
        if not threshold_artifact_id or not handoff_artifact_id:
            raise ExplorationDecisionPublicationError(
                "thresholds_invalid",
                "bootstrap Prediction threshold locator is missing",
            )
        thresholds = _json_object(threshold_path, "thresholds")
        if file_sha256(threshold_path) != threshold_sha:
            raise ExplorationDecisionPublicationError(
                "thresholds_invalid",
                "bootstrap Prediction threshold snapshot changed after readiness",
            )
    else:
        thresholds = _json_object(
            handoff_path.parent / "inputs" / "thresholds.json", "thresholds"
        )
        handoff_artifact_id = ""
    handoffs = [
        row
        for row in store.query(
            project_id=project_id,
            agent="prediction",
            event_type="prediction_handoff_ready",
        )
        if row.get("prediction_run_id") == prediction_run_id
        and (
            row.get("handoff_artifact_id") == handoff_artifact_id
            if transaction_id
            else row.get("handoff_path") == str(handoff_path)
        )
        and (
            row.get("thresholds_artifact_id") == threshold_artifact_id
            if transaction_id
            else True
        )
    ]
    if len(handoffs) != 1:
        raise ExplorationDecisionPublicationError(
            "exploration_decision_handoff_ambiguous",
            "current formal Prediction handoff is missing or ambiguous",
        )
    handoff = handoffs[0]
    source = report.get("source") or {}
    targets = tuple(source.get("required_targets") or ())
    candidate_ids = tuple(handoff.get("candidate_ids") or ())
    batteries = tuple(
        row
        for row in store.query(
            project_id=project_id,
            agent="prediction",
            event_type="battery_evaluated",
        )
        if row.get("workflow_id") == handoff.get("workflow_id")
        and row.get("run_id") == handoff.get("run_id")
        and row.get("prediction_run_id") == prediction_run_id
        and row.get("candidate_id") in candidate_ids
    )
    if {row.get("candidate_id") for row in batteries} != set(candidate_ids):
        raise ExplorationDecisionPublicationError(
            "exploration_decision_battery_incomplete",
            "current formal battery evidence is incomplete",
        )
    return _PublicationSources(
        project_id, handoff, targets, batteries, thresholds, prediction_run_id
    )


def _resolve_shortlist(*, store, project_config, sources, source_round):
    handoff = sources.handoff
    payload = exploration_shortlist(
        sources.batteries,
        targets=list(sources.targets),
        thresholds=sources.thresholds,
    )
    matches = [
        row
        for row in store.query(
            project_id=sources.project_id,
            agent="critic",
            event_type="exploration_shortlist",
        )
        if row.get("workflow_id") == handoff.get("workflow_id")
        and row.get("run_id") == handoff.get("run_id")
        and row.get("round") == source_round
        and set(row.get("targets") or ()) == set(sources.targets)
    ]
    if len(matches) > 1 or (matches and not _same_payload(matches[0], payload)):
        raise ExplorationDecisionPublicationError(
            "exploration_shortlist_ambiguous",
            "current formal exploration shortlist conflicts",
        )
    if matches:
        return matches[0]
    trace = TraceContext(
        sources.project_id,
        str(handoff.get("workflow_id") or ""),
        str(handoff.get("run_id") or ""),
    )
    prospective = build_exploration_shortlist_event(
        payload,
        targets=list(sources.targets),
        round_num=source_round,
        trace_context=trace,
    )
    _build_decision(project_config, sources, prospective, source_round, store)
    event_id = record_exploration_shortlist(
        payload,
        targets=list(sources.targets),
        round_num=source_round,
        trace_context=trace,
        store=store,
    )
    return next(row for row in store.query() if row.get("event_id") == event_id)


def _build_decision(project_config, sources, shortlist, source_round, store):
    handoff = sources.handoff
    return build_exploration_decision(
        battery_events=sources.batteries,
        shortlist_event=shortlist,
        prediction_handoff_event=handoff,
        project_config=project_config,
        thresholds=sources.thresholds,
        project_id=sources.project_id,
        workflow_id=str(handoff.get("workflow_id") or ""),
        run_id=str(handoff.get("run_id") or ""),
        target_ids=sources.targets,
        source_round=source_round,
        store=store,
    )


def _resolve_decision(*, store, decision, sources, source_round):
    rows = [
        row
        for row in store.query(
            project_id=sources.project_id,
            agent="critic",
            event_type="exploration_decision",
        )
        if row.get("workflow_id") == sources.handoff.get("workflow_id")
        and row.get("run_id") == sources.handoff.get("run_id")
        and row.get("prediction_run_id") == sources.prediction_run_id
        and row.get("source_round") == source_round
    ]
    if len(rows) > 1:
        raise ExplorationDecisionPublicationError(
            "exploration_decision_ambiguous", "current formal Decision is ambiguous"
        )
    if rows:
        restored = ExplorationDecision.from_dict(rows[0])
        if restored.to_dict() != decision.to_dict():
            raise ExplorationDecisionPublicationError(
                "exploration_decision_ambiguous", "current formal Decision conflicts"
            )
        return restored
    record_exploration_decision(decision, store=store)
    return decision


def publish_exploration_decision(
    *,
    store,
    project_config: Mapping[str, Any],
    critic_report_path: str | Path,
    prediction,
    source_round: int,
) -> ExplorationDecision | None:
    """Inspect or publish the current shortlist and immutable Decision."""
    report = _json_object(Path(critic_report_path), "critic_report")
    if not requires_exploration_decision(report):
        return None
    sources = _resolve_sources(
        store=store,
        project_config=project_config,
        report=report,
        prediction=prediction,
    )
    shortlist = _resolve_shortlist(
        store=store,
        project_config=project_config,
        sources=sources,
        source_round=source_round,
    )
    decision = _build_decision(
        project_config, sources, shortlist, source_round, store
    )
    return _resolve_decision(
        store=store,
        decision=decision,
        sources=sources,
        source_round=source_round,
    )


__all__ = [
    "ExplorationDecisionPublicationError",
    "publish_exploration_decision",
]
