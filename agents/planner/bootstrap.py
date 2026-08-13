"""Canonical Planner plan for initial, pre-Critic Prediction execution."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Callable, Mapping

from contracts.plan import MANDATORY_POLICY_CONSTRAINTS, PLAN_SCHEMA_VERSION, PLANNER_VERSION
from contracts.trace import derive_workflow_id
from contracts.trace import TraceContext
from data_layer import EvidenceLogger
from prediction_pipeline.contracts import file_sha256
from prediction_pipeline.contracts import object_sha256
from prediction_pipeline.execution_identity import (
    build_prediction_execution_identity,
    validate_prediction_execution_identity,
)
from prediction_pipeline.protocol import PREDICTOR_PROTOCOL

from .approval import _approval
from .config import APPROVAL_SCHEMA_VERSION, PlannerConfig
from .errors import PlannerContractError
from .io import _atomic_json, _read_json
from .plan_builder import _compute_plan_metadata
from .task_builder import _task


BOOTSTRAP_SOURCE_KIND = "initial_prediction_bootstrap"


def _normalize_bootstrap_estimate(
    plan: dict[str, Any], metadata: dict[str, object]
) -> None:
    """Keep bootstrap task estimates and the budget summary in one interpretation."""
    required_task_ids = set(plan["approval_request"]["required_task_ids"])
    required_gpu_resources = [
        task["resource_request"]
        for task in plan["tasks"]
        if task["task_id"] in required_task_ids
        and task["resource_request"]["class"] == "gpu"
    ]
    estimates = [resource.get("estimated_gpu_minutes") for resource in required_gpu_resources]
    estimates_available = bool(estimates) and all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= 0
        and resource.get("estimate_status") == "estimated"
        for resource, value in zip(required_gpu_resources, estimates)
    )
    estimator_version = metadata.get("estimator_version")
    budget = plan["budget_request"]
    if estimates_available:
        total = float(sum(float(value) for value in estimates))
        metadata["total_estimated_gpu_minutes"] = total
        metadata["estimator_version"] = "simple-v1"
        metadata["estimate_calibration_status"] = "provisional"
        budget.update({
            "gpu_minutes": total,
            "gpu_minutes_status": "estimated",
            "gpu_minutes_estimator_version": "simple-v1",
            "gpu_minutes_calibration_status": "provisional",
        })
        return

    metadata["total_estimated_gpu_minutes"] = None
    metadata["estimate_calibration_status"] = "unavailable"
    budget.update({
        "gpu_minutes": None,
        "gpu_minutes_status": "benchmark_required",
        "gpu_minutes_estimator_version": estimator_version,
        "gpu_minutes_calibration_status": "unavailable",
    })


def _normalize_source(source: Mapping[str, Any]) -> dict[str, Any]:
    required_strings = (
        "project_id",
        "approved_content_binding",
        "launcher_run_id",
        "research_completion_event_id",
        "design_invocation_id",
        "design_completion_event_id",
        "design_transaction_id",
    )
    normalized = {}
    for key in required_strings:
        value = source.get(key)
        if not isinstance(value, str) or not value.strip():
            raise PlannerContractError(
                "bootstrap_source_invalid", f"bootstrap source requires {key}"
            )
        normalized[key] = value
    candidate_ids = source.get("candidate_ids")
    if (
        not isinstance(candidate_ids, (list, tuple))
        or not candidate_ids
        or any(not isinstance(value, str) or not value for value in candidate_ids)
        or len(candidate_ids) != len(set(candidate_ids))
    ):
        raise PlannerContractError(
            "bootstrap_candidate_scope_invalid",
            "bootstrap source requires a non-empty unique exact candidate set",
        )
    normalized["candidate_ids"] = sorted(candidate_ids)
    try:
        normalized["execution_identity"] = validate_prediction_execution_identity(
            source.get("execution_identity")
        )
    except (TypeError, ValueError) as exc:
        raise PlannerContractError(getattr(exc, "code", "bootstrap_identity_invalid"), str(exc)) from exc
    retry = source.get("retry")
    if retry is not None:
        if not isinstance(retry, Mapping):
            raise PlannerContractError("bootstrap_retry_invalid", "retry binding must be an object")
        required_retry = {
            "retry_index", "prior_plan_id", "prior_run_id", "prior_task_id",
            "prior_attempt_id", "prior_transaction_id", "failure_event_id",
            "failure_status",
        }
        if set(retry) != required_retry:
            raise PlannerContractError(
                "bootstrap_retry_invalid", "retry binding fields are incomplete or unsupported"
            )
        if (
            not isinstance(retry.get("retry_index"), int)
            or isinstance(retry.get("retry_index"), bool)
            or retry["retry_index"] < 1
            or retry.get("failure_status") != "failed"
            or any(
                not isinstance(retry.get(key), str) or not retry[key]
                for key in required_retry - {"retry_index", "failure_status"}
            )
        ):
            raise PlannerContractError(
                "bootstrap_retry_invalid", "retry binding is not a terminal failure proof"
            )
        normalized["retry"] = dict(retry)
    return normalized


def build_initial_prediction_bootstrap_plan(
    *, source: Mapping[str, Any], config: PlannerConfig | None = None
) -> dict[str, Any]:
    """Build the single-task immutable bootstrap plan from formal Design refs."""
    config = config or PlannerConfig()
    normalized = _normalize_source(source)
    candidate_ids = normalized["candidate_ids"]
    if len(candidate_ids) > config.max_prediction_candidates_per_task:
        raise PlannerContractError(
            "bootstrap_candidate_limit_exceeded",
            "committed exact candidate set exceeds the configured Prediction limit",
        )
    source_digest = object_sha256(normalized)
    workflow_id = derive_workflow_id(
        normalized["project_id"],
        normalized["launcher_run_id"],
        source_digest,
        1,
    )
    tasks: list[dict] = []
    _task(
        tasks,
        agent="prediction",
        action="evaluate_new_design_candidates",
        phase="evaluate",
        priority="P0",
        disposition="required",
        reason_codes=["initial_prediction_evidence_missing"],
        candidate_ids=candidate_ids,
        parameters={
            "reuse_complete_evidence": True,
            "evidence_mode": "reuse_or_generate_full",
            "predictor_protocol": dict(PREDICTOR_PROTOCOL),
            "execution_identity": normalized["execution_identity"],
        },
        candidate_limit=len(candidate_ids),
        approval=_approval(
            action="evaluate_new_design_candidates", critic_approval_required=False
        ),
        outputs=["prediction_handoff.json"],
        constraints=[
            "exact_initial_design_candidate_scope",
            "reuse_complete_prediction_evidence",
            "single_gpu_serial_execution",
        ],
    )
    input_digest = object_sha256({
        "source": normalized,
        "planner_version": PLANNER_VERSION,
        "max_prediction_candidates_per_task": config.max_prediction_candidates_per_task,
    })
    plan_id = f"planner_{input_digest[:12]}"
    plan_source = {
        "kind": BOOTSTRAP_SOURCE_KIND,
        **normalized,
        "workflow_id": workflow_id,
    }
    required = [tasks[0]["task_id"]]
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "planner_version": PLANNER_VERSION,
        "plan_id": plan_id,
        "workflow_id": workflow_id,
        "input_digest": input_digest,
        "source": plan_source,
        "status": "awaiting_approval",
        "summary": (
            f"Initial Design committed {len(candidate_ids)} candidate(s); "
            "full Prediction execution requires explicit approval."
        ),
        "cycle": {
            "source_round": 1,
            "target_round": 1,
            "round_advancement_deferred_to_orchestrator": True,
        },
        "budget_request": {
            "configured_design_budget_snapshot": {},
            "configured_design_budget_total": 0,
            "requested_design_proposals": 0,
            "requested_gpu_job_slots": 1,
            "gpu_minutes": None,
            "gpu_minutes_status": "benchmark_required",
            "reservation_status": "not_reserved",
        },
        "policy_constraints": sorted(MANDATORY_POLICY_CONSTRAINTS),
        "approval_request": {
            "artifact_required": True,
            "required_task_ids": required,
            "optional_task_ids": [],
            "approval_schema_version": APPROVAL_SCHEMA_VERSION,
            "approval_must_bind_plan_sha256": True,
        },
        "execution": {
            "automatic_dispatch_allowed": False,
            "blocked_task_ids": [],
            "entry_task_ids": required,
            "orchestrator_required": True,
        },
        "tasks": tasks,
    }
    plan["decision_metadata"] = _compute_plan_metadata(tasks, {}, config, {})
    _normalize_bootstrap_estimate(plan, plan["decision_metadata"])
    return plan


def _validate_formal_source(source: Mapping[str, Any], store: Any) -> None:
    project_id = str(source["project_id"])
    events = store.query(project_id=project_id)
    by_id = {event.get("event_id"): event for event in events}
    research = by_id.get(source["research_completion_event_id"])
    completion = by_id.get(source["design_completion_event_id"])
    if (
        not isinstance(research, Mapping)
        or research.get("agent") != "research"
        or research.get("event_type") != "research_completion_receipt"
        or research.get("launcher_run_id") != source["launcher_run_id"]
        or research.get("approved_content_binding")
        != source["approved_content_binding"]
    ):
        raise PlannerContractError(
            "bootstrap_research_completion_invalid",
            "formal Research completion is missing or belongs to another source",
        )
    if (
        not isinstance(completion, Mapping)
        or completion.get("agent") != "design"
        or completion.get("event_type") != "design_initial_completion"
        or completion.get("design_invocation_id") != source["design_invocation_id"]
        or completion.get("launcher_run_id") != source["launcher_run_id"]
        or completion.get("approved_content_binding")
        != source["approved_content_binding"]
        or completion.get("transaction_id") != source["design_transaction_id"]
        or sorted(completion.get("candidate_ids") or []) != source["candidate_ids"]
    ):
        raise PlannerContractError(
            "bootstrap_design_completion_invalid",
            "formal Initial Design completion differs from bootstrap source",
        )
    transaction_id = str(source["design_transaction_id"])
    if store.get_transaction_status(transaction_id) != "COMMITTED":
        raise PlannerContractError(
            "bootstrap_design_transaction_invalid",
            "Initial Design transaction is not committed",
        )
    registrations = store.query(
        project_id=project_id,
        transaction_id=transaction_id,
        agent="design",
        event_type="candidate_registered",
    )
    registered = sorted(
        str((event.get("candidate") or {}).get("candidate_id") or "")
        for event in registrations
    )
    if registered != source["candidate_ids"]:
        raise PlannerContractError(
            "bootstrap_candidate_registration_mismatch",
            "Design transaction registrations differ from the exact candidate set",
        )


def run_initial_prediction_bootstrap(
    *,
    source: Mapping[str, Any],
    output_root: str | Path,
    store: Any,
    config: PlannerConfig | None = None,
    publisher: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Validate, persist, and idempotently register one bootstrap plan."""
    normalized = _normalize_source(source)
    _validate_formal_source(normalized, store)
    plan = build_initial_prediction_bootstrap_plan(source=normalized, config=config)
    plan_path = (
        Path(output_root).expanduser().resolve()
        / plan["plan_id"]
        / "execution_plan.json"
    )
    if plan_path.exists():
        if _read_json(plan_path, "bootstrap_plan") != plan:
            raise PlannerContractError(
                "bootstrap_plan_output_conflict",
                "bootstrap plan path contains different immutable content",
            )
    else:
        _atomic_json(plan_path, plan)
    plan_sha = file_sha256(plan_path)
    matching = [
        event for event in store.query(
            project_id=normalized["project_id"],
            agent="planner",
            event_type="planner_plan",
        )
        if event.get("source_kind") == BOOTSTRAP_SOURCE_KIND
        and event.get("design_completion_event_id")
        == normalized["design_completion_event_id"]
        and event.get("retry") == normalized.get("retry")
    ]
    if matching and any(
        event.get("plan_id") != plan["plan_id"]
        or event.get("plan_sha256") != plan_sha
        for event in matching
    ):
        raise PlannerContractError(
            "bootstrap_plan_conflict",
            "formal bootstrap plan records conflict for the same Design source",
        )
    if not matching:
        payload = {
            "source_kind": BOOTSTRAP_SOURCE_KIND,
            "plan_id": plan["plan_id"],
            "plan_path": str(plan_path),
            "plan_sha256": plan_sha,
            "status": plan["status"],
            "task_count": 1,
            "required_approval_task_ids": ["T001"],
            "launcher_run_id": normalized["launcher_run_id"],
            "research_completion_event_id": normalized["research_completion_event_id"],
            "design_invocation_id": normalized["design_invocation_id"],
            "design_completion_event_id": normalized["design_completion_event_id"],
            "design_transaction_id": normalized["design_transaction_id"],
            "candidate_ids": normalized["candidate_ids"],
            "execution_identity": normalized["execution_identity"],
        }
        if normalized.get("retry") is not None:
            payload["retry"] = normalized["retry"]
        emit = publisher or EvidenceLogger.log
        emit(
            "planner",
            "planner_plan",
            payload,
            phase="evaluate",
            trace_context=TraceContext(
                project_id=normalized["project_id"],
                workflow_id=plan["workflow_id"],
                plan_id=plan["plan_id"],
            ),
        )
    return {"plan": plan, "plan_path": str(plan_path), "plan_sha256": plan_sha}


def retry_initial_prediction_bootstrap(
    *,
    failed_plan: Mapping[str, Any],
    failure: Mapping[str, Any],
    output_root: str | Path,
    store: Any,
    config: PlannerConfig | None = None,
    publisher: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Publish a newly approvable plan after one terminal bootstrap failure."""
    source = failed_plan.get("source")
    if not isinstance(source, Mapping) or source.get("kind") != BOOTSTRAP_SOURCE_KIND:
        raise PlannerContractError(
            "bootstrap_retry_source_invalid", "retry requires a bootstrap plan"
        )
    from contracts.bootstrap_retry import (
        BootstrapRetryProofError,
        validate_bootstrap_retry_failure,
    )
    try:
        validate_bootstrap_retry_failure(
            store, failed_plan=failed_plan, failure=failure
        )
    except BootstrapRetryProofError as exc:
        raise PlannerContractError(
            "bootstrap_retry_failure_invalid",
            "retry failure proof is missing, conflicting, or committed",
        ) from exc
    prior_retry = source.get("retry") or {}
    retry_index = int(prior_retry.get("retry_index") or 0) + 1
    retry = {
        "retry_index": retry_index,
        "prior_plan_id": failed_plan.get("plan_id"),
        "prior_run_id": failure["run_id"],
        "prior_task_id": failure["task_id"],
        "prior_attempt_id": failure["attempt_id"],
        "prior_transaction_id": failure["transaction_id"],
        "failure_event_id": failure.get("evidence_id"),
        "failure_status": "failed",
    }
    next_source = {
        key: value for key, value in source.items()
        if key not in {"kind", "workflow_id", "retry", "execution_identity"}
    }
    next_source["retry"] = retry
    next_source["execution_identity"] = build_prediction_execution_identity()
    return run_initial_prediction_bootstrap(
        source=next_source,
        output_root=output_root,
        store=store,
        config=config,
        publisher=publisher,
    )


__all__ = [
    "BOOTSTRAP_SOURCE_KIND",
    "build_initial_prediction_bootstrap_plan",
    "run_initial_prediction_bootstrap",
    "retry_initial_prediction_bootstrap",
]
