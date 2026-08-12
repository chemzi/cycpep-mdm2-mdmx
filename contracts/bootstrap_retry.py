"""Shared formal proof for an explicitly retryable bootstrap execution."""

from __future__ import annotations

from typing import Any, Mapping


class BootstrapRetryProofError(ValueError):
    pass


def validate_bootstrap_retry_failure(
    store: Any,
    *,
    failed_plan: Mapping[str, Any],
    failure: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the unique failure event only for an exact retryable terminal proof."""
    source = failed_plan.get("source") or {}
    required = (
        "plan_id", "workflow_id", "run_id", "task_id", "attempt_id",
        "transaction_id", "evidence_id",
    )
    if any(not isinstance(failure.get(key), str) or not failure[key] for key in required):
        raise BootstrapRetryProofError("retry requires a fully bound Worker failure")
    transaction = store.get_transaction(failure["transaction_id"])
    events = [
        event for event in store.query(
            project_id=source.get("project_id"),
            agent="execution",
            event_type="execution_task_failed",
        )
        if event.get("event_id") == failure["evidence_id"]
    ]
    binding_keys = tuple(key for key in required if key != "evidence_id")
    valid = (
        isinstance(transaction, Mapping)
        and transaction.get("status") in {"FAILED", "ROLLED_BACK"}
        and (transaction.get("error") or {}).get("retryable") is True
        and transaction.get("project_id") == source.get("project_id")
        and transaction.get("workflow_id") == failure.get("workflow_id")
        and transaction.get("run_id") == failure.get("run_id")
        and transaction.get("task_id") == failure.get("task_id")
        and transaction.get("attempt_id") == failure.get("attempt_id")
        and transaction.get("action") == "evaluate_new_design_candidates"
        and (transaction.get("metadata") or {}).get("plan_id") == failed_plan.get("plan_id")
        and failure.get("plan_id") == failed_plan.get("plan_id")
        and len(events) == 1
    )
    if valid:
        event = events[0]
        valid = (
            all(event.get(key) == failure.get(key) for key in binding_keys)
            and event.get("project_id") == source.get("project_id")
            and event.get("action") == "evaluate_new_design_candidates"
            and event.get("retryable") is True
        )
    if not valid:
        raise BootstrapRetryProofError(
            "retry failure proof is missing, conflicting, or not retryable terminal"
        )
    return dict(events[0])


__all__ = ["BootstrapRetryProofError", "validate_bootstrap_retry_failure"]
