"""Planner Agent package (PR6 split from agents/planner.py).

Public names are re-exported so existing imports keep working.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from .errors import PlannerContractError  # noqa: E402
from .config import (  # noqa: E402
    APPROVAL_SCHEMA_VERSION,
    DESIGN_ITERATION_ACTIONS,
    LEGACY_PLAN_SCHEMA_VERSION,
    MANDATORY_POLICY_CONSTRAINTS,
    PLAN_ID_RE,
    PLAN_SCHEMA_VERSION,
    PLANNER_VERSION,
    PRIORITY_RANK,
    RECOMMENDATION_MAPPINGS,
    REPORT_ID_RE,
    SEVERITY_RANK,
    PlannerConfig,
)
from .service import adjust, build_plan, plan, run  # noqa: E402
from .approval import record_approval  # noqa: E402
from .validation import _validate_plan_for_approval, _validate_sha256  # noqa: E402
from .cli import build_parser, main  # noqa: E402

__all__ = [
    "PlannerContractError",
    "PlannerConfig",
    "PLANNER_VERSION",
    "PLAN_SCHEMA_VERSION",
    "LEGACY_PLAN_SCHEMA_VERSION",
    "APPROVAL_SCHEMA_VERSION",
    "REPORT_ID_RE",
    "PLAN_ID_RE",
    "MANDATORY_POLICY_CONSTRAINTS",
    "SEVERITY_RANK",
    "PRIORITY_RANK",
    "RECOMMENDATION_MAPPINGS",
    "DESIGN_ITERATION_ACTIONS",
    "build_plan",
    "run",
    "plan",
    "adjust",
    "record_approval",
    "_validate_plan_for_approval",
    "_validate_sha256",
    "build_parser",
    "main",
]
