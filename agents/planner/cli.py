"""cli - split from agents/planner.py (PR6)."""

from __future__ import annotations

import argparse, json
from .approval import record_approval
from .config import PlannerConfig
from .errors import PlannerContractError
from .service import run

def _config_from_args(args: argparse.Namespace) -> PlannerConfig:
    return PlannerConfig(
        design_batch_size=args.design_batch_size,
        optional_design_batch_size=args.optional_design_batch_size,
        max_design_proposals_per_plan=args.max_design_proposals,
        max_prediction_candidates_per_task=args.max_prediction_candidates,
    )

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="build a plan from a Critic report")
    build.add_argument("--critic-report", required=True)
    build.add_argument("--output")
    build.add_argument("--design-batch-size", type=int, default=12)
    build.add_argument("--optional-design-batch-size", type=int, default=3)
    build.add_argument("--max-design-proposals", type=int, default=48)
    build.add_argument("--max-prediction-candidates", type=int, default=48)

    approve = subparsers.add_parser(
        "approve", help="record explicit human approval for selected plan tasks"
    )
    approve.add_argument("--plan", required=True)
    approve.add_argument("--task", action="append", dest="tasks", required=True)
    approve.add_argument("--approver", required=True)
    approve.add_argument("--justification", required=True)
    approve.add_argument("--max-gpu-job-slots", type=int)
    approve.add_argument("--max-gpu-minutes", type=float)
    approve.add_argument("--max-design-proposals", type=int)
    approve.add_argument("--max-prediction-candidates", type=int)
    approve.add_argument("--output")
    return parser

def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "build":
            result = run(
                critic_report_path=args.critic_report,
                output_path=args.output,
                config=_config_from_args(args),
            )
            payload = {
                "status": "complete",
                "plan_id": result["plan"]["plan_id"],
                "plan_status": result["plan"]["status"],
                "plan_path": result["plan_path"],
                "plan_sha256": result["plan_sha256"],
                "task_count": len(result["plan"]["tasks"]),
                "required_approval_task_ids": result["plan"]["approval_request"][
                    "required_task_ids"
                ],
            }
        elif args.command == "approve":
            result = record_approval(
                plan_path=args.plan,
                task_ids=args.tasks,
                approver=args.approver,
                justification=args.justification,
                max_gpu_job_slots=args.max_gpu_job_slots,
                max_gpu_minutes=args.max_gpu_minutes,
                max_design_proposals=args.max_design_proposals,
                max_prediction_candidates=args.max_prediction_candidates,
                output_path=args.output,
            )
            payload = {
                "status": "complete",
                "approval_id": result["approval"]["approval_id"],
                "approval_path": result["approval_path"],
                "approval_sha256": result["approval_sha256"],
                "approved_task_ids": result["approval"]["approved_task_ids"],
            }
        else:
            raise AssertionError(args.command)
    except (PlannerContractError, OSError, ValueError) as exc:
        print(json.dumps({
            "status": "error",
            "code": getattr(exc, "code", exc.__class__.__name__),
            "message": str(exc),
        }, ensure_ascii=False))
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0
