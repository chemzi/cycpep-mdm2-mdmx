"""Recovery for execution commits interrupted before Orchestrator closure.

A recovery pass must never roll back a transaction whose owner Worker is
still alive.  Two guards enforce that:

* lease/heartbeat -- a commit marker records its owner worker and a
  monotonic-updated heartbeat.  A marker whose heartbeat is younger than
  ``stall_seconds`` is treated as possibly-live and is skipped, never
  compensated.
* three-state Orchestrator verdict -- closure is CLOSED / OPEN (and the
  owner is confirmed dead) / UNKNOWN.  UNKNOWN never triggers compensation;
  the transaction is surfaced as unresolved instead.

The result of a pass is a structured :class:`RecoveryResult` so callers can
fail closed when anything could not be resolved safely.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
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

# Verdict returned by the ``orchestrator_state`` probe: CLOSED / OPEN / UNKNOWN.
OrchestratorProbe = Callable[[Mapping[str, object]], str]


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
        now: float | None = None,
    ) -> RecoveryResult:
        probe = orchestrator_state or probe_orchestrator_state
        now = time.monotonic() if now is None else now
        recovered: list[str] = []
        skipped_active: list[str] = []
        self.unresolved_transactions = []
        self.marker_errors = []
        for marker in Path(staging_root).glob("*/metadata/commit.json"):
            payload = self._read_marker(marker)
            if payload is None:
                continue
            try:
                transaction_id = self._recover_marker(marker, payload, probe, now)
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

    def _recover_marker(
        self,
        marker: Path,
        payload: dict,
        probe: OrchestratorProbe,
        now: float,
    ) -> str | None:
        status = payload.get("status")
        if status in {"PREPARED", "COMMITTED"}:
            return self._recover_commit_window(marker, payload, probe, now)
        if status in {
            "COMPENSATING", "COMPENSATION_FAILED", "COMPENSATION_UNRESOLVED"
        }:
            return self._finish_compensation(marker, payload)
        return None

    def _recover_commit_window(
        self,
        marker: Path,
        payload: dict,
        probe: OrchestratorProbe,
        now: float,
    ) -> str:
        transaction_id = str(payload["transaction_id"])
        database_status = self.store.get_transaction_status(transaction_id)
        if database_status != "COMMITTED":
            self._remove_unregistered_artifacts(payload)
            payload["status"] = "PREPARED_NO_DB_COMMIT"
            durable_atomic_json(marker, payload)
            return transaction_id
        # DB committed but Orchestrator closure unknown: only touch the
        # transaction when its owner is confirmed stalled.
        if self._is_live(payload, now):
            raise _SkippedActive(transaction_id)
        payload["recovery_state"] = "DB_COMMITTED_AWAITING_ORCHESTRATOR"
        durable_atomic_json(marker, payload)
        context = payload.get("context") or {}
        verdict = probe(context)
        if verdict == CLOSED:
            payload["status"] = "ORCHESTRATOR_CLOSED"
            durable_atomic_json(marker, payload)
            return transaction_id
        if verdict == UNKNOWN:
            payload["status"] = "RECOVERY_UNRESOLVED"
            payload["recovery_error"] = "orchestrator state unknown; refusing to compensate"
            durable_atomic_json(marker, payload)
            self.unresolved_transactions.append(transaction_id)
            return None
        return self._finish_compensation(marker, payload)

    def _is_live(self, payload: Mapping[str, object], now: float) -> bool:
        heartbeat = payload.get("heartbeat_monotonic")
        if heartbeat is None:
            # No heartbeat recorded: fall back to marker freshness only when the
            # payload carries a creation timestamp we can reason about.  Absent
            # any liveness signal we conservatively treat it as NOT live so a
            # genuinely crashed legacy transaction can still be recovered.
            return False
        try:
            return (now - float(heartbeat)) < self.stall_seconds
        except (TypeError, ValueError):
            return False

    def _finish_compensation(self, marker: Path, payload: dict) -> str | None:
        transaction_id = str(payload["transaction_id"])
        try:
            conflicts = self.store.rollback_transaction(transaction_id)
            if conflicts:
                raise RuntimeError(f"state compensation conflicts: {conflicts}")
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
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self._record_marker_error(marker, exc)
            return None
        if not isinstance(payload, dict):
            self._record_marker_error(marker, ValueError("marker must be a JSON object"))
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
