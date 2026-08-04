"""Execution task and task-status contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Mapping

from .action import ActionType, coerce_action_type, get_action_spec
from .trace import TraceContext


class TaskStatus(str, Enum):
    BLOCKED = "blocked"
    BLOCKED_DEPENDENCY = "blocked_dependency"
    AWAITING_APPROVAL = "awaiting_approval"
    PENDING_DEPENDENCY = "pending_dependency"
    READY = "ready"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class ExecutionGateStatus(str, Enum):
    PROPOSED = "proposed"
    BLOCKED = "blocked"


TERMINAL_TASK_STATUSES = frozenset({
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED,
    TaskStatus.SKIPPED,
})
SUCCESS_TASK_STATUSES = frozenset({TaskStatus.SUCCEEDED, TaskStatus.SKIPPED})
MUTABLE_TASK_STATUSES = frozenset({
    TaskStatus.BLOCKED,
    TaskStatus.BLOCKED_DEPENDENCY,
    TaskStatus.AWAITING_APPROVAL,
    TaskStatus.PENDING_DEPENDENCY,
    TaskStatus.READY,
})


def _status(value: str | TaskStatus | None) -> TaskStatus | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, TaskStatus) else TaskStatus(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unknown task status: {value!r}") from exc


@dataclass(frozen=True)
class ExecutionTask:
    """A typed boundary view over the Planner's existing JSON task shape."""

    task_id: str
    action: ActionType
    phase: str
    status: TaskStatus | None = None
    depends_on: tuple[str, ...] = ()
    resource_request: Mapping[str, Any] = field(default_factory=dict)
    approval: Mapping[str, Any] | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)
    trace_context: TraceContext | None = None
    execution_gate: Mapping[str, Any] | None = None
    candidate_scope: Mapping[str, Any] | None = None
    outputs: Any = ()
    agent: str | None = None
    priority: str | int | None = None
    disposition: str | None = None
    reason_codes: tuple[str, ...] = ()
    constraints: Any = field(default_factory=dict)
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not re.fullmatch(r"T[0-9]{3}", self.task_id):
            raise ValueError("task_id must match T[0-9]{3}")
        object.__setattr__(self, "action", coerce_action_type(self.action))
        if not isinstance(self.phase, str) or not self.phase:
            raise ValueError("phase must be non-empty")
        object.__setattr__(self, "status", _status(self.status))
        object.__setattr__(self, "depends_on", tuple(self.depends_on))
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        normalized_outputs = []
        for output in self.outputs:
            if isinstance(output, Mapping):
                normalized_outputs.append(dict(output))
            elif isinstance(output, str):
                # Planner v1 declares output filenames as strings; execution
                # receipts use mapping-shaped ArtifactRefs later in the flow.
                normalized_outputs.append(output)
            else:
                raise ValueError("outputs must contain strings or objects")
        object.__setattr__(self, "outputs", tuple(normalized_outputs))
        object.__setattr__(self, "resource_request", dict(self.resource_request))
        expected_resource_class = get_action_spec(self.action).resource_class
        if self.resource_request.get("class") != expected_resource_class:
            raise ValueError(
                "resource_request.class must match the canonical ActionSpec "
                f"resource_class {expected_resource_class!r}"
            )
        object.__setattr__(self, "parameters", dict(self.parameters))
        if isinstance(self.constraints, Mapping):
            normalized_constraints: Any = dict(self.constraints)
        elif isinstance(self.constraints, (list, tuple)):
            normalized_constraints = tuple(self.constraints)
        else:
            raise ValueError("constraints must be an object or array")
        object.__setattr__(self, "constraints", normalized_constraints)
        object.__setattr__(self, "extensions", dict(self.extensions))

    def to_dict(self, *, include_none: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = dict(self.extensions)
        result.update({
            "task_id": self.task_id,
            "action": self.action.value,
            "phase": self.phase,
            "depends_on": list(self.depends_on),
            "resource_request": dict(self.resource_request),
            "parameters": dict(self.parameters),
            "reason_codes": list(self.reason_codes),
            "constraints": (
                dict(self.constraints)
                if isinstance(self.constraints, Mapping)
                else list(self.constraints)
            ),
        })
        optional = {
            "status": self.status.value if self.status is not None else None,
            "approval": dict(self.approval) if self.approval is not None else None,
            "trace_context": (
                self.trace_context.to_dict() if self.trace_context is not None else None
            ),
            "execution_gate": (
                dict(self.execution_gate) if self.execution_gate is not None else None
            ),
            "candidate_scope": (
                dict(self.candidate_scope) if self.candidate_scope is not None else None
            ),
            "outputs": [
                dict(output) if isinstance(output, Mapping) else output
                for output in self.outputs
            ] if self.outputs else None,
            "agent": self.agent,
            "priority": self.priority,
            "disposition": self.disposition,
        }
        for key, value in optional.items():
            if value is not None or include_none:
                result[key] = value
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExecutionTask":
        if not isinstance(value, Mapping):
            raise ValueError("execution task must be an object")
        known = {
            "task_id", "action", "phase", "status", "depends_on", "resource_request",
            "approval", "parameters", "trace_context", "execution_gate",
            "candidate_scope", "outputs", "agent", "priority", "disposition",
            "reason_codes", "constraints",
        }
        trace_value = value.get("trace_context")
        trace = TraceContext.from_dict(trace_value) if trace_value is not None else None
        return cls(
            task_id=value.get("task_id"),
            action=coerce_action_type(value.get("action")),
            phase=value.get("phase"),
            status=value.get("status"),
            depends_on=tuple(value.get("depends_on", ())),
            resource_request=dict(value.get("resource_request", {})),
            approval=(dict(value["approval"]) if value.get("approval") is not None else None),
            parameters=dict(value.get("parameters", {})),
            trace_context=trace,
            execution_gate=(
                dict(value["execution_gate"])
                if value.get("execution_gate") is not None else None
            ),
            candidate_scope=(
                dict(value["candidate_scope"])
                if value.get("candidate_scope") is not None else None
            ),
            outputs=tuple(value.get("outputs", ())),
            agent=value.get("agent"),
            priority=value.get("priority"),
            disposition=value.get("disposition"),
            reason_codes=tuple(value.get("reason_codes", ())),
            constraints=value.get("constraints", {}),
            extensions={key: value[key] for key in value if key not in known},
        )
