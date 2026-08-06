"""budget - split from agents/planner.py (PR6)."""

from __future__ import annotations

from .errors import PlannerContractError

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
