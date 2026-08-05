"""Typed result returned by every Execution handler."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .staging import StagedArtifact


@dataclass(frozen=True)
class ExecutionActionResult:
    candidate_updates: tuple[Mapping[str, Any], ...] = ()
    state_updates: Mapping[str, Any] | None = None
    artifacts: tuple[StagedArtifact, ...] = ()
    outputs: tuple[tuple[str, Path], ...] = ()
    processes: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_updates", tuple(self.candidate_updates))
        object.__setattr__(self, "state_updates", dict(self.state_updates or {}))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(self, "outputs", tuple(self.outputs))
        object.__setattr__(self, "processes", tuple(self.processes))

    @property
    def elapsed_seconds(self) -> float:
        return sum(float(item.get("elapsed_seconds") or 0.0) for item in self.processes)
