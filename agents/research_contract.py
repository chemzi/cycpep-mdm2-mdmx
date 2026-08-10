"""Public Research invocation receipts used by coordination callers.

These receipts live in the existing Evidence Store.  They record correlation
and completion proof; they do not introduce Research task or workflow state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from data_layer import EvidenceLogger, get_storage_backend
from target_bootstrap import assert_project_approved


@dataclass(frozen=True)
class ResearchCorrelation:
    research_invocation_id: str
    launcher_run_id: str
    project_id: str
    approved_content_binding: str

    def __post_init__(self) -> None:
        for field_name in (
            "research_invocation_id",
            "launcher_run_id",
            "project_id",
            "approved_content_binding",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")

    def to_payload(self) -> dict[str, str]:
        return {
            "research_invocation_id": self.research_invocation_id,
            "launcher_run_id": self.launcher_run_id,
            "project_id": self.project_id,
            "approved_content_binding": self.approved_content_binding,
        }

    @classmethod
    def from_value(
        cls, value: "ResearchCorrelation | Mapping[str, Any]"
    ) -> "ResearchCorrelation":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("correlation must be ResearchCorrelation or a mapping")
        return cls(
            research_invocation_id=value.get("research_invocation_id"),
            launcher_run_id=value.get("launcher_run_id"),
            project_id=value.get("project_id"),
            approved_content_binding=value.get("approved_content_binding"),
        )


@dataclass(frozen=True)
class ResearchRunResult:
    result: Any
    receipt_event_id: str
    research_evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class ResearchInvocationStatus:
    status: str
    start_event_id: str | None = None
    completion_event_id: str | None = None
    research_evidence_ids: tuple[str, ...] = ()
    blocker_code: str | None = None


class ResearchInvocationBlocked(RuntimeError):
    """The formal receipts do not authorize a new Research invocation."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def run_with_receipt(
    state=None,
    force_recompute=False,
    skip_pipeline=False,
    project_config=None,
    *,
    correlation,
) -> ResearchRunResult:
    """Run Research once with durable Launcher correlation receipts."""
    from agents import research as research_agent

    config = (
        project_config
        if project_config is not None
        else research_agent._get_project_config()
    )
    assert_project_approved(config)
    expected = ResearchCorrelation.from_value(correlation)
    approved_binding = (config.get("review") or {}).get("approved_digest")
    if (
        expected.project_id != config.get("project_id")
        or expected.approved_content_binding != approved_binding
    ):
        raise ValueError("Research correlation does not match the approved project")

    current = validate_research_invocation(expected)
    if current.status != "not_started":
        raise ResearchInvocationBlocked(
            current.blocker_code or "research_already_started",
            f"Research invocation is not safe to start: {current.status}",
        )

    previous = research_agent._injected_project_config
    if project_config is not None:
        research_agent._injected_project_config = project_config
    try:
        append_start_receipt(expected)
        research_evidence_ids = []
        result = research_agent._run_impl(
            state=state,
            force_recompute=force_recompute,
            skip_pipeline=skip_pipeline,
            receipt_evidence_ids=research_evidence_ids,
        )
        formal_ids = tuple(research_evidence_ids)
        receipt_event_id = append_completion_receipt(expected, formal_ids)
        return ResearchRunResult(
            result=result,
            receipt_event_id=receipt_event_id,
            research_evidence_ids=formal_ids,
        )
    finally:
        research_agent._injected_project_config = previous


def append_start_receipt(correlation: ResearchCorrelation) -> str:
    return EvidenceLogger.log(
        "research",
        "research_invocation_started",
        correlation.to_payload(),
        phase="research",
    )


def append_completion_receipt(
    correlation: ResearchCorrelation, research_evidence_ids: tuple[str, ...]
) -> str:
    if not research_evidence_ids:
        raise ValueError("Research completion requires formal Research Evidence IDs")
    return EvidenceLogger.log(
        "research",
        "research_completion_receipt",
        {
            **correlation.to_payload(),
            "research_evidence_ids": list(research_evidence_ids),
        },
        phase="research",
    )


def validate_research_invocation(
    correlation: ResearchCorrelation | Mapping[str, Any], *, store=None
) -> ResearchInvocationStatus:
    """Resolve one invocation exclusively from its formal Evidence receipts."""
    expected = ResearchCorrelation.from_value(correlation)
    backend = store or get_storage_backend()
    starts = _correlated_events(
        backend=backend,
        event_type="research_invocation_started",
        expected=expected,
    )
    completions = _correlated_events(
        backend=backend,
        event_type="research_completion_receipt",
        expected=expected,
    )
    if not starts:
        if completions:
            return ResearchInvocationStatus(
                status="conflicting", blocker_code="research_correlation_conflict"
            )
        return ResearchInvocationStatus(status="not_started")
    if len(starts) != 1 or not _binding_matches(starts[0], expected):
        return ResearchInvocationStatus(
            status="conflicting", blocker_code="research_correlation_conflict"
        )

    start = starts[0]
    if not completions:
        return ResearchInvocationStatus(
            status="started_without_completion",
            start_event_id=start["event_id"],
            blocker_code="research_completion_ambiguous",
        )
    if len(completions) != 1 or not _binding_matches(completions[0], expected):
        return ResearchInvocationStatus(
            status="conflicting",
            start_event_id=start["event_id"],
            blocker_code="research_correlation_conflict",
        )

    completion = completions[0]
    evidence_ids = completion.get("research_evidence_ids")
    if not _valid_research_evidence(
        backend, evidence_ids, project_id=expected.project_id
    ):
        return ResearchInvocationStatus(
            status="conflicting",
            start_event_id=start["event_id"],
            completion_event_id=completion["event_id"],
            blocker_code="research_completion_invalid",
        )
    return ResearchInvocationStatus(
        status="completed",
        start_event_id=start["event_id"],
        completion_event_id=completion["event_id"],
        research_evidence_ids=tuple(evidence_ids),
    )


def _correlated_events(*, backend, event_type: str, expected: ResearchCorrelation):
    events = backend.query(
        project_id=expected.project_id,
        agent="research",
        event_type=event_type,
    )
    return [
        event
        for event in events
        if event.get("research_invocation_id") == expected.research_invocation_id
        or event.get("launcher_run_id") == expected.launcher_run_id
    ]


def _binding_matches(event: Mapping[str, Any], expected: ResearchCorrelation) -> bool:
    return all(event.get(key) == value for key, value in expected.to_payload().items())


def _valid_research_evidence(
    backend, evidence_ids: Any, *, project_id: str
) -> bool:
    if (
        not isinstance(evidence_ids, list)
        or not evidence_ids
        or any(not isinstance(event_id, str) or not event_id for event_id in evidence_ids)
        or len(set(evidence_ids)) != len(evidence_ids)
    ):
        return False
    events_by_id = {
        event["event_id"]: event
        for event in backend.query(project_id=project_id)
        if event.get("project_id") == project_id
    }
    if getattr(backend, "project_id", None) == project_id:
        # Legacy Research Evidence predates explicit project_id payloads.  It
        # remains valid only through a Store instance already scoped to the
        # expected project; explicitly foreign rows are never accepted.
        events_by_id.update({
            event["event_id"]: event
            for event in backend.query()
            if event.get("project_id") is None
        })
    referenced = [events_by_id.get(event_id) for event_id in evidence_ids]
    return all(
        event is not None
        and event.get("agent") == "research"
        and event.get("event_type") == "research_targets"
        for event in referenced
    )
