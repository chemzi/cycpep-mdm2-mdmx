"""io - split from agents/orchestrator.py (PR6)."""

from __future__ import annotations

try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None
    import msvcrt  # type: ignore

import contextlib, json, math, os, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .errors import OrchestratorContractError

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()

def _read_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OrchestratorContractError(f"{label}_missing", f"missing {label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise OrchestratorContractError(
            f"{label}_malformed", f"invalid JSON in {path}"
        ) from exc
    if not isinstance(value, dict):
        raise OrchestratorContractError(f"{label}_type", f"{label} must be an object")
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

@contextlib.contextmanager
def _exclusive_file_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as stream:
        if fcntl is not None:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        else:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write("0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            else:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)

@contextlib.contextmanager
def _run_lock(run_path: Path):
    lock_path = run_path.with_name(f".{run_path.name}.lock")
    with _exclusive_file_lock(lock_path):
        yield

def _finite_nonnegative(value: Any, code: str, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise OrchestratorContractError(code, f"{label} must be a number") from exc
    if not math.isfinite(number) or number < 0:
        raise OrchestratorContractError(code, f"{label} must be finite and non-negative")
    return number
