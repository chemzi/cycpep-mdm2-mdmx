"""Typed result returned by Execution handlers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .staging import StagedArtifact


@dataclass(frozen=True)
class StateAppendMutation:
    """Append one state-list item only when its semantic identity is absent."""

    key: str
    item: Mapping[str, Any]
    identity_path: tuple[str, ...]
    identity_value: Any

    def __post_init__(self) -> None:
        object.__setattr__(self, "item", dict(self.item))
        object.__setattr__(self, "identity_path", tuple(self.identity_path))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "append_if_absent",
            "key": self.key,
            "item": dict(self.item),
            "identity_path": list(self.identity_path),
            "identity_value": self.identity_value,
        }


@dataclass(frozen=True)
class CandidatePatchMutation:
    """Patch one existing candidate row during the formal transaction commit."""

    candidate_id: str
    patch: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "patch", dict(self.patch))

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "patch": dict(self.patch),
        }


@dataclass(frozen=True)
class ExecutionActionResult:
    candidate_updates: tuple[Mapping[str, Any], ...] = ()
    candidate_patches: tuple[CandidatePatchMutation, ...] = ()
    state_updates: Mapping[str, Any] | None = None
    state_appends: tuple[StateAppendMutation, ...] = ()
    artifacts: tuple[StagedArtifact, ...] = ()
    evidence_events: tuple[Mapping[str, Any], ...] = ()
    outputs: tuple[tuple[str, Path], ...] = ()
    processes: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_updates", tuple(self.candidate_updates))
        object.__setattr__(self, "candidate_patches", tuple(self.candidate_patches))
        object.__setattr__(self, "state_updates", dict(self.state_updates or {}))
        object.__setattr__(self, "state_appends", tuple(self.state_appends))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(
            self, "evidence_events", tuple(dict(item) for item in self.evidence_events)
        )
        object.__setattr__(self, "outputs", tuple(self.outputs))
        object.__setattr__(self, "processes", tuple(self.processes))

    @property
    def elapsed_seconds(self) -> float:
        return sum(float(item.get("elapsed_seconds") or 0.0) for item in self.processes)
