"""io - split from agents/critic.py (PR6)."""

from __future__ import annotations

from contracts.io import atomic_write_json, read_json_object
from pathlib import Path
from typing import Any
from .errors import CriticContractError


def _json_object(path: Path, label: str) -> dict:
    return read_json_object(path, label, error_cls=CriticContractError)


def _resolve_path(raw: Any, base: Path, label: str) -> Path:
    value = str(raw or "").strip()
    if not value:
        raise CriticContractError(f"{label}_missing", f"missing {label} path")
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _atomic_json(path: Path, value: dict) -> None:
    return atomic_write_json(path, value)
