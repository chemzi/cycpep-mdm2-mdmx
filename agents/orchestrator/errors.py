"""errors - split from agents/orchestrator.py (PR6)."""

from __future__ import annotations


class OrchestratorContractError(ValueError):
    """Plan, approval, run state, claim, resource, or output is unsafe."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
