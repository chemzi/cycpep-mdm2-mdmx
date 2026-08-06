"""io - split from agents/planner.py (PR6)."""

from __future__ import annotations

from contracts.io import atomic_write_json, read_json_object
from pathlib import Path

from .errors import PlannerContractError


def _read_json(path: Path, label: str) -> dict:
    return read_json_object(path, label, error_cls=PlannerContractError)


def _atomic_json(path: Path, value: dict) -> None:
    return atomic_write_json(path, value)
