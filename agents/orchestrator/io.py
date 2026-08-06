"""io - split from agents/orchestrator.py (PR6)."""

from __future__ import annotations

try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None
    import msvcrt  # type: ignore

import contextlib, math, os
from contracts.io import atomic_write_json, read_json_object
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .errors import OrchestratorContractError


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, label: str) -> dict:
    return read_json_object(path, label, error_cls=OrchestratorContractError)


def _atomic_json(path: Path, value: dict) -> None:
    return atomic_write_json(path, value)


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
