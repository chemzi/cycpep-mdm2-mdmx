"""Planner Agent package (PR6 split from agents/planner.py).

Public names are re-exported so existing imports keep working.
"""

from __future__ import annotations

from .errors import PlannerContractError
from .config import (
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
from .service import adjust, build_plan, plan, run
from .approval import record_approval
from contracts.plan import validate_plan_for_approval, validate_sha256
from .cli import build_parser, main

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
    "validate_plan_for_approval",
    "validate_sha256",
    "build_parser",
    "main",
]
