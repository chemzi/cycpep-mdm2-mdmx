"""Validate and materialize transaction-managed Prediction proposals."""

from __future__ import annotations

import json
from pathlib import Path

import data_layer

from .contracts import ExecutionContractError
from .results import (
    CandidatePatchMutation,
    ExecutionActionResult,
    StateAppendMutation,
)


_PATCH_FIELDS = frozenset({
    "metrics_json", "l1_pass", "l2_pass", "l3_pass", "l4_pass", "l5_pass",
    "l6_pass", "l7_pass", "all_layers_pass", "metric_clearance",
    "competition_clearance", "triage_status", "threshold_audit_json", "plddt",
    "nc_distance_pre", "nc_distance_post", "ring_closure_pre",
    "ring_closure_post", "scrmsd", "dg_method", "site_consistency",
    "pose_rmsd", "seed_convergence", "final_status", "notes", "last_updated",
}) | frozenset(
    field for field in data_layer.INDEX_COLUMNS
    if field.startswith((
        "ipsae_", "ipae_", "iptm_", "colab_iptm_", "dg_", "sc_", "dsasa_",
        "hotspot_cov_", "site_consistency_", "pose_rmsd_", "seed_convergence_",
    ))
)
_STATUSES = frozenset({
    "finalized", "awaiting_threshold_calibration", "prediction_pending",
    "needs_optimization", "invalid",
})
_EVIDENCE_TYPES = frozenset({
    "candidate_scored", "prediction_recorded", "candidate_finalized",
    "prediction_run_started", "prediction_handoff_ready",
})
_CANDIDATE_EVIDENCE_TYPES = frozenset({
    "candidate_scored", "prediction_recorded", "candidate_finalized",
})


def _json_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionContractError(
            "prediction_effects_invalid", f"invalid {label}: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise ExecutionContractError(
            "prediction_effects_invalid", f"{label} must be an object"
        )
    return value


def _candidate_patches(
    effects: dict,
    candidate_ids: list[str],
    transaction_id: str,
    expected_protocol: dict,
) -> tuple[dict[str, dict], dict[str, dict]]:
    raw_patches = effects.get("candidate_patches")
    if not isinstance(raw_patches, list) or any(
        not isinstance(item, dict) for item in raw_patches
    ):
        raise ExecutionContractError(
            "prediction_effects_invalid", "candidate_patches must be an array"
        )
    patch_ids = [str(item.get("candidate_id") or "") for item in raw_patches]
    if sorted(patch_ids) != sorted(candidate_ids) or len(patch_ids) != len(raw_patches):
        raise ExecutionContractError(
            "prediction_effects_scope_mismatch",
            "Prediction candidate patches differ from the approved task scope",
        )
    patches: dict[str, dict] = {}
    prediction_metadata: dict[str, dict] = {}
    for item in raw_patches:
        candidate_id = str(item["candidate_id"])
        patch = item.get("patch")
        if (
            set(item) != {"candidate_id", "patch"}
            or not isinstance(patch, dict)
            or not patch
        ):
            raise ExecutionContractError(
                "prediction_effects_invalid", "candidate patch must be a non-empty object"
            )
        unexpected = set(patch) - _PATCH_FIELDS
        if unexpected:
            raise ExecutionContractError(
                "prediction_effects_scope_mismatch",
                f"Prediction candidate patch contains non-owned fields: {sorted(unexpected)}",
            )
        if patch.get("final_status") not in _STATUSES:
            raise ExecutionContractError(
                "prediction_effects_invalid", "candidate patch has invalid Prediction status"
            )
        try:
            metrics = json.loads(patch.get("metrics_json") or "")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ExecutionContractError(
                "prediction_effects_invalid", "candidate patch metrics_json is invalid"
            ) from exc
        prediction = metrics.get("prediction") if isinstance(metrics, dict) else None
        if (
            not isinstance(prediction, dict)
            or prediction.get("protocol_identity") != expected_protocol
            or prediction.get("record_artifact_id")
            != f"{transaction_id}-prediction-record-{candidate_id}"
            or "record_path" in prediction
        ):
            raise ExecutionContractError(
                "prediction_effects_scope_mismatch",
                "candidate patch does not reference its transaction record identity",
            )
        patches[candidate_id] = patch
        prediction_metadata[candidate_id] = prediction
    return patches, prediction_metadata


def _record_proposals(
    effects: dict,
    candidate_ids: list[str],
    transaction_id: str,
    run_id: str,
    expected_protocol: dict,
) -> dict[str, dict]:
    records = effects.get("record_artifacts")
    if (
        not isinstance(records, list)
        or len(records) != len(candidate_ids)
        or any(not isinstance(item, dict) for item in records)
        or {str(item.get("candidate_id") or "") for item in records}
        != set(candidate_ids)
    ):
        raise ExecutionContractError(
            "prediction_effects_scope_mismatch",
            "Prediction record artifacts differ from the approved task scope",
        )
    by_candidate = {}
    for item in records:
        candidate_id = str(item["candidate_id"])
        path = Path(str(item.get("path") or "")).expanduser().resolve()
        if (
            set(item) != {"candidate_id", "artifact_id", "path"}
            or item.get("artifact_id")
            != f"{transaction_id}-prediction-record-{candidate_id}"
            or not path.is_file()
        ):
            raise ExecutionContractError(
                "prediction_record_invalid", f"Prediction record is invalid: {path}"
            )
        record = _json_object(path, "prediction record")
        if (
            record.get("run_id") != run_id
            or record.get("protocol_identity") != expected_protocol
            or (record.get("candidate") or {}).get("candidate_id") != candidate_id
        ):
            raise ExecutionContractError(
                "prediction_effects_scope_mismatch",
                "Prediction record identity differs from the approved task scope",
            )
        by_candidate[candidate_id] = dict(item, path=str(path))
    return by_candidate


def _state_proposals(
    effects: dict,
    records: dict[str, dict],
    transaction_id: str,
    run_id: str,
    expected_protocol: dict,
) -> None:
    state_updates = effects.get("state_updates")
    prediction = (
        state_updates.get("prediction") if isinstance(state_updates, dict) else None
    )
    if (
        not isinstance(state_updates, dict)
        or set(state_updates) != {"phase", "prediction"}
        or state_updates.get("phase") != "evaluate"
        or not isinstance(prediction, dict)
        or prediction.get("run_id") != run_id
        or prediction.get("protocol_identity") != expected_protocol
        or prediction.get("handoff_artifact_id")
        != f"{transaction_id}-prediction_handoff"
        or "handoff_path" in prediction
        or "handoff_sha256" in prediction
        or "run_dir" in prediction
    ):
        raise ExecutionContractError(
            "prediction_effects_scope_mismatch",
            "Prediction state proposal exceeds the evaluate/prediction scope",
        )
    expected_records = {
        candidate_id: {"artifact_id": item["artifact_id"]}
        for candidate_id, item in records.items()
    }
    if prediction.get("record_artifacts") != expected_records:
        raise ExecutionContractError(
            "prediction_effects_scope_mismatch",
            "Prediction state record identities do not match staged records",
        )
    state_appends = effects.get("state_appends")
    if not isinstance(state_appends, list) or len(state_appends) != 1:
        raise ExecutionContractError(
            "prediction_effects_scope_mismatch",
            "Prediction must propose exactly one iteration_history append",
        )
    append = state_appends[0]
    item = append.get("item") if isinstance(append, dict) else None
    if (
        not isinstance(append, dict)
        or set(append) != {
            "kind", "key", "item", "identity_path", "identity_value",
        }
        or append.get("kind") != "append_if_absent"
        or append.get("key") != "iteration_history"
        or append.get("identity_path") != ["summary", "run_id"]
        or append.get("identity_value") != run_id
        or not isinstance(item, dict)
        or set(item) != {"phase", "agent", "timestamp", "summary"}
        or item.get("phase") != "evaluate"
        or item.get("agent") != "prediction"
        or item.get("summary") != prediction
    ):
        raise ExecutionContractError(
            "prediction_effects_scope_mismatch",
            "Prediction state append is not the current run summary",
        )


def _handoff_proposal(
    effects: dict,
    records: dict[str, dict],
    prediction_metadata: dict[str, dict],
    transaction_id: str,
    run_id: str,
    expected_protocol: dict,
) -> dict:
    proposal = effects.get("handoff_artifact")
    if not isinstance(proposal, dict):
        raise ExecutionContractError(
            "prediction_effects_invalid", "handoff_artifact must be an object"
        )
    path = Path(str(proposal.get("path") or "")).expanduser().resolve()
    if (
        set(proposal) != {"artifact_id", "path"}
        or proposal.get("artifact_id") != f"{transaction_id}-prediction_handoff"
        or not path.is_file()
    ):
        raise ExecutionContractError(
            "prediction_handoff_invalid", "Prediction handoff is invalid"
        )
    handoff = _json_object(path, "prediction handoff")
    if (
        handoff.get("run_id") != run_id
        or handoff.get("protocol_identity") != expected_protocol
        or not isinstance(handoff.get("categories"), dict)
    ):
        raise ExecutionContractError(
            "prediction_effects_scope_mismatch",
            "Prediction handoff identity differs from the approved task scope",
        )
    categories = handoff["categories"]
    if any(
        not isinstance(category, list)
        or any(not isinstance(item, dict) for item in category)
        for category in categories.values()
    ):
        raise ExecutionContractError(
            "prediction_effects_scope_mismatch",
            "Prediction handoff categories must contain record objects",
        )
    entries = [
        item
        for category in categories.values()
        for item in category
    ]
    if len(entries) != len(records) or {
        str(item.get("candidate_id") or "") for item in entries
    } != set(records):
        raise ExecutionContractError(
            "prediction_effects_scope_mismatch",
            "Prediction handoff differs from the approved candidate scope",
        )
    for item in entries:
        candidate_id = str(item["candidate_id"])
        record = records[candidate_id]
        if (
            item.get("record_artifact_id") != record["artifact_id"]
            or Path(str(item.get("record_path") or "")).resolve()
            != Path(record["path"])
            or item.get("record_sha256")
            != prediction_metadata[candidate_id].get("record_sha256")
        ):
            raise ExecutionContractError(
                "prediction_effects_scope_mismatch",
                "Prediction handoff record does not match its candidate proposal",
            )
    return proposal


def _evidence_proposals(
    effects: dict,
    records: dict[str, dict],
    handoff: dict,
    expected_protocol: dict,
) -> None:
    events = effects.get("evidence_events")
    if not isinstance(events, list) or any(not isinstance(item, dict) for item in events):
        raise ExecutionContractError(
            "prediction_effects_invalid", "evidence_events must be an array of objects"
        )
    for event in events:
        event_type = event.get("event_type")
        candidate_id = event.get("candidate_id")
        if (
            event_type not in _EVIDENCE_TYPES
            or event.get("protocol_identity") != expected_protocol
            or "artifact_sha256" in event
            or "handoff_sha256" in event
            or (candidate_id is not None and candidate_id not in records)
            or (event_type in _CANDIDATE_EVIDENCE_TYPES and not candidate_id)
        ):
            raise ExecutionContractError(
                "prediction_effects_scope_mismatch",
                "Prediction evidence exceeds the approved candidate/protocol scope",
            )
        if candidate_id and (
            event.get("record_artifact_id") != records[candidate_id]["artifact_id"]
        ):
            raise ExecutionContractError(
                "prediction_effects_scope_mismatch",
                "Prediction evidence record identity does not match its candidate",
            )
        if event_type == "prediction_handoff_ready" and (
            event.get("handoff_artifact_id") != handoff["artifact_id"]
        ):
            raise ExecutionContractError(
                "prediction_effects_scope_mismatch",
                "Prediction handoff evidence does not match the handoff artifact",
            )


def load_prediction_transaction_effects(
    *,
    path: Path,
    candidate_ids: list[str],
    run_id: str,
    transaction_id: str,
    expected_protocol: dict,
) -> dict:
    effects = _json_object(path, "prediction transaction effects")
    if (
        set(effects) != {
            "schema_version", "run_id", "protocol_identity", "candidate_patches",
            "state_updates", "state_appends", "evidence_events",
            "record_artifacts", "handoff_artifact",
        }
        or effects.get("schema_version") != 1
        or effects.get("run_id") != run_id
    ):
        raise ExecutionContractError(
            "prediction_effects_invalid",
            "Prediction effects have an invalid schema or run identity",
        )
    if effects.get("protocol_identity") != expected_protocol:
        raise ExecutionContractError(
            "prediction_protocol_mismatch",
            "Prediction effects do not match the task protocol identity",
        )
    _, prediction_metadata = _candidate_patches(
        effects, candidate_ids, transaction_id, expected_protocol
    )
    records = _record_proposals(
        effects, candidate_ids, transaction_id, run_id, expected_protocol
    )
    _state_proposals(
        effects, records, transaction_id, run_id, expected_protocol
    )
    handoff = _handoff_proposal(
        effects,
        records,
        prediction_metadata,
        transaction_id,
        run_id,
        expected_protocol,
    )
    _evidence_proposals(effects, records, handoff, expected_protocol)
    return effects


def typed_prediction_result(
    effects: dict,
    handoff: Path,
    processes: list[dict],
) -> ExecutionActionResult:
    return ExecutionActionResult(
        candidate_patches=tuple(
            CandidatePatchMutation(str(item["candidate_id"]), item["patch"])
            for item in effects["candidate_patches"]
        ),
        state_updates=effects["state_updates"],
        state_appends=tuple(
            StateAppendMutation(
                key=str(item["key"]),
                item=item["item"],
                identity_path=tuple(item["identity_path"]),
                identity_value=item["identity_value"],
            )
            for item in effects["state_appends"]
        ),
        evidence_events=tuple(effects["evidence_events"]),
        outputs=(("prediction_handoff", handoff),),
        processes=tuple(processes),
    )
