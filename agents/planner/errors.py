"""errors - split from agents/planner.py (PR6)."""

from __future__ import annotations


class PlannerContractError(ValueError):
    """A Critic report, State snapshot, plan, or approval is unsafe to use."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
