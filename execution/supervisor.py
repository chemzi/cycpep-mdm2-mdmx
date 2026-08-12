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
    _atomic_json(path, value, durable=False)


def durable_atomic_json(path: Path, value: dict) -> None:
    """Atomically persist recovery metadata before dependent side effects."""
    _atomic_json(path, value, durable=True)


def _atomic_json(path: Path, value: dict, *, durable: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.flush()
            if durable:
                os.fsync(stream.fileno())
        os.replace(temporary, path)
        if durable:
            _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _signal_group(process: subprocess.Popen, sig: int) -> None:
    """Send a signal to the whole process group (POSIX only)."""
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def _terminate_group(
    process: subprocess.Popen,
    grace_seconds: float = 10.0,
    diagnostics_path: Path | None = None,
) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        # os.killpg is POSIX-only; on Windows take down the whole process
        # tree with taskkill so grandchildren cannot outlive the timeout
        # or interrupt (P2-1). taskkill is a separate binary here, not the
        # launched job, so shell=False discipline is preserved.
        kill_error = None
        try:
            completed = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                text=True,
                timeout=max(1.0, grace_seconds),
            )
            if completed.returncode != 0:
                kill_error = (completed.stderr or completed.stdout).strip() or (
                    f"taskkill exit {completed.returncode}"
                )
        except (OSError, subprocess.SubprocessError) as exc:
            kill_error = f"{type(exc).__name__}: {exc}"
        if kill_error:
            try:
                process.kill()
            except OSError:
                pass
            if diagnostics_path is not None:
                try:
                    diagnostics_path.write_text(
                        f"taskkill failed: {kill_error}\n", encoding="utf-8"
                    )
                except OSError:
                    pass
        try:
            process.wait(timeout=max(1.0, grace_seconds))
        except subprocess.TimeoutExpired:
            pass
        return
    _signal_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    _signal_group(process, signal.SIGKILL)
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
    # Keep venv/bin/python as the invoked entrypoint even when it is a symlink.
    executable = Path(os.path.abspath(Path(argv[0]).expanduser()))
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
            _terminate_group(
                process, diagnostics_path=logs_dir / "terminate_diagnostics.txt"
            )
            returncode = process.returncode
        except BaseException:
            interrupted = True
            _terminate_group(
                process, diagnostics_path=logs_dir / "terminate_diagnostics.txt"
            )
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
