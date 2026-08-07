"""Validation and commit boundary for staged execution effects."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Iterable, Mapping

from contracts.transaction import TransactionContext, TransactionStatus
from storage.base import Store
from prediction_pipeline.contracts import file_sha256

from .recovery import RecoveryManager
from .staging import StagedArtifact


class CommitManager:
    def __init__(self, store: Store, artifact_root: str | Path):
        self.store = store
        self.artifact_root = Path(artifact_root)
        self.recovery = RecoveryManager(store)

    @staticmethod
    def validate(artifacts: Iterable[StagedArtifact]) -> list[StagedArtifact]:
        validated = []
        for artifact in artifacts:
            path = Path(artifact.staged_path)
            if not path.is_file():
                raise ValueError(f"staged artifact is missing: {path}")
            if path.stat().st_size != artifact.size_bytes:
                raise ValueError(f"staged artifact size changed: {artifact.artifact_id}")
            if file_sha256(path) != artifact.sha256:
                raise ValueError(f"staged artifact sha256 changed: {artifact.artifact_id}")
            validated.append(artifact)
        return validated

    def commit(
        self,
        context: TransactionContext,
        *,
        candidate_updates: Iterable[Mapping[str, object]] = (),
        state_updates: Mapping[str, object] | None = None,
        state_appends: Iterable[object] = (),
        artifacts: Iterable[StagedArtifact] = (),
        evidence_events: Iterable[Mapping[str, object]] = (),
        staging_path: str | Path,
    ) -> list[str]:
        staged = self.validate(artifacts)
        context.transition(TransactionStatus.COMMITTING)
        marker = Path(staging_path) / "metadata" / "commit.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        moved: list[Path] = []
        temporary_paths: list[Path] = []
        manifest = {
            "transaction_id": context.transaction_id,
            "status": "PREPARED",
            "artifacts": [],
        }
        registrations = []
        try:
            for artifact in staged:
                destination = (
                    self.artifact_root
                    / context.workflow_id
                    / context.task_id
                    / artifact.artifact_id
                    / Path(artifact.staged_path).name
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise FileExistsError(f"artifact destination already exists: {destination}")
                temporary = destination.with_name(
                    f".{destination.name}.{context.transaction_id}.tmp"
                )
                shutil.copyfile(artifact.staged_path, temporary)
                with temporary.open("rb+") as stream:
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary_paths.append(temporary)
                manifest["artifacts"].append({
                    "artifact_id": artifact.artifact_id,
                    "path": str(destination),
                    "temporary": str(temporary),
                })
                registrations.append({
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "path": str(destination),
                    "size_bytes": artifact.size_bytes,
                    "sha256": artifact.sha256,
                })
            marker.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            for artifact, temporary in zip(manifest["artifacts"], temporary_paths):
                destination = Path(artifact["path"])
                os.replace(temporary, destination)
                moved.append(destination)
            event_ids = self.store.commit_transaction(
                context=context.to_dict(),
                candidate_updates=[
                    item.to_dict() if hasattr(item, "to_dict") else dict(item)
                    for item in candidate_updates
                ],
                state_updates=dict(state_updates or {}),
                state_appends=[
                    item.to_dict() if hasattr(item, "to_dict") else dict(item)
                    for item in state_appends
                ],
                artifacts=registrations,
                evidence_events=evidence_events,
            )
        except BaseException:
            for path in temporary_paths:
                if path.exists():
                    path.unlink()
            for path in moved:
                if path.exists():
                    path.unlink()
            manifest["status"] = "ROLLED_BACK"
            marker.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            if context.status == TransactionStatus.COMMITTING:
                context.transition(TransactionStatus.ROLLED_BACK)
            raise
        context.transition(TransactionStatus.COMMITTED)
        manifest["status"] = "COMMITTED"
        try:
            marker.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
        return event_ids

    def recover_pending(self, staging_root: str | Path) -> list[str]:
        return self.recovery.recover_pending(staging_root)

    def rollback_committed(
        self, context: TransactionContext, staging_path: str | Path
    ) -> None:
        marker = Path(staging_path) / "metadata" / "commit.json"
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["status"] = "COMPENSATING"
        marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            conflicts = self.store.rollback_transaction(context.transaction_id)
            if conflicts:
                raise RuntimeError(f"state compensation conflicts: {conflicts}")
            self.recovery.remove_artifact_files(payload)
        except BaseException as exc:
            payload["status"] = "COMPENSATION_FAILED"
            payload["compensation_error"] = {
                "code": exc.__class__.__name__,
                "message": str(exc),
            }
            marker.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            raise
        payload["status"] = "ROLLED_BACK"
        payload.pop("compensation_error", None)
        marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        context.transition(TransactionStatus.ROLLED_BACK)
