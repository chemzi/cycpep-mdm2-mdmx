"""Formal Store-to-Planner handoff for one ExplorationDecision."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from agents.planner import RECOMMENDATION_MAPPINGS
from contracts.action import ActionType
from contracts.exploration_decision import (
    ExplorationDecision,
    ExplorationDecisionContractError,
    require_trace_id,
)
from storage.base import EvidenceStore


class ExplorationDecisionHandoffError(ValueError):
    """Raised when formal workflow handoff publications are unusable."""

    component = "workflow"

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ExplorationDecisionHandoff:
    """Immutable workflow identity and optional canonical Decision handoff."""

    workflow_id: str
    required: bool
    decision: ExplorationDecision | None = None

    @property
    def exploration_decision(self) -> dict[str, Any] | None:
        """Return a detached canonical mapping suitable for Planner service."""
        return self.decision.to_dict() if self.decision is not None else None


def resolve_exploration_decision_handoff(
    *,
    store: EvidenceStore,
    critic_report_path: str | Path,
    project_id: str,
    state: Mapping[str, Any],
) -> ExplorationDecisionHandoff:
    """Resolve the formal Prediction identity and current-round Decision."""
    report = _read_critic_report(critic_report_path)
    source = report.get("source")
    if not isinstance(source, Mapping) or source.get("project_id") != project_id:
        raise ExplorationDecisionHandoffError(
            "critic_handoff_project_mismatch",
            "selected Critic artifact does not match the runtime project",
        )
    prediction_run_id = source.get("prediction_run_id")
    if not isinstance(prediction_run_id, str) or not prediction_run_id:
        raise ExplorationDecisionHandoffError(
            "critic_handoff_prediction_run_missing",
            "selected Critic artifact has no Prediction run identity",
        )

    required = requires_exploration_decision(report)
    workflow_id = _resolve_prediction_workflow_id(
        store, project_id=project_id, prediction_run_id=prediction_run_id
    )
    source_round = _source_round(state)
    decision = _resolve_decision(
        store,
        project_id=project_id,
        prediction_run_id=prediction_run_id,
        source_round=source_round,
        required=required,
    )
    return ExplorationDecisionHandoff(
        workflow_id=workflow_id,
        required=required,
        decision=decision,
    )


def _read_critic_report(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExplorationDecisionHandoffError(
            "critic_handoff_invalid", "selected Critic artifact is not readable JSON"
        ) from exc
    if not isinstance(value, dict):
        raise ExplorationDecisionHandoffError(
            "critic_handoff_invalid", "selected Critic artifact must be an object"
        )
    return value


def requires_exploration_decision(report: Mapping[str, Any]) -> bool:
    recommendations = report.get("recommendations")
    if not isinstance(recommendations, list):
        raise ExplorationDecisionHandoffError(
            "critic_recommendations_invalid",
            "selected Critic artifact recommendations must be an array",
        )
    required = False
    for recommendation in recommendations:
        if not isinstance(recommendation, Mapping):
            raise ExplorationDecisionHandoffError(
                "critic_recommendations_invalid",
                "selected Critic artifact recommendation must be an object",
            )
        action = recommendation.get("action")
        mapping = RECOMMENDATION_MAPPINGS.get(action)
        if mapping is None:
            raise ExplorationDecisionHandoffError(
                "planner_action_unknown",
                f"Planner has no public mapping for recommendation {action!r}",
            )
        required = required or mapping.task_action is ActionType.ITERATE_DESIGN
    return required


def _resolve_prediction_workflow_id(
    store: EvidenceStore, *, project_id: str, prediction_run_id: str
) -> str:
    events = [
        event
        for event in store.query(
            project_id=project_id,
            agent="prediction",
            event_type="prediction_handoff_ready",
        )
        if event.get("prediction_run_id") == prediction_run_id
    ]
    if not events:
        raise ExplorationDecisionHandoffError(
            "prediction_workflow_identity_missing",
            "formal Prediction workflow identity is missing",
        )
    if len(events) != 1:
        raise ExplorationDecisionHandoffError(
            "prediction_workflow_identity_ambiguous",
            "formal Prediction workflow identity is ambiguous",
        )
    try:
        return require_trace_id(events[0].get("workflow_id"), "workflow_id")
    except ExplorationDecisionContractError as exc:
        raise ExplorationDecisionHandoffError(
            "prediction_workflow_identity_invalid",
            "formal Prediction workflow identity is invalid",
        ) from exc


def _source_round(state: Mapping[str, Any]) -> int:
    try:
        return int(state.get("round") or 1)
    except (TypeError, ValueError) as exc:
        raise ExplorationDecisionHandoffError(
            "planner_state_round_invalid", "Planner State round is invalid"
        ) from exc


def _resolve_decision(
    store: EvidenceStore,
    *,
    project_id: str,
    prediction_run_id: str,
    source_round: int,
    required: bool,
) -> ExplorationDecision | None:
    matches = [
        event
        for event in store.query(
            project_id=project_id,
            agent="critic",
            event_type="exploration_decision",
        )
        if event.get("prediction_run_id") == prediction_run_id
        and event.get("source_round") == source_round
    ]
    if not matches:
        if required:
            raise ExplorationDecisionHandoffError(
                "exploration_decision_required",
                "closed-loop iterate_design planning requires an ExplorationDecision",
            )
        return None
    if len(matches) != 1:
        raise ExplorationDecisionHandoffError(
            "exploration_decision_ambiguous",
            "formal ExplorationDecision publication is ambiguous",
        )
    try:
        return ExplorationDecision.from_dict(matches[0])
    except (ExplorationDecisionContractError, TypeError, ValueError) as exc:
        raise ExplorationDecisionHandoffError(
            "exploration_decision_invalid",
            "formal ExplorationDecision publication is invalid",
        ) from exc


__all__ = [
    "ExplorationDecisionHandoff",
    "ExplorationDecisionHandoffError",
    "requires_exploration_decision",
    "resolve_exploration_decision_handoff",
]
