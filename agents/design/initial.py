"""Recoverable public boundary for the Launcher's initial Design invocation.

The receipts in this module are formal observations owned by Design.  They do
not introduce task or workflow state; the existing Store remains the evidence
authority and the existing route remains the scientific implementation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from contracts.event import EvidenceEvent
from project_config import target_slug

from .config import (
    DESIGN_PIPELINE_VERSION,
    DESIGN_PROTOCOL_SHA256,
)


DESIGN_INITIAL_STARTED = "design_initial_invocation_started"
DESIGN_INITIAL_COMPLETED = "design_initial_completion"
DESIGN_RECOVERY_AMBIGUOUS = "design_recovery_ambiguous"
INITIAL_DESIGN_CONTRACT_GAP = "initial_design_contract_gap"


def design_initial_invocation_id(launcher_run_id: str) -> str:
    """Map one Launcher UUID namespace to its fixed initial Design namespace."""
    if not isinstance(launcher_run_id, str) or not launcher_run_id.startswith("launcher_"):
        raise ValueError("launcher_run_id must use the launcher_<uuid-payload> namespace")
    payload = launcher_run_id.removeprefix("launcher_")
    try:
        parsed = uuid.UUID(payload)
    except (ValueError, AttributeError) as exc:
        raise ValueError(
            "launcher_run_id must use the launcher_<uuid-payload> namespace"
        ) from exc
    if payload not in {parsed.hex, str(parsed)}:
        raise ValueError(
            "launcher_run_id must use a canonical lowercase UUID payload"
        )
    return f"design_initial_{payload}"


@dataclass(frozen=True)
class InitialDesignCorrelation:
    """Required binding for one launcher-correlated initial Design call."""

    design_invocation_id: str
    launcher_run_id: str
    project_id: str
    approved_content_binding: str

    def __post_init__(self) -> None:
        expected = design_initial_invocation_id(self.launcher_run_id)
        if self.design_invocation_id != expected:
            raise ValueError(
                "design_invocation_id does not match the fixed launcher namespace"
            )
        for name in ("project_id", "approved_content_binding"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")

    @classmethod
    def from_launcher(
        cls,
        *,
        launcher_run_id: str,
        project_id: str,
        approved_content_binding: str,
    ) -> "InitialDesignCorrelation":
        return cls(
            design_invocation_id=design_initial_invocation_id(launcher_run_id),
            launcher_run_id=launcher_run_id,
            project_id=project_id,
            approved_content_binding=approved_content_binding,
        )

    def to_payload(self) -> dict[str, str]:
        return {
            "design_invocation_id": self.design_invocation_id,
            "launcher_run_id": self.launcher_run_id,
            "project_id": self.project_id,
            "approved_content_binding": self.approved_content_binding,
        }


class InitialDesignContractError(RuntimeError):
    """Stable structured blocker raised before an unsafe Design invocation."""

    component = "design"

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class InitialDesignValidation:
    """Read-only formal recovery result returned by the Design validator."""

    status: str
    design_invocation_id: str
    start_event_id: str | None = None
    completion_event_id: str | None = None
    jobs: tuple[dict[str, Any], ...] = ()
    candidate_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    blocker_code: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class InitialDesignResult:
    """Formal references from a completed initial Design invocation."""

    status: str
    design_invocation_id: str
    start_event_id: str
    completion_event_id: str
    jobs: tuple[dict[str, Any], ...]
    candidate_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event(
    event_type: str,
    payload: Mapping[str, Any],
    *,
    targets: tuple[str, ...],
) -> dict[str, Any]:
    return EvidenceEvent(
        timestamp=_utcnow(),
        event_id=uuid.uuid4().hex,
        agent="design",
        event_type=event_type,
        payload=dict(payload),
        phase="design",
        targets=targets,
    ).to_dict()


def _store_for(store):
    if store is not None:
        return store
    # Keep importing ``agents.design`` compatible with legacy callers that
    # provide only the historical data_layer surface.  The new Store seam is
    # needed only when the new initial boundary is actually invoked.
    from data_layer import get_storage_backend
    return get_storage_backend()


def _assert_store_project(store, project_id: str) -> None:
    stored_project = getattr(store, "project_id", project_id)
    if stored_project != project_id:
        raise InitialDesignContractError(
            INITIAL_DESIGN_CONTRACT_GAP,
            "Design Store project does not match the invocation binding",
        )


def _conflict(correlation: InitialDesignCorrelation, message: str) -> InitialDesignValidation:
    return InitialDesignValidation(
        status="conflict",
        design_invocation_id=correlation.design_invocation_id,
        blocker_code=DESIGN_RECOVERY_AMBIGUOUS,
        message=message,
    )


def _matches_correlation(event: Mapping[str, Any], correlation: InitialDesignCorrelation) -> bool:
    return (
        event.get("design_invocation_id") == correlation.design_invocation_id
        or event.get("launcher_run_id") == correlation.launcher_run_id
    )


def _has_binding(event: Mapping[str, Any], correlation: InitialDesignCorrelation) -> bool:
    return all(event.get(key) == value for key, value in correlation.to_payload().items())


def _string_tuple(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        return None
    if len(value) != len(set(value)):
        return None
    return tuple(value)


def validate_initial_invocation(
    correlation: InitialDesignCorrelation,
    *,
    store=None,
) -> InitialDesignValidation:
    """Resolve Design recovery exclusively from exact Store-backed receipts."""
    formal_store = _store_for(store)
    _assert_store_project(formal_store, correlation.project_id)
    starts = [
        event
        for event in formal_store.query(
            project_id=correlation.project_id,
            agent="design",
            event_type=DESIGN_INITIAL_STARTED,
        )
        if _matches_correlation(event, correlation)
    ]
    completions = [
        event
        for event in formal_store.query(
            project_id=correlation.project_id,
            agent="design",
            event_type=DESIGN_INITIAL_COMPLETED,
        )
        if _matches_correlation(event, correlation)
    ]
    if not starts and not completions:
        return InitialDesignValidation(
            status="not_started",
            design_invocation_id=correlation.design_invocation_id,
        )
    if len(starts) != 1:
        return _conflict(correlation, "initial Design start receipt is missing or non-unique")
    start = starts[0]
    start_jobs = start.get("jobs")
    if (
        not _has_binding(start, correlation)
        or not isinstance(start_jobs, list)
        or not start_jobs
        or any(not isinstance(job, Mapping) for job in start_jobs)
    ):
        return _conflict(correlation, "initial Design start binding or jobs are invalid")
    if not completions:
        return InitialDesignValidation(
            status="started_without_completion",
            design_invocation_id=correlation.design_invocation_id,
            start_event_id=start.get("event_id"),
            jobs=tuple(dict(job) for job in start_jobs),
            blocker_code=DESIGN_RECOVERY_AMBIGUOUS,
            message="initial Design started without a valid completion receipt",
        )
    if len(completions) != 1:
        return _conflict(correlation, "initial Design completion receipt is non-unique")
    completion = completions[0]
    if not _has_binding(completion, correlation):
        return _conflict(correlation, "initial Design completion binding is invalid")
    if completion.get("jobs") != start.get("jobs"):
        return _conflict(correlation, "initial Design job binding changed after start")

    candidate_ids = _string_tuple(completion.get("candidate_ids"))
    artifact_ids = _string_tuple(completion.get("artifact_ids"))
    evidence_ids = _string_tuple(completion.get("evidence_ids"))
    if candidate_ids is None or artifact_ids is None or evidence_ids is None:
        return _conflict(correlation, "initial Design formal reference lists are invalid")
    if start.get("event_id") not in evidence_ids:
        return _conflict(correlation, "initial Design completion does not reference its start")
    if any(formal_store.get(candidate_id) is None for candidate_id in candidate_ids):
        return _conflict(correlation, "initial Design candidate reference is missing")
    if any(formal_store.get_artifact(artifact_id) is None for artifact_id in artifact_ids):
        return _conflict(correlation, "initial Design artifact reference is missing")
    known_evidence = {
        event.get("event_id")
        for event in formal_store.query(
            project_id=correlation.project_id, agent="design"
        )
    }
    if any(evidence_id not in known_evidence for evidence_id in evidence_ids):
        return _conflict(correlation, "initial Design evidence reference is missing")

    return InitialDesignValidation(
        status="completed",
        design_invocation_id=correlation.design_invocation_id,
        start_event_id=start["event_id"],
        completion_event_id=completion["event_id"],
        jobs=tuple(dict(job) for job in completion["jobs"]),
        candidate_ids=candidate_ids,
        artifact_ids=artifact_ids,
        evidence_ids=evidence_ids,
    )


def _result_from_validation(validation: InitialDesignValidation) -> InitialDesignResult:
    if (
        validation.status != "completed"
        or validation.start_event_id is None
        or validation.completion_event_id is None
    ):
        raise ValueError("completed Design validation is required")
    return InitialDesignResult(
        status="completed",
        design_invocation_id=validation.design_invocation_id,
        start_event_id=validation.start_event_id,
        completion_event_id=validation.completion_event_id,
        jobs=validation.jobs,
        candidate_ids=validation.candidate_ids,
        artifact_ids=validation.artifact_ids,
        evidence_ids=validation.evidence_ids,
    )


def materialize_initial_jobs(design) -> tuple[dict[str, Any], ...]:
    """Resolve the one safe generic v1 job without executing a Design route."""
    project = design.project_config
    targets = project.get("targets") if isinstance(project, Mapping) else None
    if not isinstance(targets, list) or not targets:
        raise InitialDesignContractError(
            INITIAL_DESIGN_CONTRACT_GAP,
            "approved project has no unambiguous initial Design target",
        )
    try:
        config = design.merge_config()
    except (KeyError, ValueError, RuntimeError, OSError) as exc:
        raise InitialDesignContractError(
            INITIAL_DESIGN_CONTRACT_GAP,
            f"initial Design job cannot be materialized: {exc}",
        ) from exc
    job = {
        "job_id": f"initial_route_A_{target_slug(config['target_id'])}",
        "route": "A",
        "target_id": config["target_id"],
        "config": {
            "modality": config["modality"],
            "target_id": config["target_id"],
            "target_pdb_sha256": config.get("target_pdb_sha256"),
            "chain": config["chain"],
            "hotspots": config["hotspots"],
            "lengths": list(config["lengths"]),
            "n": config["n"],
            "seed": config["seed"],
            "design_pipeline_version": DESIGN_PIPELINE_VERSION,
            "design_protocol_sha256": DESIGN_PROTOCOL_SHA256,
        },
    }
    return (job,)


def _formal_references(
    store,
    candidates: list[Mapping[str, Any]],
    start_event_id: str,
    *,
    project_id: str,
):
    candidate_ids = tuple(str(candidate["candidate_id"]) for candidate in candidates)
    if len(candidate_ids) != len(set(candidate_ids)):
        raise InitialDesignContractError(
            DESIGN_RECOVERY_AMBIGUOUS,
            "initial Design returned duplicate candidate identities",
        )
    artifact_ids: list[str] = []
    for candidate in candidates:
        candidate_artifacts = candidate.get("artifact_ids") or []
        if candidate.get("artifact_id"):
            candidate_artifacts = [*candidate_artifacts, candidate["artifact_id"]]
        artifact_ids.extend(str(value) for value in candidate_artifacts)
    artifact_ids = list(dict.fromkeys(artifact_ids))

    candidate_events = []
    for event in store.query(
        project_id=project_id,
        agent="design",
        event_type="candidate_registered",
    ):
        candidate = event.get("candidate")
        if isinstance(candidate, Mapping) and candidate.get("candidate_id") in candidate_ids:
            candidate_events.append(event["event_id"])
    evidence_ids = tuple(dict.fromkeys([start_event_id, *candidate_events]))
    return candidate_ids, tuple(artifact_ids), evidence_ids


def run_initial(design, correlation: InitialDesignCorrelation, *, store=None) -> InitialDesignResult:
    """Execute the existing generic route once, guarded by durable receipts."""
    formal_store = _store_for(store)
    _assert_store_project(formal_store, correlation.project_id)
    if design.project_config.get("project_id") != correlation.project_id:
        raise InitialDesignContractError(
            INITIAL_DESIGN_CONTRACT_GAP,
            "Design context project does not match the invocation binding",
        )
    approved = (design.project_config.get("review") or {}).get("approved_digest")
    if approved != correlation.approved_content_binding:
        raise InitialDesignContractError(
            INITIAL_DESIGN_CONTRACT_GAP,
            "Design approved-content binding does not match the invocation",
        )

    recovery = validate_initial_invocation(correlation, store=formal_store)
    if recovery.status == "completed":
        return _result_from_validation(recovery)
    if recovery.status != "not_started":
        raise InitialDesignContractError(
            recovery.blocker_code or DESIGN_RECOVERY_AMBIGUOUS,
            recovery.message or "initial Design recovery is ambiguous",
        )

    jobs = materialize_initial_jobs(design)
    targets = tuple(job["target_id"] for job in jobs)
    start_payload = {**correlation.to_payload(), "jobs": list(jobs)}
    start_event_id = formal_store.append(
        _event(DESIGN_INITIAL_STARTED, start_payload, targets=targets)
    )

    candidates: list[Mapping[str, Any]] = []
    for job in jobs:
        controls = job["config"]
        result = design.design_rfpeptides(
            target_spec={"target_id": job["target_id"]},
            design_config={
                "lengths": list(controls["lengths"]),
                "n": controls["n"],
                "seed": controls["seed"],
            },
        )
        candidates.extend(result)

    candidate_ids, artifact_ids, evidence_ids = _formal_references(
        formal_store,
        candidates,
        start_event_id,
        project_id=correlation.project_id,
    )
    completion_payload = {
        **correlation.to_payload(),
        "jobs": list(jobs),
        "candidate_ids": list(candidate_ids),
        "artifact_ids": list(artifact_ids),
        "evidence_ids": list(evidence_ids),
    }
    completion_event_id = formal_store.append(
        _event(DESIGN_INITIAL_COMPLETED, completion_payload, targets=targets)
    )
    validation = validate_initial_invocation(correlation, store=formal_store)
    if validation.status != "completed":
        raise InitialDesignContractError(
            validation.blocker_code or DESIGN_RECOVERY_AMBIGUOUS,
            validation.message or "initial Design completion could not be validated",
        )
    if validation.completion_event_id != completion_event_id:
        raise InitialDesignContractError(
            DESIGN_RECOVERY_AMBIGUOUS,
            "initial Design completion receipt is not unique",
        )
    return _result_from_validation(validation)
