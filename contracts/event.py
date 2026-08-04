"""Evidence Event Envelope and lightweight validation.

This is intentionally a Python validator so the repository does not acquire a
new runtime dependency merely to validate the append-only JSONL ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping
import re

from .trace import TRACE_ID_RE, TraceContext


VALID_AGENTS = frozenset({
    "research", "design", "prediction", "critic", "planner", "orchestrator",
    "execution", "reporter", "system",
})
VALID_PHASES = frozenset({
    "research", "design", "evaluate", "critic", "iterate", "report"
})

VALID_EVENT_TYPES = frozenset({
    "research_targets", "research_cache_invalidated", "threshold_calibration",
    "tool_call", "design_batch", "candidate_registered", "candidate_scored",
    "candidate_eliminated", "evaluate_layer_start", "evaluate_layer_complete",
    "critic_review", "planner_adjust", "planner_plan", "planner_approval_recorded",
    "prediction_run_started", "prediction_recorded", "prediction_handoff_ready",
    "orchestrator_run_initialized", "orchestrator_approval_loaded",
    "orchestrator_task_claimed", "orchestrator_task_completed",
    "orchestrator_task_failed", "orchestrator_task_skipped",
    "orchestrator_claim_recovered", "orchestrator_task_retry_requested",
    "execution_task_started", "execution_task_completed", "execution_task_failed",
    "state_project_config_sync", "threshold_cache_sync", "candidate_index_migrated",
    "colabdesign_verify_skipped", "cheap_filter_empty", "route_c_fallback_binder",
    "route_c_under_target", "mdm_legacy_defaults_active", "invalid_RFDIFF_TIMESTEPS",
    "ligandmpnn_multiple_fasta", "ligandmpnn_fasta_no_id_marker",
    "pdb_insertion_code_detected", "benchmark_reference_candidate_registered",
    "candidate_finalized", "error", "test",
})

EVENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
TRACE_KEYS = {
    "project_id", "workflow_id", "run_id", "plan_id", "task_id", "attempt_id",
    "candidate_id", "parent_event_id",
}
ENVELOPE_KEYS = {
    "timestamp", "event_id", "agent", "event_type", "phase", "round", "targets",
    "blocks", "payload", *TRACE_KEYS,
}
FAILURE_EVENT_TYPES = frozenset({"orchestrator_task_failed", "execution_task_failed"})


def _timestamp(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp must be a non-empty ISO-8601 string")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be ISO-8601") from exc
    return value


@dataclass(frozen=True)
class EvidenceEvent:
    timestamp: str
    event_id: str
    agent: str
    event_type: str
    payload: Mapping[str, Any]
    trace_context: TraceContext | None = None
    phase: str | None = None
    round_num: int | None = None
    targets: tuple[str, ...] | None = None
    blocks: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        _timestamp(self.timestamp)
        if not isinstance(self.event_id, str) or not EVENT_ID_RE.fullmatch(self.event_id):
            raise ValueError(f"invalid event_id: {self.event_id!r}")
        if self.agent not in VALID_AGENTS:
            raise ValueError(f"unknown evidence agent: {self.agent!r}")
        if self.event_type not in VALID_EVENT_TYPES:
            raise ValueError(f"unknown evidence event_type: {self.event_type!r}")
        if self.phase is not None and self.phase not in VALID_PHASES:
            raise ValueError(f"unknown evidence phase: {self.phase!r}")
        if not isinstance(self.payload, Mapping):
            raise ValueError("event payload must be an object")
        if self.event_type in FAILURE_EVENT_TYPES:
            for field in ("code", "message", "component", "retryable"):
                if field not in self.payload:
                    raise ValueError(f"{self.event_type} requires error field {field}")
            if not isinstance(self.payload["code"], str) or not self.payload["code"]:
                raise ValueError("error code must be a non-empty string")
            if not isinstance(self.payload["message"], str) or not self.payload["message"]:
                raise ValueError("error message must be a non-empty string")
            if not isinstance(self.payload["component"], str) or not self.payload["component"]:
                raise ValueError("error component must be a non-empty string")
            if not isinstance(self.payload["retryable"], bool):
                raise ValueError("error retryable must be boolean")
            if "error_code" in self.payload:
                raise ValueError("use canonical error field code, not error_code")
        if "trace_context" in self.payload:
            raise ValueError("trace fields must be top-level, not nested in trace_context")
        for key in TRACE_KEYS.intersection(self.payload):
            value = self.payload[key]
            if value is not None and (
                not isinstance(value, str) or not TRACE_ID_RE.fullmatch(value)
            ):
                raise ValueError(f"invalid trace field {key}: {value!r}")
        if "attempt" in self.payload:
            attempt = self.payload["attempt"]
            if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
                raise ValueError("attempt must be a positive integer")
        if self.round_num is not None and (
            isinstance(self.round_num, bool)
            or not isinstance(self.round_num, int)
            or self.round_num < 1
        ):
            raise ValueError("round must be a positive integer")
        for field_name in ("targets", "blocks"):
            values = getattr(self, field_name)
            if values is not None and any(not isinstance(item, str) or not item for item in values):
                raise ValueError(f"{field_name} must contain non-empty strings")
        object.__setattr__(self, "payload", dict(self.payload))
        if self.targets is not None:
            object.__setattr__(self, "targets", tuple(self.targets))
        if self.blocks is not None:
            object.__setattr__(self, "blocks", tuple(self.blocks))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "timestamp": self.timestamp,
            "event_id": self.event_id,
            "agent": self.agent,
            "event_type": self.event_type,
        }
        if self.phase is not None:
            result["phase"] = self.phase
        if self.round_num is not None:
            result["round"] = self.round_num
        if self.targets is not None:
            result["targets"] = list(self.targets)
        if self.blocks is not None:
            result["blocks"] = list(self.blocks)
        if self.trace_context is not None:
            result.update(self.trace_context.to_dict())
        for key, value in self.payload.items():
            if key in TRACE_KEYS:
                if self.trace_context is None:
                    # Legacy callers may already put common ids at the top level.
                    result[key] = value
                elif result.get(key) not in (None, value):
                    raise ValueError(f"payload conflicts with trace field {key}")
                else:
                    result[key] = value
            elif key in {"timestamp", "event_id", "agent", "event_type"}:
                if result[key] != value:
                    raise ValueError(f"payload conflicts with envelope field {key}")
            else:
                result[key] = value
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvidenceEvent":
        if not isinstance(value, Mapping):
            raise ValueError("evidence event must be an object")
        trace_values = {key: value[key] for key in TRACE_KEYS if key in value}
        trace = None
        if "workflow_id" in trace_values and "project_id" in trace_values:
            trace = TraceContext.from_dict(trace_values)
        payload = {
            key: item
            for key, item in value.items()
            if key not in {"timestamp", "event_id", "agent", "event_type", "phase", "round", "targets", "blocks"}
            and key not in TRACE_KEYS
        }
        if (
            value.get("event_type") in FAILURE_EVENT_TYPES
            and "error_code" in payload
            and "code" not in payload
        ):
            # Read historical PR2 failure rows through the canonical envelope;
            # the append-only ledger itself is never rewritten.
            payload["code"] = payload.pop("error_code")
        if value.get("event_type") in FAILURE_EVENT_TYPES and isinstance(payload.get("error"), Mapping):
            legacy_error = payload["error"]
            legacy_code = legacy_error.get("code", legacy_error.get("error_code"))
            for field, legacy_value in {
                "code": legacy_code,
                "message": legacy_error.get("message"),
                "component": legacy_error.get("component"),
                "retryable": legacy_error.get("retryable"),
            }.items():
                if field not in payload and legacy_value is not None:
                    payload[field] = legacy_value
        return cls(
            timestamp=value.get("timestamp"),
            event_id=value.get("event_id"),
            agent=value.get("agent"),
            event_type=value.get("event_type"),
            payload=payload,
            trace_context=trace,
            phase=value.get("phase"),
            round_num=value.get("round"),
            targets=tuple(value["targets"]) if value.get("targets") is not None else None,
            blocks=tuple(value["blocks"]) if value.get("blocks") is not None else None,
        )
