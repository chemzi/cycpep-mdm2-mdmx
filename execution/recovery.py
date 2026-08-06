"""Recovery for filesystem moves prepared before database commit."""

from __future__ import annotations

import json
from pathlib import Path

from storage.base import Store


class RecoveryManager:
    def __init__(self, store: Store):
        self.store = store

    def recover_pending(self, staging_root: str | Path) -> list[str]:
        recovered = []
        for marker in Path(staging_root).glob("*/metadata/commit.json"):
            payload = json.loads(marker.read_text(encoding="utf-8"))
            if payload.get("status") != "PREPARED":
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
