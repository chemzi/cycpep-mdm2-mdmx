"""Validation and commit boundary for staged execution effects."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from contracts.transaction import TransactionContext, TransactionStatus
from storage.base import Store
from prediction_pipeline.contracts import file_sha256

from .recovery import (
    OrchestratorProbe,
    RecoveryManager,
    RecoveryResult,
    owner_lease,
    utc_now,
)
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
        self.owner_instance_id = uuid.uuid4().hex

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
        candidate_patches: Iterable[Mapping[str, object]] = (),
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
        store_invoked = False
        manifest = {
            "transaction_id": context.transaction_id,
            "context": context.to_dict(),
            "status": "PREPARED",
            "artifacts": [],
            **owner_lease(
                worker_id=(context.metadata or {}).get("worker_id"),
                instance_id=self.owner_instance_id,
            ),
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
            store_invoked = True
            event_ids = self.store.commit_transaction(
                context=context.to_dict(),
                candidate_updates=[
                    item.to_dict() if hasattr(item, "to_dict") else dict(item)
                    for item in candidate_updates
                ],
                candidate_patches=[
                    item.to_dict() if hasattr(item, "to_dict") else dict(item)
                    for item in candidate_patches
                ],
                state_updates=dict(state_updates or {}),
                state_appends=[
                    item.to_dict() if hasattr(item, "to_dict") else dict(item)
                    for item in state_appends
                ],
                artifacts=registrations,
                evidence_events=evidence_events,
            )
        except BaseException as exc:
            self._handle_commit_failure(
                context=context,
                marker=marker,
                manifest=manifest,
                temporary_paths=temporary_paths,
                moved=moved,
                store_invoked=store_invoked,
                commit_error=exc,
            )
            raise
        context.transition(TransactionStatus.COMMITTED)
        manifest["status"] = "COMMITTED"
        manifest["context"] = context.to_dict()
        manifest["heartbeat_at"] = utc_now()
        try:
            durable_atomic_json(marker, manifest)
        except OSError:
            pass
        return CommitResult(tuple(event_ids), tuple(registrations))

    def _handle_commit_failure(
        self,
        *,
        context: TransactionContext,
        marker: Path,
        manifest: dict,
        temporary_paths: Iterable[Path],
        moved: Iterable[Path],
        store_invoked: bool,
        commit_error: BaseException,
    ) -> None:
        database_status: str | None = None
        probe_error: Exception | None = None
        if store_invoked:
            try:
                database_status = self.store.get_transaction_status(
                    context.transaction_id
                )
            except Exception as exc:
                probe_error = exc
        definitely_not_committed = (
            not store_invoked
            or (probe_error is None and database_status in {None, "FAILED", "ROLLED_BACK"})
        )
        if definitely_not_committed:
            self._remove_paths((*temporary_paths, *moved))
            manifest["status"] = "ROLLED_BACK"
            durable_atomic_json(marker, manifest)
            if context.status == TransactionStatus.COMMITTING:
                context.transition(TransactionStatus.ROLLED_BACK)
            return

        if database_status == "COMMITTED":
            context.transition(TransactionStatus.COMMITTED)
        manifest["status"] = "RECOVERY_UNRESOLVED"
        manifest["context"] = context.to_dict()
        manifest["database_status_after_commit_error"] = database_status or "UNKNOWN"
        manifest["recovery_error"] = {
            "code": commit_error.__class__.__name__,
            "message": str(commit_error),
            "probe_error": (
                {
                    "code": probe_error.__class__.__name__,
                    "message": str(probe_error),
                }
                if probe_error is not None
                else None
            ),
        }
        try:
            durable_atomic_json(marker, manifest)
        except OSError:
            # The durable PREPARED marker already on disk remains sufficient
            # for startup recovery to probe the database without deleting files.
            pass

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
                # Re-hash after copy+fsync so a staged file modified in the
                # validate->copy window cannot be registered under a stale digest.
                if file_sha256(temporary) != artifact.sha256:
                    raise ValueError(
                        f"artifact content changed during commit: {artifact.artifact_id}"
                    )
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
        orchestrator_state: OrchestratorProbe | None = None,
    ) -> RecoveryResult:
        return self.recovery.recover_pending(
            staging_root, orchestrator_state=orchestrator_state
        )

    def refresh_heartbeat(self, context: TransactionContext, staging_path: str | Path) -> None:
        """Best-effort UTC heartbeat for this manager's active transaction."""
        marker = Path(staging_path) / "metadata" / "commit.json"
        if not marker.is_file():
            return
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if (
            payload.get("transaction_id") != context.transaction_id
            or payload.get("owner_instance_id") != self.owner_instance_id
        ):
            return
        payload["heartbeat_at"] = utc_now()
        try:
            durable_atomic_json(marker, payload)
        except OSError:
            pass

    def rollback_committed(
        self, context: TransactionContext, staging_path: str | Path
    ) -> None:
        marker = Path(staging_path) / "metadata" / "commit.json"
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["status"] = "COMPENSATING"
        durable_atomic_json(marker, payload)
        try:
            conflicts = self.store.rollback_transaction(context.transaction_id)
        except BaseException as exc:
            payload["status"] = "COMPENSATION_UNRESOLVED"
            payload["compensation_error"] = {
                "code": exc.__class__.__name__,
                "message": str(exc),
            }
            durable_atomic_json(marker, payload)
            raise
        if conflicts:
            payload["status"] = "COMPENSATION_CONFLICT"
            payload["compensation_error"] = {
                "code": "COMPENSATION_CONFLICT",
                "message": f"state compensation conflicts: {conflicts}",
            }
            durable_atomic_json(marker, payload)
            context.transition(TransactionStatus.COMPENSATION_CONFLICT)
            raise RuntimeError(f"state compensation conflicts: {conflicts}")
        try:
            self.recovery.remove_artifact_files(payload)
        except OSError as exc:
            payload["status"] = "COMPENSATION_UNRESOLVED"
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
