"""Recovery for filesystem moves prepared before database commit."""

from __future__ import annotations

import json
from pathlib import Path

from storage.base import Store


class RecoveryManager:
    def __init__(self, store: Store):
        self.store = store
        self.unresolved_transactions: list[str] = []

    @staticmethod
    def remove_artifact_files(payload: dict) -> None:
        for artifact in payload.get("artifacts", []):
            path = Path(artifact["path"])
            if path.exists():
                path.unlink()
            temporary = Path(artifact["temporary"])
            if temporary.exists():
                temporary.unlink()

    def recover_pending(self, staging_root: str | Path) -> list[str]:
        recovered = []
        self.unresolved_transactions = []
        for marker in Path(staging_root).glob("*/metadata/commit.json"):
            payload = json.loads(marker.read_text(encoding="utf-8"))
            status = payload.get("status")
            if status in {"COMPENSATING", "COMPENSATION_FAILED", "COMPENSATION_UNRESOLVED"}:
                transaction_id = str(payload["transaction_id"])
                try:
                    conflicts = self.store.rollback_transaction(transaction_id)
                    if conflicts:
                        raise RuntimeError(f"state compensation conflicts: {conflicts}")
                    self.remove_artifact_files(payload)
                except BaseException as exc:
                    payload["status"] = "COMPENSATION_UNRESOLVED"
                    payload["compensation_error"] = {
                        "code": exc.__class__.__name__,
                        "message": str(exc),
                    }
                    marker.write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    self.unresolved_transactions.append(transaction_id)
                    continue
                payload["status"] = "ROLLED_BACK"
                payload.pop("compensation_error", None)
                marker.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                recovered.append(transaction_id)
                continue
            if status != "PREPARED":
                continue
            for artifact in payload.get("artifacts", []):
                registered = self.store.get_artifact(str(artifact["artifact_id"]))
                path = Path(artifact["path"])
                if registered is None and path.exists():
                    path.unlink()
                temporary = Path(artifact["temporary"])
                if temporary.exists():
                    temporary.unlink()
            payload["status"] = "RECOVERED"
            marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            recovered.append(str(payload["transaction_id"]))
        return recovered
