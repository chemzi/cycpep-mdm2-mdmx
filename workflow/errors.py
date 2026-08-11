"""Stable Launcher error normalization and browser-safe message handling."""

from __future__ import annotations

import re
from typing import Any

from .models import StructuredError


_MAX_BROWSER_MESSAGE = 320
_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|password|secret|authorization)\s*[:=]\s*([^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/][^\s,;]*|file://[^\s,;]*|\\\\[^\s,;]+|(?<!\w)/(?:[^/\s]+/)+[^\s,;]*)"
)


class DiagnosticContractError(RuntimeError):
    component = "launcher"

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def sanitize_message(message: Any, *, fallback: str = "Launcher operation failed.") -> str:
    text = str(message or "").replace("\r", "\n")
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("Traceback (") or stripped.startswith("File \""):
            break
        lines.append(stripped)
        if len(" ".join(lines)) >= _MAX_BROWSER_MESSAGE:
            break
    bounded = " ".join(lines) or fallback
    bounded = _SECRET_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", bounded)
    bounded = _BEARER_RE.sub("Bearer [REDACTED]", bounded)
    bounded = _PATH_RE.sub("[internal path]", bounded)
    bounded = " ".join(bounded.split())
    if len(bounded) > _MAX_BROWSER_MESSAGE:
        bounded = bounded[: _MAX_BROWSER_MESSAGE - 3].rstrip() + "..."
    return bounded or fallback


def normalize_error(error: BaseException, *, component: str) -> StructuredError:
    code = getattr(error, "code", None)
    normalized_code = code if isinstance(code, str) and code else type(error).__name__
    owned_component = getattr(error, "component", None)
    normalized_component = (
        owned_component if isinstance(owned_component, str) and owned_component else component
    )
    return StructuredError(
        code=normalized_code,
        component=normalized_component,
        message=sanitize_message(error),
    )


__all__ = ["DiagnosticContractError", "normalize_error", "sanitize_message"]
