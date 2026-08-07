"""Recovery for execution commits interrupted before Orchestrator closure."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Mapping

from storage.base import Store

from .supervisor import durable_atomic_json


class RecoveryManager:
    def __init__(self, store: Store):
        self.store = store
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
        orchestrator_closed: Callable[[Mapping[str, object]], bool] | None = None,
    ) -> list[str]:
        recovered = []
        self.unresolved_transactions = []
        self.marker_errors = []
        for marker in Path(staging_root).glob("*/metadata/commit.json"):
            payload = self._read_marker(marker)
            if payload is None:
                continue
            try:
                transaction_id = self._recover_marker(
                    marker, payload, orchestrator_closed
                )
            except Exception as exc:
                self._record_marker_error(marker, exc)
                transaction_id = payload.get("transaction_id")
                if transaction_id:
                    self.unresolved_transactions.append(str(transaction_id))
                continue
            if transaction_id:
                recovered.append(transaction_id)
        return recovered

    def _recover_marker(
        self,
        marker: Path,
        payload: dict,
        orchestrator_closed: Callable[[Mapping[str, object]], bool] | None,
    ) -> str | None:
        status = payload.get("status")
        if status in {"PREPARED", "COMMITTED"}:
            return self._recover_commit_window(
                marker, payload, orchestrator_closed
            )
        if status in {
            "COMPENSATING", "COMPENSATION_FAILED", "COMPENSATION_UNRESOLVED"
        }:
            return self._finish_compensation(marker, payload)
        return None

    def _recover_commit_window(
        self,
        marker: Path,
        payload: dict,
        orchestrator_closed: Callable[[Mapping[str, object]], bool] | None,
    ) -> str:
        transaction_id = str(payload["transaction_id"])
        database_status = self.store.get_transaction_status(transaction_id)
        if database_status != "COMMITTED":
            self._remove_unregistered_artifacts(payload)
            payload["status"] = "PREPARED_NO_DB_COMMIT"
            durable_atomic_json(marker, payload)
            return transaction_id
        payload["recovery_state"] = "DB_COMMITTED_AWAITING_ORCHESTRATOR"
        durable_atomic_json(marker, payload)
        context = payload.get("context") or {}
        if orchestrator_closed and orchestrator_closed(context):
            payload["status"] = "ORCHESTRATOR_CLOSED"
            durable_atomic_json(marker, payload)
            return transaction_id
        return self._finish_compensation(marker, payload)

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
