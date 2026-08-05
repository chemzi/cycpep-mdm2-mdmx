­r‡^Ñf¥–Ø¦{O,yÊ'vÃ®¶›­"""Single commit boundary for execution side effects."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from typing import Any, Iterable, Mapping

from contracts.transaction import TransactionContext, TransactionStatus
from .staging import StagedArtifact


class CommitManager:
    def __init__(self, store: Any, artifact_root: str | Path):
        self.store = store
        self.artifact_root = Path(artifact_root)

    def validate(self, artifacts: Iterable[StagedArtifact]) -> list[StagedArtifact]:
        valid = []
        for artifact in artifacts:
            path = Path(artifact.staged_path)
            if not path.is_file():
                raise ValueError(f"staged artifact is missing: {path}")
            digest = sha256(path.read_bytes()).hexdigest()
            if digest != artifact.sha256:
                raise ValueError(f"staged artifact hash mismatch: {artifact.artifact_id}")
            valid.append(artifact)
        return valid

    def commit(
        self,
        context: TransactionContext,
        *,
        candidate_updates: Iterable[Mapping[str, Any]] = (),
        state_updates: Mapping[str, Any] | None = None,
        artifacts: Iterable[StagedArtifact] = (),
        staging_path: str | Path | None = None,
    ) -> list[str]:
        staged = self.validate(artifacts)
        context.transition(TransactionStatus.COMMITTING)
        committed_paths: list[Path] = []
        temporary_paths: list[Path] = []
        pending_moves: list[tuple[Path, Path]] = []
        marker = Path(staging_path) / "commit.json" if staging_path else None
        manifest = {"transaction_id": context.transaction_id, "status": "PREPARED", "artifacts": []}
        try:
            registrations = []
            for item in staged:
                target = self.artifact_root / context.workflow_id / context.task_id / item.artifact_id
                target.mkdir(parents=True, exist_ok=True)
                destination = target / Path(item.staged_path).name
                temporary = destination.with_name(f".{destination.name}.{context.transaction_id}.tmp")
                shutil.copyfile(item.staged_path, temporary)
                with temporary.open("rb+") as stream:
                    os.fsync(stream.fileno())
                temporary_paths.append(temporary)
                pending_moves.append((temporary, destination))
                manifest["artifacts"].append({"artifact_id": item.artifact_id, "path": str(destination), "temporary": str(temporary)})
                registrations.append({
                    "artifact_id": item.artifact_id,
                    "artifact_type": item.artifact_type,
                    "path": str(destination),
                    "sha256": item.sha256,
                    "producer_task_id": context.task_id,
                })
            if marker:
                marker.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            for temporary, destination in pending_moves:
                os.replace(temporary, destination)
                committed_paths.append(destination)
            event = {
                "workflow_id": context.workflow_id, "run_id": context.run_id,
                "task_id": context.task_id, "event_type": "execution_completed",
                "agent": "execution", "transaction_id": context.transaction_id,
                "attempt_id": context.attempt_id,
            }
            event_ids = self.store.commit_transaction(
                context=context.to_dict(),
                candidate_updates=list(candidate_updates),
                state_updates=dict(state_updates or {}),
                artifacts=registrations,
                completed_event=event,
            )
            context.transition(TransactionStatus.COMMITTED)
            if marker and marker.exists():
                manifest["status"] = "COMMITTED"
                marker.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            return event_ids
        except Exception:
            for path in temporary_paths:
                if path.exists():
                    path.unlink()
            for path in committed_paths:
                if path.exists():
                    path.unlink()
            if marker:
                manifest["status"] = "ROLLED_BACK"
                marker.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            if context.status == TransactionStatus.COMMITTING:
                context.transition(TransactionStatus.ROLLED_BACK)
            raise

    def recover_pending(self, staging_root: str | Path) -> list[str]:
        """Resolve crash markers conservatively using the durable artifact registry."""
        recovered: list[str] = []
        for marker in Path(staging_root).glob("*/commit.json"):
            payload = json.loads(marker.read_text(encoding="utf-8"))
            if payload.get("status") != "PREPARED":
                continue
            for item in payload.get("artifacts", []):
                registered = getattr(self.store, "get_artifact", lambda _: None)(item["artifact_id"])
                path = Path(item["path"])
                if registered is None and path.exists():
                    path.unlink()
                temporary = Path(item.get("temporary", ""))
                if temporary and temporary.exists():
                    temporary.unlink()
            payload["status"] = "RECOVERED"
            marker.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            recovered.append(payload["transaction_id"])
        return recovered
