­r‡^Ñf¥–Ø¦{OlyÊ'vÃ®¶›­"""Execution transaction contract.

The contract is deliberately independent from a storage backend.  A transaction
belongs to exactly one task and one attempt; retries create a new attempt and
therefore a new transaction.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping
import uuid


class TransactionStatus(str, Enum):
    CREATED = "CREATED"
    STAGING = "STAGING"
    VALIDATING = "VALIDATING"
    COMMITTING = "COMMITTING"
    COMMITTED = "COMMITTED"
    ROLLED_BACK = "ROLLED_BACK"
    FAILED = "FAILED"


VALID_TRANSITIONS: dict[TransactionStatus, frozenset[TransactionStatus]] = {
    TransactionStatus.CREATED: frozenset({TransactionStatus.STAGING}),
    TransactionStatus.STAGING: frozenset({TransactionStatus.VALIDATING, TransactionStatus.FAILED}),
    TransactionStatus.VALIDATING: frozenset({TransactionStatus.COMMITTING, TransactionStatus.FAILED}),
    TransactionStatus.COMMITTING: frozenset({TransactionStatus.COMMITTED, TransactionStatus.ROLLED_BACK}),
    TransactionStatus.ROLLED_BACK: frozenset({TransactionStatus.FAILED}),
    TransactionStatus.COMMITTED: frozenset(),
    TransactionStatus.FAILED: frozenset(),
}


@dataclass(frozen=True)
class ErrorInfo:
    error_code: str
    component: str
    task_id: str
    transaction_id: str
    retryable: bool
    message: str
    workflow_id: str = ""
    attempt_id: str = ""
    action_name: str = ""
    agent_name: str = ""
    stack_trace: str = ""
    input_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TransactionContext:
    transaction_id: str
    workflow_id: str
    run_id: str
    task_id: str
    attempt_id: str
    created_at: str
    status: TransactionStatus = TransactionStatus.CREATED
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        workflow_id: str,
        run_id: str,
        task_id: str,
        attempt_id: str | None = None,
        transaction_id: str | None = None,
    ) -> "TransactionContext":
        now = datetime.now(timezone.utc).isoformat()
        return cls(
            transaction_id=transaction_id or f"tx-{uuid.uuid4().hex}",
            workflow_id=workflow_id,
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id or f"attempt-{uuid.uuid4().hex}",
            created_at=now,
        )

    def transition(self, status: TransactionStatus) -> None:
        status = TransactionStatus(status)
        if status == self.status:
            return
        allowed = VALID_TRANSITIONS[self.status]
        if status not in allowed:
            raise ValueError(f"invalid transaction transition: {self.status.value} -> {status.value}")
        object.__setattr__(self, "status", status)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TransactionContext":
        payload = dict(value)
        payload["status"] = TransactionStatus(payload.get("status", TransactionStatus.CREATED))
        return cls(**payload)
