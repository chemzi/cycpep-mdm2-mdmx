"""Recovery for execution commits interrupted before Orchestrator closure.

Persisted monotonic clocks are deliberately not used as leases: their epoch
changes across boots.  New markers identify their host, boot/session, PID and
process creation identity and also carry a UTC heartbeat.  A matching live
local process is authoritative even after the heartbeat stall threshold.
Remote or identity-unverifiable owners become UNKNOWN when their heartbeat is
stale; UNKNOWN is never sufficient authority for destructive recovery.

The result of a pass is a structured :class:`RecoveryResult` so callers can
fail closed when anything could not be resolved safely.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from storage.base import Store

from .supervisor import durable_atomic_json

# A marker whose heartbeat is younger than this is considered possibly-live.
DEFAULT_STALL_SECONDS = 300.0

# Orchestrator closure verdicts.
CLOSED = "closed"
OPEN = "open"
UNKNOWN = "unknown"

_COMMIT_WINDOW_MARKER_STATUSES = frozenset({
    "PREPARED",
    "COMMITTED",
    "RECOVERY_UNRESOLVED",
})
_COMPENSATION_MARKER_STATUSES = frozenset({
    "COMPENSATING",
    "COMPENSATION_FAILED",
    "COMPENSATION_CONFLICT",
    "COMPENSATION_UNRESOLVED",
})
_PENDING_MARKER_STATUSES = (
    _COMMIT_WINDOW_MARKER_STATUSES | _COMPENSATION_MARKER_STATUSES
)
_UNRESOLVED_DATABASE_STATUSES = frozenset({
    "COMMITTING",
    "RECOVERY_UNRESOLVED",
    "COMPENSATING",
    "COMPENSATION_FAILED",
    "COMPENSATION_CONFLICT",
    "COMPENSATION_UNRESOLVED",
})

# Owner lease verdicts.  Unlike a boolean, UNKNOWN cannot accidentally be
# treated as proof that a process is dead.
OWNER_LIVE = "live"
OWNER_DEAD = "dead"
OWNER_UNKNOWN = "unknown"

# Verdict returned by the ``orchestrator_state`` probe: CLOSED / OPEN / UNKNOWN.
OrchestratorProbe = Callable[[Mapping[str, object]], str]


def utc_now() -> str:
    """Return an interoperable UTC timestamp for persisted lease metadata."""
    return datetime.now(timezone.utc).isoformat()


def owner_lease(*, worker_id: object, instance_id: str) -> dict[str, object]:
    """Capture the current process identity for a durable commit marker."""
    pid = os.getpid()
    return {
        "owner_worker_id": worker_id,
        "owner_pid": pid,
        "owner_host": socket.gethostname(),
        "owner_process_identity": _process_identity(pid),
        "owner_instance_id": instance_id,
        "owner_boot_id": _boot_identity(),
        "owner_session_id": _session_identity(pid),
        "heartbeat_at": utc_now(),
    }


def _process_identity(pid: int) -> str | None:
    """Return a PID-reuse-resistant process creation identity when available."""
    if os.name == "nt":
        return _windows_process_creation_identity(pid)
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        value = stat_path.read_text(encoding="utf-8")
    except OSError:
        return None
    closing_parenthesis = value.rfind(")")
    fields_after_name = value[closing_parenthesis + 2 :].split()
    if closing_parenthesis < 0 or len(fields_after_name) < 20:
        return None
    return fields_after_name[19]


def _windows_process_creation_identity(pid: int) -> str | None:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return None
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        try:
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                return None
            value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return str(value)
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):
        return None


def _boot_identity() -> str | None:
    boot_id = Path("/proc/sys/kernel/random/boot_id")
    try:
        return boot_id.read_text(encoding="utf-8").strip() or None
    except OSError:
        pass
    if os.name != "nt":
        return None
    try:
        import ctypes

        get_tick_count = ctypes.windll.kernel32.GetTickCount64
        get_tick_count.restype = ctypes.c_ulonglong
        uptime_seconds = get_tick_count() / 1000.0
    except (AttributeError, OSError):
        return None
    # The calculated boot epoch varies by a few milliseconds between calls;
    # a five-minute bucket is stable while still distinguishing real reboots.
    boot_epoch_bucket = int((time.time() - uptime_seconds) // 300)
    return f"windows-boot-{boot_epoch_bucket}"


def _session_identity(pid: int) -> str | None:
    if os.name != "nt":
        try:
            return str(os.getsid(pid))
        except (AttributeError, OSError):
            return None
    try:
        import ctypes
        from ctypes import wintypes

        session_id = wintypes.DWORD()
        if ctypes.windll.kernel32.ProcessIdToSessionId(pid, ctypes.byref(session_id)):
            return str(session_id.value)
    except (AttributeError, OSError):
        pass
    return None


def _process_exists(pid: int) -> bool | None:
    """Return True/False only for a conclusive OS liveness answer."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            error = ctypes.get_last_error()
            if error == 5:  # access denied still means alive
                return True
            if error == 87:  # invalid PID / process no longer exists
                return False
            return None
        except (AttributeError, OSError, ValueError):
            return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OverflowError:
        return False
    except OSError as exc:
        return False if exc.errno == errno.ESRCH else None
    return True


@dataclass(frozen=True)
class RecoveryResult:
    """Structured outcome of one recovery pass."""

    recovered: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    marker_errors: tuple[dict[str, str], ...] = ()
    skipped_active: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        """True only when nothing is unresolved and no marker is corrupt."""
        return not self.unresolved and not self.marker_errors


def probe_orchestrator_state(context: Mapping[str, object]) -> str:
    """Three-state Orchestrator closure probe.

    Returns CLOSED only when the matching attempt is recorded SUCCEEDED,
    OPEN when the run snapshot is readable and the attempt is not closed,
    and UNKNOWN when the snapshot cannot be trusted (read/parse error).
    """
    metadata = context.get("metadata") or {}
    run_path = metadata.get("orchestrator_run_path") if isinstance(metadata, dict) else None
    if not run_path:
        return UNKNOWN
    try:
        from agents.orchestrator import status
        from contracts.task import TaskStatus
        from contracts.trace import TraceContext

        snapshot = status(run_path=run_path)["run"]
        task_state = snapshot["tasks"][str(context["task_id"])]
        attempt = int(task_state.get("attempts") or 0)
    except Exception:
        return UNKNOWN
    closed = (
        task_state.get("status") == TaskStatus.SUCCEEDED.value
        and context.get("attempt_id")
        == TraceContext.attempt_id_for(str(context["task_id"]), attempt)
    )
    return CLOSED if closed else OPEN


class RecoveryManager:
    def __init__(self, store: Store, *, stall_seconds: float = DEFAULT_STALL_SECONDS):
        self.store = store
        self.stall_seconds = float(stall_seconds)
        self.unresolved_transactions: list[str] = []
        self.marker_errors: list[dict[str, str]] = []

    @staticmethod
    def remove_artifact_files(payload: Mapping[str, object]) -> None:
        for artifact in payload.get("artifacts", []):
            path = Path(str(artifact["path"]))
            if path.exists():
                path.unlink()
            temporary_value = artifact.get("temporary")
            if temporary_value:
                temporary = Path(str(temporary_value))
                if temporary.exists():
                    temporary.unlink()

    def recover_pending(
        self,
        staging_root: str | Path,
        *,
        orchestrator_state: OrchestratorProbe | None = None,
        now: datetime | float | None = None,
    ) -> RecoveryResult:
        probe = orchestrator_state or probe_orchestrator_state
        now_utc = self._normalize_now(now)
        recovered: list[str] = []
        skipped_active: list[str] = []
        self.unresolved_transactions = []
        self.marker_errors = []
        for marker in Path(staging_root).glob("*/metadata/commit.json"):
            payload = self._read_marker(marker)
            if payload is None:
                continue
            try:
                transaction_id = self._recover_marker(marker, payload, probe, now_utc)
            except _SkippedActive as exc:
                skipped_active.append(str(exc))
                continue
            except Exception as exc:
                self._record_marker_error(marker, exc)
                transaction_id = payload.get("transaction_id")
                if transaction_id:
                    self.unresolved_transactions.append(str(transaction_id))
                continue
            if transaction_id:
                recovered.append(transaction_id)
        return RecoveryResult(
            recovered=tuple(recovered),
            unresolved=tuple(self.unresolved_transactions),
            marker_errors=tuple(self.marker_errors),
            skipped_active=tuple(skipped_active),
        )

    def inspect_pending(
        self,
        staging_root: str | Path,
        *,
        orchestrator_state: OrchestratorProbe | None = None,
        run_id: str | None = None,
        now: datetime | float | None = None,
    ) -> RecoveryResult:
        """Inspect recovery authority without mutating markers or formal rows."""

        probe = orchestrator_state or probe_orchestrator_state
        now_utc = self._normalize_now(now)
        unresolved = [
            str(transaction["transaction_id"])
            for transaction in self.store.list_transactions(run_id=run_id)
            if transaction.get("transaction_id")
            and transaction.get("status") in _UNRESOLVED_DATABASE_STATUSES
        ]
        marker_errors: list[dict[str, str]] = []
        skipped_active: list[str] = []
        for marker in Path(staging_root).glob("*/metadata/commit.json"):
            payload, marker_error = _read_marker_payload(marker)
            if marker_error is not None:
                marker_errors.append(marker_error)
                continue
            assert payload is not None
            if payload.get("status") not in _PENDING_MARKER_STATUSES:
                continue
            context = payload.get("context") or {}
            if (
                run_id is not None
                and isinstance(context, Mapping)
                and context.get("run_id") not in {None, run_id}
            ):
                continue
            transaction_id = payload.get("transaction_id")
            if not transaction_id:
                marker_errors.append(
                    {"path": str(marker), "code": "missing_transaction_id"}
                )
                continue
            transaction_id = str(transaction_id)
            if self._owner_liveness(payload, now_utc) == OWNER_LIVE:
                skipped_active.append(transaction_id)
                continue
            if self._inspection_requires_recovery(payload, probe):
                unresolved.append(transaction_id)
        return RecoveryResult(
            unresolved=tuple(dict.fromkeys(unresolved)),
            marker_errors=tuple(marker_errors),
            skipped_active=tuple(dict.fromkeys(skipped_active)),
        )

    def _inspection_requires_recovery(
        self, payload: Mapping[str, object], probe: OrchestratorProbe
    ) -> bool:
        """Return whether a non-live marker still needs owner reconciliation."""

        if payload.get("status") in _COMPENSATION_MARKER_STATUSES:
            return True
        transaction_id = str(payload["transaction_id"])
        if self.store.get_transaction_status(transaction_id) != "COMMITTED":
            return True
        context = payload.get("context") or {}
        if not isinstance(context, Mapping):
            return True
        return probe(context) != CLOSED

    def _recover_marker(
        self,
        marker: Path,
        payload: dict,
        probe: OrchestratorProbe,
        now: datetime,
    ) -> str | None:
        status = payload.get("status")
        if status in _COMMIT_WINDOW_MARKER_STATUSES:
            return self._recover_commit_window(marker, payload, probe, now)
        if status in _COMPENSATION_MARKER_STATUSES:
            return self._finish_compensation(marker, payload)
        return None

    def _recover_commit_window(
        self,
        marker: Path,
        payload: dict,
        probe: OrchestratorProbe,
        now: datetime,
    ) -> str:
        transaction_id = str(payload["transaction_id"])
        # Owner liveness precedes every destructive branch, including a
        # PREPARED marker whose database row is not visible yet.
        owner_state = self._owner_liveness(payload, now)
        if owner_state == OWNER_LIVE:
            raise _SkippedActive(transaction_id)
        database_status = self.store.get_transaction_status(transaction_id)
        if database_status in {"COMPENSATION_CONFLICT", "COMPENSATION_UNRESOLVED"}:
            payload["status"] = database_status
            durable_atomic_json(marker, payload)
            self.unresolved_transactions.append(transaction_id)
            return ""
        if database_status not in {None, "COMMITTED", "FAILED", "ROLLED_BACK"}:
            return self._mark_unresolved(
                marker,
                payload,
                transaction_id,
                f"database transaction status is unresolved: {database_status}",
            )
        if database_status != "COMMITTED":
            if owner_state != OWNER_DEAD:
                return self._mark_unresolved(
                    marker,
                    payload,
                    transaction_id,
                    "owner liveness unknown; refusing artifact cleanup",
                )
            self._remove_unregistered_artifacts(payload)
            payload["status"] = (
                "ROLLED_BACK" if database_status == "ROLLED_BACK"
                else "PREPARED_NO_DB_COMMIT"
            )
            payload.pop("recovery_error", None)
            durable_atomic_json(marker, payload)
            return transaction_id
        payload["recovery_state"] = "DB_COMMITTED_AWAITING_ORCHESTRATOR"
        durable_atomic_json(marker, payload)
        context = payload.get("context") or {}
        verdict = probe(context)
        if verdict == CLOSED:
            payload["status"] = "ORCHESTRATOR_CLOSED"
            payload.pop("recovery_error", None)
            durable_atomic_json(marker, payload)
            return transaction_id
        if verdict == UNKNOWN:
            return self._mark_unresolved(
                marker,
                payload,
                transaction_id,
                "orchestrator state unknown; refusing to compensate",
            )
        if owner_state != OWNER_DEAD:
            return self._mark_unresolved(
                marker,
                payload,
                transaction_id,
                "owner liveness unknown; refusing compensation",
            )
        return self._finish_compensation(marker, payload)

    def _owner_liveness(
        self, payload: Mapping[str, object], now: datetime
    ) -> str:
        owner_host = payload.get("owner_host")
        owner_pid = self._parse_pid(payload.get("owner_pid"))
        heartbeat_fresh = self._heartbeat_is_fresh(payload.get("heartbeat_at"), now)
        local_owner = (
            isinstance(owner_host, str)
            and owner_host.casefold() == socket.gethostname().casefold()
        )
        if local_owner:
            marker_boot = payload.get("owner_boot_id")
            current_boot = _boot_identity()
            if marker_boot and current_boot and marker_boot != current_boot:
                return OWNER_DEAD
            if owner_pid is None:
                return OWNER_LIVE if heartbeat_fresh else OWNER_UNKNOWN
            process_exists = _process_exists(owner_pid)
            if process_exists is False:
                return OWNER_DEAD
            if process_exists is None:
                return OWNER_LIVE if heartbeat_fresh else OWNER_UNKNOWN
            marker_process = payload.get("owner_process_identity")
            current_process = _process_identity(owner_pid)
            if marker_process and current_process:
                return (
                    OWNER_LIVE
                    if str(marker_process) == current_process
                    else OWNER_DEAD
                )
            return OWNER_LIVE if heartbeat_fresh else OWNER_UNKNOWN
        return OWNER_LIVE if heartbeat_fresh else OWNER_UNKNOWN

    def _mark_unresolved(
        self,
        marker: Path,
        payload: dict,
        transaction_id: str,
        message: str,
    ) -> str:
        payload["status"] = "RECOVERY_UNRESOLVED"
        payload["recovery_error"] = message
        durable_atomic_json(marker, payload)
        self.unresolved_transactions.append(transaction_id)
        return ""

    @staticmethod
    def _parse_pid(value: object) -> int | None:
        try:
            pid = int(value)
        except (TypeError, ValueError):
            return None
        return pid if pid > 0 else None

    def _heartbeat_is_fresh(self, value: object, now: datetime) -> bool:
        if not isinstance(value, str):
            return False
        try:
            heartbeat = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        if heartbeat.tzinfo is None:
            return False
        age = (now - heartbeat.astimezone(timezone.utc)).total_seconds()
        return age < self.stall_seconds

    @staticmethod
    def _normalize_now(value: datetime | float | None) -> datetime:
        if value is None:
            return datetime.now(timezone.utc)
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc) if value.tzinfo else value.replace(
                tzinfo=timezone.utc
            )
        return datetime.fromtimestamp(float(value), timezone.utc)

    def _finish_compensation(self, marker: Path, payload: dict) -> str | None:
        transaction_id = str(payload["transaction_id"])
        try:
            conflicts = self.store.rollback_transaction(transaction_id)
            if conflicts:
                payload["status"] = "COMPENSATION_CONFLICT"
                payload["compensation_error"] = {
                    "code": "COMPENSATION_CONFLICT",
                    "message": f"state compensation conflicts: {conflicts}",
                }
                durable_atomic_json(marker, payload)
                self.unresolved_transactions.append(transaction_id)
                return None
            self.remove_artifact_files(payload)
        except Exception as exc:
            payload["status"] = "COMPENSATION_UNRESOLVED"
            payload["compensation_error"] = {
                "code": exc.__class__.__name__,
                "message": str(exc),
            }
            durable_atomic_json(marker, payload)
            self.unresolved_transactions.append(transaction_id)
            return None
        payload["status"] = "ROLLED_BACK"
        payload.pop("compensation_error", None)
        payload.pop("recovery_error", None)
        durable_atomic_json(marker, payload)
        return transaction_id

    def _remove_unregistered_artifacts(self, payload: Mapping[str, object]) -> None:
        for artifact in payload.get("artifacts", []):
            registered = self.store.get_artifact(str(artifact["artifact_id"]))
            if registered is None:
                path = Path(str(artifact["path"]))
                if path.exists():
                    path.unlink()
            temporary_value = artifact.get("temporary")
            if temporary_value:
                temporary = Path(str(temporary_value))
                if temporary.exists():
                    temporary.unlink()

    def _read_marker(self, marker: Path) -> dict | None:
        payload, marker_error = _read_marker_payload(marker)
        if marker_error is not None:
            self.marker_errors.append(marker_error)
            return None
        return payload

    def _record_marker_error(self, marker: Path, exc: Exception) -> None:
        self.marker_errors.append({
            "path": str(marker),
            "code": exc.__class__.__name__,
            "message": str(exc),
        })


class _SkippedActive(Exception):
    """Internal signal: a marker's owner still looks alive; do not touch it."""


def _read_marker_payload(
    marker: Path,
) -> tuple[dict[str, object] | None, dict[str, str] | None]:
    """Decode one marker for both mutating recovery and read-only inspection."""

    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("marker must be a JSON object")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, {
            "path": str(marker),
            "code": exc.__class__.__name__,
            "message": str(exc),
        }
    return payload, None
