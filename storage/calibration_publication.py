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
    if (
        artifact_value.get("artifact_type") != "calibration_baseline"
        or artifact_value.get("size_bytes") != artifact_path.stat().st_size
    ):
        raise CalibrationBaselineError("calibration artifact metadata mismatch")
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


def _registered_artifact_is_complete(row: Any, binding: Mapping[str, Any]) -> bool:
    if (
        row is None
        or row["artifact_type"] != "calibration_baseline"
        or row["sha256"] != binding.get("artifact_sha256")
    ):
        return False
    path = Path(str(row["path"] or ""))
    return (
        path.is_file()
        and row["size_bytes"] == path.stat().st_size
        and file_sha256(path) == binding.get("artifact_sha256")
    )


def _validate_store_authority(
    state: Mapping[str, Any], binding: Mapping[str, Any], project_id: str
) -> None:
    project_config = state.get("project_config") or {}
    review = project_config.get("review") or {}
    approved_digest = state.get("approved_digest") or review.get("approved_digest")
    if binding.get("project_id") != project_id:
        raise CalibrationBaselineError("calibration publication project mismatch")
    if binding.get("approved_digest") != approved_digest:
        raise CalibrationBaselineError("calibration publication approved project mismatch")
    approved_dataset_sha256 = None
    if binding.get("calibration_authority") == "approved_real":
        approved_dataset_sha256 = review.get("approved_scored_dataset_sha256")
    if binding.get("approved_scored_dataset_sha256") != approved_dataset_sha256:
        raise CalibrationBaselineError(
            "calibration publication approved scored dataset mismatch"
        )


def _classify_publication_replay(
    connection: Any,
    state: Mapping[str, Any],
    binding: Mapping[str, Any],
    thresholds: Mapping[str, Any],
    artifact: Mapping[str, Any],
) -> bool:
    publication_id = str(binding["publication_id"])
    event_exists = connection.execute(
        "SELECT 1 FROM evidence_events WHERE event_id = ? AND event_type = ?",
        (f"{publication_id}-published", "threshold_calibration_published"),
    ).fetchone() is not None
    row = connection.execute(
        "SELECT * FROM artifacts WHERE artifact_id = ?", (binding["artifact_id"],)
    ).fetchone()
    active_matches = (
        state.get("threshold_calibration_binding") == binding
        and state.get("thresholds") == thresholds
    )
    if active_matches:
        artifact_matches = row is not None and {
            key: row[key] for key in _artifact_identity(artifact)
        } == _artifact_identity(artifact)
        if event_exists and artifact_matches and _registered_artifact_is_complete(
            row, binding
        ):
            return True
        raise CalibrationBaselineError(
            "active calibration has incomplete authority; recovery required"
        )
    if row is not None or event_exists:
        raise CalibrationBaselineError(
            "stale publication replay cannot reactivate a superseded baseline"
        )
    return False


def _write_publication(
    store: "SQLiteStore",
    connection: Any,
    state: dict[str, Any],
    binding: Mapping[str, Any],
    thresholds: Mapping[str, Any],
    artifact: Mapping[str, Any],
    now: str,
) -> None:
    _insert_artifact(connection, artifact, now)
    state["thresholds"] = dict(thresholds)
    state["threshold_calibration_binding"] = dict(binding)
    store._write_state(connection, store.project_id, state)
    ownership = SQLiteOwnership(connection, store.project_id)
    ownership.advance_state("thresholds", None)
    ownership.advance_state("threshold_calibration_binding", None)
    store._append_formal_event(connection, {
        "event_id": f"{binding['publication_id']}-published",
        "agent": "research",
        "event_type": "threshold_calibration_published",
        "project_id": store.project_id,
        "calibration_binding": dict(binding),
    })


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

    with store._write() as connection:
        state = store._state_in(connection, store.project_id)
        _validate_store_authority(state, binding_value, store.project_id)
        if _classify_publication_replay(
            connection, state, binding_value, threshold_value, artifact_value
        ):
            return {"status": "idempotent", "publication_id": publication_id}
        _write_publication(
            store, connection, state, binding_value, threshold_value,
            artifact_value, now,
        )
    return {"status": "published", "publication_id": publication_id}
