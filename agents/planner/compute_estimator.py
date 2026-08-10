"""Simple compute-aware estimates for Planner v1.x.

This module provides a lightweight, auditable estimator that enriches
Planner tasks with conservative GPU-minute and cost estimates and returns
plan-level compute metadata required by a minimal "compute-aware" Planner.

Heuristics are intentionally simple for v1: per-proposal GPU minutes and
per-minute cost are configurable constants. The estimator does NOT perform
scheduling or multi-GPU packing — it only annotates the plan so downstream
Orchestrator/Frontend can make budgeting decisions and graceful-stop policies.
"""

from __future__ import annotations

from typing import Dict, Any, List

# Tunable conservative defaults for first-pass estimator
GPU_MINUTES_PER_PROPOSAL = 5.0
GPU_COST_PER_MINUTE_USD = 0.02


def enrich_plan_with_compute_estimates(
    tasks: List[dict],
    budgets: Dict[str, int],
    config: Any | None = None,
    *,
    max_rounds: int = 3,
    task_timeout_minutes: int = 120,
    global_budget_minutes: float | None = None,
    on_budget_exhausted: str = "graceful_stop_return_current_best",
) -> dict:
    """Annotate `tasks` in-place and return a compute metadata dict.

    - For GPU tasks, set `resource_request.estimated_gpu_minutes` and
      `resource_request.estimated_cost_usd` and mark `estimate_status`.
    - For non-GPU tasks, ensure estimate_status is `not_applicable`.
    - Compute a per-route summary from the provided `budgets` snapshot.
    """

    total_estimated_minutes = 0.0
    for task in tasks:
        resource = task.get("resource_request") or {}
        rclass = resource.get("class")
        # Defensive defaults
        if rclass == "gpu":
            proposals = int(resource.get("proposal_count") or 0)
            candidates = int(resource.get("candidate_limit") or 0)
            # Simple heuristic: proposals dominate cost; predictions add a bit
            estimated_minutes = proposals * GPU_MINUTES_PER_PROPOSAL + candidates * (GPU_MINUTES_PER_PROPOSAL * 0.25)
            estimated_cost = round(estimated_minutes * GPU_COST_PER_MINUTE_USD, 4)
            resource["estimated_gpu_minutes"] = float(estimated_minutes)
            resource["estimated_cost_usd"] = float(estimated_cost)
            resource["estimate_status"] = "estimated"
            total_estimated_minutes += estimated_minutes
        else:
            resource["estimated_gpu_minutes"] = None
            resource["estimated_cost_usd"] = 0.0
            resource["estimate_status"] = "not_applicable"
        task["resource_request"] = resource

    # Route-level capacity snapshot (reflect planner's design budgets)
    route_estimate = {}
    for k, v in sorted((budgets or {}).items()):
        if k.startswith("route_"):
            route_estimate[k] = int(v)

    compute_meta = {
        "route_resource_estimate": route_estimate,
        "max_rounds": int(max_rounds),
        "task_timeout_minutes": int(task_timeout_minutes),
        "global_budget_minutes": float(global_budget_minutes) if global_budget_minutes is not None else None,
        "on_budget_exhausted": on_budget_exhausted,
        "total_estimated_gpu_minutes": float(total_estimated_minutes),
        "estimator_version": "simple-v1",
    }
    return compute_meta
