"""errors - split from agents/critic.py (PR6)."""

from __future__ import annotations


class CriticContractError(ValueError):
    """Prediction handoff cannot be trusted by Critic."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
