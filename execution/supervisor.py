"""Small subprocess supervisor used by closed Execution handlers."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from .contracts import ExecutionContractError


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: dict) -> None:
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


def _terminate_group(process: subprocess.Popen, grace_seconds: float = 10.0) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait(timeout=max(1.0, grace_seconds))


def run_process(
    argv: Sequence[str | Path],
    *,
    cwd: Path,
    logs_dir: Path,
    timeout_seconds: int,
    environment: Mapping[str, str] | None = None,
    label: str,
) -> dict:
    """Run one fixed argument vector with logs and deterministic termination.

    ``shell`` is deliberately hard-coded to ``False``.  Handlers construct the
    argument vector; Planner parameters never become executable/script paths.
    """
    if not argv:
        raise ExecutionContractError("process_argv_invalid", "empty process argv")
    executable = Path(argv[0]).expanduser().resolve()
    if not executable.is_file():
        raise ExecutionContractError(
            "execution_tool_unavailable", f"{label} executable not found: {executable}"
        )
    if timeout_seconds < 1:
        raise ExecutionContractError("process_timeout_invalid", "timeout must be positive")
    cwd = cwd.expanduser().resolve()
    if not cwd.is_dir():
        raise ExecutionContractError("process_cwd_invalid", f"working directory missing: {cwd}")
    logs_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = logs_dir / "stdout.log"
    stderr_path = logs_dir / "stderr.log"
    trace_path = logs_dir / "process.json"
    normalized_argv = [str(executable), *[str(value) for value in argv[1:]]]
    started_at = _utcnow()
    started_monotonic = time.monotonic()
    env = dict(os.environ)
    if environment:
        env.update({str(key): str(value) for key, value in environment.items()})

    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr:
        process = subprocess.Popen(
            normalized_argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            text=True,
            shell=False,
            start_new_session=True,
        )
        atomic_json(trace_path, {
            "label": label,
            "argv": normalized_argv,
            "cwd": str(cwd),
            "pid": process.pid,
            "process_group": process.pid,
            "started_at": started_at,
            "status": "running",
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
        })
        timed_out = False
        interrupted = False
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_group(process)
            returncode = process.returncode
        except BaseException:
            interrupted = True
            _terminate_group(process)
            raise
        finally:
            elapsed = max(0.0, time.monotonic() - started_monotonic)
            atomic_json(trace_path, {
                "label": label,
                "argv": normalized_argv,
                "cwd": str(cwd),
                "pid": process.pid,
                "process_group": process.pid,
                "started_at": started_at,
                "completed_at": _utcnow(),
                "elapsed_seconds": elapsed,
                "returncode": process.returncode,
                "timed_out": timed_out,
                "interrupted": interrupted,
                "status": (
                    "interrupted" if interrupted else "timed_out" if timed_out
                    else "succeeded" if process.returncode == 0 else "failed"
                ),
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
            })

    result = {
        "label": label,
        "argv": normalized_argv,
        "cwd": str(cwd),
        "pid": process.pid,
        "started_at": started_at,
        "completed_at": _utcnow(),
        "elapsed_seconds": elapsed,
        "returncode": returncode,
        "timed_out": timed_out,
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "trace": str(trace_path),
    }
    if timed_out:
        raise ExecutionContractError(
            "execution_process_timeout", f"{label} exceeded {timeout_seconds}s"
        )
    if returncode != 0:
        tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-1500:]
        raise ExecutionContractError(
            "execution_process_failed", f"{label} exited {returncode}: {tail}"
        )
    return result
