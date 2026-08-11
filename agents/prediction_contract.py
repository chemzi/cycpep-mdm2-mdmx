"""Public Prediction correlation and formal recovery contracts.

The Store-backed start receipt owns the original internal run locator.  The
Launcher journal may mirror that locator, but it is never consulted here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from prediction_pipeline.contracts import ContractError, file_sha256, object_sha256


_LAUNCHER_ID = re.compile(r"^launcher_([0-9a-f]{32})$")


@dataclass(frozen=True)
class PredictionCorrelation:
    prediction_invocation_id: str
    prediction_run_id: str
    launcher_run_id: str
    project_id: str
    approved_content_binding: str

    def __post_init__(self) -> None:
        match = _LAUNCHER_ID.fullmatch(self.launcher_run_id)
        if match is None:
            raise ContractError(
                "prediction_launcher_id_invalid",
                "launcher_run_id must use a canonical launcher_<32-lowercase-hex> namespace",
            )
        payload = match.group(1)
        if (
            self.prediction_invocation_id != f"prediction_invocation_{payload}"
            or self.prediction_run_id != f"prediction_{payload}"
        ):
            raise ContractError(
                "prediction_identity_mismatch",
                "Prediction identities must preserve the Launcher UUID payload exactly",
            )
        if not self.project_id or not self.approved_content_binding:
            raise ContractError(
                "prediction_correlation_invalid",
                "Prediction correlation requires project and approved-content bindings",
            )

    @classmethod
    def for_launcher(
        cls,
        *,
        launcher_run_id: str,
        project_id: str,
        approved_content_binding: str,
    ) -> "PredictionCorrelation":
        match = _LAUNCHER_ID.fullmatch(launcher_run_id)
        if match is None:
            raise ContractError(
                "prediction_launcher_id_invalid",
                "launcher_run_id must use a canonical launcher_<32-lowercase-hex> namespace",
            )
        payload = match.group(1)
        return cls(
            prediction_invocation_id=f"prediction_invocation_{payload}",
            prediction_run_id=f"prediction_{payload}",
            launcher_run_id=f"launcher_{payload}",
            project_id=project_id,
            approved_content_binding=approved_content_binding,
        )

    def manifest_fields(self) -> dict[str, str]:
        return {
            "prediction_invocation_id": self.prediction_invocation_id,
            "launcher_run_id": self.launcher_run_id,
            "approved_content_binding": self.approved_content_binding,
        }

    def receipt_fields(self) -> dict[str, str]:
        return {
            **self.manifest_fields(),
            "prediction_run_id": self.prediction_run_id,
            "project_id": self.project_id,
        }


@dataclass(frozen=True)
class PredictionInvocationInputs:
    project_digest: str
    thresholds_digest: str
    config_digest: str
    batch_digest: str
    candidate_ids: tuple[str, ...]

    @classmethod
    def from_pipeline(cls, pipeline: Any) -> "PredictionInvocationInputs":
        return cls(
            project_digest=pipeline.project_digest,
            thresholds_digest=pipeline.thresholds_digest,
            config_digest=pipeline.config_digest,
            batch_digest=pipeline.batch_digest,
            candidate_ids=tuple(sorted(
                str(row.get("candidate_id") or "") for row in pipeline.rows
            )),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "project_digest": self.project_digest,
            "thresholds_digest": self.thresholds_digest,
            "config_digest": self.config_digest,
            "batch_digest": self.batch_digest,
            "candidate_ids": list(self.candidate_ids),
        }


@dataclass(frozen=True)
class PredictionInvocationRecovery:
    status: str
    prediction_invocation_id: str
    prediction_run_id: str
    start_event_id: str | None = None
    completion_event_id: str | None = None
    run_root: Path | None = None
    handoff_path: Path | None = None
    blocker_code: str | None = None


def start_receipt_payload(
    correlation: PredictionCorrelation,
    *,
    run_root: Path,
    inputs: PredictionInvocationInputs,
    expected_run_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        **correlation.receipt_fields(),
        "prediction_run_locator": {
            "root": str(run_root.resolve()),
            "run_id": correlation.prediction_run_id,
        },
        "expected_run_manifest": dict(expected_run_manifest),
        **inputs.to_payload(),
    }


def _load_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _relevant_start(event: Mapping[str, Any], expected: PredictionCorrelation) -> bool:
    return (
        event.get("prediction_invocation_id") == expected.prediction_invocation_id
        or event.get("prediction_run_id") == expected.prediction_run_id
        or event.get("launcher_run_id") == expected.launcher_run_id
    )


def _matches_correlation(event: Mapping[str, Any], expected: PredictionCorrelation) -> bool:
    return all(event.get(key) == value for key, value in expected.receipt_fields().items())


def _inputs_from_event(event: Mapping[str, Any]) -> PredictionInvocationInputs | None:
    candidate_ids = event.get("candidate_ids")
    if (
        not isinstance(candidate_ids, list)
        or not candidate_ids
        or not all(isinstance(value, str) and value for value in candidate_ids)
        or candidate_ids != sorted(set(candidate_ids))
    ):
        return None
    values = {
        key: event.get(key)
        for key in ("project_digest", "thresholds_digest", "config_digest", "batch_digest")
    }
    if not all(isinstance(value, str) and value for value in values.values()):
        return None
    return PredictionInvocationInputs(
        project_digest=values["project_digest"],
        thresholds_digest=values["thresholds_digest"],
        config_digest=values["config_digest"],
        batch_digest=values["batch_digest"],
        candidate_ids=tuple(candidate_ids),
    )


def _batch_digest(rows: list[Any], inputs: PredictionInvocationInputs) -> str | None:
    if not all(isinstance(row, dict) for row in rows):
        return None
    identity = {
        "rows": [
            {
                "candidate_id": row.get("candidate_id"),
                "sequence": row.get("sequence"),
                "manifest_path": row.get("manifest_path"),
            }
            for row in rows
        ],
        "project_digest": inputs.project_digest,
        "config_digest": inputs.config_digest,
    }
    return object_sha256(identity)


def _manifest_matches_receipt(
    manifest: Mapping[str, Any],
    correlation: PredictionCorrelation,
    inputs: PredictionInvocationInputs,
) -> bool:
    return (
        manifest.get("run_id") == correlation.prediction_run_id
        and manifest.get("project_id") == correlation.project_id
        and manifest.get("project_digest") == inputs.project_digest
        and manifest.get("thresholds_digest") == inputs.thresholds_digest
        and manifest.get("config_digest") == inputs.config_digest
        and manifest.get("batch_digest") == inputs.batch_digest
        and all(
            manifest.get(key) == value
            for key, value in correlation.receipt_fields().items()
        )
    )


def _relevant_completion(
    event: Mapping[str, Any], expected: PredictionCorrelation
) -> bool:
    return (
        event.get("prediction_invocation_id") == expected.prediction_invocation_id
        or event.get("prediction_run_id") == expected.prediction_run_id
        or event.get("run_id") == expected.prediction_run_id
        or event.get("launcher_run_id") == expected.launcher_run_id
    )


def _ambiguous(
    correlation: PredictionCorrelation,
    *,
    start_event_id: str | None = None,
    run_root: Path | None = None,
    code: str = "prediction_recovery_ambiguous",
) -> PredictionInvocationRecovery:
    return PredictionInvocationRecovery(
        status="conflicting" if code == "prediction_correlation_conflict" else "started_without_completion",
        prediction_invocation_id=correlation.prediction_invocation_id,
        prediction_run_id=correlation.prediction_run_id,
        start_event_id=start_event_id,
        run_root=run_root,
        blocker_code=code,
    )


def validate_prediction_invocation(
    correlation: PredictionCorrelation,
    *,
    store: Any,
    expected_inputs: PredictionInvocationInputs | None = None,
) -> PredictionInvocationRecovery:
    """Validate one exact Prediction invocation without ambient path lookup."""
    events = store.query(
        project_id=correlation.project_id,
        agent="prediction",
        event_type="prediction_invocation_started",
    )
    starts = [event for event in events if _relevant_start(event, correlation)]
    if not starts:
        return PredictionInvocationRecovery(
            status="not_started",
            prediction_invocation_id=correlation.prediction_invocation_id,
            prediction_run_id=correlation.prediction_run_id,
        )
    if len(starts) != 1 or not _matches_correlation(starts[0], correlation):
        return _ambiguous(correlation, code="prediction_correlation_conflict")

    start = starts[0]
    inputs = _inputs_from_event(start)
    locator = start.get("prediction_run_locator")
    expected_run_manifest = start.get("expected_run_manifest")
    if (
        inputs is None
        or (expected_inputs is not None and inputs != expected_inputs)
        or not isinstance(locator, dict)
        or not isinstance(expected_run_manifest, dict)
        or not _manifest_matches_receipt(expected_run_manifest, correlation, inputs)
        or locator.get("run_id") != correlation.prediction_run_id
        or not isinstance(locator.get("root"), str)
    ):
        return _ambiguous(
            correlation,
            start_event_id=start.get("event_id"),
            code="prediction_correlation_conflict",
        )
    run_root = Path(locator["root"])
    if not run_root.is_absolute() or run_root.resolve() != run_root:
        return _ambiguous(
            correlation,
            start_event_id=start.get("event_id"),
            code="prediction_correlation_conflict",
        )

    run_dir = run_root / correlation.prediction_run_id
    manifest = _load_object(run_dir / "run_manifest.json")
    project = _load_object(run_dir / "inputs" / "project.json")
    thresholds = _load_object(run_dir / "inputs" / "thresholds.json")
    handoff_path = run_dir / "prediction_handoff.json"
    handoff = _load_object(handoff_path)
    # Candidate rows are intentionally an array rather than an object.
    try:
        raw_rows = json.loads((run_dir / "inputs" / "candidate_rows.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        raw_rows = None
    if (
        manifest is None
        or project is None
        or thresholds is None
        or not isinstance(raw_rows, list)
        or handoff is None
        or manifest != expected_run_manifest
        or object_sha256(project) != inputs.project_digest
        or object_sha256(thresholds) != inputs.thresholds_digest
        or _batch_digest(raw_rows, inputs) != inputs.batch_digest
        or tuple(sorted(str(row.get("candidate_id") or "") for row in raw_rows)) != inputs.candidate_ids
        or handoff.get("run_id") != correlation.prediction_run_id
        or handoff.get("project_id") != correlation.project_id
        or any(handoff.get(key) != value for key, value in correlation.receipt_fields().items())
    ):
        return _ambiguous(
            correlation,
            start_event_id=start.get("event_id"),
            run_root=run_root,
        )

    completions = [
        event for event in store.query(
            project_id=correlation.project_id,
            agent="prediction",
            event_type="prediction_handoff_ready",
        )
        if _relevant_completion(event, correlation)
    ]
    if len(completions) != 1:
        return _ambiguous(
            correlation,
            start_event_id=start.get("event_id"),
            run_root=run_root,
        )
    completion = completions[0]
    if (
        not _matches_correlation(completion, correlation)
        or completion.get("run_id") != correlation.prediction_run_id
        or completion.get("handoff_path") != str(handoff_path)
        or completion.get("handoff_sha256") != file_sha256(handoff_path)
    ):
        return _ambiguous(
            correlation,
            start_event_id=start.get("event_id"),
            run_root=run_root,
        )
    return PredictionInvocationRecovery(
        status="completed",
        prediction_invocation_id=correlation.prediction_invocation_id,
        prediction_run_id=correlation.prediction_run_id,
        start_event_id=start.get("event_id"),
        completion_event_id=completion.get("event_id"),
        run_root=run_root,
        handoff_path=handoff_path,
    )


__all__ = [
    "PredictionCorrelation",
    "PredictionInvocationInputs",
    "PredictionInvocationRecovery",
    "start_receipt_payload",
    "validate_prediction_invocation",
]
