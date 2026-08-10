"""Versioned diagnostic and browser-safe Launcher data contracts.

These models describe observations and opaque references only.  They expose no
API for changing formal workflow, task, transaction, or scientific state.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Mapping


DIAGNOSTIC_SCHEMA_VERSION = 1
_TRACE_FIELDS = (
    "workflow_id", "run_id", "plan_id", "task_id", "attempt_id", "transaction_id"
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string when provided")
    return value


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name} must be an array")
    result = tuple(value)
    if any(not isinstance(item, str) or not item for item in result):
        raise ValueError(f"{field_name} must contain non-empty strings")
    return result


@dataclass(frozen=True)
class OpaqueReference:
    kind: str
    id: str

    def __post_init__(self) -> None:
        _optional_text(self.kind, "reference kind")
        _optional_text(self.id, "reference id")

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.id}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OpaqueReference":
        return cls(kind=value.get("kind"), id=value.get("id"))


@dataclass(frozen=True)
class StructuredError:
    code: str
    component: str
    message: str

    def __post_init__(self) -> None:
        _optional_text(self.code, "error code")
        _optional_text(self.component, "error component")
        _optional_text(self.message, "error message")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "component": self.component, "message": self.message}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "StructuredError":
        return cls(
            code=value.get("code"),
            component=value.get("component"),
            message=value.get("message"),
        )


@dataclass(frozen=True)
class FormalTrace:
    workflow_id: str | None = None
    run_id: str | None = None
    plan_id: str | None = None
    task_id: str | None = None
    attempt_id: str | None = None
    transaction_id: str | None = None

    def __post_init__(self) -> None:
        for name in _TRACE_FIELDS:
            _optional_text(getattr(self, name), name)

    def to_dict(self) -> dict[str, str | None]:
        return {name: getattr(self, name) for name in _TRACE_FIELDS}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "FormalTrace":
        source = value or {}
        return cls(**{name: source.get(name) for name in _TRACE_FIELDS})


@dataclass(frozen=True)
class PredictionRunLocator:
    """Internal-only exact Prediction run location mirror."""

    root: str
    run_id: str

    def __post_init__(self) -> None:
        _optional_text(self.root, "prediction run root")
        _optional_text(self.run_id, "prediction run id")

    def to_dict(self) -> dict[str, str]:
        return {"root": self.root, "run_id": self.run_id}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PredictionRunLocator":
        return cls(root=value.get("root"), run_id=value.get("run_id"))


@dataclass(frozen=True)
class CallObservation:
    boundary: str
    component: str
    started_at: str
    completed_at: str | None = None
    input_refs: tuple[OpaqueReference, ...] = ()
    output_refs: tuple[OpaqueReference, ...] = ()
    formal_trace: FormalTrace = field(default_factory=FormalTrace)
    observed_formal_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "boundary": self.boundary,
            "component": self.component,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "input_refs": [item.to_dict() for item in self.input_refs],
            "output_refs": [item.to_dict() for item in self.output_refs],
            "formal_trace": self.formal_trace.to_dict(),
            "observed_formal_status": self.observed_formal_status,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CallObservation":
        return cls(
            boundary=value.get("boundary"),
            component=value.get("component"),
            started_at=value.get("started_at"),
            completed_at=value.get("completed_at"),
            input_refs=tuple(OpaqueReference.from_dict(item) for item in value.get("input_refs", ())),
            output_refs=tuple(OpaqueReference.from_dict(item) for item in value.get("output_refs", ())),
            formal_trace=FormalTrace.from_dict(value.get("formal_trace")),
            observed_formal_status=value.get("observed_formal_status"),
        )


@dataclass(frozen=True)
class BrowserResult:
    status: str
    launcher_run_id: str | None = None
    project_id: str | None = None
    approved_content_binding: str | None = None
    boundary: str | None = None
    prediction_invocation_id: str | None = None
    prediction_run_id: str | None = None
    formal_trace: FormalTrace = field(default_factory=FormalTrace)
    evidence_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    required_task_ids: tuple[str, ...] = ()
    task_status_counts: Mapping[str, int] = field(default_factory=dict)
    last_known_formal_status: str | None = None
    error: StructuredError | None = None
    schema_version: int = DIAGNOSTIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _optional_text(self.status, "status")
        if not isinstance(self.formal_trace, FormalTrace):
            raise TypeError("formal_trace must be FormalTrace")
        if self.error is not None and not isinstance(self.error, StructuredError):
            raise TypeError("error must be StructuredError")
        _string_tuple(self.required_task_ids, "required_task_ids")
        if not isinstance(self.task_status_counts, Mapping) or any(
            not isinstance(key, str)
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for key, value in self.task_status_counts.items()
        ):
            raise TypeError("task_status_counts must map strings to non-negative integers")

    def to_dict(self) -> dict[str, Any]:
        error_value = None
        if self.error is not None:
            # Local import avoids a module cycle while keeping every browser
            # projection safe even when a caller constructs StructuredError.
            from .errors import sanitize_message

            error_value = self.error.to_dict()
            error_value["message"] = sanitize_message(error_value["message"])
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "launcher_run_id": self.launcher_run_id,
            "project_id": self.project_id,
            "approved_content_binding": self.approved_content_binding,
            "boundary": self.boundary,
            "prediction_invocation_id": self.prediction_invocation_id,
            "prediction_run_id": self.prediction_run_id,
            "formal_trace": self.formal_trace.to_dict(),
            "evidence_ids": list(self.evidence_ids),
            "artifact_ids": list(self.artifact_ids),
            "required_task_ids": list(self.required_task_ids),
            "task_status_counts": dict(self.task_status_counts),
            "last_known_formal_status": self.last_known_formal_status,
            "error": error_value,
        }


@dataclass(frozen=True)
class LauncherCommandResult:
    payload: BrowserResult
    exit_code: int

    def __post_init__(self) -> None:
        if not isinstance(self.payload, BrowserResult):
            raise TypeError("payload must be BrowserResult")
        if isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int):
            raise TypeError("exit_code must be an integer")


@dataclass(frozen=True)
class DiagnosticReport:
    launcher_run_id: str
    project_id: str
    approved_content_binding: str
    project_locator: str
    created_at: str
    updated_at: str
    current_boundary: str | None = None
    failed_boundary: str | None = None
    last_completed_boundary: str | None = None
    calls: tuple[CallObservation, ...] = ()
    input_refs: tuple[OpaqueReference, ...] = ()
    output_refs: tuple[OpaqueReference, ...] = ()
    prediction_invocation_id: str | None = None
    prediction_run_id: str | None = None
    prediction_run_locator: PredictionRunLocator | None = None
    formal_trace: FormalTrace = field(default_factory=FormalTrace)
    evidence_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    last_known_formal_status: str | None = None
    failure: StructuredError | None = None
    schema_version: int = DIAGNOSTIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != DIAGNOSTIC_SCHEMA_VERSION:
            raise ValueError("unsupported launcher diagnostic schema version")
        for name in (
            "launcher_run_id", "project_id", "approved_content_binding", "project_locator",
            "created_at", "updated_at",
        ):
            _optional_text(getattr(self, name), name)
        if not isinstance(self.formal_trace, FormalTrace):
            raise TypeError("formal_trace must be FormalTrace")
        if self.failure is not None and not isinstance(self.failure, StructuredError):
            raise TypeError("failure must be StructuredError")

    @classmethod
    def initial(
        cls,
        *,
        launcher_run_id: str,
        project_id: str,
        approved_content_binding: str,
        project_locator: str,
    ) -> "DiagnosticReport":
        now = _utcnow()
        return cls(
            launcher_run_id=launcher_run_id,
            project_id=project_id,
            approved_content_binding=approved_content_binding,
            project_locator=project_locator,
            created_at=now,
            updated_at=now,
            current_boundary="research",
            last_completed_boundary="project_approval",
        )

    def with_observation(self, **updates: Any) -> "DiagnosticReport":
        """Return an updated observation; this never authorizes formal work."""

        return replace(self, updated_at=_utcnow(), failure=None, failed_boundary=None, **updates)

    def with_failure(
        self, *, boundary: str, error: StructuredError, formal_status: str | None = None
    ) -> "DiagnosticReport":
        from .errors import sanitize_message

        safe_error = replace(error, message=sanitize_message(error.message))
        return replace(
            self,
            updated_at=_utcnow(),
            current_boundary=boundary,
            failed_boundary=boundary,
            failure=safe_error,
            last_known_formal_status=formal_status or self.last_known_formal_status,
        )

    def browser_projection(
        self,
        *,
        status: str,
        required_task_ids: tuple[str, ...] = (),
        task_status_counts: Mapping[str, int] | None = None,
    ) -> BrowserResult:
        return BrowserResult(
            status=status,
            launcher_run_id=self.launcher_run_id,
            project_id=self.project_id,
            approved_content_binding=self.approved_content_binding,
            boundary=self.failed_boundary or self.current_boundary,
            prediction_invocation_id=self.prediction_invocation_id,
            prediction_run_id=self.prediction_run_id,
            formal_trace=self.formal_trace,
            evidence_ids=self.evidence_ids,
            artifact_ids=self.artifact_ids,
            required_task_ids=required_task_ids,
            task_status_counts=dict(task_status_counts or {}),
            last_known_formal_status=self.last_known_formal_status,
            error=self.failure,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "launcher_run_id": self.launcher_run_id,
            "project_id": self.project_id,
            "approved_content_binding": self.approved_content_binding,
            "project_locator": self.project_locator,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "current_boundary": self.current_boundary,
            "failed_boundary": self.failed_boundary,
            "last_completed_boundary": self.last_completed_boundary,
            "calls": [item.to_dict() for item in self.calls],
            "input_refs": [item.to_dict() for item in self.input_refs],
            "output_refs": [item.to_dict() for item in self.output_refs],
            "prediction_invocation_id": self.prediction_invocation_id,
            "prediction_run_id": self.prediction_run_id,
            "prediction_run_locator": (
                None if self.prediction_run_locator is None else self.prediction_run_locator.to_dict()
            ),
            "formal_trace": self.formal_trace.to_dict(),
            "evidence_ids": list(self.evidence_ids),
            "artifact_ids": list(self.artifact_ids),
            "last_known_formal_status": self.last_known_formal_status,
            "failure": None if self.failure is None else self.failure.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DiagnosticReport":
        if value.get("schema_version") != DIAGNOSTIC_SCHEMA_VERSION:
            raise ValueError("unsupported launcher diagnostic schema version")
        locator = value.get("prediction_run_locator")
        failure = value.get("failure")
        return cls(
            schema_version=value["schema_version"],
            launcher_run_id=value.get("launcher_run_id"),
            project_id=value.get("project_id"),
            approved_content_binding=value.get("approved_content_binding"),
            project_locator=value.get("project_locator"),
            created_at=value.get("created_at"),
            updated_at=value.get("updated_at"),
            current_boundary=value.get("current_boundary"),
            failed_boundary=value.get("failed_boundary"),
            last_completed_boundary=value.get("last_completed_boundary"),
            calls=tuple(CallObservation.from_dict(item) for item in value.get("calls", ())),
            input_refs=tuple(OpaqueReference.from_dict(item) for item in value.get("input_refs", ())),
            output_refs=tuple(OpaqueReference.from_dict(item) for item in value.get("output_refs", ())),
            prediction_invocation_id=value.get("prediction_invocation_id"),
            prediction_run_id=value.get("prediction_run_id"),
            prediction_run_locator=(None if locator is None else PredictionRunLocator.from_dict(locator)),
            formal_trace=FormalTrace.from_dict(value.get("formal_trace")),
            evidence_ids=_string_tuple(value.get("evidence_ids"), "evidence_ids"),
            artifact_ids=_string_tuple(value.get("artifact_ids"), "artifact_ids"),
            last_known_formal_status=value.get("last_known_formal_status"),
            failure=None if failure is None else StructuredError.from_dict(failure),
        )


__all__ = [
    "BrowserResult", "CallObservation", "DIAGNOSTIC_SCHEMA_VERSION",
    "DiagnosticReport", "FormalTrace", "LauncherCommandResult", "OpaqueReference",
    "PredictionRunLocator", "StructuredError",
]
