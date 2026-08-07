"""Validation and commit boundary for staged execution effects."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from contracts.transaction import TransactionContext, TransactionStatus
from storage.base import Store
from prediction_pipeline.contracts import file_sha256

from .recovery import RecoveryManager
from .staging import StagedArtifact
from .supervisor import durable_atomic_json


@dataclass(frozen=True)
class CommitResult:
    event_ids: tuple[str, ...]
    artifacts: tuple[Mapping[str, object], ...]


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
    ) -> CommitResult:
        staged = self.validate(artifacts)
        context.transition(TransactionStatus.COMMITTING)
        marker = Path(staging_path) / "metadata" / "commit.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        moved: list[Path] = []
        registrations: list[dict] = []
        temporary_paths: list[Path] = []
        manifest = {
            "transaction_id": context.transaction_id,
            "context": context.to_dict(),
            "status": "PREPARED",
            "artifacts": [],
        }
        try:
            manifest_artifacts, registrations, temporary_paths = self._prepare_artifacts(
                context, staged
            )
            manifest["artifacts"] = manifest_artifacts
            durable_atomic_json(marker, manifest)
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
            self._remove_paths((*temporary_paths, *moved))
            manifest["status"] = "ROLLED_BACK"
            durable_atomic_json(marker, manifest)
            if context.status == TransactionStatus.COMMITTING:
                context.transition(TransactionStatus.ROLLED_BACK)
            raise
        context.transition(TransactionStatus.COMMITTED)
        manifest["status"] = "COMMITTED"
        try:
            durable_atomic_json(marker, manifest)
        except OSError:
            pass
        return CommitResult(tuple(event_ids), tuple(registrations))

    def _prepare_artifacts(
        self, context: TransactionContext, staged: Iterable[StagedArtifact]
    ) -> tuple[list[dict], list[dict], list[Path]]:
        manifest_artifacts = []
        registrations = []
        temporary_paths = []
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
                    raise FileExistsError(
                        f"artifact destination already exists: {destination}"
                    )
                temporary = destination.with_name(
                    f".{destination.name}.{context.transaction_id}.tmp"
                )
                shutil.copyfile(artifact.staged_path, temporary)
                with temporary.open("rb+") as stream:
                    stream.flush()
                    os.fsync(stream.fileno())
                registration = {
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "path": str(destination),
                    "size_bytes": artifact.size_bytes,
                    "sha256": artifact.sha256,
                }
                temporary_paths.append(temporary)
                registrations.append(registration)
                manifest_artifacts.append(dict(registration, temporary=str(temporary)))
        except BaseException:
            self._remove_paths(temporary_paths)
            raise
        return manifest_artifacts, registrations, temporary_paths

    @staticmethod
    def _remove_paths(paths: Iterable[Path]) -> None:
        for path in paths:
            if path.exists():
                path.unlink()

    def recover_pending(
        self,
        staging_root: str | Path,
        *,
        orchestrator_closed: Callable[[Mapping[str, object]], bool] | None = None,
    ) -> list[str]:
        return self.recovery.recover_pending(
            staging_root, orchestrator_closed=orchestrator_closed
        )

    def rollback_committed(
        self, context: TransactionContext, staging_path: str | Path
    ) -> None:
        marker = Path(staging_path) / "metadata" / "commit.json"
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["status"] = "COMPENSATING"
        durable_atomic_json(marker, payload)
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
            durable_atomic_json(marker, payload)
            raise
        payload["status"] = "ROLLED_BACK"
        payload.pop("compensation_error", None)
        durable_atomic_json(marker, payload)
        context.transition(TransactionStatus.ROLLED_BACK)
