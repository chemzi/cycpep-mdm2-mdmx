"""Canonical action vocabulary and capability descriptions.

Recommendation mappings are deliberately separate from executable capability.
The latter is represented by :data:`ACTION_CATALOG`; the execution registry
binds executable entries to the real handlers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class ActionType(str, Enum):
    ITERATE_DESIGN = "iterate_design"
    EVALUATE_NEW_DESIGN_CANDIDATES = "evaluate_new_design_candidates"
    REVIEW_PREDICTION_HANDOFF = "review_prediction_handoff"
    PROPOSE_THRESHOLD_CALIBRATION = "propose_threshold_calibration"
    DOCK_SHORTLISTED_CANDIDATES = "dock_shortlisted_candidates"
    RUN_MD_ON_DOCKING_CONSENSUS = "run_md_on_docking_consensus"
    REGENERATE_INVALID_ARTIFACTS = "regenerate_invalid_artifacts"
    AUDIT_DUPLICATE_CANDIDATES = "audit_duplicate_candidates"
    REPAIR_CANDIDATE_INDEX = "repair_candidate_index"
    PREPARE_FINAL_CANDIDATE_REPORT = "prepare_final_candidate_report"


def _as_action(value: str | ActionType) -> ActionType:
    try:
        return value if isinstance(value, ActionType) else ActionType(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unknown action: {value!r}") from exc


@dataclass(frozen=True)
class ActionSpec:
    """Execution capability metadata, not Planner policy."""

    action: ActionType
    handler_name: str | None
    executable: bool
    resource_class: str
    output_roles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        action = _as_action(self.action)
        object.__setattr__(self, "action", action)
        if self.handler_name is not None and not isinstance(self.handler_name, str):
            raise ValueError("handler_name must be a string or None")
        if not isinstance(self.executable, bool):
            raise ValueError("executable must be boolean")
        if not isinstance(self.resource_class, str) or not self.resource_class:
            raise ValueError("resource_class must be non-empty")
        roles = tuple(self.output_roles)
        if any(not isinstance(role, str) or not role for role in roles):
            raise ValueError("output_roles must contain non-empty strings")
        object.__setattr__(self, "output_roles", roles)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "handler_name": self.handler_name,
            "executable": self.executable,
            "resource_class": self.resource_class,
            "output_roles": list(self.output_roles),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ActionSpec":
        if not isinstance(value, Mapping):
            raise ValueError("ActionSpec must be an object")
        return cls(
            action=_as_action(value.get("action")),
            handler_name=value.get("handler_name"),
            executable=value.get("executable"),
            resource_class=value.get("resource_class"),
            output_roles=tuple(value.get("output_roles", ())),
        )


@dataclass(frozen=True)
class RecommendationMapping:
    """Planner-only mapping from a recommendation to a task action."""

    recommendation: str
    task_action: ActionType
    agent: str
    phase: str
    kind: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_action", _as_action(self.task_action))
        for name in ("recommendation", "agent", "phase", "kind"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be non-empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "recommendation": self.recommendation,
            "task_action": self.task_action.value,
            "agent": self.agent,
            "phase": self.phase,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RecommendationMapping":
        return cls(
            recommendation=value["recommendation"],
            task_action=_as_action(value["task_action"]),
            agent=value["agent"],
            phase=value["phase"],
            kind=value["kind"],
        )


ACTION_CATALOG: dict[ActionType, ActionSpec] = {
    ActionType.ITERATE_DESIGN: ActionSpec(
        ActionType.ITERATE_DESIGN, "iterate_design", True, "gpu", ("design_result",)
    ),
    ActionType.EVALUATE_NEW_DESIGN_CANDIDATES: ActionSpec(
        ActionType.EVALUATE_NEW_DESIGN_CANDIDATES,
        "evaluate_new_design_candidates",
        True,
        "gpu",
        ("prediction_handoff",),
    ),
    ActionType.REVIEW_PREDICTION_HANDOFF: ActionSpec(
        ActionType.REVIEW_PREDICTION_HANDOFF,
        "review_prediction_handoff",
        True,
        "cpu",
        ("critic_report",),
    ),
    ActionType.PROPOSE_THRESHOLD_CALIBRATION: ActionSpec(
        ActionType.PROPOSE_THRESHOLD_CALIBRATION,
        "propose_threshold_calibration",
        True,
        "network_cpu",
        ("calibration_proposal",),
    ),
    ActionType.DOCK_SHORTLISTED_CANDIDATES: ActionSpec(
        ActionType.DOCK_SHORTLISTED_CANDIDATES, None, False, "gpu"
    ),
    ActionType.RUN_MD_ON_DOCKING_CONSENSUS: ActionSpec(
        ActionType.RUN_MD_ON_DOCKING_CONSENSUS, None, False, "gpu"
    ),
    ActionType.REGENERATE_INVALID_ARTIFACTS: ActionSpec(
        ActionType.REGENERATE_INVALID_ARTIFACTS, None, False, "gpu"
    ),
    ActionType.AUDIT_DUPLICATE_CANDIDATES: ActionSpec(
        ActionType.AUDIT_DUPLICATE_CANDIDATES, None, False, "cpu"
    ),
    ActionType.REPAIR_CANDIDATE_INDEX: ActionSpec(
        ActionType.REPAIR_CANDIDATE_INDEX, None, False, "cpu"
    ),
    ActionType.PREPARE_FINAL_CANDIDATE_REPORT: ActionSpec(
        ActionType.PREPARE_FINAL_CANDIDATE_REPORT, None, False, "cpu"
    ),
}

EXECUTABLE_ACTION_TYPES = frozenset(
    action for action, spec in ACTION_CATALOG.items() if spec.executable
)
V2_RESERVED_ACTION_TYPES = frozenset({
    ActionType.DOCK_SHORTLISTED_CANDIDATES,
    ActionType.RUN_MD_ON_DOCKING_CONSENSUS,
})
KNOWN_UNIMPLEMENTED_ACTION_TYPES = frozenset(
    action
    for action, spec in ACTION_CATALOG.items()
    if not spec.executable and action not in V2_RESERVED_ACTION_TYPES
)
ALL_ACTION_TYPES = frozenset(ACTION_CATALOG)


def coerce_action_type(value: str | ActionType) -> ActionType:
    return _as_action(value)


def get_action_spec(value: str | ActionType) -> ActionSpec:
    action = _as_action(value)
    try:
        return ACTION_CATALOG[action]
    except KeyError as exc:  # defensive if the enum and catalog drift
        raise ValueError(f"action has no catalog entry: {action.value}") from exc
