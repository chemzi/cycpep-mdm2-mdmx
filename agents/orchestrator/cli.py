"""cli - split from agents/orchestrator.py (PR6)."""

from __future__ import annotations

import argparse, json
from agents.planner import PlannerContractError
from .completion import complete
from .errors import OrchestratorContractError
from .service import initialize, status
from .claim import claim
from .state_machine import authorize, fail, recover, skip

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="initialize/reopen a run")
    init.add_argument("--plan", required=True)
    init.add_argument("--approval", action="append", default=[])
    init.add_argument("--output")

    auth = commands.add_parser("authorize", help="attach one approval")
    auth.add_argument("--run", required=True)
    auth.add_argument("--approval", required=True)

    claim_cmd = commands.add_parser("claim", help="claim one ready task")
    claim_cmd.add_argument("--run", required=True)
    claim_cmd.add_argument("--task", required=True)
    claim_cmd.add_argument("--worker", required=True)

    complete_cmd = commands.add_parser("complete", help="complete one claimed task")
    complete_cmd.add_argument("--run", required=True)
    complete_cmd.add_argument("--task", required=True)
    complete_cmd.add_argument("--claim-token", required=True)
    complete_cmd.add_argument("--output", action="append", dest="outputs", required=True)
    complete_cmd.add_argument("--gpu-minutes", type=float)

    fail_cmd = commands.add_parser("fail", help="fail one claimed task")
    fail_cmd.add_argument("--run", required=True)
    fail_cmd.add_argument("--task", required=True)
    fail_cmd.add_argument("--claim-token", required=True)
    fail_cmd.add_argument("--reason", required=True)
    fail_cmd.add_argument("--retryable", action="store_true")
    fail_cmd.add_argument("--gpu-minutes", type=float)

    skip_cmd = commands.add_parser("skip", help="skip one optional task")
    skip_cmd.add_argument("--run", required=True)
    skip_cmd.add_argument("--task", required=True)
    skip_cmd.add_argument("--reason", required=True)

    recover_cmd = commands.add_parser("recover", help="close an abandoned claim")
    recover_cmd.add_argument("--run", required=True)
    recover_cmd.add_argument("--task", required=True)
    recover_cmd.add_argument("--claim-token", required=True)
    recover_cmd.add_argument("--operator", required=True)
    recover_cmd.add_argument("--reason", required=True)
    recover_cmd.add_argument("--confirmed-process-stopped", action="store_true")
    recover_cmd.add_argument("--gpu-minutes", type=float)

    status_cmd = commands.add_parser("status", help="show run status")
    status_cmd.add_argument("--run", required=True)
    return parser

def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "init":
            result = initialize(
                plan_path=args.plan,
                approval_paths=args.approval,
                output_path=args.output,
            )
        elif args.command == "authorize":
            result = authorize(run_path=args.run, approval_path=args.approval)
        elif args.command == "claim":
            result = claim(run_path=args.run, task_id=args.task, worker=args.worker)
        elif args.command == "complete":
            result = complete(
                run_path=args.run,
                task_id=args.task,
                claim_token=args.claim_token,
                output_paths=args.outputs,
                gpu_minutes=args.gpu_minutes,
            )
        elif args.command == "fail":
            result = fail(
                run_path=args.run,
                task_id=args.task,
                claim_token=args.claim_token,
                reason=args.reason,
                retryable=args.retryable,
                gpu_minutes=args.gpu_minutes,
            )
        elif args.command == "skip":
            result = skip(run_path=args.run, task_id=args.task, reason=args.reason)
        elif args.command == "recover":
            result = recover(
                run_path=args.run,
                task_id=args.task,
                claim_token=args.claim_token,
                operator=args.operator,
                reason=args.reason,
                process_stopped_confirmed=args.confirmed_process_stopped,
                gpu_minutes=args.gpu_minutes,
            )
        elif args.command == "status":
            result = status(run_path=args.run)
        else:
            raise AssertionError(args.command)
    except (OrchestratorContractError, PlannerContractError, OSError, ValueError) as exc:
        print(json.dumps({
            "status": "error",
            "code": getattr(exc, "code", exc.__class__.__name__),
            "message": str(exc),
        }, ensure_ascii=False))
        return 2
    run = result["run"]
    print(json.dumps({
        "status": "complete",
        "run_id": run["run_id"],
        "run_status": run["status"],
        "run_path": result["run_path"],
        "task_statuses": {
            task_id: value["status"] for task_id, value in run["tasks"].items()
        },
        **({
            "task_id": result["task_id"],
            "claim_token": result.get("claim_token"),
            "dispatch_packet_path": result.get("dispatch_packet_path"),
        } if "task_id" in result else {}),
    }, ensure_ascii=False, indent=2))
    return 0
