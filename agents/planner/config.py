"""Planner policy configuration and module constants (PR6)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from contracts.action import RecommendationMapping
from contracts.plan import (
    LEGACY_PLAN_SCHEMA_VERSION,
    MANDATORY_POLICY_CONSTRAINTS,
    PLANNER_VERSION,
    PLAN_SCHEMA_VERSION,
)

from .errors import PlannerContractError

APPROVAL_SCHEMA_VERSION = 1
REPORT_ID_RE = re.compile(r"^critic_[0-9a-f]{12}$")
PLAN_ID_RE = re.compile(r"^planner_[0-9a-f]{12}$")

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

@dataclass(frozen=True)
class PlannerConfig:
    """Planning policy; values request capacity but never grant execution."""

    design_batch_size: int = 12
    optional_design_batch_size: int = 3
    max_design_proposals_per_plan: int = 48
    max_prediction_candidates_per_task: int = 48
    max_rounds: int = 3
    task_timeout_minutes: int = 120
    global_budget_minutes: float | None = None
    on_budget_exhausted: str = "graceful_stop_return_current_best"
    # Estimator tunables (conservative defaults). These are documented
    # conservative priors, not measured benchmarks. Adjust via PlannerConfig
    # in tests or deployment when benchmarks are available.
    gpu_minutes_per_proposal: float = 5.0
    gpu_minutes_per_candidate_factor: float = 0.25
    gpu_cost_per_minute_usd: float = 0.02

    def __post_init__(self) -> None:
        for name in (
            "design_batch_size",
            "optional_design_batch_size",
            "max_design_proposals_per_plan",
            "max_prediction_candidates_per_task",
            "max_rounds",
            "task_timeout_minutes",
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
        if self.global_budget_minutes is not None:
            # Reject non-finite values (NaN/Inf) and negative numbers
            try:
                budget_minutes = float(self.global_budget_minutes)
            except (TypeError, ValueError):
                raise PlannerContractError(
                    "planner_config_invalid", "global_budget_minutes must be a finite number"
                )
            if not math.isfinite(budget_minutes) or budget_minutes < 0:
                raise PlannerContractError(
                    "planner_config_invalid", "global_budget_minutes must be non-negative and finite"
                )
        # Validate estimator tunables: must be finite and non-negative
        for name in ("gpu_minutes_per_proposal", "gpu_minutes_per_candidate_factor", "gpu_cost_per_minute_usd"):
            try:
                v = float(getattr(self, name))
            except (TypeError, ValueError):
                raise PlannerContractError(
                    "planner_config_invalid", f"{name} must be a finite number"
                )
            if not math.isfinite(v) or v < 0:
                raise PlannerContractError(
                    "planner_config_invalid", f"{name} must be non-negative and finite"
                )
