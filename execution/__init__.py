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
    "ExecutionWorker",
    "ensure_transaction_recovery_clean",
    "inspect_transaction_recovery",
    "StagedArtifact",
    "StagingArea",
]


def __getattr__(name: str):
    if name in {
        "ExecutionFailure", "ExecutionWorker", "ensure_transaction_recovery_clean",
        "inspect_transaction_recovery"
    }:
        from .worker import (
            ExecutionFailure,
            ExecutionWorker,
            ensure_transaction_recovery_clean,
            inspect_transaction_recovery,
        )

        return {
            "ExecutionFailure": ExecutionFailure,
            "ExecutionWorker": ExecutionWorker,
            "ensure_transaction_recovery_clean": ensure_transaction_recovery_clean,
            "inspect_transaction_recovery": inspect_transaction_recovery,
        }[name]
    raise AttributeError(name)
