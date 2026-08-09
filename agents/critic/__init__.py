"""Critic Agent package (PR6 split from agents/critic.py).

Public names are re-exported so existing imports keep working.
"""

from __future__ import annotations

from .errors import CriticContractError
from .config import (
    ACTION_DEFAULTS,
    ALLOWED_STATUSES,
    CRITIC_VERSION,
    LAYER_ISSUES,
    LAYER_KEYS,
    LAYER_METRICS,
    REPORT_SCHEMA_VERSION,
    SEVERITY_RANK,
    CriticConfig,
)
from .report import review, run
from .cli import build_parser, main

__all__ = [
    "CriticContractError",
    "CriticConfig",
    "CRITIC_VERSION",
    "REPORT_SCHEMA_VERSION",
    "ALLOWED_STATUSES",
    "LAYER_KEYS",
    "SEVERITY_RANK",
    "LAYER_ISSUES",
    "LAYER_METRICS",
    "ACTION_DEFAULTS",
    "review",
    "run",
    "build_parser",
    "main",
]
