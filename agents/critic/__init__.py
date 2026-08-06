"""Critic Agent package (PR6 split from agents/critic.py).

Public names are re-exported so existing imports keep working.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from .errors import CriticContractError  # noqa: E402
from .config import (  # noqa: E402
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
from .report import review, run  # noqa: E402
from .cli import build_parser, main  # noqa: E402

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
