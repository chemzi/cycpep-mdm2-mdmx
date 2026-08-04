"""Small common error envelope used in failure evidence."""

from __future__ import annotations

from dataclasses import dataclass
import errno
from typing import Any, Mapping


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
    code: str
    message: str
    component: str
    retryable: bool | None = None

    def __post_init__(self) -> None:
        for name in ("code", "message", "component"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be non-empty")
        if self.retryable is not None and not isinstance(self.retryable, bool):
            raise ValueError("retryable must be boolean or None")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "component": self.component,
            "retryable": self.retryable,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ErrorInfo":
        return cls(
            code=value.get("code"),
            message=value.get("message"),
            component=value.get("component"),
            retryable=value.get("retryable"),
        )

    @classmethod
    def from_exception(
        cls,
        exc: BaseException,
        *,
        component: str,
        code: str = "execution_error",
        retryable: bool | None = None,
    ) -> "ErrorInfo":
        return cls(
            code=code,
            message=str(exc),
            component=component,
            retryable=(classify_retryable(exc) if retryable is None else retryable),
        )
