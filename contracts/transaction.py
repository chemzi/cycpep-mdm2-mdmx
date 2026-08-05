"""Execution transaction and failure contracts.

This module owns the transaction state machine.  Callers must use
``TransactionContext.transition``; the status field is immutable from the
outside so a worker cannot accidentally skip a lifecycle phase.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping
import uuid

from .errors import ErrorType


class TransactionStatus(str, Enum):
    CREATED = "CREATED"
    STAGING = "STAGING"
    VALIDATING = "VALIDATING"
    COMMITTING = "COMMITTING"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


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
class TransactionContext:
    transaction_id: str
    workflow_id: str
    run_id: str
    task_id: str
    attempt_id: str
    created_at: str
    status: TransactionStatus = TransactionStatus.CREATED
    action: str = ""
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
        action: str = "",
    ) -> "TransactionContext":
        return cls(
            transaction_id=transaction_id or f"tx-{uuid.uuid4().hex}",
            workflow_id=workflow_id,
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id or f"attempt-{uuid.uuid4().hex}",
            created_at=datetime.now(timezone.utc).isoformat(),
            action=action,
        )

    def transition(self, next_status: TransactionStatus) -> None:
        """Perform the only permitted status mutation."""

        next_status = TransactionStatus(next_status)
        if next_status == self.status:
            return
        if next_status not in VALID_TRANSITIONS[self.status]:
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
        payload["status"] = TransactionStatus(payload.get("status", TransactionStatus.CREATED))
        return cls(**payload)
