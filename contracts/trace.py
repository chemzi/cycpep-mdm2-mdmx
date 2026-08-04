"""Trace context shared by Planner, Orchestrator, Execution and Evidence."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

TRACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _validate_id(value: str | None, label: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{label} is required")
        return None
    if not isinstance(value, str) or not TRACE_ID_RE.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


@dataclass(frozen=True)
class TraceContext:
    project_id: str
    workflow_id: str
    run_id: str | None = None
    plan_id: str | None = None
    task_id: str | None = None
    attempt_id: str | None = None
    candidate_id: str | None = None
    parent_event_id: str | None = None

    def __post_init__(self) -> None:
        _validate_id(self.project_id, "project_id", required=True)
        _validate_id(self.workflow_id, "workflow_id", required=True)
        for name in (
            "run_id",
            "plan_id",
            "task_id",
            "attempt_id",
            "candidate_id",
            "parent_event_id",
        ):
            value = _validate_id(getattr(self, name), name)
            if name == "task_id" and value is not None and not re.fullmatch(
                r"T[0-9]{3}", value
            ):
                raise ValueError(f"invalid task_id: {value!r}")

    def to_dict(self, *, omit_none: bool = True) -> dict[str, str]:
        values = {
            "project_id": self.project_id,
            "workflow_id": self.workflow_id,
            "run_id": self.run_id,
            "plan_id": self.plan_id,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "candidate_id": self.candidate_id,
            "parent_event_id": self.parent_event_id,
        }
        return {
            key: value
            for key, value in values.items()
            if value is not None or not omit_none
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TraceContext":
        if not isinstance(value, Mapping):
            raise ValueError("trace context must be an object")
        return cls(
            project_id=value.get("project_id"),
            workflow_id=value.get("workflow_id"),
            run_id=value.get("run_id"),
            plan_id=value.get("plan_id"),
            task_id=value.get("task_id"),
            attempt_id=value.get("attempt_id"),
            candidate_id=value.get("candidate_id"),
            parent_event_id=value.get("parent_event_id"),
        )

    def with_updates(self, **updates: str | None) -> "TraceContext":
        values = self.to_dict(omit_none=False)
        values.update(updates)
        return type(self)(**values)

    @staticmethod
    def attempt_id_for(task_id: str, attempt: int) -> str:
        if not isinstance(task_id, str) or not re.fullmatch(r"T[0-9]{3}", task_id):
            raise ValueError("task_id must match T[0-9]{3}")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("attempt must be a positive integer")
        return f"{task_id}-A{attempt:02d}"


def derive_workflow_id(
    project_id: str,
    source_id: str,
    source_sha256: str,
    source_round: int | str | None = None,
) -> str:
    """Derive a stable workflow id without introducing another hash utility."""

    # Lazy import avoids loading prediction_pipeline.__init__ while data_layer
    # is importing the contracts package.  The repository's existing canonical
    # SHA-256 implementation remains the single hash dependency.
    from prediction_pipeline.contracts import object_sha256

    _validate_id(project_id, "project_id", required=True)
    _validate_id(source_id, "source_id", required=True)
    if not isinstance(source_sha256, str) or not SHA256_RE.fullmatch(source_sha256):
        raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
    return "workflow_" + object_sha256({
        "project_id": project_id,
        "source_id": source_id,
        "source_sha256": source_sha256,
        "source_round": source_round,
    })[:12]
