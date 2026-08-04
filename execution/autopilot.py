"""Project-scoped automatic workflow controller built on reviewed contracts.

The controller never accepts a shell command.  It freezes Planner plans,
records the user's explicit budget authorization, initializes Orchestrator
runs, and asks Execution Worker to drain only registered semantic actions.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from execution.supervisor import atomic_json


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _configure_runtime(args: argparse.Namespace) -> None:
    os.environ["CYCPEP_PROJECT_CONFIG"] = str(Path(args.project_config).resolve())
    os.environ["CYCPEP_DATA_DIR"] = str(Path(args.data_dir).resolve())
    os.environ["CYCPEP_EVIDENCE_DIR"] = str(Path(args.evidence_dir).resolve())
    os.environ["CYCPEP_EXECUTION_ROOT"] = str(Path(args.work_root).resolve() / "execution")
    if args.core_python:
        os.environ["CYCPEP_EXECUTION_PYTHON"] = str(Path(args.core_python).resolve())
    if args.design_python:
        os.environ["CYCPEP_DESIGN_AGENT_PYTHON"] = str(Path(args.design_python).resolve())
    if args.prediction_python:
        os.environ["CYCPEP_PREDICTION_PYTHON"] = str(Path(args.prediction_python).resolve())


def _approval_for_plan(plan_result: dict, args: argparse.Namespace) -> str | None:
    from agents.planner import record_approval

    task_ids = plan_result["plan"]["approval_request"]["required_task_ids"]
    if not task_ids:
        return None
    approved = record_approval(
        plan_path=plan_result["plan_path"],
        task_ids=task_ids,
        approver=args.approver,
        justification=args.justification,
        max_gpu_job_slots=1,
        max_gpu_minutes=args.max_gpu_minutes,
        max_design_proposals=args.max_design_proposals,
        max_prediction_candidates=args.max_prediction_candidates,
    )
    return approved["approval_path"]


def _execute_plan(plan_result: dict, args: argparse.Namespace, worker_id: str) -> dict:
    from agents.orchestrator import initialize
    from execution.worker import drain_run

    approval = _approval_for_plan(plan_result, args)
    initialized = initialize(
        plan_path=plan_result["plan_path"],
        approval_paths=[approval] if approval else [],
    )
    result = drain_run(
        run_path=initialized["run_path"],
        worker_id=worker_id,
    )
    if result["status"] not in {"completed", "completed_required"}:
        raise RuntimeError(
            f"Orchestrator {result['run_id']} stopped with status={result['status']} "
            f"tasks={result['task_statuses']}"
        )
    return {"plan": plan_result, "run": result, "run_path": initialized["run_path"]}


def _output_with_role(run_path: str | Path, role: str) -> Path | None:
    run = json.loads(Path(run_path).read_text(encoding="utf-8"))
    for task in run.get("tasks", {}).values():
        for output in task.get("outputs") or []:
            if output.get("role") == role:
                return Path(output["path"]).resolve()
    return None


def run(args: argparse.Namespace) -> dict:
    _configure_runtime(args)
    # Import only after project-scoped environment variables are fixed.
    from agents import planner
    from data_layer import State
    from project_config import load_project_config
    from target_bootstrap import assert_project_approved

    work_root = Path(args.work_root).resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    status_path = work_root / "autopilot_status.json"
    config = load_project_config(args.project_config)
    assert_project_approved(config)
    State.sync_project_config(config)
    status = {
        "schema_version": 1,
        "project_id": config["project_id"],
        "status": "running",
        "stage": "research",
        "started_at": _utcnow(),
        "updated_at": _utcnow(),
        "runs": [],
        "budget": {
            "max_design_proposals": args.max_design_proposals,
            "max_prediction_candidates": args.max_prediction_candidates,
            "max_gpu_minutes": args.max_gpu_minutes,
            "max_rounds": args.max_rounds,
        },
    }
    atomic_json(status_path, status)
    try:
        research_plan = planner.run_bootstrap(
            stage="research",
            output_root=work_root / "plans" / "research",
        )
        research_run = _execute_plan(research_plan, args, "autopilot-research")
        status["runs"].append({
            "stage": "research", "plan_id": research_plan["plan"]["plan_id"],
            "run_id": research_run["run"]["run_id"], "run_path": research_run["run_path"],
            "status": research_run["run"]["status"],
        })
        status.update({"stage": "design", "updated_at": _utcnow()})
        atomic_json(status_path, status)

        # Structure preparation re-approves a hash-bound config, so reload it
        # before freezing the Design plan.
        State.sync_project_config(load_project_config(args.project_config))
        design_plan = planner.run_bootstrap(
            stage="design",
            output_root=work_root / "plans" / "design_round_1",
            proposal_count=args.max_design_proposals,
            prediction_limit=args.max_prediction_candidates,
        )
        design_run = _execute_plan(design_plan, args, "autopilot-design-1")
        status["runs"].append({
            "stage": "design_round_1", "plan_id": design_plan["plan"]["plan_id"],
            "run_id": design_run["run"]["run_id"], "run_path": design_run["run_path"],
            "status": design_run["run"]["status"],
        })
        critic_path = _output_with_role(design_run["run_path"], "critic_report")
        if critic_path is None:
            raise RuntimeError("initial Design cycle completed without a Critic report")

        final_report = json.loads(critic_path.read_text(encoding="utf-8"))
        for round_index in range(2, args.max_rounds + 1):
            if final_report.get("verdict") == "clear":
                break
            status.update({"stage": f"iteration_{round_index}", "updated_at": _utcnow()})
            atomic_json(status_path, status)
            iteration_plan = planner.run(
                critic_report_path=critic_path,
                output_path=work_root / "plans" / f"iteration_{round_index}" / "execution_plan.json",
            )
            proposed_actions = {
                task["action"] for task in iteration_plan["plan"]["tasks"]
                if task["execution_gate"]["status"] == "proposed"
            }
            from execution.contracts import CORE_ACTIONS
            unsupported = sorted(proposed_actions - CORE_ACTIONS)
            if unsupported:
                raise RuntimeError(f"Planner requested unavailable automatic actions: {unsupported}")
            iteration_run = _execute_plan(
                iteration_plan, args, f"autopilot-iteration-{round_index}"
            )
            status["runs"].append({
                "stage": f"iteration_{round_index}",
                "plan_id": iteration_plan["plan"]["plan_id"],
                "run_id": iteration_run["run"]["run_id"],
                "run_path": iteration_run["run_path"],
                "status": iteration_run["run"]["status"],
            })
            next_critic = _output_with_role(iteration_run["run_path"], "critic_report")
            if next_critic is None:
                break
            critic_path = next_critic
            final_report = json.loads(critic_path.read_text(encoding="utf-8"))

        final_status = "completed_clear" if final_report.get("verdict") == "clear" else "completed_review_required"
        status.update({
            "status": final_status,
            "stage": "complete",
            "critic_report": str(critic_path),
            "critic_verdict": final_report.get("verdict"),
            "finished_at": _utcnow(),
            "updated_at": _utcnow(),
        })
        atomic_json(status_path, status)
        return status
    except BaseException as exc:
        status.update({
            "status": "failed",
            "error": {"code": getattr(exc, "code", exc.__class__.__name__), "message": str(exc)},
            "finished_at": _utcnow(),
            "updated_at": _utcnow(),
        })
        atomic_json(status_path, status)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-config", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--work-root", required=True)
    parser.add_argument("--core-python")
    parser.add_argument("--design-python")
    parser.add_argument("--prediction-python")
    parser.add_argument("--approver", required=True)
    parser.add_argument("--justification", required=True)
    parser.add_argument("--max-design-proposals", type=int, default=12)
    parser.add_argument("--max-prediction-candidates", type=int, default=12)
    parser.add_argument("--max-gpu-minutes", type=float, default=360.0)
    parser.add_argument("--max-rounds", type=int, default=3)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = run(args)
    except BaseException as exc:
        print(json.dumps({
            "status": "failed",
            "code": getattr(exc, "code", exc.__class__.__name__),
            "message": str(exc),
        }, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
