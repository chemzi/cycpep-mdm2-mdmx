"""Audited Planner for Critic-driven cyclic-peptide iteration.

Planner converts one immutable Critic report into a deterministic execution
plan.  It does not execute tools, mutate thresholds, delete candidates, consume
GPU budget, or advance the iteration counter.  ``build_plan()`` is pure;
``run()`` persists the plan and records an idempotent State/Evidence summary.

The optional ``approve`` CLI writes a separate digest-bound approval artifact.
It must only be used after an identified human has approved the listed task IDs
and explicit resource ceilings.  Future Orchestrator code must require this
artifact before dispatching a task whose ``approval.required`` value is true.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_layer import CandidateIndex, EvidenceLogger, State  # noqa: E402
from prediction_pipeline.contracts import file_sha256, object_sha256  # noqa: E402
from project_config import target_slug  # noqa: E402
from contracts.action import RecommendationMapping, get_action_spec  # noqa: E402
from contracts.trace import TraceContext, derive_workflow_id  # noqa: E402


PLANNER_VERSION = "1.3.0"

# P0 compute-cost model.  These are deterministic planning estimates, not
# runtime accounting.  Calibrate the constants from benchmark evidence later.
ROUTE_GPU_MINUTES_PER_PROPOSAL = {
    "A": 2.0,
    "B": 5.0,
    "C": 10.0,
}
DEFAULT_GPU_MINUTES_PER_PROPOSAL = 5.0
DEFAULT_GPU_MINUTES_PER_CANDIDATE = 30.0
COST_MODEL_VERSION = "p0_route_duration_v1"
PLAN_SCHEMA_VERSION = 2
LEGACY_PLAN_SCHEMA_VERSION = 1
APPROVAL_SCHEMA_VERSION = 1
REPORT_ID_RE = re.compile(r"^critic_[0-9a-f]{12}$")
PLAN_ID_RE = re.compile(r"^planner_[0-9a-f]{12}$")

MANDATORY_POLICY_CONSTRAINTS = frozenset({
    "do_not_change_thresholds_automatically",
    "do_not_delete_candidates_automatically",
    "do_not_start_gpu_jobs_without_planner_budget_and_execution_approval",
    "reuse_complete_prediction_evidence",
})

SEVERITY_RANK = {"blocker": 0, "high": 1, "medium": 2, "info": 3}
PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2}

# Critic recommendations are deliberately closed-world.  These mappings are
# Planner policy only; executable capability is answered by the Action Registry
# and its typed ActionSpec catalog.
RECOMMENDATION_MAPPINGS: dict[str, RecommendationMapping] = {
    "regenerate_invalid_artifact": RecommendationMapping(
        "regenerate_invalid_artifact", "regenerate_invalid_artifacts",
        "prediction/design", "iterate", "recovery",
    ),
    "complete_prediction_evidence": RecommendationMapping(
        "complete_prediction_evidence", "evaluate_new_design_candidates",
        "prediction", "evaluate", "prediction",
    ),
    "calibrate_thresholds": RecommendationMapping(
        "calibrate_thresholds", "propose_threshold_calibration",
        "research", "research", "policy_review",
    ),
    "deduplicate_candidates": RecommendationMapping(
        "deduplicate_candidates", "audit_duplicate_candidates",
        "design/data", "iterate", "data_review",
    ),
    "repair_candidate_index": RecommendationMapping(
        "repair_candidate_index", "repair_candidate_index",
        "design/data", "iterate", "recovery",
    ),
}

DESIGN_ITERATION_ACTIONS = frozenset({
    "improve_monomer_quality",
    "iterate_interface_design",
    "iterate_interface_physics",
    "repair_cyclization_geometry",
    "retarget_reviewed_hotspots",
    "improve_design_consistency",
    "increase_sequence_diversity",
    "generate_review_cohort",
    "regenerate_design_reference",
    "improve_pose_robustness",
})

for _recommendation in DESIGN_ITERATION_ACTIONS:
    RECOMMENDATION_MAPPINGS[_recommendation] = RecommendationMapping(
        _recommendation, "iterate_design", "design", "design", "design"
    )


class PlannerContractError(ValueError):
    """A Critic report, State snapshot, plan, or approval is unsafe to use."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PlannerConfig:
    """Planning policy; values request capacity but never grant execution."""

    design_batch_size: int = 12
    optional_design_batch_size: int = 3
    max_design_proposals_per_plan: int = 48
    max_prediction_candidates_per_task: int = 48
    max_rounds: int = 5
    task_timeout_seconds: int = 3600

    def __post_init__(self) -> None:
        for name in (
            "design_batch_size",
            "optional_design_batch_size",
            "max_design_proposals_per_plan",
            "max_prediction_candidates_per_task",
            "max_rounds",
            "task_timeout_seconds",
        ):
            if int(getattr(self, name)) < 1:
                raise PlannerContractError(
                    "planner_config_invalid", f"{name} must be positive"
                )
        if self.design_batch_size > self.max_design_proposals_per_plan:
            raise PlannerContractError(
                "planner_config_invalid",
                "design_batch_size exceeds max_design_proposals_per_plan",
            )
        if self.optional_design_batch_size > self.max_design_proposals_per_plan:
            raise PlannerContractError(
                "planner_config_invalid",
                "optional_design_batch_size exceeds max_design_proposals_per_plan",
            )


def _read_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PlannerContractError(f"{label}_missing", f"missing {label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PlannerContractError(
            f"{label}_malformed", f"invalid JSON in {path}"
        ) from exc
    if not isinstance(value, dict):
        raise PlannerContractError(f"{label}_type", f"{label} must be an object")
    return value


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _validate_sha256(value: Any, code: str, label: str) -> str:
    digest = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise PlannerContractError(code, f"{label} must be a full SHA-256")
    return digest


def _validate_critic_report(report: dict, state: dict, report_sha256: str) -> None:
    if report.get("schema_version") != 1:
        raise PlannerContractError(
            "critic_schema_unsupported", "Planner requires Critic report schema v1"
        )
    report_id = str(report.get("report_id") or "")
    if not REPORT_ID_RE.fullmatch(report_id):
        raise PlannerContractError("critic_report_id_invalid", "invalid Critic report ID")
    input_digest = _validate_sha256(
        report.get("input_digest"), "critic_input_digest_invalid", "Critic input_digest"
    )
    if report_id != f"critic_{input_digest[:12]}":
        raise PlannerContractError(
            "critic_report_id_mismatch", "Critic report ID is not bound to input_digest"
        )

    verdict = report.get("verdict")
    if verdict not in {"blocked", "iterate", "review", "clear"}:
        raise PlannerContractError("critic_verdict_invalid", "unknown Critic verdict")
    if bool(report.get("passed")) != (verdict == "clear"):
        raise PlannerContractError(
            "critic_verdict_inconsistent", "Critic passed flag conflicts with verdict"
        )

    source = report.get("source")
    if not isinstance(source, dict):
        raise PlannerContractError("critic_source_invalid", "Critic source must be an object")
    project_id = str(source.get("project_id") or "").strip()
    state_project_id = str(state.get("project_id") or "").strip()
    if project_id and state_project_id and project_id != state_project_id:
        raise PlannerContractError(
            "planner_project_mismatch", "State and Critic report project IDs differ"
        )

    state_critic = state.get("critic") or {}
    if isinstance(state_critic, dict) and state_critic.get("report_id"):
        if state_critic["report_id"] != report_id:
            raise PlannerContractError(
                "state_critic_mismatch", "State points to a different Critic report"
            )
        declared_sha = str(state_critic.get("report_sha256") or "").strip().lower()
        if declared_sha and declared_sha != report_sha256:
            raise PlannerContractError(
                "state_critic_hash_mismatch", "State Critic report SHA-256 differs"
            )

    issues = report.get("issues")
    recommendations = report.get("recommendations")
    handoff = report.get("planner_handoff")
    if not isinstance(issues, list) or not isinstance(recommendations, list):
        raise PlannerContractError(
            "critic_feedback_invalid", "Critic issues and recommendations must be arrays"
        )
    if not isinstance(handoff, dict):
        raise PlannerContractError(
            "critic_planner_handoff_invalid", "Critic planner_handoff must be an object"
        )
    if handoff.get("critic_report_id") != report_id:
        raise PlannerContractError(
            "critic_planner_handoff_mismatch", "planner_handoff report ID differs"
        )

    issues_by_code: dict[str, dict] = {}
    for issue in issues:
        if not isinstance(issue, dict):
            raise PlannerContractError("critic_issue_invalid", "Critic issue must be an object")
        code = str(issue.get("code") or "").strip()
        action = str(issue.get("recommended_action") or "").strip()
        if not code or code in issues_by_code:
            raise PlannerContractError(
                "critic_issue_duplicate", f"missing or duplicate Critic issue code: {code!r}"
            )
        if issue.get("severity") not in SEVERITY_RANK:
            raise PlannerContractError(
                "critic_issue_severity_invalid", f"invalid severity for {code}"
            )
        if action not in RECOMMENDATION_MAPPINGS:
            raise PlannerContractError(
                "planner_action_unknown", f"Planner has no safe mapping for {action!r}"
            )
        candidate_ids = issue.get("candidate_ids")
        if not isinstance(candidate_ids, list):
            raise PlannerContractError(
                "critic_issue_candidates_invalid", f"candidate_ids for {code} must be an array"
            )
        issues_by_code[code] = issue

    recommendation_actions: list[str] = []
    covered_codes: set[str] = set()
    for recommendation in recommendations:
        if not isinstance(recommendation, dict):
            raise PlannerContractError(
                "critic_recommendation_invalid", "Critic recommendation must be an object"
            )
        action = str(recommendation.get("action") or "").strip()
        if action not in RECOMMENDATION_MAPPINGS:
            raise PlannerContractError(
                "planner_action_unknown", f"Planner has no safe mapping for {action!r}"
            )
        if action in recommendation_actions:
            raise PlannerContractError(
                "critic_recommendation_duplicate", f"duplicate recommendation {action}"
            )
        if recommendation.get("priority") not in PRIORITY_RANK:
            raise PlannerContractError(
                "critic_priority_invalid", f"invalid priority for {action}"
            )
        reason_codes = recommendation.get("reason_codes")
        if not isinstance(reason_codes, list) or not reason_codes:
            raise PlannerContractError(
                "critic_reason_codes_invalid", f"recommendation {action} has no reasons"
            )
        for code in reason_codes:
            issue = issues_by_code.get(code)
            if issue is None or issue.get("recommended_action") != action:
                raise PlannerContractError(
                    "critic_recommendation_mismatch",
                    f"recommendation {action} is not supported by issue {code!r}",
                )
            covered_codes.add(code)
        recommendation_actions.append(action)

    if covered_codes != set(issues_by_code):
        raise PlannerContractError(
            "critic_recommendation_incomplete", "not every Critic issue is mapped"
        )
    if handoff.get("issue_codes") != [issue["code"] for issue in issues]:
        raise PlannerContractError(
            "critic_handoff_issues_mismatch", "planner_handoff issue codes differ"
        )
    if handoff.get("recommended_actions") != recommendation_actions:
        raise PlannerContractError(
            "critic_handoff_actions_mismatch", "planner_handoff actions differ"
        )
    constraints = set(handoff.get("policy_constraints") or [])
    missing_constraints = sorted(MANDATORY_POLICY_CONSTRAINTS - constraints)
    if missing_constraints:
        raise PlannerContractError(
            "critic_policy_constraint_missing",
            f"Critic handoff lacks mandatory constraints: {missing_constraints}",
        )


def _budget_snapshot(state: dict) -> tuple[dict[str, int], int]:
    raw = state.get("design_budget")
    if raw in (None, {}):
        return {}, 0
    if not isinstance(raw, dict):
        raise PlannerContractError(
            "design_budget_invalid", "State design_budget must be an object"
        )
    normalized = {}
    for name, value in sorted(raw.items()):
        if isinstance(value, bool):
            raise PlannerContractError(
                "design_budget_invalid", f"budget {name} must be a non-negative integer"
            )
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise PlannerContractError(
                "design_budget_invalid", f"budget {name} must be a non-negative integer"
            ) from exc
        if number < 0 or float(value) != number:
            raise PlannerContractError(
                "design_budget_invalid", f"budget {name} must be a non-negative integer"
            )
        normalized[str(name)] = number
    return normalized, sum(normalized.values())


def _reason_disposition(reason_codes: list[str], issues_by_code: dict[str, dict]) -> str:
    severities = {issues_by_code[code]["severity"] for code in reason_codes}
    if "blocker" in severities:
        return "recovery"
    if "high" in severities:
        return "required"
    if "medium" in severities:
        return "review_required"
    return "optional"


def _candidate_ids(reason_codes: list[str], issues_by_code: dict[str, dict]) -> list[str]:
    return sorted({
        str(candidate_id)
        for code in reason_codes
        for candidate_id in issues_by_code[code].get("candidate_ids", [])
        if str(candidate_id).strip()
    })


def _materialize_design_jobs(
    *,
    state: dict,
    required_targets: list[str],
    budgets: dict[str, int],
    requested: int,
    seed_material: str,
) -> list[dict]:
    """Build deterministic Route A jobs from approved target configuration.

    Target-specific Route A budgets are preferred.  The legacy shared
    ``route_A`` key remains supported for old State fixtures.  Route B/C are
    used only when no Route A capacity exists, because their motif provenance
    is less generally transferable across targets.
    """
    if requested < 1 or not required_targets:
        return []
    project = state.get("project_config") or {}
    target_values = {
        str(item.get("id")): item
        for item in project.get("targets", [])
        if isinstance(item, dict) and item.get("id")
    }
    seed_base = int(object_sha256({
        "material": seed_material,
        "targets": required_targets,
        "round": int(state.get("round") or 1),
    })[:8], 16) % (2**31)

    specific = []
    for target_id in required_targets:
        key = f"route_A_{target_slug(target_id)}"
        if budgets.get(key, 0) > 0:
            specific.append((target_id, key, budgets[key]))
    shared = budgets.get("route_A", 0)
    allocations: dict[str, int] = {target_id: 0 for target_id in required_targets}
    route = "A"
    if specific:
        remaining = min(requested, sum(capacity for _, _, capacity in specific))
        capacities = {target_id: capacity for target_id, _, capacity in specific}
        while remaining:
            progressed = False
            for target_id, _, _ in specific:
                if allocations[target_id] < capacities[target_id] and remaining:
                    allocations[target_id] += 1
                    remaining -= 1
                    progressed = True
            if not progressed:
                break
    elif shared > 0:
        remaining = min(requested, shared)
        while remaining:
            for target_id in required_targets:
                if not remaining:
                    break
                allocations[target_id] += 1
                remaining -= 1
    else:
        fallback_route = "C" if budgets.get("route_C", 0) > 0 else (
            "B" if budgets.get("route_B", 0) > 0 else None
        )
        if fallback_route:
            route = fallback_route
            capacity = budgets[f"route_{fallback_route}"]
            allocations[required_targets[0]] = min(requested, capacity)

    jobs = []
    for index, target_id in enumerate(required_targets):
        count = allocations[target_id]
        if count < 1:
            continue
        design = (target_values.get(target_id) or {}).get("design") or {}
        lengths = design.get("lengths") or [8, 10, 12]
        normalized_lengths = sorted({int(value) for value in lengths})
        if not normalized_lengths or any(value < 5 or value > 30 for value in normalized_lengths):
            raise PlannerContractError(
                "design_lengths_invalid", f"target {target_id} has invalid design lengths"
            )
        jobs.append({
            "route": route,
            "target_id": target_id,
            "lengths": normalized_lengths,
            "proposal_count": count,
            "seed": (seed_base + index) % (2**31),
        })
    return jobs



def _round_budget_status(state: dict, config: PlannerConfig) -> dict:
    """Return the deterministic global round-budget state."""
    current_round = max(1, int(state.get("round") or 1))
    exhausted = current_round >= config.max_rounds
    return {
        "current_round": current_round,
        "max_rounds": config.max_rounds,
        "exhausted": exhausted,
        "reason": "max_rounds_exhausted" if exhausted else None,
    }


def _candidate_identifier(row: dict) -> str | None:
    for key in ("candidate_id", "id", "sequence_id"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return None


def _select_current_best(candidate_rows: list[dict] | None) -> dict | None:
    """Choose a stable best-effort candidate without inventing a new scorer.

    Prefer already-finalized/all-layer-pass candidates; otherwise preserve the
    CandidateIndex ordering and use the first available row.
    """
    rows = [row for row in (candidate_rows or []) if isinstance(row, dict)]
    if not rows:
        return None

    def is_truthy(value: object) -> bool:
        return str(value or "").strip().lower() in {"true", "1", "yes"}

    best = next(
        (
            row for row in rows
            if is_truthy(row.get("all_layers_pass"))
            or str(row.get("final_status") or "").lower() in {"finalized", "passed", "clear"}
            or str(row.get("status") or "").lower() in {"finalized", "passed", "clear"}
        ),
        rows[0],
    )
    return {
        "candidate_id": _candidate_identifier(best),
        "status": best.get("final_status") or best.get("status"),
        "all_layers_pass": is_truthy(best.get("all_layers_pass")),
    }


def _estimate_resource_request(
    *,
    action: str,
    resource_class: str,
    parameters: dict,
    proposal_count: int,
    candidate_limit: int,
    timeout_seconds: int,
) -> dict:
    """Estimate compute x duration for one Planner task.

    The estimate is deliberately simple and deterministic for P0.  Route A/B/C
    design jobs use route-specific GPU-minute rates; other GPU actions fall back
    to candidate/proposal-duration baselines.  Actual usage remains an
    Orchestrator/Execution concern.
    """
    request = {
        "class": resource_class,
        "gpu_job_slots": 1 if resource_class == "gpu" else 0,
        "proposal_count": int(proposal_count),
        "candidate_limit": int(candidate_limit),
        "timeout_seconds": int(timeout_seconds),
        "estimated_gpu_minutes": 0.0,
        "estimated_gpu_hours": 0.0,
        "estimate_status": "not_applicable",
        "cost_model_version": COST_MODEL_VERSION,
    }
    if resource_class != "gpu":
        return request

    design_jobs = parameters.get("design_jobs")
    if isinstance(design_jobs, list) and design_jobs:
        estimated_minutes = 0.0
        for job in design_jobs:
            if not isinstance(job, dict):
                continue
            route = str(job.get("route") or "").upper()
            count = max(0, int(job.get("proposal_count") or 0))
            rate = ROUTE_GPU_MINUTES_PER_PROPOSAL.get(
                route, DEFAULT_GPU_MINUTES_PER_PROPOSAL
            )
            estimated_minutes += count * rate
    elif candidate_limit > 0:
        estimated_minutes = int(candidate_limit) * DEFAULT_GPU_MINUTES_PER_CANDIDATE
    else:
        units = max(int(proposal_count), 1)
        route = str(parameters.get("route") or "").upper()
        rate = ROUTE_GPU_MINUTES_PER_PROPOSAL.get(
            route, DEFAULT_GPU_MINUTES_PER_PROPOSAL
        )
        estimated_minutes = units * rate

    request.update({
        "estimated_gpu_minutes": round(float(estimated_minutes), 2),
        "estimated_gpu_hours": round(float(estimated_minutes) / 60.0, 3),
        "estimate_status": "estimated",
    })
    return request


def _plan_resource_summary(tasks: list[dict]) -> dict:
    estimated_gpu_minutes = round(sum(
        float((task.get("resource_request") or {}).get("estimated_gpu_minutes") or 0.0)
        for task in tasks
    ), 2)
    return {
        "estimated_gpu_minutes": estimated_gpu_minutes,
        "estimated_gpu_hours": round(estimated_gpu_minutes / 60.0, 3),
        "gpu_task_count": sum(
            1 for task in tasks
            if (task.get("resource_request") or {}).get("class") == "gpu"
        ),
        "cost_model_version": COST_MODEL_VERSION,
    }



def _approval(
    *,
    action: str,
    critic_approval_required: bool,
    data_integrity: bool = False,
) -> dict:
    resource_class = get_action_spec(action).resource_class
    types = []
    if resource_class == "gpu":
        types.append("execution_budget")
    if critic_approval_required:
        types.append("scientific_policy")
    if data_integrity:
        types.append("data_integrity")
    return {
        "required": bool(types),
        "types": types,
        "status": "pending" if types else "not_required",
    }


def _task(
    tasks: list[dict],
    *,
    agent: str,
    action: str,
    phase: str,
    priority: str,
    disposition: str,
    reason_codes: list[str],
    candidate_ids: list[str] | None = None,
    from_task_id: str | None = None,
    parameters: dict | None = None,
    proposal_count: int = 0,
    candidate_limit: int = 0,
    approval: dict | None = None,
    depends_on: list[str] | None = None,
    outputs: list[str] | None = None,
    constraints: list[str] | None = None,
    block_reasons: list[str] | None = None,
    timeout_seconds: int = 3600,
) -> dict:
    task_id = f"T{len(tasks) + 1:03d}"
    try:
        action_spec = get_action_spec(action)
    except ValueError as exc:
        raise PlannerContractError(
            "planner_action_unknown", f"Planner task has unknown action {action!r}"
        ) from exc
    resource_class = action_spec.resource_class
    if action_spec.executable:
        from execution.action_registry import handler_for

        if handler_for(action_spec.action) is None:
            raise PlannerContractError(
                "planner_action_handler_missing",
                f"Planner task action {action!r} has no executable registry handler",
            )
    effective_block_reasons = list(block_reasons or [])
    if not action_spec.executable and "blocked_unimplemented" not in effective_block_reasons:
        effective_block_reasons.append("blocked_unimplemented")
    normalized_parameters = dict(parameters or {})
    value = {
        "task_id": task_id,
        "agent": agent,
        "action": action,
        "phase": phase,
        "priority": priority,
        "disposition": disposition,
        "reason_codes": sorted(set(reason_codes)),
        "candidate_scope": {
            "candidate_ids": sorted(set(candidate_ids or [])),
            "from_task_id": from_task_id,
        },
        "parameters": normalized_parameters,
        "resource_request": _estimate_resource_request(
            action=action,
            resource_class=resource_class,
            parameters=normalized_parameters,
            proposal_count=proposal_count,
            candidate_limit=candidate_limit,
            timeout_seconds=timeout_seconds,
        ),
        "approval": approval or _approval(
            action=action, critic_approval_required=False
        ),
        "depends_on": list(depends_on or []),
        "outputs": list(outputs or []),
        "constraints": sorted(set(constraints or [])),
        "execution_gate": {
            "status": "blocked" if effective_block_reasons else "proposed",
            "block_reasons": effective_block_reasons,
        },
    }
    # Validate the adapter shape immediately while retaining the immutable
    # dict representation consumed by existing plan artifacts.
    from contracts.task import ExecutionTask
    try:
        ExecutionTask.from_dict(value)
    except (TypeError, ValueError) as exc:
        raise PlannerContractError(
            "planner_task_contract_invalid", f"task {task_id} is not a valid ExecutionTask"
        ) from exc
    tasks.append(value)
    return value


def _add_critic_followup(
    tasks: list[dict],
    *,
    depends_on: list[str],
    priority: str,
    disposition: str,
    reason_codes: list[str],
    from_task_id: str | None = None,
) -> dict:
    existing = next(
        (
            task for task in tasks
            if task["action"] == "review_prediction_handoff"
            and task["depends_on"] == depends_on
        ),
        None,
    )
    if existing:
        existing["reason_codes"] = sorted(set(existing["reason_codes"] + reason_codes))
        return existing
    return _task(
        tasks,
        agent="critic",
        action="review_prediction_handoff",
        phase="critic",
        priority=priority,
        disposition=disposition,
        reason_codes=reason_codes,
        from_task_id=from_task_id,
        parameters={"min_cohort": 3, "low_diversity_similarity": 0.80},
        depends_on=depends_on,
        outputs=["critic_report.json"],
        constraints=["consume_immutable_prediction_handoff"],
    )


def build_plan(
    *,
    critic_report_path: str | Path,
    state: dict | None = None,
    config: PlannerConfig | None = None,
    project_config: dict | None = None,
    candidate_rows: list[dict] | None = None,
) -> dict:
    """Purely convert one frozen Critic report into an execution plan.

    ``project_config`` optionally injects an explicit approved project config
    (PR5, Engineering Standard §7); it takes precedence over the project entry
    carried in ``state`` and must agree with ``state``'s ``project_id``.
    When omitted, behaviour is unchanged.

    The injected config must also be injected into Execution (or carried in
    State), which re-verifies the digest and fails closed with
    ``project_config_drift`` on mismatch.
    """
    config = config or PlannerConfig()
    state = dict(state if state is not None else State.load())
    if project_config is not None:
        state["project_config"] = project_config
        injected_project_id = str(project_config.get("project_id") or "").strip()
        state_project_id = str(state.get("project_id") or "").strip()
        if injected_project_id and state_project_id and injected_project_id != state_project_id:
            raise PlannerContractError(
                "planner_project_mismatch",
                "injected project config differs from State project ID",
            )
    report_path = Path(critic_report_path).expanduser().resolve()
    report = _read_json(report_path, "critic_report")
    report_sha = file_sha256(report_path)
    _validate_critic_report(report, state, report_sha)
    budgets, total_design_budget = _budget_snapshot(state)
    source_round = int(state.get("round") or 1)
    round_budget = _round_budget_status(state, config)
    project_id = str(
        report.get("source", {}).get("project_id")
        or state.get("project_id")
        or "unknown_project"
    ).strip()
    supplied_workflow_id = (
        state.get("workflow_id")
        or report.get("workflow_id")
        or report.get("source", {}).get("workflow_id")
    )
    if supplied_workflow_id:
        try:
            TraceContext(project_id=project_id, workflow_id=str(supplied_workflow_id))
        except ValueError as exc:
            raise PlannerContractError(
                "planner_workflow_id_invalid", "Planner received an invalid workflow_id"
            ) from exc
        workflow_id = str(supplied_workflow_id)
    else:
        workflow_id = derive_workflow_id(
            project_id, report["report_id"], report_sha, source_round
        )

    issues_by_code = {issue["code"]: issue for issue in report["issues"]}
    recommendations_by_action = {
        recommendation["action"]: recommendation
        for recommendation in report["recommendations"]
    }
    tasks: list[dict] = []

    design_recommendations = [
        recommendation
        for recommendation in report["recommendations"]
        if recommendation["action"] in DESIGN_ITERATION_ACTIONS
    ]
    if design_recommendations:
        reason_codes = sorted({
            code
            for recommendation in design_recommendations
            for code in recommendation["reason_codes"]
        })
        disposition = _reason_disposition(reason_codes, issues_by_code)
        priority = min(
            (recommendation["priority"] for recommendation in design_recommendations),
            key=PRIORITY_RANK.__getitem__,
        )
        requested = (
            config.optional_design_batch_size
            if disposition == "optional"
            else config.design_batch_size
        )
        requested_proposal_count = min(
            requested, config.max_design_proposals_per_plan, total_design_budget
        )
        required_targets = list(report["source"].get("required_targets") or [])
        design_jobs = _materialize_design_jobs(
            state=state,
            required_targets=required_targets,
            budgets=budgets,
            requested=requested_proposal_count,
            seed_material=report_sha,
        )
        proposal_count = sum(job["proposal_count"] for job in design_jobs)
        block_reasons = []
        if proposal_count < 1:
            block_reasons.append("design_budget_missing_or_exhausted")
        design_task = _task(
            tasks,
            agent="design",
            action="iterate_design",
            phase="design",
            priority=priority,
            disposition=disposition,
            reason_codes=reason_codes,
            candidate_ids=_candidate_ids(reason_codes, issues_by_code),
            parameters={
                "strategy_directives": [
                    recommendation["action"] for recommendation in design_recommendations
                ],
                "required_targets": required_targets,
                "route_budget_snapshot": budgets,
                "design_jobs": design_jobs,
                "project_config_digest": object_sha256(
                    state.get("project_config") or {}
                ),
                "reuse_existing_prediction_evidence": True,
            },
            proposal_count=proposal_count,
            candidate_limit=proposal_count,
            approval=_approval(action="iterate_design", critic_approval_required=False),
            outputs=["design_task_result.json"],
            constraints=[
                "append_candidates_only",
                "preserve_source_candidate_evidence",
                "single_gpu_serial_execution",
            ],
            block_reasons=block_reasons,
        )
        prediction_task = _task(
            tasks,
            agent="prediction",
            action="evaluate_new_design_candidates",
            phase="evaluate",
            priority=priority,
            disposition=disposition,
            reason_codes=reason_codes,
            from_task_id=design_task["task_id"],
            parameters={
                "reuse_complete_evidence": True,
                "evidence_mode": "reuse_or_generate_full",
                "predictor_protocol": "af2_boltz2_prodigy_rosetta_postrelax_v1",
            },
            candidate_limit=min(proposal_count, config.max_prediction_candidates_per_task),
            approval=_approval(
                action="evaluate_new_design_candidates", critic_approval_required=False
            ),
            depends_on=[design_task["task_id"]],
            outputs=["prediction_handoff.json"],
            constraints=[
                "evaluate_only_new_or_incomplete_candidates",
                "reuse_complete_prediction_evidence",
                "single_gpu_serial_execution",
            ],
            block_reasons=(
                ["upstream_design_task_blocked"] if block_reasons else []
            ),
        )
        _add_critic_followup(
            tasks,
            depends_on=[prediction_task["task_id"]],
            priority=priority,
            disposition=disposition,
            reason_codes=reason_codes,
            from_task_id=prediction_task["task_id"],
        )

    for recommendation in report["recommendations"]:
        action = recommendation["action"]
        if action in DESIGN_ITERATION_ACTIONS:
            continue
        mapping = RECOMMENDATION_MAPPINGS[action]
        reason_codes = list(recommendation["reason_codes"])
        candidate_ids = _candidate_ids(reason_codes, issues_by_code)
        disposition = _reason_disposition(reason_codes, issues_by_code)
        resource_class = get_action_spec(mapping.task_action).resource_class
        constraints = []
        data_integrity = False
        parameters: dict[str, Any] = {}
        outputs: list[str] = []
        if action == "calibrate_thresholds":
            constraints.extend([
                "produce_calibration_proposal_only",
                "do_not_apply_thresholds_without_human_approval",
            ])
            parameters["threshold_keys"] = sorted({
                str(key)
                for code in reason_codes
                for evidence in issues_by_code[code].get("evidence", [])
                if isinstance(evidence, dict)
                for key in evidence.get("threshold_keys", [])
            })
            outputs = ["threshold_calibration_proposal.json"]
        elif action == "deduplicate_candidates":
            constraints.extend([
                "audit_only",
                "do_not_delete_candidates_automatically",
            ])
            outputs = ["duplicate_resolution_proposal.json"]
        elif action == "repair_candidate_index":
            data_integrity = True
            constraints.extend([
                "reconcile_against_immutable_prediction_records",
                "preserve_previous_index_backup",
            ])
            outputs = ["candidate_index_repair_report.json"]
        elif action == "complete_prediction_evidence":
            constraints.extend([
                "run_only_missing_prediction_steps",
                "reuse_complete_prediction_evidence",
                "single_gpu_serial_execution",
            ])
            parameters.update({
                "reuse_complete_evidence": True,
                "evidence_mode": "reuse_or_generate_full",
                "predictor_protocol": "af2_boltz2_prodigy_rosetta_postrelax_v1",
            })
            outputs = ["prediction_handoff.json"]
        elif action == "regenerate_invalid_artifact":
            constraints.extend([
                "regenerate_only_invalid_artifacts",
                "preserve_invalid_artifact_for_audit",
                "single_gpu_serial_execution",
            ])
            outputs = ["prediction_handoff.json"]
        task = _task(
            tasks,
            agent=mapping.agent,
            action=mapping.task_action.value,
            phase=mapping.phase,
            priority=recommendation["priority"],
            disposition=disposition,
            reason_codes=reason_codes,
            candidate_ids=candidate_ids,
            parameters=parameters,
            candidate_limit=(
                min(len(candidate_ids), config.max_prediction_candidates_per_task)
                if resource_class == "gpu" else 0
            ),
            approval=_approval(
                action=mapping.task_action.value,
                critic_approval_required=bool(recommendation.get("approval_required")),
                data_integrity=data_integrity,
            ),
            outputs=outputs,
            constraints=constraints,
        )
        if action in {
            "complete_prediction_evidence",
            "regenerate_invalid_artifact",
        }:
            _add_critic_followup(
                tasks,
                depends_on=[task["task_id"]],
                priority=recommendation["priority"],
                disposition=disposition,
                reason_codes=reason_codes,
                from_task_id=task["task_id"],
            )
        elif action == "repair_candidate_index":
            _add_critic_followup(
                tasks,
                depends_on=[task["task_id"]],
                priority="P0",
                disposition="recovery",
                reason_codes=reason_codes,
            )

    # Global round budget is a normal degraded-completion condition.  Build the
    # deterministic candidate/result metadata but schedule no additional work.
    if round_budget["exhausted"]:
        tasks.clear()

    if report["verdict"] == "clear" and not round_budget["exhausted"]:
        _task(
            tasks,
            agent="reporter",
            action="prepare_final_candidate_report",
            phase="report",
            priority="P1",
            disposition="required",
            reason_codes=[],
            parameters={
                "prediction_run_id": report["source"].get("prediction_run_id"),
                "critic_report_id": report["report_id"],
                "candidate_nomination_requires_human_review": True,
            },
            outputs=["candidate_review_packet"],
            constraints=["do_not_nominate_candidates_automatically"],
        )

    # Apply the configured per-task timeout to the generated execution contract.
    for task in tasks:
        request = task.get("resource_request") or {}
        request["timeout_seconds"] = config.task_timeout_seconds

    # A Critic blocker freezes every scientific/optional branch.  Only recovery
    # tasks and their verification step remain eligible for approval/dispatch.
    if report["verdict"] == "blocked":
        for task in tasks:
            if task["disposition"] != "recovery":
                task["execution_gate"]["status"] = "blocked"
                if "critic_blocker_requires_recovery" not in task["execution_gate"][
                    "block_reasons"
                ]:
                    task["execution_gate"]["block_reasons"].append(
                        "critic_blocker_requires_recovery"
                    )

    blocked_tasks = [
        task["task_id"] for task in tasks
        if task["execution_gate"]["status"] == "blocked"
        and task["disposition"] != "optional"
    ]
    required_approval_tasks = [
        task["task_id"] for task in tasks
        if task["approval"]["required"]
        and task["disposition"] != "optional"
        and task["execution_gate"]["status"] == "proposed"
    ]
    optional_task_ids = [
        task["task_id"] for task in tasks if task["disposition"] == "optional"
    ]
    if round_budget["exhausted"]:
        status = "degraded_best_effort"
    elif report["verdict"] == "blocked":
        status = "recovery_only"
    elif blocked_tasks:
        status = "blocked"
    elif required_approval_tasks:
        status = "awaiting_approval"
    elif tasks:
        status = "ready"
    else:
        status = "no_action"

    has_required_iteration = any(
        task["phase"] in {"design", "evaluate", "iterate"}
        and task["disposition"] != "optional"
        for task in tasks
    )
    target_round = source_round + 1 if has_required_iteration and not round_budget["exhausted"] else source_round
    input_digest = object_sha256({
        "critic_report_path": str(report_path),
        "critic_report_sha256": report_sha,
        "workflow_id": workflow_id,
        "state": {
            "project_id": state.get("project_id"),
            "round": source_round,
            "design_budget": budgets,
            "project_config_digest": object_sha256(
                state.get("project_config") or {}
            ),
            "critic_report_id": (state.get("critic") or {}).get("report_id")
            if isinstance(state.get("critic"), dict) else None,
        },
        "config": asdict(config),
        "planner_version": PLANNER_VERSION,
    })
    plan_id = f"planner_{input_digest[:12]}"
    resource_summary = _plan_resource_summary(tasks)
    best_effort = _select_current_best(candidate_rows)
    if round_budget["exhausted"]:
        summary = (
            f"Planner round budget exhausted at round {source_round}/{config.max_rounds}; "
            "no further iteration was scheduled and the current best available "
            "candidate is returned for best-effort delivery."
        )
    else:
        summary = (
            f"Planner converted Critic verdict={report['verdict']} into {len(tasks)} task(s): "
            f"status={status}; required approvals={len(required_approval_tasks)}; "
            f"blocked tasks={len(blocked_tasks)}; optional tasks={len(optional_task_ids)}; "
            f"estimated GPU={resource_summary['estimated_gpu_hours']:.3f} GPU·h."
        )
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "planner_version": PLANNER_VERSION,
        "plan_id": plan_id,
        "workflow_id": workflow_id,
        "input_digest": input_digest,
        "source": {
            "critic_report": str(report_path),
            "critic_report_sha256": report_sha,
            "critic_report_id": report["report_id"],
            "critic_verdict": report["verdict"],
            "prediction_run_id": report["source"].get("prediction_run_id"),
            "project_id": report["source"].get("project_id"),
            "workflow_id": workflow_id,
        },
        "status": status,
        "summary": summary,
        "cycle": {
            "source_round": source_round,
            "target_round": target_round,
            "round_advancement_deferred_to_orchestrator": True,
        },
        "budget_request": {
            "configured_design_budget_snapshot": budgets,
            "configured_design_budget_total": total_design_budget,
            "requested_design_proposals": sum(
                task["resource_request"]["proposal_count"] for task in tasks
            ),
            "requested_gpu_job_slots": sum(
                task["resource_request"]["gpu_job_slots"] for task in tasks
                if task["disposition"] != "optional"
            ),
            "gpu_minutes": resource_summary["estimated_gpu_minutes"],
            "gpu_hours": resource_summary["estimated_gpu_hours"],
            "gpu_minutes_status": "estimated",
            "cost_model_version": resource_summary["cost_model_version"],
            "reservation_status": "not_reserved",
        },
        "resource_summary": resource_summary,
        "budget_status": round_budget,
        "degradation": (
            {
                "active": True,
                "reason": round_budget["reason"],
                "delivery_mode": "best_effort",
                "best_candidate": best_effort,
                "message": (
                    "Planner budget exhausted; returning the current best available "
                    "candidate instead of scheduling another iteration."
                ),
            }
            if round_budget["exhausted"] else
            {"active": False, "reason": None, "delivery_mode": None, "best_candidate": None}
        ),
        "policy_constraints": sorted(MANDATORY_POLICY_CONSTRAINTS),
        "approval_request": {
            "artifact_required": bool(required_approval_tasks),
            "required_task_ids": required_approval_tasks,
            "optional_task_ids": optional_task_ids,
            "approval_schema_version": APPROVAL_SCHEMA_VERSION,
            "approval_must_bind_plan_sha256": True,
        },
        "execution": {
            "automatic_dispatch_allowed": False,
            "blocked_task_ids": blocked_tasks,
            "entry_task_ids": [
                task["task_id"] for task in tasks
                if not task["depends_on"]
                and task["execution_gate"]["status"] == "proposed"
            ],
            "orchestrator_required": True,
        },
        "tasks": tasks,
    }


def run(
    *,
    critic_report_path: str | Path,
    output_path: str | Path | None = None,
    state: dict | None = None,
    config: PlannerConfig | None = None,
    project_config: dict | None = None,
    candidate_rows: list[dict] | None = None,
) -> dict:
    """Build, persist, and idempotently register a Planner execution plan."""
    state = dict(state if state is not None else State.load())
    plan = build_plan(
        critic_report_path=critic_report_path,
        state=state,
        config=config,
        project_config=project_config,
        candidate_rows=(candidate_rows if candidate_rows is not None else CandidateIndex.load()),
    )
    report_path = Path(critic_report_path).expanduser().resolve()
    if output_path is None:
        output_path = report_path.parent / "planner" / plan["plan_id"] / "execution_plan.json"
    output_path = Path(output_path).expanduser().resolve()
    _atomic_json(output_path, plan)
    plan_sha = file_sha256(output_path)
    summary = {
        "planner_version": PLANNER_VERSION,
        "plan_id": plan["plan_id"],
        "workflow_id": plan["workflow_id"],
        "plan_path": str(output_path),
        "plan_sha256": plan_sha,
        "critic_report_id": plan["source"]["critic_report_id"],
        "status": plan["status"],
        "task_count": len(plan["tasks"]),
        "required_approval_task_ids": plan["approval_request"]["required_task_ids"],
        "estimated_gpu_minutes": plan["resource_summary"]["estimated_gpu_minutes"],
        "estimated_gpu_hours": plan["resource_summary"]["estimated_gpu_hours"],
        "budget_status": plan["budget_status"],
        "degradation": plan["degradation"],
    }
    phase = "report" if plan["source"]["critic_verdict"] == "clear" else "iterate"
    State.update({"phase": phase, "planner": summary})
    history = State.load().get("iteration_history") or []
    if not any(
        entry.get("agent") == "planner"
        and (entry.get("summary") or {}).get("plan_id") == plan["plan_id"]
        for entry in history
    ):
        State.append_history({"phase": phase, "agent": "planner", "summary": summary})
    if not any(
        entry.get("event_type") == "planner_plan"
        and entry.get("plan_id") == plan["plan_id"]
        for entry in EvidenceLogger.get_all()
    ):
        EvidenceLogger.planner_plan(
            plan_id=plan["plan_id"],
            plan_path=str(output_path),
            plan_sha256=plan_sha,
            critic_report_id=plan["source"]["critic_report_id"],
            critic_report_path=plan["source"].get("critic_report"),
            critic_report_sha256=plan["source"].get("critic_report_sha256"),
            status=plan["status"],
            task_count=len(plan["tasks"]),
            required_approval_task_ids=plan["approval_request"]["required_task_ids"],
            estimated_gpu_minutes=plan["resource_summary"]["estimated_gpu_minutes"],
            estimated_gpu_hours=plan["resource_summary"]["estimated_gpu_hours"],
            resource_requests=[
                {
                    "task_id": task["task_id"],
                    "agent": task["agent"],
                    "action": task["action"],
                    "route": (
                        next(
                            (
                                str(job.get("route") or "")
                                for job in (task.get("parameters") or {}).get("design_jobs", [])
                                if isinstance(job, dict) and job.get("route")
                            ),
                            None,
                        )
                        or (task.get("parameters") or {}).get("route")
                    ),
                    "parameters": task.get("parameters") or {},
                    **(task.get("resource_request") or {}),
                }
                for task in plan["tasks"]
            ],
            budget_status=plan["budget_status"],
            degradation=plan["degradation"],
            trace_context=TraceContext(
                project_id=str(plan["source"].get("project_id") or "unknown_project"),
                workflow_id=plan["workflow_id"],
                plan_id=plan["plan_id"],
            ),
        )
    return {"plan": plan, "plan_path": str(output_path), "plan_sha256": plan_sha}


def _canonicalize_plan(plan: dict) -> dict:
    """Adapt a legacy v1 plan in memory to the strict v2 trace contract."""
    if not isinstance(plan, dict):
        raise PlannerContractError("plan_invalid", "plan must be an object")
    version = plan.get("schema_version")
    if version == LEGACY_PLAN_SCHEMA_VERSION:
        canonical = json.loads(json.dumps(plan))
        source = canonical.get("source")
        if not isinstance(source, dict):
            raise PlannerContractError("plan_source_invalid", "plan source must be an object")
        project_id = str(source.get("project_id") or "unknown_project")
        workflow_id = canonical.get("workflow_id") or source.get("workflow_id")
        if not workflow_id:
            source_id = str(source.get("critic_report_id") or canonical.get("plan_id") or "plan")
            source_sha256 = str(
                source.get("critic_report_sha256") or canonical.get("input_digest") or ""
            )
            if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
                source_sha256 = object_sha256(plan)
            workflow_id = derive_workflow_id(
                project_id,
                source_id,
                source_sha256,
                (canonical.get("cycle") or {}).get("source_round", 1),
            )
        canonical["workflow_id"] = str(workflow_id)
        source["workflow_id"] = str(workflow_id)
        canonical["schema_version"] = PLAN_SCHEMA_VERSION
        return canonical
    if version != PLAN_SCHEMA_VERSION:
        raise PlannerContractError("plan_schema_unsupported", "unsupported plan schema")
    return plan


def _validate_plan_for_approval(plan: dict, plan_path: Path) -> dict:
    """Recheck security invariants before an approval artifact can be issued."""
    plan = _canonicalize_plan(plan)
    if plan.get("planner_version") != PLANNER_VERSION:
        raise PlannerContractError(
            "plan_version_unsupported", "approval requires the current Planner version"
        )
    constraints = set(plan.get("policy_constraints") or [])
    missing = sorted(MANDATORY_POLICY_CONSTRAINTS - constraints)
    if missing:
        raise PlannerContractError(
            "plan_policy_constraint_missing",
            f"plan lacks mandatory constraints: {missing}",
        )
    execution = plan.get("execution")
    if not isinstance(execution, dict) or execution.get("automatic_dispatch_allowed") is not False:
        raise PlannerContractError(
            "plan_execution_policy_invalid", "plan must prohibit automatic dispatch"
        )
    if execution.get("orchestrator_required") is not True:
        raise PlannerContractError(
            "plan_execution_policy_invalid", "plan must require Orchestrator validation"
        )

    source = plan.get("source")
    if not isinstance(source, dict):
        raise PlannerContractError("plan_source_invalid", "plan source must be an object")
    workflow_id = plan.get("workflow_id")
    source_workflow_id = source.get("workflow_id")
    if not workflow_id or not source_workflow_id:
        raise PlannerContractError(
            "plan_workflow_id_missing", "canonical plan requires workflow_id at plan and source"
        )
    if workflow_id != source_workflow_id:
        raise PlannerContractError(
            "plan_workflow_id_mismatch", "plan and source workflow_id differ"
        )
    try:
        TraceContext(
            project_id=str(source.get("project_id") or "unknown_project"),
            workflow_id=str(workflow_id),
            plan_id=str(plan.get("plan_id") or "") or None,
        )
    except ValueError as exc:
        raise PlannerContractError("plan_workflow_id_invalid", "plan workflow_id is invalid") from exc
    critic_path_value = str(source.get("critic_report") or "").strip()
    if not critic_path_value:
        raise PlannerContractError("plan_source_invalid", "plan has no Critic report path")
    critic_path = Path(critic_path_value).expanduser()
    if not critic_path.is_absolute():
        critic_path = (plan_path.parent / critic_path).resolve()
    else:
        critic_path = critic_path.resolve()
    declared_critic_sha = _validate_sha256(
        source.get("critic_report_sha256"),
        "plan_source_hash_invalid",
        "plan Critic report SHA-256",
    )
    if file_sha256(critic_path) != declared_critic_sha:
        raise PlannerContractError(
            "plan_source_hash_mismatch", "Critic report changed after planning"
        )

    tasks = plan.get("tasks")
    if not isinstance(tasks, list):
        raise PlannerContractError("plan_tasks_invalid", "plan tasks must be an array")
    tasks_by_id: dict[str, dict] = {}
    for task in tasks:
        if not isinstance(task, dict):
            raise PlannerContractError("plan_task_invalid", "every plan task must be an object")
        task_id = str(task.get("task_id") or "")
        if not re.fullmatch(r"T[0-9]{3}", task_id) or task_id in tasks_by_id:
            raise PlannerContractError(
                "plan_task_id_invalid", f"missing, invalid, or duplicate task ID: {task_id!r}"
            )
        resource = task.get("resource_request")
        approval = task.get("approval")
        if not isinstance(resource, dict) or not isinstance(approval, dict):
            raise PlannerContractError(
                "plan_task_contract_invalid", f"task {task_id} lacks resource/approval contract"
            )
        if resource.get("class") == "gpu" and (
            approval.get("required") is not True
            or "execution_budget" not in (approval.get("types") or [])
        ):
            raise PlannerContractError(
                "plan_gpu_approval_missing",
                f"GPU task {task_id} lacks execution-budget approval",
            )
        gate = task.get("execution_gate")
        if not isinstance(gate, dict) or gate.get("status") not in {"proposed", "blocked"}:
            raise PlannerContractError(
                "plan_task_gate_invalid", f"task {task_id} has an invalid execution gate"
            )
        try:
            from execution.contracts import ALL_KNOWN_ACTIONS, validate_task_parameters
            from contracts.action import get_action_spec

            action = str(task.get("action") or "")
            if action not in ALL_KNOWN_ACTIONS:
                raise PlannerContractError(
                    "plan_action_unknown", f"task {task_id} has unknown action {action!r}"
                )
            spec = get_action_spec(action)
            if not spec.executable and gate.get("status") == "proposed":
                raise PlannerContractError(
                    "plan_unimplemented_action_proposed",
                    f"task {task_id} uses non-executable action {action}",
                )
            if spec.executable and gate.get("status") == "proposed":
                validate_task_parameters(task)
        except PlannerContractError:
            raise
        except Exception as exc:
            raise PlannerContractError(
                getattr(exc, "code", "plan_execution_contract_invalid"), str(exc)
            ) from exc
        tasks_by_id[task_id] = task
    for task_id, task in tasks_by_id.items():
        dependencies = task.get("depends_on")
        if not isinstance(dependencies, list) or task_id in dependencies:
            raise PlannerContractError(
                "plan_dependency_invalid", f"task {task_id} has invalid dependencies"
            )
        unknown = sorted(set(dependencies) - set(tasks_by_id))
        if unknown:
            raise PlannerContractError(
                "plan_dependency_unknown", f"task {task_id} depends on {unknown}"
            )
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visited:
            return
        if task_id in visiting:
            raise PlannerContractError(
                "plan_dependency_cycle", "plan task dependencies contain a cycle"
            )
        visiting.add(task_id)
        for dependency in tasks_by_id[task_id]["depends_on"]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in tasks_by_id:
        visit(task_id)
    return plan


def record_approval(
    *,
    plan_path: str | Path,
    task_ids: list[str],
    approver: str,
    justification: str,
    max_gpu_job_slots: int | None = None,
    max_gpu_minutes: float | None = None,
    max_design_proposals: int | None = None,
    max_prediction_candidates: int | None = None,
    output_path: str | Path | None = None,
) -> dict:
    """Record explicit human approval bound to one immutable plan digest."""
    plan_path = Path(plan_path).expanduser().resolve()
    plan = _read_json(plan_path, "planner_plan")
    plan = _validate_plan_for_approval(plan, plan_path)
    plan_id = str(plan.get("plan_id") or "")
    input_digest = _validate_sha256(
        plan.get("input_digest"), "plan_input_digest_invalid", "plan input_digest"
    )
    if not PLAN_ID_RE.fullmatch(plan_id) or plan_id != f"planner_{input_digest[:12]}":
        raise PlannerContractError("plan_id_invalid", "plan ID is not bound to input_digest")
    plan_sha = file_sha256(plan_path)
    approver = str(approver or "").strip()
    justification = str(justification or "").strip()
    if not approver or not justification:
        raise PlannerContractError(
            "approval_identity_required", "approver and justification are required"
        )
    selected = sorted(set(str(task_id).strip() for task_id in task_ids if str(task_id).strip()))
    if not selected:
        raise PlannerContractError(
            "approval_scope_required", "approval requires explicit task IDs"
        )
    tasks_by_id = {task["task_id"]: task for task in plan.get("tasks", [])}
    unknown = sorted(set(selected) - set(tasks_by_id))
    if unknown:
        raise PlannerContractError(
            "approval_task_unknown", f"approval references unknown tasks: {unknown}"
        )
    non_approvable = [
        task_id for task_id in selected
        if not tasks_by_id[task_id].get("approval", {}).get("required")
    ]
    if non_approvable:
        raise PlannerContractError(
            "approval_task_not_required",
            f"tasks do not request approval: {non_approvable}",
        )
    blocked = [
        task_id for task_id in selected
        if tasks_by_id[task_id].get("execution_gate", {}).get("status") == "blocked"
    ]
    if blocked:
        raise PlannerContractError(
            "approval_task_blocked", f"blocked tasks cannot be approved: {blocked}"
        )

    gpu_tasks = [
        tasks_by_id[task_id] for task_id in selected
        if tasks_by_id[task_id]["resource_request"]["class"] == "gpu"
    ]
    if gpu_tasks:
        required_concurrent_slots = max(
            task["resource_request"]["gpu_job_slots"] for task in gpu_tasks
        )
        if max_gpu_job_slots is None or max_gpu_job_slots < required_concurrent_slots:
            raise PlannerContractError(
                "approval_gpu_limit_insufficient",
                "max_gpu_job_slots must cover the maximum concurrent task request",
            )
        if max_gpu_minutes is None or max_gpu_minutes <= 0:
            raise PlannerContractError(
                "approval_gpu_minutes_required",
                "max_gpu_minutes must be positive for selected GPU tasks",
            )
    requested_proposals = sum(
        task["resource_request"]["proposal_count"] for task in gpu_tasks
    )
    if requested_proposals and (
        max_design_proposals is None or max_design_proposals < requested_proposals
    ):
        raise PlannerContractError(
            "approval_design_limit_insufficient",
            "max_design_proposals is lower than the selected plan request",
        )
    requested_predictions = sum(
        task["resource_request"]["candidate_limit"] for task in gpu_tasks
        if task["agent"] in {"prediction", "prediction/design", "design/prediction"}
    )
    if requested_predictions and (
        max_prediction_candidates is None
        or max_prediction_candidates < requested_predictions
    ):
        raise PlannerContractError(
            "approval_prediction_limit_insufficient",
            "max_prediction_candidates is lower than the selected plan request",
        )

    semantic = {
        "schema_version": APPROVAL_SCHEMA_VERSION,
        "plan_id": plan_id,
        "plan_path": str(plan_path),
        "plan_sha256": plan_sha,
        "project_id": (plan.get("source") or {}).get("project_id"),
        "approved_task_ids": selected,
        "approver": approver,
        "justification": justification,
        "budget_limits": {
            "max_gpu_job_slots": max_gpu_job_slots,
            "max_gpu_minutes": max_gpu_minutes,
            "max_design_proposals": max_design_proposals,
            "max_prediction_candidates": max_prediction_candidates,
        },
    }
    approval_id = f"approval_{object_sha256(semantic)[:12]}"
    approval = dict(semantic)
    approval.update({
        "approval_id": approval_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "authorization_semantics": (
            "Only approved_task_ids within budget_limits may be dispatched; "
            "any plan content change invalidates this approval."
        ),
    })
    if output_path is None:
        output_path = plan_path.parent / "approvals" / f"{approval_id}.json"
    output_path = Path(output_path).expanduser().resolve()
    if output_path.exists():
        existing = _read_json(output_path, "planner_approval")
        existing_semantic = {
            key: existing.get(key) for key in semantic
        }
        if existing_semantic != semantic or existing.get("approval_id") != approval_id:
            raise PlannerContractError(
                "approval_output_conflict", "approval path contains different content"
            )
        approval = existing
    else:
        _atomic_json(output_path, approval)
    approval_sha = file_sha256(output_path)
    if not any(
        entry.get("event_type") == "planner_approval_recorded"
        and entry.get("approval_id") == approval_id
        for entry in EvidenceLogger.get_all()
    ):
        EvidenceLogger.planner_approval_recorded(
            approval_id=approval_id,
            approval_path=str(output_path),
            approval_sha256=approval_sha,
            plan_id=plan_id,
            plan_sha256=plan_sha,
            approved_task_ids=selected,
            approver=approver,
            budget_limits=semantic["budget_limits"],
            trace_context=TraceContext(
                project_id=str(semantic.get("project_id") or "unknown_project"),
                workflow_id=str(plan.get("workflow_id") or derive_workflow_id(
                    str(semantic.get("project_id") or "unknown_project"),
                    str(plan.get("source", {}).get("critic_report_id") or plan_id),
                    str(plan.get("source", {}).get("critic_report_sha256") or plan_sha),
                    plan.get("cycle", {}).get("source_round", 1),
                )),
                plan_id=plan_id,
            ),
        )
    return {
        "approval": approval,
        "approval_path": str(output_path),
        "approval_sha256": approval_sha,
    }


def plan(
    phase: str | None = None,
    state: dict | None = None,
    candidate_rows: list[dict] | None = None,
    project_config: dict | None = None,
    config: PlannerConfig | None = None,
) -> list[dict]:
    """Compatibility bootstrap planner for runs that have no Critic report yet."""
    state = dict(state if state is not None else State.load())
    if project_config is not None:
        state["project_config"] = project_config
        injected_project_id = str(project_config.get("project_id") or "").strip()
        state_project_id = str(state.get("project_id") or "").strip()
        if injected_project_id and state_project_id and injected_project_id != state_project_id:
            raise PlannerContractError(
                "planner_project_mismatch",
                "injected project config differs from State project ID",
            )
    candidate_rows = list(
        candidate_rows if candidate_rows is not None else CandidateIndex.load()
    )
    config = config or PlannerConfig()
    round_budget = _round_budget_status(state, config)
    if round_budget["exhausted"]:
        return [{
            "agent": "planner",
            "action": "deliver_best_effort",
            "phase": "report",
            "reason": "max_rounds_exhausted",
            "execution_allowed": False,
            "status": "degraded",
            "delivery_mode": "best_effort",
            "budget_status": round_budget,
            "best_candidate": _select_current_best(candidate_rows),
            "message": (
                "Planner budget exhausted; returning the current best available "
                "candidate instead of continuing the iteration loop."
            ),
        }]

    project = state.get("project_config") or {}
    review = project.get("review") or {}
    if review.get("status") != "approved" or (
        review.get("approved_digest") != review.get("content_digest")
    ):
        return [{
            "agent": "research",
            "action": "review_and_approve_project_config",
            "phase": "research",
            "reason": "project configuration lacks a current digest-bound approval",
            "execution_allowed": False,
        }]
    has_research = bool(
        state.get("pocket_differences")
        or state.get("known_dual_binders")
        or state.get("research_pipeline_meta")
    )
    if not has_research:
        return [{
            "agent": "research",
            "action": "run",
            "phase": "research",
            "reason": "approved project has no Research result in State",
            "execution_allowed": True,
        }]
    if not candidate_rows:
        return [{
            "agent": "design",
            "action": "generate_candidates",
            "phase": "design",
            "reason": "Research is present but CandidateIndex is empty",
            "execution_allowed": False,
            "approval_required": "execution_budget",
        }]
    prediction = state.get("prediction") or {}
    if not prediction.get("handoff_path"):
        return [{
            "agent": "prediction",
            "action": "run",
            "phase": "evaluate",
            "reason": "candidates exist but State has no Prediction handoff",
            "execution_allowed": False,
            "approval_required": "execution_budget",
        }]
    critic = state.get("critic") or {}
    if not critic.get("report_path"):
        return [{
            "agent": "critic",
            "action": "review_prediction_handoff",
            "phase": "critic",
            "reason": "Prediction handoff exists but State has no Critic report",
            "execution_allowed": True,
            "handoff_path": prediction["handoff_path"],
        }]
    return [{
        "agent": "planner",
        "action": "build_from_critic",
        "phase": phase or "iterate",
        "reason": "Critic report is ready for deterministic planning",
        "execution_allowed": True,
        "critic_report_path": critic["report_path"],
    }]


def adjust(
    report: str | Path,
    state: dict | None = None,
    project_config: dict | None = None,
    config: PlannerConfig | None = None,
    candidate_rows: list[dict] | None = None,
) -> dict:
    """Backward-compatible name for pure Critic-driven planning."""
    return build_plan(
        critic_report_path=report,
        state=state,
        config=config,
        project_config=project_config,
        candidate_rows=candidate_rows,
    )


def _config_from_args(args: argparse.Namespace) -> PlannerConfig:
    return PlannerConfig(
        design_batch_size=args.design_batch_size,
        optional_design_batch_size=args.optional_design_batch_size,
        max_design_proposals_per_plan=args.max_design_proposals,
        max_prediction_candidates_per_task=args.max_prediction_candidates,
        max_rounds=args.max_rounds,
        task_timeout_seconds=args.task_timeout_seconds,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="build a plan from a Critic report")
    build.add_argument("--critic-report", required=True)
    build.add_argument("--output")
    build.add_argument("--design-batch-size", type=int, default=12)
    build.add_argument("--optional-design-batch-size", type=int, default=3)
    build.add_argument("--max-design-proposals", type=int, default=48)
    build.add_argument("--max-prediction-candidates", type=int, default=48)
    build.add_argument("--max-rounds", type=int, default=5)
    build.add_argument("--task-timeout-seconds", type=int, default=3600)

    approve = subparsers.add_parser(
        "approve", help="record explicit human approval for selected plan tasks"
    )
    approve.add_argument("--plan", required=True)
    approve.add_argument("--task", action="append", dest="tasks", required=True)
    approve.add_argument("--approver", required=True)
    approve.add_argument("--justification", required=True)
    approve.add_argument("--max-gpu-job-slots", type=int)
    approve.add_argument("--max-gpu-minutes", type=float)
    approve.add_argument("--max-design-proposals", type=int)
    approve.add_argument("--max-prediction-candidates", type=int)
    approve.add_argument("--output")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "build":
            result = run(
                critic_report_path=args.critic_report,
                output_path=args.output,
                config=_config_from_args(args),
            )
            payload = {
                "status": "complete",
                "plan_id": result["plan"]["plan_id"],
                "plan_status": result["plan"]["status"],
                "plan_path": result["plan_path"],
                "plan_sha256": result["plan_sha256"],
                "task_count": len(result["plan"]["tasks"]),
                "required_approval_task_ids": result["plan"]["approval_request"][
                    "required_task_ids"
                ],
            }
        elif args.command == "approve":
            result = record_approval(
                plan_path=args.plan,
                task_ids=args.tasks,
                approver=args.approver,
                justification=args.justification,
                max_gpu_job_slots=args.max_gpu_job_slots,
                max_gpu_minutes=args.max_gpu_minutes,
                max_design_proposals=args.max_design_proposals,
                max_prediction_candidates=args.max_prediction_candidates,
                output_path=args.output,
            )
            payload = {
                "status": "complete",
                "approval_id": result["approval"]["approval_id"],
                "approval_path": result["approval_path"],
                "approval_sha256": result["approval_sha256"],
                "approved_task_ids": result["approval"]["approved_task_ids"],
            }
        else:
            raise AssertionError(args.command)
    except (PlannerContractError, OSError, ValueError) as exc:
        print(json.dumps({
            "status": "error",
            "code": getattr(exc, "code", exc.__class__.__name__),
            "message": str(exc),
        }, ensure_ascii=False))
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())