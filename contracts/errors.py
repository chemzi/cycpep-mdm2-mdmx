"""Small common error envelope used in failure evidence."""

from __future__ import annotations

from dataclasses import dataclass
import errno
from enum import Enum
import traceback as traceback_module
from typing import Any, Mapping


class ErrorType(str, Enum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    CONTRACT_ERROR = "CONTRACT_ERROR"
    SYSTEM_ERROR = "SYSTEM_ERROR"
    IO_ERROR = "IO_ERROR"


def classify_retryable(exc: BaseException) -> bool:
    """Return the explicit retry policy for one execution failure.

    Contract violations are deterministic and therefore fail closed.  Runtime
    transport/process failures may opt in through a ``retryable`` attribute;
    common transient I/O failures are retryable by default.
    """
    declared = getattr(exc, "retryable", None)
    if isinstance(declared, bool):
        return declared
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return isinstance(exc, OSError) and exc.errno in {
        errno.EAGAIN,
        errno.EBUSY,
        errno.ETIMEDOUT,
        errno.ECONNRESET,
        errno.ECONNREFUSED,
        errno.ENETDOWN,
        errno.ENETUNREACH,
    }


@dataclass(frozen=True)
class ErrorInfo:
    """Unified failure envelope shared by Evidence, Worker and Store layers."""

    code: str
    message: str
    component: str
    retryable: bool | None = None
    error_type: ErrorType | str = ErrorType.SYSTEM_ERROR
    traceback: str = ""
    task_id: str = ""
    transaction_id: str = ""
    workflow_id: str = ""
    run_id: str = ""
    attempt_id: str = ""
    action_name: str = ""
    agent_name: str = ""
    input_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("code", "message", "component"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be non-empty")
        if self.retryable is not None and not isinstance(self.retryable, bool):
            raise ValueError("retryable must be boolean or None")
        object.__setattr__(self, "error_type", ErrorType(self.error_type))

    @property
    def error_code(self) -> str:
        """Compatibility spelling used by the first PR34 test draft."""

        return self.code

    @property
    def stack_trace(self) -> str:
        """Compatibility spelling kept for legacy failure JSON consumers."""

        return self.traceback

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "component": self.component,
            "retryable": self.retryable,
            "error_type": self.error_type.value,
            "traceback": self.traceback,
            "stack_trace": self.traceback,
            "task_id": self.task_id,
            "transaction_id": self.transaction_id,
            "workflow_id": self.workflow_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "action_name": self.action_name,
            "agent_name": self.agent_name,
            "input_hash": self.input_hash,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ErrorInfo":
        payload = dict(value)
        return cls(
            code=payload.get("code") or payload.get("error_code") or "execution_error",
            message=payload.get("message"),
            component=payload.get("component"),
            retryable=payload.get("retryable"),
            error_type=payload.get("error_type", ErrorType.SYSTEM_ERROR),
            traceback=payload.get("traceback") or payload.get("stack_trace") or "",
            task_id=payload.get("task_id", ""),
            transaction_id=payload.get("transaction_id", ""),
            workflow_id=payload.get("workflow_id", ""),
            run_id=payload.get("run_id", ""),
            attempt_id=payload.get("attempt_id", ""),
            action_name=payload.get("action_name", ""),
            agent_name=payload.get("agent_name", ""),
            input_hash=payload.get("input_hash", ""),
        )

    @classmethod
    def from_exception(
        cls,
        exc: BaseException,
        *,
        component: str,
        code: str | None = None,
        error_code: str | None = None,
        retryable: bool | None = None,
        error_type: ErrorType | str = ErrorType.SYSTEM_ERROR,
        **identity: Any,
    ) -> "ErrorInfo":
        return cls(
            code=code or error_code or "execution_error",
            message=str(exc) or exc.__class__.__name__,
            component=component,
            retryable=(classify_retryable(exc) if retryable is None else retryable),
            error_type=error_type,
            traceback=traceback_module.format_exc(limit=30),
            **identity,
        )
