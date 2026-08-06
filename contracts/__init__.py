"""Small, dependency-free contracts shared across workflow boundaries.

The package intentionally contains serialization and validation only.  Domain
policy stays in the Planner and execution capability stays in the execution
registry.
"""

from .action import (
    ACTION_CATALOG,
    ALL_ACTION_TYPES,
    EXECUTABLE_ACTION_TYPES,
    KNOWN_UNIMPLEMENTED_ACTION_TYPES,
    V2_RESERVED_ACTION_TYPES,
    ActionSpec,
    ActionType,
    RecommendationMapping,
    coerce_action_type,
    get_action_spec,
)
from .artifact import ArtifactRef
from .approval import Approval
from .errors import ErrorInfo
from .event import EvidenceEvent, VALID_AGENTS, VALID_EVENT_TYPES
from .task import (
    ExecutionGateStatus,
    ExecutionTask,
    MUTABLE_TASK_STATUSES,
    SUCCESS_TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
    TaskStatus,
)
from .trace import TraceContext, derive_workflow_id
from .transaction import TransactionContext, TransactionStatus
from .candidate_update import (
    CANDIDATE_UPDATE_SCHEMA_VERSION,
    CandidateUpdate,
    CandidateUpdateBatch,
)

__all__ = [
    "ACTION_CATALOG",
    "ALL_ACTION_TYPES",
    "EXECUTABLE_ACTION_TYPES",
    "KNOWN_UNIMPLEMENTED_ACTION_TYPES",
    "V2_RESERVED_ACTION_TYPES",
    "ActionSpec",
    "ActionType",
    "RecommendationMapping",
    "coerce_action_type",
    "get_action_spec",
    "ArtifactRef",
    "Approval",
    "ErrorInfo",
    "EvidenceEvent",
    "VALID_AGENTS",
    "VALID_EVENT_TYPES",
    "ExecutionGateStatus",
    "ExecutionTask",
    "MUTABLE_TASK_STATUSES",
    "SUCCESS_TASK_STATUSES",
    "TERMINAL_TASK_STATUSES",
    "TaskStatus",
    "TraceContext",
    "derive_workflow_id",
    "TransactionContext",
    "TransactionStatus",
    "CANDIDATE_UPDATE_SCHEMA_VERSION",
    "CandidateUpdate",
    "CandidateUpdateBatch",
]
