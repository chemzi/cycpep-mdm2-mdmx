"""service - split from agents/planner.py (PR6)."""

from __future__ import annotations

from contracts.action import get_action_spec
from contracts.trace import TraceContext, derive_workflow_id
from data_layer import CandidateIndex, EvidenceLogger, State
from dataclasses import asdict
from pathlib import Path
from prediction_pipeline.contracts import file_sha256, object_sha256
from typing import Any
from .approval import _approval
from .budget import _budget_snapshot
from .config import PlannerConfig
from .errors import PlannerContractError
from .io import _atomic_json, _read_json
from .task_builder import (
    _add_critic_followup,
    _candidate_ids,
    _materialize_design_jobs,
    _reason_disposition,
    _task,
)
from .validation import _validate_critic_report

from .config import (
    APPROVAL_SCHEMA_VERSION,
    DESIGN_ITERATION_ACTIONS,
    MANDATORY_POLICY_CONSTRAINTS,
    PLANNER_VERSION,
    PLAN_SCHEMA_VERSION,
    PRIORITY_RANK,
    RECOMMENDATION_MAPPINGS,
)

def build_plan(
    *,
    critic_report_path: str | Path,
    state: dict | None = None,
    config: PlannerConfig | None = None,
    project_config: dict | None = None,
) -> dict:
    """Purely convert one frozen Critic report into an execution plan.

    ``project_config`` optionally injects an explicit approved project config
    (PR5, Engineering Standard §7); it takes precedence over the project entry
    carried in ``state`` and must agree with ``state``'s ``project_id``.
    When omitted, behaviour is unchanged.

    The injected config must also be injected into Execution (or carried in
    State), which re-verifies the digest and fails closed with
    ``project_config_drift`` on mismatch.
    """
    config = config or PlannerConfig()
    state = dict(state if state is not None else State.load())
    if project_config is not None:
        state["project_config"] = project_config
        injected_project_id = str(project_config.get("project_id") or "").strip()
        state_project_id = str(state.get("project_id") or "").strip()
        if injected_project_id and state_project_id and injected_project_id != state_project_id:
            raise PlannerContractError(
                "planner_project_mismatch",
                "injected project config differs from State project ID",
            )
    report_path = Path(critic_report_path).expanduser().resolve()
    report = _read_json(report_path, "critic_report")
    report_sha = file_sha256(report_path)
    _validate_critic_report(report, state, report_sha)
    budgets, total_design_budget = _budget_snapshot(state)
    source_round = int(state.get("round") or 1)
    project_id = str(
        report.get("source", {}).get("project_id")
        or state.get("project_id")
        or "unknown_project"
    ).strip()
    supplied_workflow_id = (
        state.get("workflow_id")
        or report.get("workflow_id")
        or report.get("source", {}).get("workflow_id")
    )
    if supplied_workflow_id:
        try:
            TraceContext(project_id=project_id, workflow_id=str(supplied_workflow_id))
        except ValueError as exc:
            raise PlannerContractError(
                "planner_workflow_id_invalid", "Planner received an invalid workflow_id"
            ) from exc
        workflow_id = str(supplied_workflow_id)
    else:
        workflow_id = derive_workflow_id(
            project_id, report["report_id"], report_sha, source_round
        )

    issues_by_code = {issue["code"]: issue for issue in report["issues"]}
    recommendations_by_action = {
        recommendation["action"]: recommendation
        for recommendation in report["recommendations"]
    }
    tasks: list[dict] = []

    design_recommendations = [
        recommendation
        for recommendation in report["recommendations"]
        if recommendation["action"] in DESIGN_ITERATION_ACTIONS
    ]
    if design_recommendations:
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
            block_reasons.append("design_budget_missing_or_exhausted")
        design_task = _task(
            tasks,
            agent="design",
            action="iterate_design",
            phase="design",
            priority=priority,
            disposition=disposition,
            reason_codes=reason_codes,
            candidate_ids=_candidate_ids(reason_codes, issues_by_code),
            parameters={
                "strategy_directives": [
                    recommendation["action"] for recommendation in design_recommendations
                ],
                "required_targets": required_targets,
                "route_budget_snapshot": budgets,
                "design_jobs": design_jobs,
                "project_config_digest": object_sha256(
                    state.get("project_config") or {}
                ),
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
            block_reasons=block_reasons,
        )
        prediction_task = _task(
            tasks,
            agent="prediction",
            action="evaluate_new_design_candidates",
            phase="evaluate",
            priority=priority,
            disposition=disposition,
            reason_codes=reason_codes,
            from_task_id=design_task["task_id"],
            parameters={
                "reuse_complete_evidence": True,
                "evidence_mode": "reuse_or_generate_full",
                "predictor_protocol": "af2_boltz2_prodigy_rosetta_postrelax_v1",
            },
            candidate_limit=min(proposal_count, config.max_prediction_candidates_per_task),
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
                ["upstream_design_task_blocked"] if block_reasons else []
            ),
        )
        _add_critic_followup(
            tasks,
            depends_on=[prediction_task["task_id"]],
            priority=priority,
            disposition=disposition,
            reason_codes=reason_codes,
            from_task_id=prediction_task["task_id"],
        )

    for recommendation in report["recommendations"]:
        action = recommendation["action"]
        if action in DESIGN_ITERATION_ACTIONS:
            continue
        mapping = RECOMMENDATION_MAPPINGS[action]
        reason_codes = list(recommendation["reason_codes"])
        candidate_ids = _candidate_ids(reason_codes, issues_by_code)
        disposition = _reason_disposition(reason_codes, issues_by_code)
        resource_class = get_action_spec(mapping.task_action).resource_class
        constraints = []
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
                "predictor_protocol": "af2_boltz2_prodigy_rosetta_postrelax_v1",
            })
            outputs = ["prediction_handoff.json"]
        elif action == "regenerate_invalid_artifact":
            constraints.extend([
                "regenerate_only_invalid_artifacts",
                "preserve_invalid_artifact_for_audit",
                "single_gpu_serial_execution",
            ])
            outputs = ["prediction_handoff.json"]
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

    if report["verdict"] == "clear":
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

    # A Critic blocker freezes every scientific/optional branch.  Only recovery
    # tasks and their verification step remain eligible for approval/dispatch.
    if report["verdict"] == "blocked":
        for task in tasks:
            if task["disposition"] != "recovery":
                task["execution_gate"]["status"] = "blocked"
                if "critic_blocker_requires_recovery" not in task["execution_gate"][
                    "block_reasons"
                ]:
                    task["execution_gate"]["block_reasons"].append(
                        "critic_blocker_requires_recovery"
                    )

    blocked_tasks = [
        task["task_id"] for task in tasks
        if task["execution_gate"]["status"] == "blocked"
        and task["disposition"] != "optional"
    ]
    required_approval_tasks = [
        task["task_id"] for task in tasks
        if task["approval"]["required"]
        and task["disposition"] != "optional"
        and task["execution_gate"]["status"] == "proposed"
    ]
    optional_task_ids = [
        task["task_id"] for task in tasks if task["disposition"] == "optional"
    ]
    if report["verdict"] == "blocked":
        status = "recovery_only"
    elif blocked_tasks:
        status = "blocked"
    elif required_approval_tasks:
        status = "awaiting_approval"
    elif tasks:
        status = "ready"
    else:
        status = "no_action"

    has_required_iteration = any(
        task["phase"] in {"design", "evaluate", "iterate"}
        and task["disposition"] != "optional"
        for task in tasks
    )
    target_round = source_round + 1 if has_required_iteration else source_round
    input_digest = object_sha256({
        "critic_report_path": str(report_path),
        "critic_report_sha256": report_sha,
        "workflow_id": workflow_id,
        "state": {
            "project_id": state.get("project_id"),
            "round": source_round,
            "design_budget": budgets,
            "project_config_digest": object_sha256(
                state.get("project_config") or {}
            ),
            "critic_report_id": (state.get("critic") or {}).get("report_id")
            if isinstance(state.get("critic"), dict) else None,
        },
        "config": asdict(config),
        "planner_version": PLANNER_VERSION,
    })
    plan_id = f"planner_{input_digest[:12]}"
    summary = (
        f"Planner converted Critic verdict={report['verdict']} into {len(tasks)} task(s): "
        f"status={status}; required approvals={len(required_approval_tasks)}; "
        f"blocked tasks={len(blocked_tasks)}; optional tasks={len(optional_task_ids)}."
    )
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "planner_version": PLANNER_VERSION,
        "plan_id": plan_id,
        "workflow_id": workflow_id,
        "input_digest": input_digest,
        "source": {
            "critic_report": str(report_path),
            "critic_report_sha256": report_sha,
            "critic_report_id": report["report_id"],
            "critic_verdict": report["verdict"],
            "prediction_run_id": report["source"].get("prediction_run_id"),
            "project_id": report["source"].get("project_id"),
            "workflow_id": workflow_id,
        },
        "status": status,
        "summary": summary,
        "cycle": {
            "source_round": source_round,
            "target_round": target_round,
            "round_advancement_deferred_to_orchestrator": True,
        },
        "budget_request": {
            "configured_design_budget_snapshot": budgets,
            "configured_design_budget_total": total_design_budget,
            "requested_design_proposals": sum(
                task["resource_request"]["proposal_count"] for task in tasks
            ),
            "requested_gpu_job_slots": sum(
                task["resource_request"]["gpu_job_slots"] for task in tasks
                if task["disposition"] != "optional"
            ),
            "gpu_minutes": None,
            "gpu_minutes_status": "benchmark_required",
            "reservation_status": "not_reserved",
        },
        "policy_constraints": sorted(MANDATORY_POLICY_CONSTRAINTS),
        "approval_request": {
            "artifact_required": bool(required_approval_tasks),
            "required_task_ids": required_approval_tasks,
            "optional_task_ids": optional_task_ids,
            "approval_schema_version": APPROVAL_SCHEMA_VERSION,
            "approval_must_bind_plan_sha256": True,
        },
        "execution": {
            "automatic_dispatch_allowed": False,
            "blocked_task_ids": blocked_tasks,
            "entry_task_ids": [
                task["task_id"] for task in tasks
                if not task["depends_on"]
                and task["execution_gate"]["status"] == "proposed"
            ],
            "orchestrator_required": True,
        },
        "tasks": tasks,
    }

def run(
    *,
    critic_report_path: str | Path,
    output_path: str | Path | None = None,
    state: dict | None = None,
    config: PlannerConfig | None = None,
    project_config: dict | None = None,
) -> dict:
    """Build, persist, and idempotently register a Planner execution plan."""
    state = dict(state if state is not None else State.load())
    plan = build_plan(
        critic_report_path=critic_report_path,
        state=state,
        config=config,
        project_config=project_config,
    )
    report_path = Path(critic_report_path).expanduser().resolve()
    if output_path is None:
        output_path = report_path.parent / "planner" / plan["plan_id"] / "execution_plan.json"
    output_path = Path(output_path).expanduser().resolve()
    _atomic_json(output_path, plan)
    plan_sha = file_sha256(output_path)
    summary = {
        "planner_version": PLANNER_VERSION,
        "plan_id": plan["plan_id"],
        "workflow_id": plan["workflow_id"],
        "plan_path": str(output_path),
        "plan_sha256": plan_sha,
        "critic_report_id": plan["source"]["critic_report_id"],
        "status": plan["status"],
        "task_count": len(plan["tasks"]),
        "required_approval_task_ids": plan["approval_request"]["required_task_ids"],
    }
    phase = "report" if plan["source"]["critic_verdict"] == "clear" else "iterate"
    State.update({"phase": phase, "planner": summary})
    history = State.load().get("iteration_history") or []
    if not any(
        entry.get("agent") == "planner"
        and (entry.get("summary") or {}).get("plan_id") == plan["plan_id"]
        for entry in history
    ):
        State.append_history({"phase": phase, "agent": "planner", "summary": summary})
    if not any(
        entry.get("event_type") == "planner_plan"
        and entry.get("plan_id") == plan["plan_id"]
        for entry in EvidenceLogger.get_all()
    ):
        EvidenceLogger.planner_plan(
            plan_id=plan["plan_id"],
            plan_path=str(output_path),
            plan_sha256=plan_sha,
            critic_report_id=plan["source"]["critic_report_id"],
            critic_report_path=plan["source"].get("critic_report"),
            critic_report_sha256=plan["source"].get("critic_report_sha256"),
            status=plan["status"],
            task_count=len(plan["tasks"]),
            required_approval_task_ids=plan["approval_request"]["required_task_ids"],
            trace_context=TraceContext(
                project_id=str(plan["source"].get("project_id") or "unknown_project"),
                workflow_id=plan["workflow_id"],
                plan_id=plan["plan_id"],
            ),
        )
    return {"plan": plan, "plan_path": str(output_path), "plan_sha256": plan_sha}

def plan(
    phase: str | None = None,
    state: dict | None = None,
    candidate_rows: list[dict] | None = None,
    project_config: dict | None = None,
) -> list[dict]:
    """Compatibility bootstrap planner for runs that have no Critic report yet."""
    state = dict(state if state is not None else State.load())
    if project_config is not None:
        state["project_config"] = project_config
        injected_project_id = str(project_config.get("project_id") or "").strip()
        state_project_id = str(state.get("project_id") or "").strip()
        if injected_project_id and state_project_id and injected_project_id != state_project_id:
            raise PlannerContractError(
                "planner_project_mismatch",
                "injected project config differs from State project ID",
            )
    candidate_rows = list(
        candidate_rows if candidate_rows is not None else CandidateIndex.load()
    )
    project = state.get("project_config") or {}
    review = project.get("review") or {}
    if review.get("status") != "approved" or (
        review.get("approved_digest") != review.get("content_digest")
    ):
        return [{
            "agent": "research",
            "action": "review_and_approve_project_config",
            "phase": "research",
            "reason": "project configuration lacks a current digest-bound approval",
            "execution_allowed": False,
        }]
    has_research = bool(
        state.get("pocket_differences")
        or state.get("known_dual_binders")
        or state.get("research_pipeline_meta")
    )
    if not has_research:
        return [{
            "agent": "research",
            "action": "run",
            "phase": "research",
            "reason": "approved project has no Research result in State",
            "execution_allowed": True,
        }]
    if not candidate_rows:
        return [{
            "agent": "design",
            "action": "generate_candidates",
            "phase": "design",
            "reason": "Research is present but CandidateIndex is empty",
            "execution_allowed": False,
            "approval_required": "execution_budget",
        }]
    prediction = state.get("prediction") or {}
    if not prediction.get("handoff_path"):
        return [{
            "agent": "prediction",
            "action": "run",
            "phase": "evaluate",
            "reason": "candidates exist but State has no Prediction handoff",
            "execution_allowed": False,
            "approval_required": "execution_budget",
        }]
    critic = state.get("critic") or {}
    if not critic.get("report_path"):
        return [{
            "agent": "critic",
            "action": "review_prediction_handoff",
            "phase": "critic",
            "reason": "Prediction handoff exists but State has no Critic report",
            "execution_allowed": True,
            "handoff_path": prediction["handoff_path"],
        }]
    return [{
        "agent": "planner",
        "action": "build_from_critic",
        "phase": phase or "iterate",
        "reason": "Critic report is ready for deterministic planning",
        "execution_allowed": True,
        "critic_report_path": critic["report_path"],
    }]

def adjust(
    report: str | Path,
    state: dict | None = None,
    project_config: dict | None = None,
) -> dict:
    """Backward-compatible name for pure Critic-driven planning."""
    return build_plan(
        critic_report_path=report,
        state=state,
        project_config=project_config,
    )
