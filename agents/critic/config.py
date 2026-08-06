"""config - split from agents/critic.py (PR6)."""

from __future__ import annotations

from dataclasses import dataclass
from .errors import CriticContractError



# ---- module-level constants (moved verbatim) ----

CRITIC_VERSION = "1.1.1"
REPORT_SCHEMA_VERSION = 1
ALLOWED_STATUSES = {
    "finalized",
    "awaiting_threshold_calibration",
    "prediction_pending",
    "needs_optimization",
    "invalid",
}
LAYER_KEYS = tuple(f"l{number}_pass" for number in range(1, 8))
SEVERITY_RANK = {"blocker": 0, "high": 1, "medium": 2, "info": 3}

LAYER_ISSUES = {
    "l1_pass": (
        "l1_monomer_quality_low",
        "Monomer structural confidence failed the configured L1 gate.",
        "improve_monomer_quality",
        "design",
    ),
    "l2_pass": (
        "l2_interface_confidence_low",
        "One or more required targets failed the ipSAE interface-confidence gate.",
        "iterate_interface_design",
        "design",
    ),
    "l3_pass": (
        "l3_interface_physics_low",
        "One or more required targets failed a physical interface gate.",
        "iterate_interface_physics",
        "design",
    ),
    "l4_pass": (
        "l4_cyclization_geometry_failed",
        "Pre/post-relax cyclic geometry failed the configured L4 gate.",
        "repair_cyclization_geometry",
        "design",
    ),
    "l5_pass": (
        "l5_hotspot_coverage_low",
        "Predicted binding poses do not sufficiently cover reviewed hotspots.",
        "retarget_reviewed_hotspots",
        "design",
    ),
    "l6_pass": (
        "l6_ensemble_convergence_low",
        "Independent predictor/model poses do not satisfy the convergence gate.",
        "improve_pose_robustness",
        "design",
    ),
    "l7_pass": (
        "l7_design_consistency_low",
        "Predicted monomer is inconsistent with the Design backbone.",
        "improve_design_consistency",
        "design",
    ),
}

LAYER_METRICS = {
    "l1_pass": ("global", ("plddt",)),
    "l2_pass": ("targets", ("ipsae",)),
    "l3_pass": ("targets", ("dg", "dg_method", "sc", "dsasa")),
    "l4_pass": (
        "global",
        ("nc_distance_pre", "nc_distance_post", "post_relax_backbone_rmsd"),
    ),
    "l5_pass": ("targets", ("hotspot_cov", "site_consistency")),
    "l6_pass": ("targets", ("pose_rmsd", "seed_convergence")),
    "l7_pass": ("global", ("scrmsd",)),
}

ACTION_DEFAULTS = {
    "regenerate_invalid_artifact": ("prediction/design", "P0", False),
    "complete_prediction_evidence": ("prediction", "P0", False),
    "regenerate_design_reference": ("design", "P0", False),
    "improve_monomer_quality": ("design", "P1", False),
    "iterate_interface_design": ("design", "P1", False),
    "iterate_interface_physics": ("design", "P1", False),
    "repair_cyclization_geometry": ("design", "P0", False),
    "retarget_reviewed_hotspots": ("design", "P1", False),
    "improve_pose_robustness": ("design", "P1", False),
    "improve_design_consistency": ("design", "P1", False),
    "calibrate_thresholds": ("research", "P2", True),
    "deduplicate_candidates": ("design", "P1", False),
    "increase_sequence_diversity": ("design", "P2", False),
    "generate_review_cohort": ("design", "P2", False),
    "repair_candidate_index": ("design/data", "P0", False),
}

@dataclass(frozen=True)
class CriticConfig:
    min_cohort_for_distribution: int = 3
    low_diversity_median_similarity: float = 0.80
    max_issue_examples: int = 20

    def __post_init__(self) -> None:
        if self.min_cohort_for_distribution < 1:
            raise CriticContractError(
                "critic_config_invalid", "minimum cohort must be positive"
            )
        if not 0 <= self.low_diversity_median_similarity <= 1:
            raise CriticContractError(
                "critic_config_invalid", "similarity threshold must be in [0, 1]"
            )
        if self.max_issue_examples < 1:
            raise CriticContractError(
                "critic_config_invalid", "max_issue_examples must be positive"
            )
