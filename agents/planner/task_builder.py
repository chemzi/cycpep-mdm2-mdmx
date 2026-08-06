"""task_builder - split from agents/planner.py (PR6)."""

from __future__ import annotations

from contracts.action import get_action_spec
from prediction_pipeline.contracts import object_sha256
from project_config import target_slug
from .approval import _approval
from .errors import PlannerContractError

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
        "parameters": dict(parameters or {}),
        "resource_request": {
            "class": resource_class,
            "gpu_job_slots": 1 if resource_class == "gpu" else 0,
            "proposal_count": int(proposal_count),
            "candidate_limit": int(candidate_limit),
            "estimated_gpu_minutes": None,
            "estimate_status": (
                "benchmark_required" if resource_class == "gpu" else "not_applicable"
            ),
        },
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
