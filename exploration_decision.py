"""Build and record deterministic E2 ExplorationDecision artifacts.

The builder consumes only explicitly supplied current-run Evidence.  It never
queries history and never calls Design, Planner, Orchestrator, or Execution.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
import uuid

from contracts.exploration_decision import (
    DECISION_ID_PREFIX,
    ExplorationDecision,
    ExplorationDecisionContractError,
    require_trace_id,
)
from contracts.trace import TraceContext
from contracts.event import EvidenceEvent
from data_layer import EvidenceLogger
from experience import (
    LENGTH_PREFERENCE_POLICY,
    no_length_adjustment_reason,
    summarize_failures,
    suggest_length_preference,
)
from prediction_pipeline.contracts import canonical_json, object_sha256
from target_bootstrap import config_digest
from threshold_contract import canonical_threshold_digest


EVENT_BATTERY = "battery_evaluated"
EVENT_SHORTLIST = "exploration_shortlist"
EVENT_HANDOFF = "prediction_handoff_ready"
EVENT_DECISION = "exploration_decision"


@dataclass(frozen=True)
class _DecisionScope:
    project_id: str
    workflow_id: str
    run_id: str
    source_round: int
    prediction_run_id: str
    prediction_handoff_id: str
    candidate_ids: tuple[str, ...]
    target_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "workflow_id": self.workflow_id,
            "run_id": self.run_id,
            "source_round": self.source_round,
            "applies_to_round": self.source_round + 1,
            "prediction_run_id": self.prediction_run_id,
            "prediction_handoff_id": self.prediction_handoff_id,
            "candidate_ids": list(self.candidate_ids),
            "target_ids": list(self.target_ids),
        }


@dataclass(frozen=True)
class _DecisionAnalysis:
    failure_summary: dict[str, Any]
    status: str
    reason: str
    baseline_weights: list[dict[str, int]]
    proposed_weights: list[dict[str, int]]
    preferred_lengths: list[int]
    statistics: list[dict[str, Any]]


def _ids(values: Iterable[str], label: str) -> tuple[str, ...]:
    normalized = tuple(sorted(values))
    if not normalized or len(normalized) != len(set(normalized)):
        raise ExplorationDecisionContractError(f"{label} must be non-empty and unique")
    for value in normalized:
        require_trace_id(value, label)
    return normalized


def _json_copy(value: Any) -> Any:
    """Copy JSON-like values without retaining mutable caller-owned objects."""
    if isinstance(value, Mapping):
        return {str(key): _json_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_copy(item) for item in value]
    return value


def _battery_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": row.get("event_id"),
        "event_type": row.get("event_type"),
        "agent": row.get("agent"),
        "phase": row.get("phase"),
        "project_id": row.get("project_id"),
        "workflow_id": row.get("workflow_id"),
        "run_id": row.get("run_id"),
        "prediction_run_id": row.get("prediction_run_id"),
        "candidate_id": row.get("candidate_id"),
        "targets": sorted(row.get("targets") or []),
        "length": row.get("length"),
        "route": row.get("route"),
        "passed": row.get("passed"),
        "competition_clearance": row.get("competition_clearance"),
        "failed_layers": sorted(row.get("failed_layers") or []),
        "hard_failures": sorted(row.get("hard_failures") or []),
        "missing_thresholds": sorted(row.get("missing_thresholds") or []),
        "triage_status": row.get("triage_status"),
        "layer_values": _json_copy(row.get("layer_values") or {}),
        "target_pass": _json_copy(row.get("target_pass") or {}),
        "protocol_identity": _json_copy(row.get("protocol_identity") or {}),
        "thresholds_digest": row.get("thresholds_digest"),
    }


def _handoff_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": row.get("event_id"),
        "event_type": row.get("event_type"),
        "agent": row.get("agent"),
        "phase": row.get("phase"),
        "project_id": row.get("project_id"),
        "workflow_id": row.get("workflow_id"),
        "run_id": row.get("run_id"),
        "prediction_run_id": row.get("prediction_run_id"),
        "targets": sorted(row.get("targets") or []),
        "candidate_ids": sorted(row.get("candidate_ids") or []),
        "protocol_identity": _json_copy(row.get("protocol_identity") or {}),
        "thresholds_digest": row.get("thresholds_digest"),
        "handoff_artifact_id": row.get("handoff_artifact_id"),
        "handoff_path": row.get("handoff_path"),
        "handoff_sha256": row.get("handoff_sha256"),
    }


def _require_formal_handoff(row: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve one supplied handoff projection against formal Evidence."""
    expected = _handoff_projection(row)
    matches = [
        item for item in EvidenceLogger.get_all()
        if item.get("event_id") == expected["event_id"]
        and item.get("event_type") == EVENT_HANDOFF
    ]
    if len(matches) != 1 or _handoff_projection(matches[0]) != expected:
        raise ExplorationDecisionContractError(
            "formal Prediction handoff Evidence mismatch"
        )
    return matches[0]


def _shortlist_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    shortlist = sorted(
        (_json_copy(item) for item in row.get("shortlist") or []),
        key=lambda item: str(item.get("candidate_id") or ""),
    )
    return {
        "event_id": row.get("event_id"),
        "event_type": row.get("event_type"),
        "agent": row.get("agent"),
        "phase": row.get("phase"),
        "project_id": row.get("project_id"),
        "workflow_id": row.get("workflow_id"),
        "run_id": row.get("run_id"),
        "round": row.get("round"),
        "targets": sorted(row.get("targets") or []),
        "k": row.get("k"),
        "n_evaluated": row.get("n_evaluated"),
        "n_passed": row.get("n_passed"),
        "shortlist": shortlist,
        "source_event_ids": sorted(row.get("source_event_ids") or []),
        "calibration": _json_copy(row.get("calibration") or {}),
        "unmapped_metrics": sorted(row.get("unmapped_metrics") or []),
    }


def _policy_envelope(project: Mapping[str, Any], target_ids: tuple[str, ...]) -> dict:
    review = project.get("review") if isinstance(project, Mapping) else None
    approved = review.get("approved_digest") if isinstance(review, Mapping) else None
    content = review.get("content_digest") if isinstance(review, Mapping) else None
    if (
        review is None
        or review.get("status") != "approved"
        or not approved
        or approved != content
        or approved != config_digest(dict(project))
    ):
        raise ExplorationDecisionContractError("project policy envelope is not approved")
    targets = {
        str(item.get("id")): item
        for item in project.get("targets") or []
        if isinstance(item, Mapping) and item.get("id")
    }
    allowed_by_target: dict[str, list[int]] = {}
    for target_id in target_ids:
        target = targets.get(target_id)
        raw = ((target or {}).get("design") or {}).get("lengths")
        if not isinstance(raw, list) or not raw:
            raise ExplorationDecisionContractError(
                f"target {target_id} has no approved design.lengths"
            )
        if any(isinstance(item, bool) or not isinstance(item, int) for item in raw):
            raise ExplorationDecisionContractError("approved lengths must be integers")
        allowed_by_target[target_id] = sorted(set(raw))
    effective = set(allowed_by_target[target_ids[0]])
    for values in allowed_by_target.values():
        effective &= set(values)
    if not effective:
        raise ExplorationDecisionContractError("selected targets have no common length")
    return {
        "project_id": project.get("project_id"),
        "approval_digest": approved,
        "allowed_lengths_by_target": allowed_by_target,
        "effective_allowed_lengths": sorted(effective),
    }


def _validate_battery_scope(
    rows: list[Mapping[str, Any]],
    *,
    candidate_ids: tuple[str, ...],
    target_ids: tuple[str, ...],
    trace: tuple[str, str, str],
    prediction_run_id: str,
    protocol_identity: Mapping[str, Any],
    threshold_digest: str,
    allowed_lengths: set[int],
) -> list[dict[str, Any]]:
    if any(not isinstance(row, Mapping) for row in rows):
        raise ExplorationDecisionContractError("battery evidence rows must be objects")
    observed_candidates = [row.get("candidate_id") for row in rows]
    if len(observed_candidates) != len(set(observed_candidates)):
        raise ExplorationDecisionContractError("duplicate current battery candidate evidence")
    if set(observed_candidates) != set(candidate_ids):
        raise ExplorationDecisionContractError(
            "battery candidate IDs must equal handoff candidate IDs"
        )
    projections = []
    for row in rows:
        if row.get("event_type") != EVENT_BATTERY:
            raise ExplorationDecisionContractError("source event is not battery_evaluated")
        if row.get("agent") != "prediction" or row.get("phase") != "evaluate":
            raise ExplorationDecisionContractError(
                "battery authority must be prediction/evaluate Evidence"
            )
        require_trace_id(row.get("event_id"), "source event_id")
        if tuple(row.get(key) for key in ("project_id", "workflow_id", "run_id")) != trace:
            raise ExplorationDecisionContractError("battery workflow trace mismatch")
        if row.get("prediction_run_id") != prediction_run_id:
            raise ExplorationDecisionContractError("battery prediction_run_id mismatch")
        if set(row.get("targets") or ()) != set(target_ids):
            raise ExplorationDecisionContractError("battery target scope mismatch")
        if row.get("protocol_identity") != protocol_identity:
            raise ExplorationDecisionContractError("battery protocol identity mismatch")
        if row.get("thresholds_digest") != threshold_digest:
            raise ExplorationDecisionContractError("battery threshold identity mismatch")
        length = row.get("length")
        if isinstance(length, bool) or not isinstance(length, int) or length not in allowed_lengths:
            raise ExplorationDecisionContractError("battery length is outside policy envelope")
        if not isinstance(row.get("passed"), bool):
            raise ExplorationDecisionContractError("battery passed must be boolean")
        projections.append(_battery_projection(row))
    return sorted(projections, key=lambda item: item["event_id"])


def _validate_handoff_scope(
    row: Mapping[str, Any],
    *,
    trace: tuple[str, str, str],
    target_ids: tuple[str, ...],
) -> tuple[dict[str, Any], tuple[str, ...], str, dict[str, Any], str]:
    if not isinstance(row, Mapping) or row.get("event_type") != EVENT_HANDOFF:
        raise ExplorationDecisionContractError("formal Prediction handoff is required")
    if row.get("agent") != "prediction" or row.get("phase") != "evaluate":
        raise ExplorationDecisionContractError(
            "handoff authority must be prediction/evaluate Evidence"
        )
    require_trace_id(row.get("event_id"), "prediction handoff event_id")
    if tuple(row.get(key) for key in ("project_id", "workflow_id", "run_id")) != trace:
        raise ExplorationDecisionContractError("handoff workflow trace mismatch")
    if set(row.get("targets") or ()) != set(target_ids):
        raise ExplorationDecisionContractError("handoff target scope mismatch")
    prediction_run_id = require_trace_id(
        row.get("prediction_run_id"), "prediction_run_id"
    )
    candidate_ids = _ids(row.get("candidate_ids") or (), "handoff candidate_ids")
    protocol = _json_copy(row.get("protocol_identity") or {})
    if not all(isinstance(protocol.get(key), str) and protocol[key] for key in ("name", "version", "sha256")):
        raise ExplorationDecisionContractError("handoff protocol identity is invalid")
    threshold_digest = row.get("thresholds_digest")
    if not isinstance(threshold_digest, str) or len(threshold_digest) != 64:
        raise ExplorationDecisionContractError("handoff threshold identity is invalid")
    artifact = row.get("handoff_artifact_id")
    path_binding = row.get("handoff_path") and row.get("handoff_sha256")
    if not artifact and not path_binding:
        raise ExplorationDecisionContractError("handoff artifact binding is required")
    return (
        _handoff_projection(row), candidate_ids, prediction_run_id,
        protocol, threshold_digest,
    )


def _validate_shortlist_scope(
    row: Mapping[str, Any],
    *,
    source_rows: list[Mapping[str, Any]],
    source_projections: list[dict[str, Any]],
    target_ids: tuple[str, ...],
    trace: tuple[str, str, str],
    source_round: int,
) -> dict[str, Any]:
    if not isinstance(row, Mapping) or row.get("event_type") != EVENT_SHORTLIST:
        raise ExplorationDecisionContractError("shortlist evidence is invalid")
    if row.get("agent") != "critic" or row.get("phase") != "critic":
        raise ExplorationDecisionContractError(
            "shortlist authority must be critic/critic Evidence"
        )
    require_trace_id(row.get("event_id"), "shortlist event_id")
    if tuple(row.get(key) for key in ("project_id", "workflow_id", "run_id")) != trace:
        raise ExplorationDecisionContractError("shortlist workflow trace mismatch")
    if row.get("round") != source_round or set(row.get("targets") or ()) != set(target_ids):
        raise ExplorationDecisionContractError("shortlist round/target scope mismatch")
    source_ids = {item["event_id"] for item in source_projections}
    if set(row.get("source_event_ids") or ()) != source_ids:
        raise ExplorationDecisionContractError("shortlist source_event_ids mismatch")
    if row.get("n_evaluated") != len(source_rows):
        raise ExplorationDecisionContractError("shortlist n_evaluated mismatch")
    if row.get("n_passed") != sum(1 for item in source_rows if item.get("passed")):
        raise ExplorationDecisionContractError("shortlist n_passed mismatch")
    passed_by_candidate = {item["candidate_id"]: item["passed"] for item in source_rows}
    for item in row.get("shortlist") or []:
        candidate_id = item.get("candidate_id") if isinstance(item, Mapping) else None
        if candidate_id not in passed_by_candidate or item.get("passed") is not passed_by_candidate[candidate_id]:
            raise ExplorationDecisionContractError("shortlist changed scientific pass")
    return _shortlist_projection(row)


def _failure_decision(
    source_rows: list[Mapping[str, Any]], allowed: list[int]
) -> _DecisionAnalysis:
    ordered_rows = sorted(source_rows, key=lambda row: (row.get("length"), row.get("event_id")))
    summary = summarize_failures(events=ordered_rows)
    summary["lengths"] = {
        key: summary["lengths"][key]
        for key in sorted(summary.get("lengths") or {}, key=int)
    }
    hint = suggest_length_preference(
        summary,
        min_failures=LENGTH_PREFERENCE_POLICY.minimum_evaluations_per_length,
        policy=LENGTH_PREFERENCE_POLICY,
    )
    baseline = [{"length": length, "weight": 1} for length in allowed]
    statistics = []
    for length in allowed:
        stat = summary["lengths"].get(str(length), {"n": 0, "failed": 0})
        n, failed = int(stat["n"]), int(stat["failed"])
        statistics.append({
            "length": length,
            "n": n,
            "failed": failed,
            "failure_rate": failed / n if n else None,
            "eligible": n >= LENGTH_PREFERENCE_POLICY.minimum_evaluations_per_length,
        })
    if hint is None or hint.get("lengths", [None])[0] not in allowed:
        reason = no_length_adjustment_reason(LENGTH_PREFERENCE_POLICY)
        return _DecisionAnalysis(
            summary, "no_adjustment", reason, baseline, baseline, [], statistics
        )
    preferred = list(hint["lengths"])
    proposed = [{"length": length, "weight": 1} for length in preferred]
    return _DecisionAnalysis(
        summary, "adjustment", hint["reason"], baseline, proposed, preferred, statistics
    )


def _semantic_inputs(
    scope: _DecisionScope,
    source_evidence: list[dict[str, Any]],
    shortlist_evidence: dict[str, Any],
    handoff_evidence: dict[str, Any],
    envelope: dict[str, Any],
    threshold_digest: str,
    protocol_identity: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        **scope.to_dict(),
        "source_evidence": source_evidence,
        "shortlist_evidence": shortlist_evidence,
        "prediction_handoff_evidence": handoff_evidence,
        "policy_envelope": envelope,
        "policy": LENGTH_PREFERENCE_POLICY.to_dict(),
        "threshold_digest": threshold_digest,
        "protocol_identity": protocol_identity,
    }


def _materialize_decision(
    *,
    scope: _DecisionScope,
    input_digest: str,
    policy_digest: str,
    threshold_digest: str,
    protocol_identity: dict[str, Any],
    source_evidence: list[dict[str, Any]],
    shortlist_evidence: dict[str, Any],
    handoff_evidence: dict[str, Any],
    envelope: dict[str, Any],
    analysis: _DecisionAnalysis,
) -> ExplorationDecision:
    support = {
        **envelope,
        "policy": LENGTH_PREFERENCE_POLICY.to_dict(),
        "length_statistics": analysis.statistics,
        "source_evidence": source_evidence,
        "shortlist_evidence": shortlist_evidence,
        "prediction_handoff_evidence": handoff_evidence,
    }
    return ExplorationDecision(
        schema_version=1,
        decision_id=f"{DECISION_ID_PREFIX}{input_digest}",
        decision_input_digest=input_digest,
        project_id=scope.project_id,
        workflow_id=scope.workflow_id,
        run_id=scope.run_id,
        source_round=scope.source_round,
        applies_to_round=scope.source_round + 1,
        prediction_run_id=scope.prediction_run_id,
        prediction_handoff_id=scope.prediction_handoff_id,
        candidate_ids=scope.candidate_ids,
        target_ids=scope.target_ids,
        source_event_ids=tuple(item["event_id"] for item in source_evidence),
        shortlist_event_id=shortlist_evidence["event_id"],
        failure_summary=analysis.failure_summary,
        adjustment={
            "knob": "peptide_length_policy_weights",
            "baseline_policy_weights": analysis.baseline_weights,
            "proposed_policy_weights": analysis.proposed_weights,
            "preferred_lengths": analysis.preferred_lengths,
        },
        evidence_support=support,
        policy_envelope_digest=policy_digest,
        threshold_digest=threshold_digest,
        protocol_identity=protocol_identity,
        decision_status=analysis.status,
        reason=analysis.reason,
    )


def build_exploration_decision(
    *,
    battery_events: Iterable[Mapping[str, Any]],
    shortlist_event: Mapping[str, Any],
    prediction_handoff_event: Mapping[str, Any],
    project_config: Mapping[str, Any],
    thresholds: Mapping[str, Any],
    project_id: str,
    workflow_id: str,
    run_id: str,
    target_ids: Iterable[str],
    source_round: int,
) -> ExplorationDecision:
    """Return one deterministic Decision from explicit current-run inputs."""
    for label, value in {
        "project_id": project_id, "workflow_id": workflow_id, "run_id": run_id,
    }.items():
        require_trace_id(value, label)
    if isinstance(source_round, bool) or not isinstance(source_round, int) or source_round < 1:
        raise ExplorationDecisionContractError("source_round must be a positive integer")
    targets = _ids(target_ids, "target_ids")
    if project_config.get("project_id") != project_id:
        raise ExplorationDecisionContractError("project config identity mismatch")
    envelope = _policy_envelope(project_config, targets)
    rows = list(battery_events)
    trace = (project_id, workflow_id, run_id)
    formal_handoff = _require_formal_handoff(prediction_handoff_event)
    handoff_projection, candidates, prediction_run_id, protocol, authoritative_threshold = (
        _validate_handoff_scope(
            formal_handoff, trace=trace, target_ids=targets
        )
    )
    supplied_threshold = canonical_threshold_digest(deepcopy(dict(thresholds)))
    if supplied_threshold != authoritative_threshold:
        raise ExplorationDecisionContractError("threshold snapshot differs from Prediction authority")
    source_projections = _validate_battery_scope(
        rows, candidate_ids=candidates, target_ids=targets, trace=trace,
        prediction_run_id=prediction_run_id, protocol_identity=protocol,
        threshold_digest=authoritative_threshold,
        allowed_lengths=set(envelope["effective_allowed_lengths"]),
    )
    shortlist_projection = _validate_shortlist_scope(
        shortlist_event, source_rows=rows, source_projections=source_projections,
        target_ids=targets, trace=trace, source_round=source_round,
    )
    policy_digest = object_sha256(envelope)
    threshold_digest = authoritative_threshold
    scope = _DecisionScope(
        project_id, workflow_id, run_id, source_round, prediction_run_id,
        handoff_projection["event_id"], candidates, targets,
    )
    semantic_inputs = _semantic_inputs(
        scope, source_projections, shortlist_projection, handoff_projection, envelope,
        threshold_digest, protocol,
    )
    input_digest = object_sha256(semantic_inputs)
    analysis = _failure_decision(rows, envelope["effective_allowed_lengths"])
    return _materialize_decision(
        scope=scope,
        input_digest=input_digest,
        policy_digest=policy_digest,
        threshold_digest=threshold_digest,
        protocol_identity=protocol,
        source_evidence=source_projections,
        shortlist_evidence=shortlist_projection,
        handoff_evidence=handoff_projection,
        envelope=envelope,
        analysis=analysis,
    )


def _formal_sources_match(decision: ExplorationDecision, rows: list[dict[str, Any]]) -> None:
    by_id = {row.get("event_id"): row for row in rows}
    support = decision.to_dict()["evidence_support"]
    for expected in support["source_evidence"]:
        observed = by_id.get(expected["event_id"])
        if observed is None or _battery_projection(observed) != expected:
            raise ExplorationDecisionContractError("formal source Evidence mismatch")
    shortlist = support["shortlist_evidence"]
    observed = by_id.get(shortlist["event_id"])
    if observed is None or _shortlist_projection(observed) != shortlist:
        raise ExplorationDecisionContractError("formal shortlist Evidence mismatch")
    handoff = support["prediction_handoff_evidence"]
    observed = by_id.get(handoff["event_id"])
    if observed is None or _handoff_projection(observed) != handoff:
        raise ExplorationDecisionContractError("formal Prediction handoff Evidence mismatch")


def _existing_decision_event(
    rows: Iterable[Mapping[str, Any]], contract: ExplorationDecision, payload: dict
) -> str | None:
    for row in rows:
        if row.get("event_type") != EVENT_DECISION or row.get("decision_id") != contract.decision_id:
            continue
        try:
            existing = ExplorationDecision.from_dict(row).to_dict()
        except (ExplorationDecisionContractError, TypeError, ValueError) as exc:
            raise ExplorationDecisionContractError(
                "existing decision_id has an invalid formal payload"
            ) from exc
        if canonical_json(existing) == canonical_json(payload):
            return row["event_id"]
        raise ExplorationDecisionContractError(
            "existing decision_id has a different canonical payload"
        )
    return None


def record_exploration_decision(decision: ExplorationDecision | Mapping[str, Any]) -> str:
    """Append or sequentially reuse one formal exploration_decision event."""
    value = decision.to_dict() if isinstance(decision, ExplorationDecision) else decision
    contract = ExplorationDecision.from_dict(value)
    payload = contract.to_dict()
    rows = EvidenceLogger.get_all()
    _formal_sources_match(contract, rows)
    existing_id = _existing_decision_event(rows, contract, payload)
    if existing_id is not None:
        return existing_id
    trace = TraceContext(
        project_id=contract.project_id,
        workflow_id=contract.workflow_id,
        run_id=contract.run_id,
    )
    event = EvidenceEvent(
        timestamp=datetime.now(timezone.utc).isoformat(),
        event_id=uuid.uuid4().hex[:12],
        agent="critic",
        event_type=EVENT_DECISION,
        payload=payload,
        trace_context=trace,
        phase="critic",
        round_num=contract.source_round,
        targets=contract.target_ids,
    )
    entry = event.to_dict()
    # Generic EvidenceLogger.log rejects this domain event. Reaching the Store
    # through the internal append primitive is reserved for this verified path.
    EvidenceLogger._write(entry)
    return entry["event_id"]
