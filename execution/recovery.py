"""Crash recovery for prepared artifact commits."""

from __future__ import annotations

import json
from pathlib import Path

from storage.base import Store


class RecoveryManager:
    def __init__(self, store: Store):
        self.store = store

    def recover_pending(self, staging_root: str | Path) -> list[str]:
        recovered: list[str] = []
        for marker in Path(staging_root).glob("*/commit.json"):
            payload = json.loads(marker.read_text(encoding="utf-8"))
            if payload.get("status") != "PREPARED":
                continue
            for item in payload.get("artifacts", []):
                artifact_id = str(item["artifact_id"])
                registered = self.store.get_artifact(artifact_id)
                path = Path(item["path"])
                if registered is None and path.exists():
                    path.unlink()
                temporary = Path(item["temporary"])
                if temporary.exists():
                    temporary.unlink()
            payload["status"] = "RECOVERED"
            marker.write_text(json.dumps(payload, indent=2), encoding="utf-8", newline="\n")
            recovered.append(str(payload["transaction_id"]))
        return recovered
