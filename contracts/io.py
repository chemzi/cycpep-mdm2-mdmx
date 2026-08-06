"""Shared JSON read/write helpers with atomic replacement.

Planner, Orchestrator, and Critic use the same envelope; each agent keeps a
thin package-local wrapper so its own error type is raised.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path


class IOContractError(ValueError):
    """A JSON artifact is missing, malformed, or not an object."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def read_json_object(
    path: Path, label: str, *, error_cls: type = IOContractError
) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise error_cls(f"{label}_missing", f"missing {label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise error_cls(f"{label}_malformed", f"invalid JSON in {path}") from exc
    if not isinstance(value, dict):
        raise error_cls(f"{label}_type", f"{label} must be an object")
    return value


def atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
