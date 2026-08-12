"""Immutable contract for an evidence-guided exploration decision.

This contract is an E2 handoff artifact.  It recommends relative peptide-length
policy weights but neither grants scientific clearance nor authorizes planning
or execution.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Any, Mapping

from .trace import SHA256_RE, TRACE_ID_RE
from prediction_pipeline.contracts import object_sha256


DECISION_SCHEMA_VERSION = 1
DECISION_ID_PREFIX = "exploration_decision_"
DECISION_STATUSES = frozenset({"adjustment", "no_adjustment"})


class ExplorationDecisionContractError(ValueError):
    """Raised when an ExplorationDecision is incomplete or inconsistent."""


def require_trace_id(value: Any, label: str) -> str:
    """Validate and return one public trace-style decision identifier."""
    if not isinstance(value, str) or not TRACE_ID_RE.fullmatch(value):
        raise ExplorationDecisionContractError(f"invalid {label}: {value!r}")
    return value


def _require_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ExplorationDecisionContractError(f"invalid {label}: {value!r}")
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _canonical_ids(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if not values:
        raise ExplorationDecisionContractError(f"{label} must not be empty")
    for value in values:
        require_trace_id(value, label)
    if values != tuple(sorted(set(values))):
        raise ExplorationDecisionContractError(f"{label} must be unique and sorted")
    return values


def _weight_lengths(value: Any, label: str) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ExplorationDecisionContractError(f"{label} must be a non-empty array")
    lengths = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"length", "weight"}:
            raise ExplorationDecisionContractError(
                f"{label} entries require only length and weight"
            )
        length, weight = item["length"], item["weight"]
        if (
            isinstance(length, bool)
            or not isinstance(length, int)
            or isinstance(weight, bool)
            or not isinstance(weight, int)
            or weight != 1
        ):
            raise ExplorationDecisionContractError(
                f"{label} entries require integer length and canonical relative weight 1"
            )
        lengths.append(length)
    if lengths != sorted(set(lengths)):
        raise ExplorationDecisionContractError(f"{label} lengths must be unique and sorted")
    return tuple(lengths)


@dataclass(frozen=True)
class ExplorationDecision:
    """Validated immutable E2 decision; nested JSON values are frozen too."""

    schema_version: int
    decision_id: str
    decision_input_digest: str
    project_id: str
    workflow_id: str
    run_id: str
    source_round: int
    applies_to_round: int
    prediction_run_id: str
    prediction_handoff_id: str
    candidate_ids: tuple[str, ...]
    target_ids: tuple[str, ...]
    source_event_ids: tuple[str, ...]
    shortlist_event_id: str
    failure_summary: Mapping[str, Any]
    adjustment: Mapping[str, Any]
    evidence_support: Mapping[str, Any]
    policy_envelope_digest: str
    threshold_digest: str
    protocol_identity: Mapping[str, Any]
    decision_status: str
    reason: str

    def __post_init__(self) -> None:
        self._validate_identity()
        self._validate_scope()
        self._validate_policy()
        self._validate_provenance()
        for name in (
            "failure_summary", "adjustment", "evidence_support", "protocol_identity"
        ):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise ExplorationDecisionContractError(f"{name} must be an object")
            object.__setattr__(self, name, _freeze(value))

    def _validate_identity(self) -> None:
        if self.schema_version != DECISION_SCHEMA_VERSION:
            raise ExplorationDecisionContractError("unsupported decision schema_version")
        digest = _require_digest(self.decision_input_digest, "decision_input_digest")
        if self.decision_id != f"{DECISION_ID_PREFIX}{digest}":
            raise ExplorationDecisionContractError("decision_id does not match input digest")
        policy_digest = _require_digest(
            self.policy_envelope_digest, "policy_envelope_digest"
        )
        _require_digest(self.threshold_digest, "threshold_digest")
        for name in (
            "project_id", "workflow_id", "run_id", "prediction_run_id",
            "prediction_handoff_id", "shortlist_event_id",
        ):
            require_trace_id(getattr(self, name), name)
        if self.decision_status not in DECISION_STATUSES:
            raise ExplorationDecisionContractError("invalid decision_status")
        if not isinstance(self.reason, str) or not self.reason:
            raise ExplorationDecisionContractError("reason must be a non-empty string")
        if policy_digest != object_sha256(self._policy_envelope()):
            raise ExplorationDecisionContractError("policy_envelope_digest is inconsistent")
        if digest != object_sha256(self._input_projection()):
            raise ExplorationDecisionContractError("decision_input_digest is inconsistent")

    def _validate_scope(self) -> None:
        if (
            isinstance(self.source_round, bool)
            or not isinstance(self.source_round, int)
            or self.source_round < 1
            or self.applies_to_round != self.source_round + 1
        ):
            raise ExplorationDecisionContractError("invalid source/applies round binding")
        _canonical_ids(self.candidate_ids, "candidate_ids")
        _canonical_ids(self.target_ids, "target_ids")
        _canonical_ids(self.source_event_ids, "source_event_ids")
        if len(self.source_event_ids) != len(self.candidate_ids):
            raise ExplorationDecisionContractError(
                "source_event_ids must cover candidate_ids exactly once"
            )

    def _validate_policy(self) -> None:
        if not isinstance(self.adjustment, Mapping) or not isinstance(
            self.evidence_support, Mapping
        ):
            raise ExplorationDecisionContractError("adjustment/support must be objects")
        if self.adjustment.get("knob") != "peptide_length_policy_weights":
            raise ExplorationDecisionContractError("unsupported adaptive knob")
        if set(self.adjustment) != {
            "knob", "baseline_policy_weights", "proposed_policy_weights",
            "preferred_lengths",
        }:
            raise ExplorationDecisionContractError("adjustment contains unsupported fields")
        baseline = _weight_lengths(
            self.adjustment.get("baseline_policy_weights"), "baseline_policy_weights"
        )
        proposed = _weight_lengths(
            self.adjustment.get("proposed_policy_weights"), "proposed_policy_weights"
        )
        if not set(proposed).issubset(baseline):
            raise ExplorationDecisionContractError("proposed weights expand the envelope")
        preferred = self.adjustment.get("preferred_lengths")
        if not isinstance(preferred, (list, tuple)):
            raise ExplorationDecisionContractError("preferred_lengths must be an array")
        preferred_tuple = tuple(preferred)
        if preferred_tuple and preferred_tuple != proposed:
            raise ExplorationDecisionContractError("preferred lengths must match proposed weights")
        effective = tuple(self.evidence_support.get("effective_allowed_lengths") or ())
        if effective != baseline:
            raise ExplorationDecisionContractError("baseline weights differ from policy envelope")
        by_target = self.evidence_support.get("allowed_lengths_by_target")
        if not isinstance(by_target, Mapping) or set(by_target) != set(self.target_ids):
            raise ExplorationDecisionContractError("target policy envelopes are incomplete")
        intersection = set(baseline)
        for values in by_target.values():
            if not isinstance(values, (list, tuple)) or not values:
                raise ExplorationDecisionContractError("target length envelope is empty")
            intersection &= set(values)
        if tuple(sorted(intersection)) != baseline:
            raise ExplorationDecisionContractError("effective policy envelope is inconsistent")
        if self.decision_status == "no_adjustment":
            if proposed != baseline or preferred_tuple:
                raise ExplorationDecisionContractError("no_adjustment must preserve baseline")
        elif proposed == baseline or not preferred_tuple:
            raise ExplorationDecisionContractError("adjustment must narrow baseline")
        self._validate_scientific_support(proposed, baseline)

    def _validate_provenance(self) -> None:
        if not isinstance(self.failure_summary, Mapping):
            raise ExplorationDecisionContractError("failure_summary must be an object")
        sources = self.evidence_support.get("source_evidence")
        if (
            not isinstance(sources, (list, tuple))
            or len(sources) != len(self.candidate_ids)
            or any(not isinstance(item, Mapping) for item in sources)
        ):
            raise ExplorationDecisionContractError("source evidence coverage is incomplete")
        source_ids = tuple(sorted(item.get("event_id") for item in sources))
        candidate_ids = tuple(sorted(item.get("candidate_id") for item in sources))
        if source_ids != self.source_event_ids or candidate_ids != self.candidate_ids:
            raise ExplorationDecisionContractError("source evidence scope is inconsistent")
        shortlist = self.evidence_support.get("shortlist_evidence")
        if not isinstance(shortlist, Mapping):
            raise ExplorationDecisionContractError("shortlist evidence is required")
        if shortlist.get("event_id") != self.shortlist_event_id:
            raise ExplorationDecisionContractError("shortlist event identity is inconsistent")
        if tuple(sorted(shortlist.get("source_event_ids") or ())) != self.source_event_ids:
            raise ExplorationDecisionContractError("shortlist sources are inconsistent")
        handoff = self.evidence_support.get("prediction_handoff_evidence")
        if not isinstance(handoff, Mapping):
            raise ExplorationDecisionContractError("Prediction handoff evidence is required")
        if (
            handoff.get("event_id") != self.prediction_handoff_id
            or handoff.get("event_type") != "prediction_handoff_ready"
            or handoff.get("agent") != "prediction"
            or handoff.get("phase") != "evaluate"
            or tuple(handoff.get(key) for key in ("project_id", "workflow_id", "run_id"))
            != (self.project_id, self.workflow_id, self.run_id)
            or handoff.get("prediction_run_id") != self.prediction_run_id
            or tuple(handoff.get("candidate_ids") or ()) != self.candidate_ids
            or tuple(handoff.get("targets") or ()) != self.target_ids
            or handoff.get("thresholds_digest") != self.threshold_digest
            or handoff.get("protocol_identity") != self.protocol_identity
        ):
            raise ExplorationDecisionContractError("Prediction handoff scope is inconsistent")
        for source in sources:
            if (
                source.get("agent") != "prediction"
                or source.get("phase") != "evaluate"
                or source.get("thresholds_digest") != self.threshold_digest
            ):
                raise ExplorationDecisionContractError("source threshold identity is inconsistent")
        handoff_binding = handoff.get("calibration_binding")
        source_bindings = [source.get("calibration_binding") for source in sources]
        if handoff_binding is not None or any(
            binding is not None for binding in source_bindings
        ):
            if not isinstance(handoff_binding, Mapping) or any(
                binding != handoff_binding for binding in source_bindings
            ):
                raise ExplorationDecisionContractError(
                    "Prediction calibration binding is inconsistent"
                )
        if (
            shortlist.get("agent") != "critic"
            or shortlist.get("phase") != "critic"
        ):
            raise ExplorationDecisionContractError("shortlist authority is inconsistent")
        protocol = self.protocol_identity
        if not isinstance(protocol, Mapping):
            raise ExplorationDecisionContractError("protocol_identity must be an object")
        for key in ("name", "version", "sha256"):
            if not isinstance(protocol.get(key), str) or not protocol[key]:
                raise ExplorationDecisionContractError(f"protocol_identity.{key} is required")
        _require_digest(protocol["sha256"], "protocol_identity.sha256")

    def _policy_envelope(self) -> dict[str, Any]:
        return {
            "project_id": self.evidence_support.get("project_id"),
            "approval_digest": self.evidence_support.get("approval_digest"),
            "allowed_lengths_by_target": _thaw(
                self.evidence_support.get("allowed_lengths_by_target")
            ),
            "effective_allowed_lengths": _thaw(
                self.evidence_support.get("effective_allowed_lengths")
            ),
        }

    def _input_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "workflow_id": self.workflow_id,
            "run_id": self.run_id,
            "source_round": self.source_round,
            "applies_to_round": self.applies_to_round,
            "prediction_run_id": self.prediction_run_id,
            "prediction_handoff_id": self.prediction_handoff_id,
            "candidate_ids": list(self.candidate_ids),
            "target_ids": list(self.target_ids),
            "source_evidence": _thaw(self.evidence_support.get("source_evidence")),
            "shortlist_evidence": _thaw(
                self.evidence_support.get("shortlist_evidence")
            ),
            "prediction_handoff_evidence": _thaw(
                self.evidence_support.get("prediction_handoff_evidence")
            ),
            "policy_envelope": self._policy_envelope(),
            "policy": _thaw(self.evidence_support.get("policy")),
            "threshold_digest": self.threshold_digest,
            "protocol_identity": _thaw(self.protocol_identity),
        }

    def _validate_scientific_support(
        self, proposed: tuple[int, ...], baseline: tuple[int, ...]
    ) -> None:
        policy = self.evidence_support.get("policy")
        params = policy.get("parameters") if isinstance(policy, Mapping) else None
        from experience import length_preference_policy

        if not isinstance(policy, Mapping) or not isinstance(params, Mapping):
            raise ExplorationDecisionContractError("length preference policy is invalid")
        try:
            expected_policy = length_preference_policy(
                str(policy.get("name")), str(policy.get("version"))
            )
        except ValueError as exc:
            raise ExplorationDecisionContractError(
                "unsupported length preference policy"
            ) from exc
        if _thaw(policy) != expected_policy.to_dict():
            raise ExplorationDecisionContractError("unsupported length preference policy")
        minimum = params.get("minimum_evaluations_per_length")
        worst_limit = params.get("worst_failure_rate")
        better_limit = params.get("better_failure_rate")
        if (
            minimum != expected_policy.minimum_evaluations_per_length
            or worst_limit != expected_policy.worst_failure_rate
            or better_limit != expected_policy.better_failure_rate
        ):
            raise ExplorationDecisionContractError("unsupported length preference policy")
        statistics = self.evidence_support.get("length_statistics")
        if not isinstance(statistics, (list, tuple)) or len(statistics) != len(baseline):
            raise ExplorationDecisionContractError("length support statistics are incomplete")
        observed = {length: {"n": 0, "failed": 0} for length in baseline}
        for source in self.evidence_support.get("source_evidence") or ():
            length = source.get("length") if isinstance(source, Mapping) else None
            if length not in observed or not isinstance(source.get("passed"), bool):
                raise ExplorationDecisionContractError("source length support is invalid")
            observed[length]["n"] += 1
            if not source["passed"]:
                observed[length]["failed"] += 1
        for item, length in zip(statistics, baseline):
            if not isinstance(item, Mapping) or item.get("length") != length:
                raise ExplorationDecisionContractError("length statistics are inconsistent")
            n, failed = item.get("n"), item.get("failed")
            if (
                isinstance(n, bool) or not isinstance(n, int) or n < 0
                or isinstance(failed, bool) or not isinstance(failed, int)
                or failed < 0 or failed > n
            ):
                raise ExplorationDecisionContractError("length statistics are invalid")
            if {"n": n, "failed": failed} != observed[length]:
                raise ExplorationDecisionContractError("length statistics lack source support")
            expected_rate = failed / n if n else None
            if item.get("failure_rate") != expected_rate or item.get("eligible") != (n >= minimum):
                raise ExplorationDecisionContractError("length statistics are inconsistent")
        summary_lengths = self.failure_summary.get("lengths")
        if not isinstance(summary_lengths, Mapping) or {
            str(length): observed[length] for length in baseline if observed[length]["n"]
        } != _thaw(summary_lengths):
            raise ExplorationDecisionContractError("failure summary length support is inconsistent")
        from experience import suggest_length_preference

        hint = suggest_length_preference(
            _thaw(self.failure_summary), policy=expected_policy
        )
        expected = hint["lengths"][0] if hint is not None else None
        if self.decision_status == "adjustment" and proposed != (expected,):
            raise ExplorationDecisionContractError("adjustment lacks conservative support")
        if self.decision_status == "no_adjustment" and expected is not None:
            raise ExplorationDecisionContractError("supported adjustment cannot be suppressed")
        self._validate_canonical_analysis(expected, expected_policy)

    def _validate_canonical_analysis(self, expected_length: int | None, policy) -> None:
        # Lazy import avoids a module-import cycle while keeping one scientific
        # implementation authoritative for legacy experience and E2.
        from experience import (
            no_length_adjustment_reason,
            summarize_failures,
            suggest_length_preference,
        )

        rows = _thaw(self.evidence_support.get("source_evidence"))
        expected_summary = summarize_failures(events=rows)
        expected_summary["lengths"] = {
            key: expected_summary["lengths"][key]
            for key in sorted(expected_summary.get("lengths") or {}, key=int)
        }
        if _thaw(self.failure_summary) != expected_summary:
            raise ExplorationDecisionContractError("failure_summary is not source-derived")
        hint = suggest_length_preference(expected_summary, policy=policy)
        if expected_length is None:
            expected_status = "no_adjustment"
            expected_reason = no_length_adjustment_reason(policy)
            expected_preferred = []
        else:
            if hint is None or hint.get("lengths") != [expected_length]:
                raise ExplorationDecisionContractError("adjustment policy is inconsistent")
            expected_status = "adjustment"
            expected_reason = hint["reason"]
            expected_preferred = [expected_length]
        if (
            self.decision_status != expected_status
            or self.reason != expected_reason
            or _thaw(self.adjustment.get("preferred_lengths")) != expected_preferred
        ):
            raise ExplorationDecisionContractError("decision output is not source-derived")

    def to_dict(self) -> dict[str, Any]:
        return {field.name: _thaw(getattr(self, field.name)) for field in fields(self)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExplorationDecision":
        if not isinstance(value, Mapping):
            raise ExplorationDecisionContractError("decision must be an object")
        names = {field.name for field in fields(cls)}
        missing = names - set(value)
        if missing:
            raise ExplorationDecisionContractError(
                f"decision missing fields: {sorted(missing)}"
            )
        payload = {name: value[name] for name in names}
        for name in ("candidate_ids", "target_ids", "source_event_ids"):
            payload[name] = tuple(payload[name])
        return cls(**payload)
