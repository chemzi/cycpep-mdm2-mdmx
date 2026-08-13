"""Browser-safe application service for target launch and the first GPU gate."""

from __future__ import annotations

import json
import math
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from agents.planner import PlannerContractError
from execution.supervisor import durable_atomic_json
from target_bootstrap import (
    ReviewRequiredError,
    TargetBootstrapper,
    approve_draft,
    assert_project_approved,
)
from workflow.control_models import (
    ApprovalCeilings,
    ControlFailure,
    ControlFailureCategory,
    ControlFailureCode,
    ManualApprovalRequest,
    PreOrchestratorApprovalProjection,
    ProjectLaunchOptions,
    ProjectLaunchRequest,
)
from workflow.errors import DiagnosticContractError
from workflow.models import LauncherCommandResult
from workflow.operator_control import (
    approve_and_resume,
    inspect_first_gate_auto_approval,
    inspect_pre_orchestrator_approval,
)
from workflow.service import launch_project, resume_launcher_run, status_launcher_run


_DRAFT_ID_RE = re.compile(r"^drf_[A-Za-z0-9]+$")


class ProjectControlError(RuntimeError):
    """One bounded control failure suitable for an HTTP adapter."""

    def __init__(self, failure: ControlFailure):
        self.failure = failure
        super().__init__(failure.to_dict()["message"])

    def to_dict(self) -> dict[str, str | None]:
        return self.failure.to_dict()


class ProjectControlService:
    """Compose existing project, Launcher, and exact approval authorities."""

    def __init__(
        self,
        drafts_root: str | Path,
        *,
        bootstrapper_factory: Callable[[], Any] = TargetBootstrapper,
        project_approver: Callable[..., dict] = approve_draft,
        project_validator: Callable[[dict], None] = assert_project_approved,
        launcher: Callable[..., LauncherCommandResult] = launch_project,
        launcher_status: Callable[..., LauncherCommandResult] = status_launcher_run,
        launcher_resume: Callable[..., LauncherCommandResult] = resume_launcher_run,
        approval_inspector: Callable[..., PreOrchestratorApprovalProjection] = (
            inspect_pre_orchestrator_approval
        ),
        auto_approval_inspector: Callable[..., PreOrchestratorApprovalProjection] = (
            inspect_first_gate_auto_approval
        ),
        approval_resumer: Callable[..., LauncherCommandResult] = approve_and_resume,
        draft_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._drafts_root = Path(drafts_root)
        self._bootstrapper_factory = bootstrapper_factory
        self._project_approver = project_approver
        self._project_validator = project_validator
        self._launcher = launcher
        self._launcher_status = launcher_status
        self._launcher_resume = launcher_resume
        self._approval_inspector = approval_inspector
        self._auto_approval_inspector = auto_approval_inspector
        self._approval_resumer = approval_resumer
        self._draft_id_factory = draft_id_factory or (
            lambda: f"drf_{uuid.uuid4().hex[:12]}"
        )

    def create_draft(self, request: ProjectLaunchRequest) -> dict[str, Any]:
        if not isinstance(request, ProjectLaunchRequest):
            raise TypeError("request must be ProjectLaunchRequest")
        options = request.options
        draft = self._bootstrapper_factory().create_draft(
            identifier=request.target_identifier,
            identifier_type=options.identifier_type,
            organism_id=options.organism_id,
            epitope=options.epitope,
            objective=options.objective,
        )
        draft_id = self._draft_id_factory()
        path = self._draft_path(draft_id)
        stored = {**draft, "draft_id": draft_id}
        durable_atomic_json(path, stored)
        return _draft_projection(stored)

    def retrieve_draft(self, draft_id: str) -> dict[str, Any]:
        return _draft_projection(self._read_draft(draft_id))

    def approve_project(
        self, draft_id: str, *, justification: str | None = None
    ) -> dict[str, Any]:
        path = self._draft_path(draft_id)
        draft = self._read_draft(draft_id)
        try:
            self._project_validator(draft)
            approved = draft
        except ReviewRequiredError:
            try:
                approved = self._project_approver(
                    path, force=False, justification=justification
                )
            except ReviewRequiredError as error:
                raise ProjectControlError(_review_failure()) from error
        return _draft_projection({**approved, "draft_id": draft_id})

    def launch_project(
        self, draft_id: str, options: ProjectLaunchOptions
    ) -> dict[str, Any]:
        if not isinstance(options, ProjectLaunchOptions):
            raise TypeError("options must be ProjectLaunchOptions")
        if options.launcher_run_id is None:
            raise ValueError("launcher_run_id is required for browser launch")
        path = self._draft_path(draft_id)
        draft = self._read_draft(draft_id)
        try:
            self._project_validator(draft)
        except ReviewRequiredError as error:
            raise ProjectControlError(_review_failure()) from error

        result = self._launcher(
            project_path=path, launcher_run_id=options.launcher_run_id
        )
        policy = options.first_gate_auto_policy
        if policy is None or result.payload.status != "awaiting_approval":
            return self._control_view(result)

        try:
            projection = self._auto_approval_inspector(
                launcher_run_id=options.launcher_run_id
            )
            failure = _auto_policy_failure(projection, policy.ceilings)
            if failure is not None:
                return self._control_view(
                    result, approval_control=projection, failure=failure
                )
            request = ManualApprovalRequest(
                launcher_run_id=projection.launcher_run_id,
                project_id=projection.project_id,
                approved_content_binding=projection.approved_content_binding,
                plan_id=projection.plan_id,
                plan_sha256=projection.plan_sha256,
                required_task_ids=projection.required_task_ids,
                approver=policy.approver,
                justification=policy.justification,
                ceilings=ApprovalCeilings(**policy.ceilings.to_dict()),
            )
            resumed = self._approval_resumer(request=request)
            return self._control_view(resumed)
        except (DiagnosticContractError, PlannerContractError) as error:
            return self._control_view(result, failure=_diagnostic_failure(error))

    def status(self, launcher_run_id: str) -> dict[str, Any]:
        result = self._launcher_status(launcher_run_id=launcher_run_id)
        error = result.payload.error
        if error is not None and error.code == "launcher_diagnostic_not_found":
            return self._control_view(
                result,
                failure=_diagnostic_failure(DiagnosticContractError(
                    error.code, "Launcher run not found."
                )),
            )
        return self._control_view(result)

    def approve_and_continue(
        self, request: ManualApprovalRequest
    ) -> dict[str, Any]:
        if not isinstance(request, ManualApprovalRequest):
            raise TypeError("request must be ManualApprovalRequest")
        try:
            result = self._approval_resumer(request=request)
        except (DiagnosticContractError, PlannerContractError) as error:
            return _empty_control_view(_diagnostic_failure(error))
        return self._control_view(result)

    def continue_run(self, launcher_run_id: str) -> dict[str, Any]:
        """Resume only through Launcher after externally owned work completes."""

        return self._control_view(
            self._launcher_resume(launcher_run_id=launcher_run_id)
        )

    def _control_view(
        self,
        result: LauncherCommandResult,
        *,
        approval_control: PreOrchestratorApprovalProjection | None = None,
        failure: ControlFailure | None = None,
    ) -> dict[str, Any]:
        if not isinstance(result, LauncherCommandResult):
            raise TypeError("Launcher must return LauncherCommandResult")
        if (
            approval_control is None
            and failure is None
            and result.payload.status == "awaiting_approval"
            and result.payload.launcher_run_id is not None
        ):
            try:
                approval_control = self._approval_inspector(
                    launcher_run_id=result.payload.launcher_run_id
                )
            except (DiagnosticContractError, PlannerContractError) as error:
                failure = _diagnostic_failure(error)
        return {
            "launcher": result.payload.to_dict(),
            "approval_control": (
                None if approval_control is None else approval_control.to_dict()
            ),
            "control_failure": None if failure is None else failure.to_dict(),
        }

    def _draft_path(self, draft_id: str) -> Path:
        if not isinstance(draft_id, str) or not _DRAFT_ID_RE.fullmatch(draft_id):
            raise FileNotFoundError("draft not found")
        return self._drafts_root / f"{draft_id}.json"

    def _read_draft(self, draft_id: str) -> dict[str, Any]:
        try:
            value = json.loads(self._draft_path(draft_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise FileNotFoundError("draft not found") from error
        if not isinstance(value, dict) or value.get("draft_id") != draft_id:
            raise FileNotFoundError("draft not found")
        return value


def _draft_projection(draft: Mapping[str, Any]) -> dict[str, Any]:
    bootstrap = draft.get("bootstrap") or {}
    return {
        "draft_id": draft.get("draft_id"),
        "project_id": draft.get("project_id"),
        "name": draft.get("name"),
        "objective": draft.get("objective"),
        "targets": [_target_projection(item) for item in draft.get("targets") or ()],
        "review": _pick(
            draft.get("review") or {},
            "status",
            "revision",
            "content_digest",
            "approved_digest",
            "blocking_issues",
            "warnings",
            "checklist",
        ),
        "bootstrap": {
            **_pick(bootstrap, "ambiguous_identifier", "assumptions"),
            "resolved_candidates": [
                _target_projection(candidate)
                for candidate in bootstrap.get("resolved_candidates") or ()
                if isinstance(candidate, Mapping)
            ],
            "selected_candidate": (
                _target_projection(bootstrap["selected_candidate"])
                if isinstance(bootstrap.get("selected_candidate"), Mapping)
                else None
            ),
        },
    }


def _target_projection(target: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **_pick(
            target,
            "id",
            "uniprot",
            "gene_name",
            "protein_name",
            "organism",
            "aliases",
            "function_summary",
            "biological_mechanism",
            "binding_site",
            "natural_partners",
            "known_binders",
            "off_targets",
            "uncertainties",
        ),
        "structure": _pick(
            target.get("structure") or {},
            "status",
            "source",
            "pdb_id",
            "model_id",
            "chain_id",
            "readiness",
            "warnings",
        ),
    }


def _pick(source: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    return {key: source.get(key) for key in keys if key in source}


def _auto_policy_failure(projection, ceilings) -> ControlFailure | None:
    gpu_tasks = [task for task in projection.tasks if task.resource_class == "gpu"]
    estimates = [task.estimated_gpu_minutes for task in gpu_tasks]
    estimate_available = bool(gpu_tasks) and all(
        task.estimate_status == "estimated"
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        for task, value in zip(gpu_tasks, estimates)
    )
    total_minutes = sum(float(value) for value in estimates) if estimate_available else None
    budget = projection.budget
    if (
        not estimate_available
        or budget.gpu_minutes_status != "estimated"
        or not isinstance(budget.gpu_minutes, (int, float))
        or isinstance(budget.gpu_minutes, bool)
        or not math.isfinite(float(budget.gpu_minutes))
        or not math.isclose(float(budget.gpu_minutes), total_minutes)
    ):
        return _failure(
            ControlFailureCode.APPROVAL_ESTIMATE_UNAVAILABLE,
            ControlFailureCategory.ESTIMATE,
            "planner",
            "A finite, consistent Planner GPU estimate is required.",
        )
    required = {
        "max_gpu_job_slots": max((task.gpu_job_slots for task in gpu_tasks), default=0),
        "max_design_proposals": sum(task.proposal_count for task in gpu_tasks),
        "max_prediction_candidates": sum(task.candidate_limit for task in gpu_tasks),
        "max_gpu_minutes": total_minutes,
    }
    limits = ceilings.to_dict()
    for name in (
        "max_gpu_job_slots",
        "max_design_proposals",
        "max_prediction_candidates",
        "max_gpu_minutes",
    ):
        if required[name] > limits[name]:
            return _failure(
                ControlFailureCode.APPROVAL_CEILING_EXCEEDED,
                ControlFailureCategory.CEILING,
                "planner",
                "The current plan exceeds an automatic approval ceiling.",
                ceiling=name,
            )
    return None


def _diagnostic_failure(
    error: DiagnosticContractError | PlannerContractError,
) -> ControlFailure:
    code_map = {
        "control_binding_invalid": ControlFailureCode.CONTROL_BINDING_INVALID,
        "control_binding_conflict": ControlFailureCode.CONTROL_BINDING_CONFLICT,
        "approval_plan_stale": ControlFailureCode.APPROVAL_PLAN_STALE,
        "approval_estimate_unavailable": ControlFailureCode.APPROVAL_ESTIMATE_UNAVAILABLE,
        "approval_ceiling_exceeded": ControlFailureCode.APPROVAL_CEILING_EXCEEDED,
        "approval_gpu_limit_insufficient": ControlFailureCode.APPROVAL_CEILING_EXCEEDED,
        "approval_gpu_minutes_required": ControlFailureCode.APPROVAL_CEILING_EXCEEDED,
        "approval_design_limit_insufficient": ControlFailureCode.APPROVAL_CEILING_EXCEEDED,
        "approval_prediction_limit_insufficient": ControlFailureCode.APPROVAL_CEILING_EXCEEDED,
        "launcher_diagnostic_not_found": ControlFailureCode.LAUNCHER_RUN_NOT_FOUND,
    }
    code = code_map.get(error.code, ControlFailureCode.LAUNCHER_OPERATION_FAILED)
    category = {
        ControlFailureCode.CONTROL_BINDING_INVALID: ControlFailureCategory.BINDING,
        ControlFailureCode.CONTROL_BINDING_CONFLICT: ControlFailureCategory.BINDING,
        ControlFailureCode.APPROVAL_PLAN_STALE: ControlFailureCategory.STALE_PLAN,
        ControlFailureCode.APPROVAL_ESTIMATE_UNAVAILABLE: ControlFailureCategory.ESTIMATE,
        ControlFailureCode.APPROVAL_CEILING_EXCEEDED: ControlFailureCategory.CEILING,
        ControlFailureCode.LAUNCHER_RUN_NOT_FOUND: ControlFailureCategory.LAUNCHER,
        ControlFailureCode.LAUNCHER_OPERATION_FAILED: ControlFailureCategory.LAUNCHER,
    }[code]
    ceiling_by_code = {
        "approval_gpu_limit_insufficient": "max_gpu_job_slots",
        "approval_gpu_minutes_required": "max_gpu_minutes",
        "approval_design_limit_insufficient": "max_design_proposals",
        "approval_prediction_limit_insufficient": "max_prediction_candidates",
        "approval_ceiling_exceeded": "max_gpu_minutes",
    }
    ceiling = ceiling_by_code.get(error.code)
    return _failure(code, category, "launcher", str(error), ceiling=ceiling)


def _review_failure() -> ControlFailure:
    return _failure(
        ControlFailureCode.PROJECT_REVIEW_BLOCKED,
        ControlFailureCategory.REVIEW,
        "review",
        "Project review must be completed before launch.",
    )


def _failure(code, category, component, message, *, ceiling=None) -> ControlFailure:
    return ControlFailure(
        code=code,
        category=category,
        component=component,
        message=message,
        ceiling=ceiling,
    )


def _empty_control_view(failure: ControlFailure) -> dict[str, Any]:
    return {
        "launcher": None,
        "approval_control": None,
        "control_failure": failure.to_dict(),
    }


__all__ = ["ProjectControlError", "ProjectControlService"]
