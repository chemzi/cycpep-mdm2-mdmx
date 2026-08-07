"""service - split from agents/planner.py (PR6)."""

from __future__ import annotations

from contracts.critic import resolve_critic_report_path
from contracts.trace import TraceContext
from data_layer import (
    CandidateIndex,
    EvidenceLogger,
    State,
    get_storage_backend,
)
from pathlib import Path
from prediction_pipeline.contracts import file_sha256
from .config import PLANNER_VERSION, PlannerConfig
from .errors import PlannerContractError
from .io import _atomic_json
from .plan_builder import build_plan

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
    approval_step = _plan_approval_step(state)
    if approval_step is not None:
        return approval_step
    return _plan_progress_step(state, candidate_rows, phase)


def _plan_approval_step(state: dict) -> list[dict] | None:
    """Return the Research approval step when project config is not approved."""
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
    return None


def _plan_progress_step(
    state: dict, candidate_rows: list[dict], phase: str | None
) -> list[dict]:
    """Return the next pipeline step for an approved project."""
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
    critic_report_path = resolve_critic_report_path(critic, get_storage_backend())
    if not critic_report_path:
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
        "critic_report_path": critic_report_path,
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
