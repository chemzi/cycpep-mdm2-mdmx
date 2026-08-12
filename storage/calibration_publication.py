"""Atomic SQLite publication for the CalibrationBaseline authority."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from calibration_baseline import (
    CalibrationBaselineError,
    validate_calibration_artifact,
    validate_publication_identity,
)
from core.integrity import file_sha256, object_sha256

from .sqlite_ownership import SQLiteOwnership

if TYPE_CHECKING:
    from .sqlite_store import SQLiteStore


def _validated_values(
    binding: Mapping[str, Any],
    thresholds: Mapping[str, Any],
    artifact: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    binding_value = dict(binding)
    threshold_value = dict(thresholds)
    artifact_value = dict(artifact)
    validate_publication_identity(binding_value)
    if artifact_value.get("artifact_id") != binding_value.get("artifact_id"):
        raise CalibrationBaselineError("artifact record identity mismatch")
    if artifact_value.get("sha256") != binding_value.get("artifact_sha256"):
        raise CalibrationBaselineError("artifact record digest mismatch")
    if object_sha256(threshold_value) != binding_value.get("thresholds_sha256"):
        raise CalibrationBaselineError("threshold snapshot digest mismatch")
    artifact_path = Path(str(artifact_value.get("path") or ""))
    if not artifact_path.is_file() or file_sha256(artifact_path) != binding_value.get(
        "artifact_sha256"
    ):
        raise CalibrationBaselineError("calibration artifact content mismatch")
    validate_calibration_artifact(
        binding_value, artifact_path, thresholds=threshold_value
    )
    return binding_value, threshold_value, artifact_value


def _artifact_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": value.get("artifact_id"),
        "artifact_type": value.get("artifact_type"),
        "size_bytes": value.get("size_bytes"),
        "sha256": value.get("sha256"),
    }


def _insert_artifact(connection: Any, artifact: Mapping[str, Any], now: str) -> None:
    connection.execute(
        "INSERT INTO artifacts(artifact_id, artifact_type, path, size_bytes, sha256, producer_task_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            artifact.get("artifact_id"), artifact.get("artifact_type"),
            artifact.get("path"), artifact.get("size_bytes"), artifact.get("sha256"),
            artifact.get("producer_task_id"), artifact.get("created_at") or now,
        ),
    )


def publish_sqlite_calibration(
    store: "SQLiteStore",
    *,
    binding: Mapping[str, Any],
    thresholds: Mapping[str, Any],
    artifact: Mapping[str, Any],
    now: str,
) -> dict[str, Any]:
    """Publish artifact, active state binding, and Evidence in one transaction."""
    binding_value, threshold_value, artifact_value = _validated_values(
        binding, thresholds, artifact
    )
    publication_id = str(binding_value["publication_id"])
    artifact_id = str(binding_value["artifact_id"])

    with store._write() as connection:
        state = store._state_in(connection, store.project_id)
        approved_digest = state.get("approved_digest") or (
            ((state.get("project_config") or {}).get("review") or {}).get(
                "approved_digest"
            )
        )
        if binding_value.get("project_id") != store.project_id:
            raise CalibrationBaselineError("calibration publication project mismatch")
        if binding_value.get("approved_digest") != approved_digest:
            raise CalibrationBaselineError("calibration publication approved project mismatch")

        active = state.get("threshold_calibration_binding")
        publication_event_id = f"{publication_id}-published"
        was_published = connection.execute(
            "SELECT 1 FROM evidence_events WHERE event_id = ? AND event_type = ?",
            (publication_event_id, "threshold_calibration_published"),
        ).fetchone() is not None
        existing_row = connection.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        expected_artifact = _artifact_identity(artifact_value)
        if isinstance(active, dict) and active.get("publication_id") == publication_id:
            observed_artifact = (
                {key: existing_row[key] for key in expected_artifact}
                if existing_row is not None
                else None
            )
            if (
                active == binding_value
                and state.get("thresholds") == threshold_value
                and observed_artifact == expected_artifact
            ):
                return {"status": "idempotent", "publication_id": publication_id}
            raise CalibrationBaselineError(
                "publication identity conflicts with different content"
            )
        if existing_row is not None:
            observed_artifact = {key: existing_row[key] for key in expected_artifact}
            if observed_artifact != expected_artifact:
                raise CalibrationBaselineError(
                    "artifact identity conflicts with different content"
                )
        else:
            _insert_artifact(connection, artifact_value, now)

        state["thresholds"] = threshold_value
        state["threshold_calibration_binding"] = binding_value
        store._write_state(connection, store.project_id, state)
        ownership = SQLiteOwnership(connection, store.project_id)
        ownership.advance_state("thresholds", None)
        ownership.advance_state("threshold_calibration_binding", None)
        if not was_published:
            store._append_formal_event(connection, {
                "event_id": publication_event_id,
                "agent": "research",
                "event_type": "threshold_calibration_published",
                "project_id": store.project_id,
                "calibration_binding": binding_value,
            })
    return {
        "status": "idempotent" if was_published else "published",
        "publication_id": publication_id,
    }
