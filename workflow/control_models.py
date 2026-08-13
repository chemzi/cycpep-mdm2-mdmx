"""Validated, browser-safe contracts for project launch and approval controls.

The models in this module carry requests and projections only.  They do not
own workflow state, persist policy, or expose runtime locators.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from .diagnostics import validate_launcher_run_id
from .errors import DiagnosticContractError, sanitize_message


_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_PLAN_ID_RE = re.compile(r"^planner_[0-9a-f]{12}$")
_TASK_ID_RE = re.compile(r"^T[0-9]{3}$")
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_IDENTIFIER_TYPES = frozenset({"auto", "gene", "uniprot", "pdb"})
_RESOURCE_CLASSES = frozenset({"cpu", "network_cpu", "gpu"})
_ESTIMATE_STATUSES = frozenset(
    {"estimated", "benchmark_required", "unavailable", "not_applicable"}
)
_CALIBRATION_STATUSES = frozenset(
    {"calibrated", "provisional", "pending", "unavailable", "not_applicable"}
)
_CEILING_NAMES = frozenset(
    {
        "max_gpu_job_slots",
        "max_gpu_minutes",
        "max_design_proposals",
        "max_prediction_candidates",
    }
)


def _text(value: Any, name: str, *, maximum: int = 320) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if value != value.strip() or "\n" in value or "\r" in value:
        raise ValueError(f"{name} must be one trimmed line")
    if len(value) > maximum:
        raise ValueError(f"{name} is too long")
    return value


def _opaque_id(value: Any, name: str) -> str:
    result = _text(value, name, maximum=128)
    if not _OPAQUE_ID_RE.fullmatch(result) or ".." in result:
        raise ValueError(f"{name} must be an opaque identifier")
    return result


def _token(value: Any, name: str) -> str:
    result = _text(value, name, maximum=64)
    if not _TOKEN_RE.fullmatch(result):
        raise ValueError(f"{name} must be a lowercase token")
    return result


def _version(value: Any, name: str) -> str:
    result = _text(value, name, maximum=64)
    if not re.fullmatch(r"^[a-z0-9][a-z0-9._-]{0,63}$", result):
        raise ValueError(f"{name} must be a version token")
    return result


def _launcher_id(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return validate_launcher_run_id(value)
    except DiagnosticContractError as error:
        raise ValueError("launcher_run_id is invalid") from error


def _digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _nonnegative_int(value: Any, name: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _positive_number(value: Any, name: str, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive finite number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return result


def _nonnegative_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a non-negative finite number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be a non-negative finite number")
    return result


def _task_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    result = tuple(values)
    if not result or len(set(result)) != len(result):
        raise ValueError("required_task_ids must be non-empty and unique")
    if any(not isinstance(value, str) or not _TASK_ID_RE.fullmatch(value) for value in result):
        raise ValueError("required_task_ids contain an invalid task identifier")
    return result


@dataclass(frozen=True)
class ApprovalCeilings:
    """Planner-compatible manual approval limits; null preserves its meaning."""

    max_gpu_job_slots: int | None = None
    max_gpu_minutes: float | None = None
    max_design_proposals: int | None = None
    max_prediction_candidates: int | None = None

    def __post_init__(self) -> None:
        _nonnegative_int(self.max_gpu_job_slots, "max_gpu_job_slots", optional=True)
        _positive_number(self.max_gpu_minutes, "max_gpu_minutes", optional=True)
        _nonnegative_int(self.max_design_proposals, "max_design_proposals", optional=True)
        _nonnegative_int(
            self.max_prediction_candidates, "max_prediction_candidates", optional=True
        )

    def to_dict(self) -> dict[str, int | float | None]:
        return {
            "max_gpu_job_slots": self.max_gpu_job_slots,
            "max_gpu_minutes": self.max_gpu_minutes,
            "max_design_proposals": self.max_design_proposals,
            "max_prediction_candidates": self.max_prediction_candidates,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ApprovalCeilings":
        return cls(
            max_gpu_job_slots=value.get("max_gpu_job_slots"),
            max_gpu_minutes=value.get("max_gpu_minutes"),
            max_design_proposals=value.get("max_design_proposals"),
            max_prediction_candidates=value.get("max_prediction_candidates"),
        )


@dataclass(frozen=True)
class AutoApprovalCeilings:
    """Complete, fail-closed ceilings for this run's first GPU gate only."""

    max_gpu_job_slots: int
    max_gpu_minutes: float
    max_design_proposals: int
    max_prediction_candidates: int

    def __post_init__(self) -> None:
        _nonnegative_int(self.max_gpu_job_slots, "max_gpu_job_slots")
        _positive_number(self.max_gpu_minutes, "max_gpu_minutes")
        _nonnegative_int(self.max_design_proposals, "max_design_proposals")
        _nonnegative_int(self.max_prediction_candidates, "max_prediction_candidates")

    def to_dict(self) -> dict[str, int | float]:
        return {
            "max_gpu_job_slots": self.max_gpu_job_slots,
            "max_gpu_minutes": self.max_gpu_minutes,
            "max_design_proposals": self.max_design_proposals,
            "max_prediction_candidates": self.max_prediction_candidates,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AutoApprovalCeilings":
        return cls(
            max_gpu_job_slots=value.get("max_gpu_job_slots"),
            max_gpu_minutes=value.get("max_gpu_minutes"),
            max_design_proposals=value.get("max_design_proposals"),
            max_prediction_candidates=value.get("max_prediction_candidates"),
        )


@dataclass(frozen=True)
class FirstGateAutoApprovalPolicy:
    approver: str
    justification: str
    ceilings: AutoApprovalCeilings

    def __post_init__(self) -> None:
        _text(self.approver, "approver", maximum=128)
        _text(self.justification, "justification")
        if not isinstance(self.ceilings, AutoApprovalCeilings):
            raise ValueError("ceilings must be AutoApprovalCeilings")

    def to_dict(self) -> dict[str, Any]:
        return {
            "approver": self.approver,
            "justification": self.justification,
            "ceilings": self.ceilings.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FirstGateAutoApprovalPolicy":
        return cls(
            approver=value.get("approver"),
            justification=value.get("justification"),
            ceilings=AutoApprovalCeilings.from_dict(value.get("ceilings") or {}),
        )


@dataclass(frozen=True)
class ProjectLaunchOptions:
    identifier_type: str = "auto"
    organism_id: int = 9606
    epitope: str | None = None
    objective: str = "binder"
    launcher_run_id: str | None = None
    first_gate_auto_policy: FirstGateAutoApprovalPolicy | None = None

    def __post_init__(self) -> None:
        if self.identifier_type not in _IDENTIFIER_TYPES:
            raise ValueError("identifier_type is unsupported")
        if isinstance(self.organism_id, bool) or not isinstance(self.organism_id, int):
            raise ValueError("organism_id must be a positive integer")
        if self.organism_id <= 0:
            raise ValueError("organism_id must be a positive integer")
        if self.epitope is not None:
            _text(self.epitope, "epitope")
        _token(self.objective, "objective")
        _launcher_id(self.launcher_run_id)
        if self.first_gate_auto_policy is not None and not isinstance(
            self.first_gate_auto_policy, FirstGateAutoApprovalPolicy
        ):
            raise ValueError("first_gate_auto_policy has an invalid type")

    def to_dict(self) -> dict[str, Any]:
        return {
            "identifier_type": self.identifier_type,
            "organism_id": self.organism_id,
            "epitope": self.epitope,
            "objective": self.objective,
            "launcher_run_id": self.launcher_run_id,
            "first_gate_auto_policy": (
                None
                if self.first_gate_auto_policy is None
                else self.first_gate_auto_policy.to_dict()
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "ProjectLaunchOptions":
        source = value or {}
        policy = source.get("first_gate_auto_policy")
        return cls(
            identifier_type=source.get("identifier_type", "auto"),
            organism_id=source.get("organism_id", 9606),
            epitope=source.get("epitope"),
            objective=source.get("objective", "binder"),
            launcher_run_id=source.get("launcher_run_id"),
            first_gate_auto_policy=(
                None if policy is None else FirstGateAutoApprovalPolicy.from_dict(policy)
            ),
        )


@dataclass(frozen=True)
class ProjectLaunchRequest:
    target_identifier: str
    options: ProjectLaunchOptions = field(default_factory=ProjectLaunchOptions)

    def __post_init__(self) -> None:
        _text(self.target_identifier, "target_identifier", maximum=128)
        if not isinstance(self.options, ProjectLaunchOptions):
            raise ValueError("options must be ProjectLaunchOptions")

    def to_dict(self) -> dict[str, Any]:
        return {"target_identifier": self.target_identifier, "options": self.options.to_dict()}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProjectLaunchRequest":
        return cls(
            target_identifier=value.get("target_identifier"),
            options=ProjectLaunchOptions.from_dict(value.get("options")),
        )


@dataclass(frozen=True)
class TaskResourceProjection:
    task_id: str
    action: str
    resource_class: str
    gpu_job_slots: int
    proposal_count: int
    candidate_limit: int
    estimated_gpu_minutes: float | None
    estimate_status: str
    estimator_version: str | None
    calibration_status: str

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not _TASK_ID_RE.fullmatch(self.task_id):
            raise ValueError("task_id is invalid")
        _token(self.action, "action")
        if self.resource_class not in _RESOURCE_CLASSES:
            raise ValueError("resource_class is unsupported")
        _nonnegative_int(self.gpu_job_slots, "gpu_job_slots")
        _nonnegative_int(self.proposal_count, "proposal_count")
        _nonnegative_int(self.candidate_limit, "candidate_limit")
        if self.estimate_status not in _ESTIMATE_STATUSES:
            raise ValueError("estimate_status is unsupported")
        if self.calibration_status not in _CALIBRATION_STATUSES:
            raise ValueError("calibration_status is unsupported")
        if self.estimate_status == "estimated":
            _nonnegative_number(self.estimated_gpu_minutes, "estimated_gpu_minutes")
            _version(self.estimator_version, "estimator_version")
            if self.calibration_status not in {"calibrated", "provisional", "pending"}:
                raise ValueError("finite estimate has an incompatible calibration_status")
        elif self.estimated_gpu_minutes is not None:
            raise ValueError("unavailable or inapplicable estimate must remain null")
        elif self.estimator_version is not None:
            _version(self.estimator_version, "estimator_version")

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "action": self.action,
            "resource_class": self.resource_class,
            "gpu_job_slots": self.gpu_job_slots,
            "proposal_count": self.proposal_count,
            "candidate_limit": self.candidate_limit,
            "estimated_gpu_minutes": self.estimated_gpu_minutes,
            "estimate_status": self.estimate_status,
            "estimator_version": self.estimator_version,
            "calibration_status": self.calibration_status,
        }


@dataclass(frozen=True)
class ApprovalBudgetProjection:
    gpu_minutes: float | None
    gpu_minutes_status: str
    estimator_version: str | None
    calibration_status: str

    def __post_init__(self) -> None:
        if self.gpu_minutes_status not in _ESTIMATE_STATUSES:
            raise ValueError("gpu_minutes_status is unsupported")
        if self.calibration_status not in _CALIBRATION_STATUSES:
            raise ValueError("calibration_status is unsupported")
        if self.gpu_minutes_status == "estimated":
            _nonnegative_number(self.gpu_minutes, "gpu_minutes")
            _version(self.estimator_version, "estimator_version")
            if self.calibration_status not in {"calibrated", "provisional", "pending"}:
                raise ValueError("finite budget has an incompatible calibration_status")
        elif self.gpu_minutes is not None:
            raise ValueError("unavailable or inapplicable GPU budget must remain null")
        elif self.estimator_version is not None:
            _version(self.estimator_version, "estimator_version")

    def to_dict(self) -> dict[str, Any]:
        return {
            "gpu_minutes": self.gpu_minutes,
            "gpu_minutes_status": self.gpu_minutes_status,
            "estimator_version": self.estimator_version,
            "calibration_status": self.calibration_status,
        }


@dataclass(frozen=True)
class PreOrchestratorApprovalProjection:
    launcher_run_id: str
    project_id: str
    approved_content_binding: str
    plan_id: str
    plan_sha256: str
    source_kind: str
    required_task_ids: tuple[str, ...]
    tasks: tuple[TaskResourceProjection, ...]
    budget: ApprovalBudgetProjection

    def __post_init__(self) -> None:
        _launcher_id(self.launcher_run_id)
        _opaque_id(self.project_id, "project_id")
        _digest(self.approved_content_binding, "approved_content_binding")
        if not isinstance(self.plan_id, str) or not _PLAN_ID_RE.fullmatch(self.plan_id):
            raise ValueError("plan_id is invalid")
        _digest(self.plan_sha256, "plan_sha256")
        _token(self.source_kind, "source_kind")
        required = _task_ids(self.required_task_ids)
        tasks = tuple(self.tasks)
        if any(not isinstance(task, TaskResourceProjection) for task in tasks):
            raise ValueError("tasks contain an invalid projection")
        if tuple(task.task_id for task in tasks) != required:
            raise ValueError("tasks must exactly match required_task_ids in order")
        if not isinstance(self.budget, ApprovalBudgetProjection):
            raise ValueError("budget must be ApprovalBudgetProjection")
        object.__setattr__(self, "required_task_ids", required)
        object.__setattr__(self, "tasks", tasks)

    @property
    def plan_digest(self) -> str:
        """Readable alias for callers that name the SHA-256 as a digest."""

        return self.plan_sha256

    def to_dict(self) -> dict[str, Any]:
        return {
            "launcher_run_id": self.launcher_run_id,
            "project_id": self.project_id,
            "approved_content_binding": self.approved_content_binding,
            "plan_id": self.plan_id,
            "plan_sha256": self.plan_sha256,
            "source_kind": self.source_kind,
            "required_task_ids": list(self.required_task_ids),
            "tasks": [task.to_dict() for task in self.tasks],
            "budget": self.budget.to_dict(),
        }


@dataclass(frozen=True)
class ManualApprovalRequest:
    launcher_run_id: str
    project_id: str
    approved_content_binding: str
    plan_id: str
    plan_sha256: str
    required_task_ids: tuple[str, ...]
    approver: str
    justification: str
    ceilings: ApprovalCeilings

    def __post_init__(self) -> None:
        _launcher_id(self.launcher_run_id)
        _opaque_id(self.project_id, "project_id")
        _digest(self.approved_content_binding, "approved_content_binding")
        if not isinstance(self.plan_id, str) or not _PLAN_ID_RE.fullmatch(self.plan_id):
            raise ValueError("plan_id is invalid")
        _digest(self.plan_sha256, "plan_sha256")
        required = _task_ids(self.required_task_ids)
        _text(self.approver, "approver", maximum=128)
        _text(self.justification, "justification")
        if not isinstance(self.ceilings, ApprovalCeilings):
            raise ValueError("ceilings must be ApprovalCeilings")
        object.__setattr__(self, "required_task_ids", required)

    @property
    def plan_digest(self) -> str:
        return self.plan_sha256

    def to_dict(self) -> dict[str, Any]:
        return {
            "launcher_run_id": self.launcher_run_id,
            "project_id": self.project_id,
            "approved_content_binding": self.approved_content_binding,
            "plan_id": self.plan_id,
            "plan_sha256": self.plan_sha256,
            "required_task_ids": list(self.required_task_ids),
            "approver": self.approver,
            "justification": self.justification,
            "ceilings": self.ceilings.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ManualApprovalRequest":
        return cls(
            launcher_run_id=value.get("launcher_run_id"),
            project_id=value.get("project_id"),
            approved_content_binding=value.get("approved_content_binding"),
            plan_id=value.get("plan_id"),
            plan_sha256=value.get("plan_sha256"),
            required_task_ids=tuple(value.get("required_task_ids") or ()),
            approver=value.get("approver"),
            justification=value.get("justification"),
            ceilings=ApprovalCeilings.from_dict(value.get("ceilings") or {}),
        )


@dataclass(frozen=True)
class ScopedReadIdentity:
    launcher_run_id: str

    def __post_init__(self) -> None:
        _launcher_id(self.launcher_run_id)

    def to_dict(self) -> dict[str, str]:
        return {"launcher_run_id": self.launcher_run_id}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ScopedReadIdentity":
        return cls(launcher_run_id=value.get("launcher_run_id"))


class ControlFailureCategory(str, Enum):
    BINDING = "binding"
    STALE_PLAN = "stale_plan"
    ESTIMATE = "estimate"
    CEILING = "ceiling"
    REVIEW = "review"
    LAUNCHER = "launcher"
    STALE = "stale_plan"


class ControlFailureCode(str, Enum):
    CONTROL_BINDING_INVALID = "control_binding_invalid"
    CONTROL_BINDING_CONFLICT = "control_binding_conflict"
    APPROVAL_PLAN_STALE = "approval_plan_stale"
    APPROVAL_ESTIMATE_UNAVAILABLE = "approval_estimate_unavailable"
    APPROVAL_CEILING_EXCEEDED = "approval_ceiling_exceeded"
    PROJECT_REVIEW_BLOCKED = "project_review_blocked"
    LAUNCHER_RUN_NOT_FOUND = "launcher_run_not_found"
    LAUNCHER_OPERATION_FAILED = "launcher_operation_failed"
    BINDING_INVALID = "control_binding_invalid"
    BINDING_CONFLICT = "control_binding_conflict"
    STALE_PLAN = "approval_plan_stale"
    ESTIMATE_UNAVAILABLE = "approval_estimate_unavailable"
    CEILING_BREACH = "approval_ceiling_exceeded"
    REVIEW_BLOCKED = "project_review_blocked"
    LAUNCHER_ERROR = "launcher_operation_failed"


_FAILURE_CATEGORIES = {
    ControlFailureCode.CONTROL_BINDING_INVALID: ControlFailureCategory.BINDING,
    ControlFailureCode.CONTROL_BINDING_CONFLICT: ControlFailureCategory.BINDING,
    ControlFailureCode.APPROVAL_PLAN_STALE: ControlFailureCategory.STALE_PLAN,
    ControlFailureCode.APPROVAL_ESTIMATE_UNAVAILABLE: ControlFailureCategory.ESTIMATE,
    ControlFailureCode.APPROVAL_CEILING_EXCEEDED: ControlFailureCategory.CEILING,
    ControlFailureCode.PROJECT_REVIEW_BLOCKED: ControlFailureCategory.REVIEW,
    ControlFailureCode.LAUNCHER_RUN_NOT_FOUND: ControlFailureCategory.LAUNCHER,
    ControlFailureCode.LAUNCHER_OPERATION_FAILED: ControlFailureCategory.LAUNCHER,
}


@dataclass(frozen=True)
class ControlFailure:
    code: ControlFailureCode
    category: ControlFailureCategory
    component: str
    message: str
    ceiling: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, ControlFailureCode):
            raise ValueError("code must be ControlFailureCode")
        if not isinstance(self.category, ControlFailureCategory):
            raise ValueError("category must be ControlFailureCategory")
        if _FAILURE_CATEGORIES[self.code] is not self.category:
            raise ValueError("failure category does not match code")
        _token(self.component, "component")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("message must be a non-empty string")
        if self.ceiling is not None and self.ceiling not in _CEILING_NAMES:
            raise ValueError("ceiling is unsupported")
        if (
            self.code is ControlFailureCode.APPROVAL_CEILING_EXCEEDED
            and self.ceiling is None
        ):
            raise ValueError("ceiling breach must identify the blocking ceiling")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "code": self.code.value,
            "category": self.category.value,
            "component": self.component,
            "message": sanitize_message(
                self.message, fallback="Control operation could not be completed."
            ),
            "ceiling": self.ceiling,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ControlFailure":
        return cls(
            code=ControlFailureCode(value.get("code")),
            category=ControlFailureCategory(value.get("category")),
            component=value.get("component"),
            message=value.get("message"),
            ceiling=value.get("ceiling"),
        )


# Explicit aliases keep route/facade naming readable without duplicating contracts.
LaunchRequest = ProjectLaunchRequest
LaunchOptions = ProjectLaunchOptions
ApprovalControlProjection = PreOrchestratorApprovalProjection
AutoApprovalPolicy = FirstGateAutoApprovalPolicy
ExactManualApprovalRequest = ManualApprovalRequest
LauncherScopedReadIdentity = ScopedReadIdentity


__all__ = [
    "ApprovalBudgetProjection",
    "ApprovalCeilings",
    "ApprovalControlProjection",
    "AutoApprovalCeilings",
    "AutoApprovalPolicy",
    "ControlFailure",
    "ControlFailureCategory",
    "ControlFailureCode",
    "FirstGateAutoApprovalPolicy",
    "ExactManualApprovalRequest",
    "LauncherScopedReadIdentity",
    "LaunchOptions",
    "LaunchRequest",
    "ManualApprovalRequest",
    "PreOrchestratorApprovalProjection",
    "ProjectLaunchOptions",
    "ProjectLaunchRequest",
    "ScopedReadIdentity",
    "TaskResourceProjection",
]
