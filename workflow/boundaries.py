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
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from contracts.plan import validate_plan_for_approval
from prediction_pipeline.contracts import file_sha256
from workflow.prediction_publication import (
    PredictionPublicationError,
    validate_prediction_publication,
)


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
                "transaction_id",
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
        events = self.store.query(
            project_id=project_id, agent="critic", event_type="critic_review"
        )
        explicit_current = [
            event
            for event in events
            if event.get("prediction_run_id") == prediction_run_id
        ]
        if explicit_current:
            return self._critic_events(
                explicit_current,
                project_id=project_id,
                prediction_run_id=prediction_run_id,
            )

        legacy = [event for event in events if "prediction_run_id" not in event]
        return self._critic_events(
            legacy,
            project_id=project_id,
            prediction_run_id=prediction_run_id,
            legacy=True,
            current_start=_unique_prediction_start_time(
                self.store,
                project_id=project_id,
                prediction_run_id=prediction_run_id,
            ),
        )

    def _critic_events(
        self,
        events: Iterable[Mapping[str, Any]],
        *,
        project_id: str,
        prediction_run_id: str,
        legacy: bool = False,
        current_start: datetime | None = None,
    ) -> FormalBoundary:
        matches: list[tuple[dict[str, Any], dict[str, Any], Path]] = []
        invalid = False
        for event in events:
            path = _formal_path(event, self.store, "report_path", "report_artifact_id")
            document = _read_json_object(path)
            if document is None:
                if legacy and _event_precedes(event, current_start):
                    continue
                invalid = True
                continue
            source = document.get("source") or {}
            if source.get("project_id") != project_id:
                invalid = True
                continue
            if source.get("prediction_run_id") != prediction_run_id:
                invalid = invalid or not legacy
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

    def bootstrap_prediction_plan(
        self,
        *,
        project_id: str,
        approved_content_binding: str,
        launcher_run_id: str,
        design_invocation_id: str,
        design_completion_event_id: str,
        design_transaction_id: str,
        candidate_ids: tuple[str, ...],
    ) -> FormalBoundary:
        """Resolve one formal pre-Critic Prediction plan for a Design completion."""
        matches: list[tuple[dict[str, Any], dict[str, Any], Path]] = []
        invalid = False
        for event in self.store.query(
            project_id=project_id, agent="planner", event_type="planner_plan"
        ):
            if event.get("source_kind") != "initial_prediction_bootstrap":
                continue
            if (
                event.get("launcher_run_id") != launcher_run_id
                or event.get("design_completion_event_id")
                != design_completion_event_id
            ):
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
                source.get("kind") != "initial_prediction_bootstrap"
                or source.get("project_id") != project_id
                or source.get("approved_content_binding")
                != approved_content_binding
                or source.get("launcher_run_id") != launcher_run_id
                or source.get("design_invocation_id") != design_invocation_id
                or source.get("design_completion_event_id")
                != design_completion_event_id
                or source.get("design_transaction_id") != design_transaction_id
                or source.get("candidate_ids") != sorted(candidate_ids)
                or event.get("candidate_ids") != source.get("candidate_ids")
                or event.get("design_transaction_id")
                != source.get("design_transaction_id")
                or event.get("execution_identity")
                != source.get("execution_identity")
                or event.get("retry") != source.get("retry")
                or not _event_binds_document(event, document, path, "plan")
            ):
                invalid = True
                continue
            matches.append((event, document, path))
        ordered = sorted(
            matches,
            key=lambda value: int(
                ((value[1].get("source") or {}).get("retry") or {})
                .get("retry_index") or 0
            ),
        )
        if ordered:
            indices = [
                int(((document.get("source") or {}).get("retry") or {}).get("retry_index") or 0)
                for _, document, _ in ordered
            ]
            if indices != list(range(len(ordered))):
                invalid = True
            immutable_source_keys = (
                "project_id", "approved_content_binding", "launcher_run_id",
                "research_completion_event_id", "design_invocation_id",
                "design_completion_event_id", "design_transaction_id",
                "candidate_ids",
            )
            initial_source = ordered[0][1].get("source") or {}
            for index in range(1, len(ordered)):
                retry = (ordered[index][1].get("source") or {}).get("retry") or {}
                current_source = ordered[index][1].get("source") or {}
                previous_plan = ordered[index - 1][1]
                expected_failure = {
                    "plan_id": retry.get("prior_plan_id"),
                    "workflow_id": previous_plan.get("workflow_id"),
                    "run_id": retry.get("prior_run_id"),
                    "task_id": retry.get("prior_task_id"),
                    "attempt_id": retry.get("prior_attempt_id"),
                    "transaction_id": retry.get("prior_transaction_id"),
                    "evidence_id": retry.get("failure_event_id"),
                }
                if (
                    retry.get("prior_plan_id") != previous_plan.get("plan_id")
                    or any(
                        current_source.get(key) != initial_source.get(key)
                        for key in immutable_source_keys
                    )
                    or not _valid_retry_failure(
                        self.store, previous_plan, expected_failure
                    )
                ):
                    invalid = True
        return _unique_document_boundary(
            "planner",
            ordered[-1:] if ordered else (),
            invalid,
            "bootstrap_plan_recovery_ambiguous",
            "plan",
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
            run_document=run,
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

    def execution_failure(
        self, *, run_id: str, failed_plan: Mapping[str, Any] | None = None
    ) -> FormalBoundary:
        """Return the latest formal Worker failure trace, when one exists."""
        events = self.store.query(
            run_id=run_id, agent="execution", event_type="execution_task_failed"
        )
        if not events:
            return FormalBoundary.not_started("execution")
        if len(events) != 1:
            return FormalBoundary.blocked(
                "execution",
                "execution_failure_recovery_ambiguous",
                "formal Worker failure proof is missing or non-unique",
            )
        event = events[0]
        if failed_plan is not None:
            from contracts.bootstrap_retry import (
                BootstrapRetryProofError,
                validate_bootstrap_retry_failure,
            )
            failure = {
                **{key: event.get(key) for key in (
                    "plan_id", "workflow_id", "run_id", "task_id", "attempt_id",
                    "transaction_id",
                )},
                "evidence_id": event.get("event_id"),
            }
            try:
                validate_bootstrap_retry_failure(
                    self.store, failed_plan=failed_plan, failure=failure
                )
            except BootstrapRetryProofError as exc:
                return FormalBoundary.blocked(
                    "execution", "execution_failure_recovery_ambiguous", str(exc)
                )
        return FormalBoundary.completed(
            "execution",
            evidence_id=event.get("event_id"),
            project_id=event.get("project_id"),
            workflow_id=event.get("workflow_id"),
            run_id=event.get("run_id"),
            plan_id=event.get("plan_id"),
            task_id=event.get("task_id"),
            attempt_id=event.get("attempt_id"),
            transaction_id=event.get("transaction_id"),
            action=event.get("action"),
            retryable=event.get("retryable"),
            formal_status="failed",
        )

    def prediction_execution(
        self,
        *,
        project_id: str,
        plan: Mapping[str, Any],
        orchestrator: FormalBoundary,
    ) -> FormalBoundary:
        """Validate Worker-owned bootstrap Prediction completion end to end."""
        context = _bootstrap_execution_context(project_id, plan, orchestrator)
        if isinstance(context, FormalBoundary):
            return context
        task, state = context
        completion = _bootstrap_execution_completion(
            self.store, project_id, plan, orchestrator, task, state
        )
        if isinstance(completion, FormalBoundary):
            return completion
        receipt, transaction_id, attempt_id = completion
        handoff = _bootstrap_prediction_handoff(
            self.store, state, str(task["task_id"]), transaction_id
        )
        if isinstance(handoff, FormalBoundary):
            return handoff
        handoff_artifact_id, artifact, handoff_path, document = handoff
        task_id = str(task["task_id"])
        try:
            publication = validate_prediction_publication(
                self.store,
                project_id=project_id,
                plan=plan,
                orchestrator_run_id=str(orchestrator.references.get("run_id") or ""),
                task=task,
                attempt_id=attempt_id,
                transaction_id=transaction_id,
                handoff_artifact_id=handoff_artifact_id,
                handoff=document,
            )
        except PredictionPublicationError as exc:
            return FormalBoundary.blocked(
                "prediction",
                "prediction_execution_correlation_invalid",
                str(exc),
            )
        record_artifact_ids = publication.artifact_ids
        prediction_evidence_ids = publication.evidence_ids
        from agents.prediction_contract import validate_prediction_owner_readiness
        prediction_run_id = str(document.get("run_id") or "")
        readiness = validate_prediction_owner_readiness(
            handoff_path=handoff_path,
            project_id=project_id,
            prediction_run_id=prediction_run_id,
            candidate_ids=tuple((plan.get("source") or {}).get("candidate_ids") or ()),
            expected_execution_identity=task["parameters"]["execution_identity"],
        )
        if readiness.status != "completed":
            return FormalBoundary.blocked(
                "prediction",
                readiness.blocker_code or "prediction_recovery_ambiguous",
                readiness.message or "Prediction owner readiness failed",
                prediction_run_id=prediction_run_id,
                handoff_path=str(handoff_path),
                transaction_id=transaction_id,
                task_id=task_id,
                attempt_id=attempt_id,
            )
        return FormalBoundary.completed(
            "prediction",
            prediction_run_id=prediction_run_id,
            handoff_path=str(handoff_path),
            transaction_id=transaction_id,
            task_id=task_id,
            attempt_id=attempt_id,
            artifact_ids=(
                str(artifact.get("artifact_id") or handoff_artifact_id),
                *record_artifact_ids,
            ),
            evidence_ids=(
                str(receipt.get("event_id") or ""), *prediction_evidence_ids
            ),
        )


def _bootstrap_execution_context(project_id, plan, orchestrator):
    source = plan.get("source") or {}
    tasks = plan.get("tasks") or []
    valid_task = (
        len(tasks) == 1
        and tasks[0].get("action") == "evaluate_new_design_candidates"
        and (tasks[0].get("candidate_scope") or {}).get("candidate_ids")
        == source.get("candidate_ids")
        and (tasks[0].get("parameters") or {}).get("execution_identity")
        == source.get("execution_identity")
    )
    if (
        source.get("kind") != "initial_prediction_bootstrap"
        or source.get("project_id") != project_id
        or not valid_task
    ):
        return FormalBoundary.blocked(
            "prediction", "prediction_execution_plan_invalid",
            "bootstrap plan project, scope, action, or execution identity is invalid",
        )
    run = orchestrator.references.get("run_document")
    states = run.get("tasks") if isinstance(run, Mapping) else None
    run_plan = run.get("plan") if isinstance(run, Mapping) else None
    if (
        orchestrator.status != "completed"
        or not isinstance(states, Mapping)
        or not isinstance(run_plan, Mapping)
        or orchestrator.references.get("plan_id") != plan.get("plan_id")
        or orchestrator.references.get("workflow_id") != plan.get("workflow_id")
        or run_plan.get("plan_id") != plan.get("plan_id")
        or run.get("workflow_id") != plan.get("workflow_id")
    ):
        return FormalBoundary.blocked(
            "prediction", "prediction_execution_correlation_invalid",
            "Orchestrator run is not bound to the bootstrap plan",
        )
    state = states.get(tasks[0].get("task_id"))
    return (tasks[0], state) if isinstance(state, Mapping) else FormalBoundary.not_started("prediction")


def _bootstrap_execution_completion(store, project_id, plan, orchestrator, task, state):
    task_id = str(task.get("task_id") or "")
    status = state.get("status")
    if status == "failed":
        return FormalBoundary.blocked(
            "prediction", "prediction_execution_failed",
            "approved Prediction execution failed", task_id=task_id,
        )
    if status != "succeeded":
        return FormalBoundary(
            status="active", boundary="prediction",
            references={"task_id": task_id, "formal_status": status},
        )
    from contracts.trace import TraceContext
    attempt_id = TraceContext.attempt_id_for(task_id, int(state.get("attempts") or 0))
    completions = [
        event for event in store.query(
            project_id=project_id,
            run_id=str(orchestrator.references.get("run_id") or ""),
            agent="execution", event_type="execution_task_completed",
        )
        if event.get("task_id") == task_id and event.get("attempt_id") == attempt_id
    ]
    if len(completions) != 1:
        return FormalBoundary.blocked(
            "prediction", "prediction_execution_correlation_invalid",
            "Worker completion receipt is missing or non-unique",
        )
    receipt = completions[0]
    transaction_id = receipt.get("transaction_id")
    identity = task["parameters"]["execution_identity"]
    if (
        not isinstance(transaction_id, str)
        or receipt.get("workflow_id") != plan.get("workflow_id")
        or receipt.get("plan_id") != plan.get("plan_id")
        or store.get_transaction_status(transaction_id) != "COMMITTED"
        or receipt.get("expected_execution_identity") != identity
        or receipt.get("observed_execution_identity") != identity
    ):
        return FormalBoundary.blocked(
            "prediction", "prediction_execution_correlation_invalid",
            "Worker receipt or committed transaction binding is invalid",
            transaction_id=transaction_id,
        )
    return receipt, transaction_id, attempt_id


def _bootstrap_prediction_handoff(store, state, task_id, transaction_id):
    handoffs = [
        item for item in (state.get("outputs") or [])
        if item.get("role") == "prediction_handoff"
    ]
    artifact_id = f"{transaction_id}-prediction_handoff"
    artifact = store.get_artifact(artifact_id)
    if len(handoffs) != 1 or artifact is None:
        return FormalBoundary.blocked(
            "prediction", "prediction_execution_correlation_invalid",
            "formal Prediction handoff Artifact is missing",
        )
    path = Path(str(artifact.get("path") or "")).expanduser().resolve()
    document = _read_json_object(path)
    digest = file_sha256(path) if document is not None else None
    if (
        document is None
        or Path(str(handoffs[0].get("path") or "")).expanduser().resolve() != path
        or handoffs[0].get("sha256") != digest
        or artifact.get("sha256") != digest
        or artifact.get("producer_task_id") != task_id
    ):
        return FormalBoundary.blocked(
            "prediction", "prediction_execution_correlation_invalid",
            "task output and committed handoff Artifact differ",
        )
    return artifact_id, artifact, path, document


def _formal_path(
    event: Mapping[str, Any], store: Any, path_key: str, artifact_key: str
) -> Path | None:
    artifact_id = event.get(artifact_key)
    if artifact_id:
        artifact = store.get_artifact(str(artifact_id))
        return Path(str(artifact["path"])).expanduser().resolve() if artifact else None
    value = event.get(path_key)
    return Path(str(value)).expanduser().resolve() if value else None


def _valid_retry_failure(store, failed_plan, failure):
    from contracts.bootstrap_retry import (
        BootstrapRetryProofError,
        validate_bootstrap_retry_failure,
    )
    try:
        validate_bootstrap_retry_failure(
            store, failed_plan=failed_plan, failure=failure
        )
    except BootstrapRetryProofError:
        return False
    return True


def _read_json_object(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _unique_prediction_start_time(
    store: Any, *, project_id: str, prediction_run_id: str
) -> datetime | None:
    events = store.query(
        project_id=project_id,
        agent="prediction",
        event_type="prediction_invocation_started",
    )
    matching = [
        event
        for event in events
        if event.get("prediction_run_id") == prediction_run_id
    ]
    if len(matching) != 1:
        return None
    return _event_time(matching[0])


def _event_precedes(event: Mapping[str, Any], boundary: datetime | None) -> bool:
    observed = _event_time(event)
    return observed is not None and boundary is not None and observed < boundary


def _event_time(event: Mapping[str, Any]) -> datetime | None:
    value = event.get("timestamp")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


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
