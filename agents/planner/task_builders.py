"""task_builders - split from agents/planner/service.py (PR6)."""

from __future__ import annotations

from contracts.action import get_action_spec
from prediction_pipeline.contracts import object_sha256
from prediction_pipeline.protocol import PREDICTOR_PROTOCOL
from typing import Any
from .approval import _approval
from .config import (
    DESIGN_ITERATION_ACTIONS,
    PRIORITY_RANK,
    RECOMMENDATION_MAPPINGS,
    PlannerConfig,
)
from .task_builder import (
    _add_critic_followup,
    _candidate_ids,
    _materialize_design_jobs,
    _reason_disposition,
    _task,
)

def _design_iteration_strategy(
    state: dict,
    report: dict,
    report_sha: str,
    budgets: dict,
    total_design_budget: int,
    config: PlannerConfig,
    issues_by_code: dict[str, dict],
) -> dict | None:
    """Compute the design branch strategy; None when no design action applies."""
    design_recommendations = [
        recommendation
        for recommendation in report["recommendations"]
        if recommendation["action"] in DESIGN_ITERATION_ACTIONS
    ]
    if not design_recommendations:
        return None
    reason_codes = sorted({
        code
        for recommendation in design_recommendations
        for code in recommendation["reason_codes"]
    })
    disposition = _reason_disposition(reason_codes, issues_by_code)
    priority = min(
        (recommendation["priority"] for recommendation in design_recommendations),
        key=PRIORITY_RANK.__getitem__,
    )
    requested = (
        config.optional_design_batch_size
        if disposition == "optional"
        else config.design_batch_size
    )
    requested_proposal_count = min(
        requested, config.max_design_proposals_per_plan, total_design_budget
    )
    required_targets = list(report["source"].get("required_targets") or [])
    design_jobs = _materialize_design_jobs(
        state=state,
        required_targets=required_targets,
        budgets=budgets,
        requested=requested_proposal_count,
        seed_material=report_sha,
    )
    proposal_count = sum(job["proposal_count"] for job in design_jobs)
    block_reasons = []
    if proposal_count < 1:
        block_reasons.append(
            "design_targets_missing" if not required_targets
            else "design_budget_missing_or_exhausted"
        )
    return {
        "reason_codes": reason_codes,
        "disposition": disposition,
        "priority": priority,
        "required_targets": required_targets,
        "design_jobs": design_jobs,
        "proposal_count": proposal_count,
        "block_reasons": block_reasons,
        "strategy_directives": [
            recommendation["action"] for recommendation in design_recommendations
        ],
    }


def _design_iteration_design_task(
    tasks: list[dict],
    strategy: dict,
    budgets: dict,
    state: dict,
    issues_by_code: dict[str, dict],
) -> dict:
    """Build the design iteration task from a design strategy."""
    reason_codes = strategy["reason_codes"]
    required_targets = strategy["required_targets"]
    design_jobs = strategy["design_jobs"]
    proposal_count = strategy["proposal_count"]
    return _task(
        tasks,
        agent="design",
        action="iterate_design",
        phase="design",
        priority=strategy["priority"],
        disposition=strategy["disposition"],
        reason_codes=reason_codes,
        candidate_ids=_candidate_ids(reason_codes, issues_by_code),
        parameters={
            "strategy_directives": strategy["strategy_directives"],
            "required_targets": required_targets,
            "route_budget_snapshot": budgets,
            "design_jobs": design_jobs,
            "project_config_digest": object_sha256(state.get("project_config") or {}),
            "reuse_existing_prediction_evidence": True,
        },
        proposal_count=proposal_count,
        candidate_limit=proposal_count,
        approval=_approval(action="iterate_design", critic_approval_required=False),
        outputs=["design_task_result.json"],
        constraints=[
            "append_candidates_only",
            "preserve_source_candidate_evidence",
            "single_gpu_serial_execution",
        ],
        block_reasons=strategy["block_reasons"],
    )


def _design_iteration_prediction_task(
    tasks: list[dict],
    strategy: dict,
    config: PlannerConfig,
    design_task: dict,
) -> dict:
    """Build the prediction task that consumes the design iteration output."""
    proposal_count = strategy["proposal_count"]
    return _task(
        tasks,
        agent="prediction",
        action="evaluate_new_design_candidates",
        phase="evaluate",
        priority=strategy["priority"],
        disposition=strategy["disposition"],
        reason_codes=strategy["reason_codes"],
        from_task_id=design_task["task_id"],
        parameters={
            "reuse_complete_evidence": True,
            "evidence_mode": "reuse_or_generate_full",
            "predictor_protocol": dict(PREDICTOR_PROTOCOL),
        },
        candidate_limit=min(
            proposal_count, config.max_prediction_candidates_per_task
        ),
        approval=_approval(
            action="evaluate_new_design_candidates", critic_approval_required=False
        ),
        depends_on=[design_task["task_id"]],
        outputs=["prediction_handoff.json"],
        constraints=[
            "evaluate_only_new_or_incomplete_candidates",
            "reuse_complete_prediction_evidence",
            "single_gpu_serial_execution",
        ],
        block_reasons=(
            ["upstream_design_task_blocked"] if strategy["block_reasons"] else []
        ),
    )


def _design_iteration_tasks(
    tasks: list[dict],
    state: dict,
    report: dict,
    report_sha: str,
    budgets: dict,
    total_design_budget: int,
    config: PlannerConfig,
    issues_by_code: dict[str, dict],
) -> None:
    """Materialize the design iteration branch: design -> prediction -> critic."""
    strategy = _design_iteration_strategy(
        state, report, report_sha, budgets, total_design_budget, config, issues_by_code
    )
    if strategy is None:
        return
    design_task = _design_iteration_design_task(
        tasks, strategy, budgets, state, issues_by_code
    )
    prediction_task = _design_iteration_prediction_task(
        tasks, strategy, config, design_task
    )
    _add_critic_followup(
        tasks,
        depends_on=[prediction_task["task_id"]],
        priority=strategy["priority"],
        disposition=strategy["disposition"],
        reason_codes=strategy["reason_codes"],
        from_task_id=prediction_task["task_id"],
    )

def _recommendation_action_config(
    action: str,
    reason_codes: list[str],
    issues_by_code: dict[str, dict],
) -> tuple[list[str], dict[str, Any], list[str], bool]:
    """Translate one Critic action into execution policy for a task."""
    constraints: list[str] = []
    data_integrity = False
    parameters: dict[str, Any] = {}
    outputs: list[str] = []
    if action == "calibrate_thresholds":
        constraints.extend([
            "produce_calibration_proposal_only",
            "do_not_apply_thresholds_without_human_approval",
        ])
        parameters["threshold_keys"] = sorted({
            str(key)
            for code in reason_codes
            for evidence in issues_by_code[code].get("evidence", [])
            if isinstance(evidence, dict)
            for key in evidence.get("threshold_keys", [])
        })
        outputs = ["threshold_calibration_proposal.json"]
    elif action == "deduplicate_candidates":
        constraints.extend([
            "audit_only",
            "do_not_delete_candidates_automatically",
        ])
        outputs = ["duplicate_resolution_proposal.json"]
    elif action == "repair_candidate_index":
        data_integrity = True
        constraints.extend([
            "reconcile_against_immutable_prediction_records",
            "preserve_previous_index_backup",
        ])
        outputs = ["candidate_index_repair_report.json"]
    elif action == "complete_prediction_evidence":
        constraints.extend([
            "run_only_missing_prediction_steps",
            "reuse_complete_prediction_evidence",
            "single_gpu_serial_execution",
        ])
        parameters.update({
            "reuse_complete_evidence": True,
            "evidence_mode": "reuse_or_generate_full",
            "predictor_protocol": dict(PREDICTOR_PROTOCOL),
        })
        outputs = ["prediction_handoff.json"]
    elif action == "regenerate_invalid_artifact":
        constraints.extend([
            "regenerate_only_invalid_artifacts",
            "preserve_invalid_artifact_for_audit",
            "single_gpu_serial_execution",
        ])
        outputs = ["prediction_handoff.json"]
    return constraints, parameters, outputs, data_integrity

def _recommendation_tasks(
    tasks: list[dict],
    report: dict,
    issues_by_code: dict[str, dict],
    config: PlannerConfig,
) -> None:
    """Materialize every non-design Critic recommendation as one task."""
    for recommendation in report["recommendations"]:
        action = recommendation["action"]
        if action in DESIGN_ITERATION_ACTIONS:
            continue
        mapping = RECOMMENDATION_MAPPINGS[action]
        reason_codes = list(recommendation["reason_codes"])
        candidate_ids = _candidate_ids(reason_codes, issues_by_code)
        disposition = _reason_disposition(reason_codes, issues_by_code)
        resource_class = get_action_spec(mapping.task_action).resource_class
        constraints, parameters, outputs, data_integrity = _recommendation_action_config(
            action, reason_codes, issues_by_code
        )
        task = _task(
            tasks,
            agent=mapping.agent,
            action=mapping.task_action.value,
            phase=mapping.phase,
            priority=recommendation["priority"],
            disposition=disposition,
            reason_codes=reason_codes,
            candidate_ids=candidate_ids,
            parameters=parameters,
            candidate_limit=(
                min(len(candidate_ids), config.max_prediction_candidates_per_task)
                if resource_class == "gpu" else 0
            ),
            approval=_approval(
                action=mapping.task_action.value,
                critic_approval_required=bool(recommendation.get("approval_required")),
                data_integrity=data_integrity,
            ),
            outputs=outputs,
            constraints=constraints,
        )
        if action in {
            "complete_prediction_evidence",
            "regenerate_invalid_artifact",
        }:
            _add_critic_followup(
                tasks,
                depends_on=[task["task_id"]],
                priority=recommendation["priority"],
                disposition=disposition,
                reason_codes=reason_codes,
                from_task_id=task["task_id"],
            )
        elif action == "repair_candidate_index":
            _add_critic_followup(
                tasks,
                depends_on=[task["task_id"]],
                priority="P0",
                disposition="recovery",
                reason_codes=reason_codes,
            )

def _add_reporter_task(tasks: list[dict], report: dict) -> None:
    """Add the final candidate report task for a clear verdict."""
    if report["verdict"] != "clear":
        return
    _task(
        tasks,
        agent="reporter",
        action="prepare_final_candidate_report",
        phase="report",
        priority="P1",
        disposition="required",
        reason_codes=[],
        parameters={
            "prediction_run_id": report["source"].get("prediction_run_id"),
            "critic_report_id": report["report_id"],
            "candidate_nomination_requires_human_review": True,
        },
        outputs=["candidate_review_packet"],
        constraints=["do_not_nominate_candidates_automatically"],
    )

def _apply_blocker_freeze(tasks: list[dict], verdict: str) -> None:
    """A Critic blocker freezes every scientific/optional branch.

    Only recovery tasks and their verification step remain eligible for
    approval/dispatch.
    """
    if verdict != "blocked":
        return
    for task in tasks:
        if task["disposition"] != "recovery":
            task["execution_gate"]["status"] = "blocked"
            if "critic_blocker_requires_recovery" not in task["execution_gate"][
                "block_reasons"
            ]:
                task["execution_gate"]["block_reasons"].append(
                    "critic_blocker_requires_recovery"
                )
