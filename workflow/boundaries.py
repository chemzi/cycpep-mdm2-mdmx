"""Read-only composition of formal Workflow Launcher boundaries.

The inspectors in this module never authorize a transition.  They translate
formal, owner-specific validation results into one small vocabulary used by
the Launcher application service: ``not_started``, ``completed``, or
``blocked``.  In particular, no result is inferred from diagnostics, phase
labels, directory enumeration, stdout, or CandidateIndex presence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from contracts.plan import validate_plan_for_approval
from prediction_pipeline.contracts import file_sha256


@dataclass(frozen=True)
class FormalBoundary:
    """One read-only observation from a formal owner."""

    status: str
    boundary: str
    blocker_code: str | None = None
    message: str | None = None
    references: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def not_started(cls, boundary: str) -> "FormalBoundary":
        return cls(status="not_started", boundary=boundary)

    @classmethod
    def completed(cls, boundary: str, **references: Any) -> "FormalBoundary":
        return cls(status="completed", boundary=boundary, references=references)

    @classmethod
    def blocked(
        cls, boundary: str, code: str, message: str, **references: Any
    ) -> "FormalBoundary":
        return cls(
            status="blocked",
            boundary=boundary,
            blocker_code=code,
            message=message,
            references=references,
        )


class FormalBoundaryInspector:
    """Compose public owner validators with exact Store-backed lookups."""

    def __init__(
        self,
        *,
        store: Any,
        research_validator: Callable[..., Any],
        design_validator: Callable[..., Any],
        prediction_validator: Callable[..., Any],
        orchestrator_status: Callable[..., Mapping[str, Any]],
    ) -> None:
        self.store = store
        self._research_validator = research_validator
        self._design_validator = design_validator
        self._prediction_validator = prediction_validator
        self._orchestrator_status = orchestrator_status

    def research(self, correlation: Any) -> FormalBoundary:
        value = self._research_validator(correlation, store=self.store)
        return self._owner_validation("research", value, "research_completion_ambiguous")

    def design(self, correlation: Any) -> FormalBoundary:
        value = self._design_validator(correlation, store=self.store)
        return self._owner_validation("design", value, "design_recovery_ambiguous")

    def prediction(self, correlation: Any, *, expected_inputs: Any = None) -> FormalBoundary:
        value = self._prediction_validator(
            correlation, store=self.store, expected_inputs=expected_inputs
        )
        return self._owner_validation(
            "prediction", value, "prediction_recovery_ambiguous"
        )

    @staticmethod
    def _owner_validation(
        boundary: str, value: Any, default_blocker: str
    ) -> FormalBoundary:
        status = getattr(value, "status", None)
        references = {
            name: getattr(value, name)
            for name in (
                "start_event_id",
                "completion_event_id",
                "research_evidence_ids",
                "candidate_ids",
                "artifact_ids",
                "evidence_ids",
                "prediction_invocation_id",
                "prediction_run_id",
                "run_root",
                "handoff_path",
            )
            if getattr(value, name, None) not in (None, (), [])
        }
        if status == "not_started":
            return FormalBoundary.not_started(boundary)
        if status == "completed":
            return FormalBoundary.completed(boundary, **references)
        code = getattr(value, "blocker_code", None) or default_blocker
        message = getattr(value, "message", None) or (
            f"{boundary} formal state is partial, conflicting, or unverifiable"
        )
        return FormalBoundary.blocked(boundary, code, message, **references)

    def critic(self, *, project_id: str, prediction_run_id: str) -> FormalBoundary:
        matches: list[tuple[dict[str, Any], dict[str, Any], Path]] = []
        invalid = False
        for event in self.store.query(
            project_id=project_id, agent="critic", event_type="critic_review"
        ):
            path = _formal_path(event, self.store, "report_path", "report_artifact_id")
            document = _read_json_object(path)
            if document is None:
                invalid = invalid or event.get("report_id") is not None
                continue
            source = document.get("source") or {}
            if source.get("prediction_run_id") != prediction_run_id:
                continue
            if not _event_binds_document(event, document, path, "report"):
                invalid = True
                continue
            matches.append((event, document, path))
        return _unique_document_boundary(
            "critic", matches, invalid, "critic_recovery_ambiguous", "report"
        )

    def planner(self, *, project_id: str, critic_report_id: str) -> FormalBoundary:
        matches: list[tuple[dict[str, Any], dict[str, Any], Path]] = []
        invalid = False
        for event in self.store.query(
            project_id=project_id, agent="planner", event_type="planner_plan"
        ):
            if event.get("critic_report_id") != critic_report_id:
                continue
            path = _formal_path(event, self.store, "plan_path", "plan_artifact_id")
            document = _read_json_object(path)
            if document is None:
                invalid = True
                continue
            try:
                validate_plan_for_approval(document, path)
            except (ValueError, OSError):
                invalid = True
                continue
            source = document.get("source") or {}
            if (
                source.get("project_id") != project_id
                or source.get("critic_report_id") != critic_report_id
                or not _event_binds_document(event, document, path, "plan")
            ):
                invalid = True
                continue
            matches.append((event, document, path))
        return _unique_document_boundary(
            "planner", matches, invalid, "planner_recovery_ambiguous", "plan"
        )

    def approvals(
        self, *, project_id: str, plan_id: str, plan_sha256: str
    ) -> FormalBoundary:
        events = [
            event
            for event in self.store.query(
                project_id=project_id,
                agent="planner",
                event_type="planner_approval_recorded",
            )
            if event.get("plan_id") == plan_id
        ]
        conflicting = [event for event in events if event.get("plan_sha256") != plan_sha256]
        valid = [event for event in events if event.get("plan_sha256") == plan_sha256]
        if conflicting:
            return FormalBoundary.blocked(
                "approval",
                "approval_binding_conflict",
                "formal approval records conflict with the immutable plan",
            )
        if not valid:
            return FormalBoundary.not_started("approval")
        return FormalBoundary.completed(
            "approval",
            approval_ids=tuple(event.get("approval_id") for event in valid),
            approval_paths=tuple(event.get("approval_path") for event in valid),
        )

    def orchestrator(self, *, run_path: str | Path) -> FormalBoundary:
        try:
            result = self._orchestrator_status(run_path=run_path)
            run = result["run"]
        except (KeyError, OSError, ValueError) as exc:
            return FormalBoundary.blocked(
                "orchestrator",
                getattr(exc, "code", "orchestrator_status_unavailable"),
                "formal Orchestrator status could not be validated",
            )
        return FormalBoundary.completed(
            "orchestrator",
            run_path=str(result["run_path"]),
            run_id=run.get("run_id"),
            workflow_id=run.get("workflow_id"),
            plan_id=(run.get("plan") or {}).get("plan_id"),
            formal_status=run.get("status"),
            summary=result.get("summary") or {},
        )

    def orchestrator_for_plan(
        self, *, project_id: str, plan_id: str
    ) -> FormalBoundary:
        """Resolve an initialized run by its formal plan-bound Evidence."""
        events = [
            event
            for event in self.store.query(
                project_id=project_id,
                agent="orchestrator",
                event_type="orchestrator_run_initialized",
            )
            if event.get("plan_id") == plan_id
        ]
        if not events:
            return FormalBoundary.not_started("orchestrator")
        identities = {
            (event.get("run_id"), event.get("run_path")) for event in events
        }
        if len(identities) != 1 or None in next(iter(identities)):
            return FormalBoundary.blocked(
                "orchestrator",
                "orchestrator_recovery_ambiguous",
                "formal Orchestrator initialization records conflict",
            )
        run_id, run_path = next(iter(identities))
        result = self.orchestrator(run_path=run_path)
        if result.status == "completed" and result.references.get("run_id") != run_id:
            return FormalBoundary.blocked(
                "orchestrator",
                "orchestrator_recovery_ambiguous",
                "formal Orchestrator run identity conflicts with its Evidence",
            )
        return result

    def transactions(self, *, run_id: str) -> FormalBoundary:
        """Report unresolved transactions for a run without performing recovery."""
        records = [
            transaction
            for transaction in self.store.list_transactions()
            if _transaction_run_id(transaction) == run_id
        ]
        unresolved = [
            item
            for item in records
            if item.get("status") not in {"COMMITTED", "ROLLED_BACK", "FAILED"}
        ]
        if unresolved:
            return FormalBoundary.blocked(
                "transaction",
                "transaction_recovery_unresolved",
                "one or more formal transactions require owner recovery",
                transaction_ids=tuple(item.get("transaction_id") for item in unresolved),
            )
        return FormalBoundary.completed(
            "transaction",
            transaction_ids=tuple(item.get("transaction_id") for item in records),
        )

    def execution_failure(self, *, run_id: str) -> FormalBoundary:
        """Return the latest formal Worker failure trace, when one exists."""
        events = self.store.query(
            run_id=run_id, agent="execution", event_type="execution_task_failed"
        )
        if not events:
            return FormalBoundary.not_started("execution")
        event = events[-1]
        return FormalBoundary.completed(
            "execution",
            evidence_id=event.get("event_id"),
            workflow_id=event.get("workflow_id"),
            run_id=event.get("run_id"),
            plan_id=event.get("plan_id"),
            task_id=event.get("task_id"),
            attempt_id=event.get("attempt_id"),
            transaction_id=event.get("transaction_id"),
            formal_status="failed",
        )


def _transaction_run_id(transaction: Mapping[str, Any]) -> Any:
    context = transaction.get("context") or transaction.get("payload") or {}
    metadata = context.get("metadata") if isinstance(context, Mapping) else {}
    return (
        transaction.get("run_id")
        or (context.get("run_id") if isinstance(context, Mapping) else None)
        or (metadata.get("run_id") if isinstance(metadata, Mapping) else None)
    )


def _formal_path(
    event: Mapping[str, Any], store: Any, path_key: str, artifact_key: str
) -> Path | None:
    artifact_id = event.get(artifact_key)
    if artifact_id:
        artifact = store.get_artifact(str(artifact_id))
        return Path(str(artifact["path"])).expanduser().resolve() if artifact else None
    value = event.get(path_key)
    return Path(str(value)).expanduser().resolve() if value else None


def _read_json_object(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _event_binds_document(
    event: Mapping[str, Any], document: Mapping[str, Any], path: Path, prefix: str
) -> bool:
    return (
        event.get(f"{prefix}_id") == document.get(f"{prefix}_id")
        and event.get(f"{prefix}_sha256") == file_sha256(path)
    )


def _unique_document_boundary(
    boundary: str,
    matches: Iterable[tuple[dict[str, Any], dict[str, Any], Path]],
    invalid: bool,
    blocker_code: str,
    prefix: str,
) -> FormalBoundary:
    values = list(matches)
    if invalid or len(values) > 1:
        return FormalBoundary.blocked(
            boundary,
            blocker_code,
            f"{boundary} formal records are missing, conflicting, or non-unique",
        )
    if not values:
        return FormalBoundary.not_started(boundary)
    event, document, path = values[0]
    return FormalBoundary.completed(
        boundary,
        **{
            f"{prefix}_id": document[f"{prefix}_id"],
            f"{prefix}_path": str(path),
            f"{prefix}_sha256": event[f"{prefix}_sha256"],
            f"{prefix}_document": document,
            "evidence_id": event.get("event_id"),
        },
    )


__all__ = ["FormalBoundary", "FormalBoundaryInspector"]
