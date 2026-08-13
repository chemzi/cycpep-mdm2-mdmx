"""Pure frozen-decision materialization for Planner design-job lengths."""

from __future__ import annotations

from .errors import PlannerContractError


def resolve_design_lengths(
    *,
    approved_lengths_by_target: dict[str, list[int]],
    required_targets: list[str],
    frozen_decision: dict | None,
) -> dict[str, list[int]]:
    """Return effective per-target lengths without reading ambient state."""
    effective = {
        target_id: list(approved_lengths_by_target[target_id])
        for target_id in required_targets
    }
    if frozen_decision is None:
        return effective
    if set(frozen_decision["target_ids"]) != set(required_targets):
        raise PlannerContractError(
            "exploration_decision_target_scope_mismatch",
            "frozen exploration decision target scope does not match materialized targets",
        )
    status = frozen_decision["decision_status"]
    if status not in {"adjustment", "no_adjustment"}:
        raise PlannerContractError(
            "exploration_decision_status_invalid",
            "frozen exploration decision status is not canonical",
        )
    if status == "no_adjustment":
        return effective
    proposed_weights = frozen_decision["adjustment"]["proposed_policy_weights"]
    if any(item["weight"] != 1 for item in proposed_weights):
        raise PlannerContractError(
            "exploration_decision_weight_invalid",
            "proposed policy entries must use canonical weight 1",
        )
    proposed = [int(item["length"]) for item in proposed_weights]
    if frozen_decision["adjustment"]["preferred_lengths"] != proposed:
        raise PlannerContractError(
            "exploration_decision_preferred_lengths_mismatch",
            "adjustment preferred lengths do not match canonical proposed weights",
        )
    for target_id in required_targets:
        if not set(proposed).issubset(approved_lengths_by_target[target_id]):
            raise PlannerContractError(
                "exploration_decision_lengths_outside_envelope",
                f"decision lengths exceed target {target_id}'s approved length envelope",
            )
    return {target_id: list(proposed) for target_id in required_targets}
