"""Closed-world execution worker for NovaPeptide Planner tasks."""

from .contracts import (
    CORE_ACTIONS,
    DISPATCH_SCHEMA_VERSION,
    EXECUTION_SCHEMA_VERSION,
    V2_RESERVED_ACTIONS,
    ExecutionContractError,
)
from .commit_manager import CommitManager
from .results import ExecutionActionResult
from .staging import StagedArtifact, StagingArea

__all__ = [
    "CORE_ACTIONS",
    "DISPATCH_SCHEMA_VERSION",
    "EXECUTION_SCHEMA_VERSION",
    "V2_RESERVED_ACTIONS",
    "ExecutionContractError",
    "CommitManager",
    "ExecutionActionResult",
    "ExecutionFailure",
    "ExecutionResult",
    "ExecutionWorker",
    "StagedArtifact",
    "StagingArea",
]


def __getattr__(name: str):
    if name in {"ExecutionFailure", "ExecutionResult", "ExecutionWorker"}:
        from .worker import ExecutionFailure, ExecutionResult, ExecutionWorker

        return {
            "ExecutionFailure": ExecutionFailure,
            "ExecutionResult": ExecutionResult,
            "ExecutionWorker": ExecutionWorker,
        }[name]
    raise AttributeError(name)
