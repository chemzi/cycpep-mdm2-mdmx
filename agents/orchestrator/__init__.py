"""Orchestrator Agent package (PR6 split from agents/orchestrator.py).

Public names are re-exported so existing imports keep working.
"""

from __future__ import annotations

from .errors import OrchestratorContractError
from .config import (
    LEGACY_RUN_SCHEMA_VERSION,
    ORCHESTRATOR_VERSION,
    RUN_ID_RE,
    RUN_SCHEMA_VERSION,
)
from .service import initialize, status
from .claim import claim
from .state_machine import authorize, fail, recover, retry, skip
from .completion import complete
from .cli import build_parser, main

__all__ = [
    "OrchestratorContractError",
    "ORCHESTRATOR_VERSION",
    "RUN_SCHEMA_VERSION",
    "LEGACY_RUN_SCHEMA_VERSION",
    "RUN_ID_RE",
    "initialize",
    "status",
    "authorize",
    "claim",
    "complete",
    "fail",
    "skip",
    "recover",
    "retry",
    "build_parser",
    "main",
]
