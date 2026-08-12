"""Formal publication proof for one bootstrap Prediction transaction."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from prediction_pipeline.contracts import file_sha256


class PredictionPublicationError(ValueError):
    pass


@dataclass(frozen=True)
class PredictionPublicationProof:
    artifact_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]


def validate_prediction_publication(
    store: Any,
    *,
    project_id: str,
    plan: Mapping[str, Any],
    orchestrator_run_id: str,
    task: Mapping[str, Any],
    attempt_id: str,
    transaction_id: str,
    handoff_artifact_id: str,
    handoff: Mapping[str, Any],
) -> PredictionPublicationProof:
    task_id = str(task.get("task_id") or "")
    expected = _expected_trace(
        project_id, plan, orchestrator_run_id, task_id, attempt_id, transaction_id
    )
    transaction = store.get_transaction(transaction_id)
    if not _transaction_matches(transaction, expected, plan):
        raise PredictionPublicationError(
            "committed Prediction transaction binding is incomplete or inconsistent"
        )
    candidate_ids = tuple((plan.get("source") or {}).get("candidate_ids") or ())
    artifact_ids = _validate_record_artifacts(
        store, transaction_id, plan, handoff, candidate_ids, expected, task
    )
    evidence_ids = _validate_prediction_evidence(
        store, transaction_id, plan, handoff, candidate_ids, expected,
        artifact_ids, handoff_artifact_id, task,
    )
    return PredictionPublicationProof(
        artifact_ids=tuple(artifact_ids[value] for value in candidate_ids),
        evidence_ids=evidence_ids,
    )


def _expected_trace(project_id, plan, run_id, task_id, attempt_id, transaction_id):
    return {
        "project_id": project_id,
        "workflow_id": str(plan.get("workflow_id") or ""),
        "run_id": run_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "transaction_id": transaction_id,
    }


def _transaction_matches(transaction, expected, plan):
    return (
        isinstance(transaction, Mapping)
        and transaction.get("status") == "COMMITTED"
        and transaction.get("action") == "evaluate_new_design_candidates"
        and all(transaction.get(key) == value for key, value in expected.items())
        and (transaction.get("metadata") or {}).get("plan_id") == plan.get("plan_id")
    )


def _validate_record_artifacts(
    store, transaction_id, plan, handoff, candidate_ids, expected, task
):
    artifacts = [
        item for item in store.list_artifacts()
        if item.get("transaction_id") == transaction_id
        and item.get("artifact_type") == "prediction_record"
    ]
    artifact_by_id = {str(item.get("artifact_id") or ""): item for item in artifacts}
    entries = [item for values in (handoff.get("categories") or {}).values() for item in values]
    entries_by_candidate = {str(item.get("candidate_id") or ""): item for item in entries}
    artifact_ids = {
        candidate_id: f"{transaction_id}-prediction-record-{candidate_id}"
        for candidate_id in candidate_ids
    }
    if (
        len(entries) != len(candidate_ids)
        or set(entries_by_candidate) != set(candidate_ids)
        or set(artifact_by_id) != set(artifact_ids.values())
    ):
        raise PredictionPublicationError(
            "exact-scope Prediction record Artifacts are missing or non-unique"
        )
    for candidate_id, artifact_id in artifact_ids.items():
        _validate_record(
            artifact_by_id[artifact_id], entries_by_candidate[candidate_id],
            candidate_id, expected, plan, handoff, task,
        )
    return artifact_ids


def _validate_record(artifact, entry, candidate_id, expected, plan, handoff, task):
    path = Path(str(artifact.get("path") or "")).expanduser().resolve()
    record = _read_json(path)
    digest = file_sha256(path) if record is not None else None
    identity = task["parameters"]["execution_identity"]
    if (
        record is None
        or entry.get("record_artifact_id") != artifact.get("artifact_id")
        or Path(str(entry.get("record_path") or "")).expanduser().resolve() != path
        or entry.get("record_sha256") != digest
        or artifact.get("sha256") != digest
        or record.get("run_id") != handoff.get("run_id")
        or record.get("execution_identity") != identity
        or (record.get("candidate") or {}).get("candidate_id") != candidate_id
        or any(artifact.get(key) != value for key, value in expected.items())
        or (artifact.get("metadata") or {}).get("plan_id") != plan.get("plan_id")
    ):
        raise PredictionPublicationError(
            f"formal Prediction record is missing or inconsistent for {candidate_id}"
        )


def _validate_prediction_evidence(
    store, transaction_id, plan, handoff, candidate_ids, expected,
    artifact_ids, handoff_artifact_id, task,
):
    events = store.query(transaction_id=transaction_id, agent="prediction")
    records = [item for item in events if item.get("event_type") == "prediction_recorded"]
    handoffs = [item for item in events if item.get("event_type") == "prediction_handoff_ready"]
    by_candidate = {}
    for event in records:
        by_candidate.setdefault(str(event.get("candidate_id") or ""), []).append(event)
    if (
        len(handoffs) != 1 or set(by_candidate) != set(candidate_ids)
        or any(len(by_candidate[value]) != 1 for value in candidate_ids)
    ):
        raise PredictionPublicationError(
            "transaction-bound Prediction record or handoff-ready Evidence is missing"
        )
    required = [handoffs[0], *[by_candidate[value][0] for value in candidate_ids]]
    for event in required:
        _validate_event(
            event, expected, plan, handoff, task, artifact_ids, handoff_artifact_id
        )
    return tuple(str(event.get("event_id") or "") for event in required)


def _validate_event(event, expected, plan, handoff, task, artifact_ids, handoff_id):
    candidate_id = str(event.get("candidate_id") or "")
    event_type = event.get("event_type")
    record_binding_valid = (
        event_type == "prediction_recorded"
        and candidate_id in artifact_ids
        and event.get("record_artifact_id") == artifact_ids[candidate_id]
        and not event.get("handoff_artifact_id")
    )
    handoff_binding_valid = (
        event_type == "prediction_handoff_ready"
        and not candidate_id
        and not event.get("record_artifact_id")
        and event.get("handoff_artifact_id") == handoff_id
    )
    if (
        any(event.get(key) != value for key, value in expected.items())
        or event.get("plan_id") != plan.get("plan_id")
        or event.get("prediction_run_id") != handoff.get("run_id")
        or event.get("execution_identity") != task["parameters"]["execution_identity"]
        or not (record_binding_valid or handoff_binding_valid)
    ):
        raise PredictionPublicationError(
            "Prediction Evidence does not share the approved execution binding"
        )


def _read_json(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


__all__ = [
    "PredictionPublicationError",
    "PredictionPublicationProof",
    "validate_prediction_publication",
]
