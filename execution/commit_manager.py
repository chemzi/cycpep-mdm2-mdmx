"""Validation and atomic commit boundary for execution side effects."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from typing import Iterable, Mapping

from contracts.transaction import TransactionContext, TransactionStatus
from storage.base import Store

from .recovery import RecoveryManager
from .staging import StagedArtifact


class CommitManager:
    def __init__(self, store: Store, artifact_root: str | Path):
        self.store = store
        self.artifact_root = Path(artifact_root)
        self.recovery = RecoveryManager(store)

    @staticmethod
    def validate(artifacts: Iterable[StagedArtifact]) -> list[StagedArtifact]:
        valid: list[StagedArtifact] = []
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
        candidate_updates: Iterable[Mapping[str, object]] = (),
        state_updates: Mapping[str, object] | None = None,
        artifacts: Iterable[StagedArtifact] = (),
        staging_path: str | Path | None = None,
    ) -> list[str]:
        staged = self.validate(artifacts)
        context.transition(TransactionStatus.COMMITTING)
        committed_paths: list[Path] = []
        temporary_paths: list[Path] = []
        pending_moves: list[tuple[Path, Path]] = []
        marker = Path(staging_path) / "commit.json" if staging_path else None
        manifest = {
            "transaction_id": context.transaction_id,
            "status": "PREPARED",
            "artifacts": [],
        }
        try:
            registrations: list[dict[str, object]] = []
            for item in staged:
                target = self.artifact_root / context.workflow_id / context.task_id / item.artifact_id
                target.mkdir(parents=True, exist_ok=True)
                destination = target / Path(item.staged_path).name
                if destination.exists():
                    raise FileExistsError(f"artifact destination already exists: {destination}")
                temporary = destination.with_name(
                    f".{destination.name}.{context.transaction_id}.tmp"
                )
                shutil.copyfile(item.staged_path, temporary)
                with temporary.open("rb+") as stream:
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary_paths.append(temporary)
                pending_moves.append((temporary, destination))
                manifest["artifacts"].append({
                    "artifact_id": item.artifact_id,
                    "path": str(destination),
                    "temporary": str(temporary),
                })
                registrations.append({
                    "artifact_id": item.artifact_id,
                    "artifact_type": item.artifact_type,
                    "path": str(destination),
                    "sha256": item.sha256,
                    "producer_task_id": context.task_id,
                })
            if marker:
                marker.write_text(
                    json.dumps(manifest, indent=2), encoding="utf-8", newline="\n"
                )
            for temporary, destination in pending_moves:
                os.replace(temporary, destination)
                committed_paths.append(destination)
            event = {
                "workflow_id": context.workflow_id,
                "run_id": context.run_id,
                "task_id": context.task_id,
                "event_type": "execution_completed",
                "agent": "execution",
                "transaction_id": context.transaction_id,
                "attempt_id": context.attempt_id,
            }
            event_ids = self.store.commit_transaction(
                context=context.to_dict(),
                candidate_updates=list(candidate_updates),
                state_updates=dict(state_updates or {}),
                artifacts=registrations,
                completed_event=event,
            )
        except Exception:
            for path in temporary_paths:
                if path.exists():
                    path.unlink()
            for path in committed_paths:
                if path.exists():
                    path.unlink()
            if marker:
                manifest["status"] = "ROLLED_BACK"
                marker.write_text(
                    json.dumps(manifest, indent=2), encoding="utf-8", newline="\n"
                )
            if context.status == TransactionStatus.COMMITTING:
                context.transition(TransactionStatus.ROLLED_BACK)
            raise
        # 数据库已经提交，正式文件不得再被回滚。标记文件只是恢复辅助，
        # 更新失败不影响已提交数据：恢复逻辑依据数据库注册决定文件去留。
        context.transition(TransactionStatus.COMMITTED)
        if marker:
            try:
                manifest["status"] = "COMMITTED"
                marker.write_text(
                    json.dumps(manifest, indent=2), encoding="utf-8", newline="\n"
                )
            except Exception:
                pass
        return event_ids

    def recover_pending(self, staging_root: str | Path) -> list[str]:
        return self.recovery.recover_pending(staging_root)
