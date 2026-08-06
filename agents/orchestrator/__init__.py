"""Orchestrator Agent package (PR6 split from agents/orchestrator.py).

Public names are re-exported so existing imports keep working.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from .errors import OrchestratorContractError  # noqa: E402
from .config import (  # noqa: E402
    LEGACY_RUN_SCHEMA_VERSION,
    ORCHESTRATOR_VERSION,
    RUN_ID_RE,
    RUN_SCHEMA_VERSION,
)
from .service import initialize, status  # noqa: E402
from .state_machine import authorize, claim, fail, recover, retry, skip  # noqa: E402
from .completion import complete  # noqa: E402
from .cli import build_parser, main  # noqa: E402

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
