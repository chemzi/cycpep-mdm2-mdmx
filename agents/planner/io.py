"""io - split from agents/planner.py (PR6)."""

from __future__ import annotations

import json, os, uuid
from pathlib import Path
from .errors import PlannerContractError

def _read_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PlannerContractError(f"{label}_missing", f"missing {label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PlannerContractError(
            f"{label}_malformed", f"invalid JSON in {path}"
        ) from exc
    if not isinstance(value, dict):
        raise PlannerContractError(f"{label}_type", f"{label} must be an object")
    return value

def _atomic_json(path: Path, value: dict) -> None:
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
