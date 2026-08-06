"""Execution transaction lifecycle and trace context."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping


class TransactionStatus(str, Enum):
    CREATED = "CREATED"
    STAGING = "STAGING"
    VALIDATING = "VALIDATING"
    COMMITTING = "COMMITTING"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


_TRANSITIONS = {
    TransactionStatus.CREATED: {TransactionStatus.STAGING},
    TransactionStatus.STAGING: {TransactionStatus.VALIDATING, TransactionStatus.FAILED},
    TransactionStatus.VALIDATING: {TransactionStatus.COMMITTING, TransactionStatus.FAILED},
    TransactionStatus.COMMITTING: {TransactionStatus.COMMITTED, TransactionStatus.ROLLED_BACK},
    TransactionStatus.ROLLED_BACK: {TransactionStatus.FAILED},
    TransactionStatus.COMMITTED: {TransactionStatus.ROLLED_BACK},
    TransactionStatus.FAILED: set(),
}


@dataclass(frozen=True)
class TransactionContext:
    transaction_id: str
    workflow_id: str
    run_id: str
    task_id: str
    attempt_id: str
    action: str
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
        attempt_id: str,
        action: str,
        transaction_id: str | None = None,
    ) -> "TransactionContext":
        if not action:
            raise ValueError("transaction action is required")
        return cls(
            transaction_id=transaction_id or f"tx-{uuid.uuid4().hex}",
            workflow_id=workflow_id,
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            action=action,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def transition(self, next_status: TransactionStatus) -> None:
        next_status = TransactionStatus(next_status)
        if next_status == self.status:
            return
        if next_status not in _TRANSITIONS[self.status]:
            raise ValueError(
                f"invalid transaction transition: {self.status.value} -> {next_status.value}"
            )
        object.__setattr__(self, "status", next_status)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TransactionContext":
        payload = dict(value)
        payload["status"] = TransactionStatus(payload.get("status", "CREATED"))
        return cls(**payload)
