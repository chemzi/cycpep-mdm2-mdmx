­r‡^Ñf¥–Ø¦{O,yÊ'vÃ®¶›­"""Single commit boundary for execution side effects."""

from __future__ import annotations

from hashlib import sha256
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
    ) -> list[str]:
        staged = self.validate(artifacts)
        context.transition(TransactionStatus.COMMITTING)
        committed_paths: list[Path] = []
        try:
            registrations = []
            for item in staged:
                target = self.artifact_root / context.workflow_id / context.task_id / item.artifact_id
                target.mkdir(parents=True, exist_ok=True)
                destination = target / Path(item.staged_path).name
                shutil.copy2(item.staged_path, destination)
                committed_paths.append(destination)
                registrations.append({
                    "artifact_id": item.artifact_id,
                    "artifact_type": item.artifact_type,
                    "path": str(destination),
                    "sha256": item.sha256,
                    "producer_task_id": context.task_id,
                })
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
            return event_ids
        except Exception:
            for path in committed_paths:
                if path.exists():
                    path.unlink()
            context.transition(TransactionStatus.ROLLED_BACK)
            raise
