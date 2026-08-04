"""Small common error envelope used in failure evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


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
        return cls(code=code, message=str(exc), component=component, retryable=retryable)
