#!/usr/bin/env python3
"""Run an isolated Planner -> Orchestrator -> Execution -> Prediction -> Critic check.

The check copies State/CandidateIndex into a new work directory, requests
completion of Prediction evidence for explicit existing candidates, and runs
the normal reviewed Execution handlers.  Source project State is never opened
for writing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


POLICY_CONSTRAINTS = [
    "do_not_change_thresholds_automatically",
    "do_not_delete_candidates_automatically",
    "do_not_start_gpu_jobs_without_planner_budget_and_execution_approval",
    "reuse_complete_prediction_evidence",
]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-config", required=True)
    parser.add_argument("--source-data-dir", required=True)
    parser.add_argument("--artifacts-root", required=True)
    parser.add_argument("--work-root", required=True)
    parser.add_argument("--candidate", action="append", required=True)
    parser.add_argument("--max-gpu-minutes", type=float, default=60.0)
    return parser


def _require_file(path: Path, label: str) -> Path:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"{label} is missing: {path}")
    return path


def _require_dir(path: Path, label: str) -> Path:
    path = path.expanduser().resolve()
    if not path.is_dir():
        raise SystemExit(f"{label} is missing: {path}")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prepare_isolated_environment(
    args: argparse.Namespace,
) -> tuple[Path, Path, Path, dict[str, str]]:
    project_config = _require_file(Path(args.project_config), "project config")
    source_data = _require_dir(Path(args.source_data_dir), "source data directory")
    artifacts_root = _require_dir(Path(args.artifacts_root), "artifact root")
    work_root = Path(args.work_root).expanduser().resolve()
    if work_root.exists() and any(work_root.iterdir()):
        raise SystemExit(f"work root must be new or empty: {work_root}")
    data_dir = work_root / "data"
    evidence_dir = work_root / "evidence"
    execution_dir = work_root / "execution"
    data_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    source_hashes = {}
    for name in ("state.json", "candidate_index.csv"):
        source = _require_file(source_data / name, f"source {name}")
        source_hashes[name] = _sha256(source)
        shutil.copy2(source, data_dir / name)

    os.environ.update({
        "CYCPEP_PROJECT_CONFIG": str(project_config),
        "CYCPEP_DATA_DIR": str(data_dir),
        "CYCPEP_EVIDENCE_DIR": str(evidence_dir),
        "CYCPEP_EXECUTION_ROOT": str(execution_dir),
        "CYCPEP_PREDICTION_ARTIFACTS": str(artifacts_root),
    })
    return work_root, artifacts_root, source_data, source_hashes


def main() -> int:
    args = _parser().parse_args()
    candidates = sorted(set(args.candidate))
    if len(candidates) != len(args.candidate):
        raise SystemExit("candidate IDs must not contain duplicates")
    work_root, artifacts_root, source_data, source_hashes_before = (
        _prepare_isolated_environment(args)
    )

    # Imports are intentionally delayed until the isolated environment is set.
    from agents.orchestrator import initialize, status
    from agents.planner import record_approval, run as planner_run
    from data_layer import ACTIVE_PROJECT_CONFIG, CandidateIndex, State
    from execution.config import ExecutionConfig
    from execution.worker import drain_run
    from prediction_pipeline.contracts import file_sha256, object_sha256

    state = State.sync_project_config(ACTIVE_PROJECT_CONFIG)
    for key in ("critic", "planner", "orchestrator"):
        state.pop(key, None)
    state["phase"] = "critic"
    State.save(state)

    missing_index = [candidate for candidate in candidates if CandidateIndex.find(candidate) is None]
    missing_artifacts = [
        candidate for candidate in candidates
        if not (artifacts_root / candidate / "artifacts.json").is_file()
    ]
    if missing_index or missing_artifacts:
        raise SystemExit(json.dumps({
            "missing_candidate_index": missing_index,
            "missing_artifacts": missing_artifacts,
        }, ensure_ascii=False))

    request_digest = object_sha256({
        "kind": "execution_existing_evidence_selfcheck",
        "project_id": state["project_id"],
        "candidate_ids": candidates,
        "artifact_sha256": {
            candidate: file_sha256(artifacts_root / candidate / "artifacts.json")
            for candidate in candidates
        },
    })
    report_id = f"critic_{request_digest[:12]}"
    issue_code = "execution_prediction_evidence_selfcheck_requested"
    bootstrap = {
        "schema_version": 1,
        "critic_version": "selfcheck-bootstrap-1",
        "report_id": report_id,
        "input_digest": request_digest,
        "source": {
            "prediction_handoff": "selfcheck://existing-artifact-request",
            "prediction_handoff_sha256": request_digest,
            "prediction_run_id": "prediction_selfcheck_bootstrap",
            "prediction_pipeline_version": "selfcheck-bootstrap-1",
            "project_id": state["project_id"],
            "required_targets": [
                item["id"] for item in ACTIVE_PROJECT_CONFIG["targets"]
                if item.get("required", True)
            ],
            "record_count": len(candidates),
        },
        "verdict": "iterate",
        "passed": False,
        "summary": "Explicit isolated request to exercise existing Prediction evidence.",
        "issue_counts": {"high": 1},
        "issues": [{
            "code": issue_code,
            "severity": "high",
            "category": "execution_selfcheck",
            "message": "Run the reviewed Prediction evidence handler for the selected candidates.",
            "candidate_ids": candidates,
            "evidence": [{
                "artifacts_root": str(artifacts_root),
                "request_digest": request_digest,
            }],
            "recommended_action": "complete_prediction_evidence",
            "owner_hint": "prediction",
            "blocks_finalization": True,
        }],
        "metrics_snapshot": {},
        "recommendations": [{
            "action": "complete_prediction_evidence",
            "owner_hint": "prediction",
            "priority": "P0",
            "reason_codes": [issue_code],
            "approval_required": False,
        }],
        "planner_handoff": {
            "critic_report_id": report_id,
            "issue_codes": [issue_code],
            "recommended_actions": ["complete_prediction_evidence"],
            "policy_constraints": POLICY_CONSTRAINTS,
        },
    }
    bootstrap_path = work_root / "bootstrap" / f"{report_id}.json"
    bootstrap_path.parent.mkdir(parents=True, exist_ok=True)
    bootstrap_path.write_text(
        json.dumps(bootstrap, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    plan_result = planner_run(critic_report_path=bootstrap_path)
    required_task_ids = plan_result["plan"]["approval_request"]["required_task_ids"]
    approval_result = record_approval(
        plan_path=plan_result["plan_path"],
        task_ids=required_task_ids,
        approver="execution-selfcheck",
        justification="Isolated MDM2/KEAP1 Execution handler validation",
        max_gpu_job_slots=1,
        max_gpu_minutes=args.max_gpu_minutes,
        max_design_proposals=1,
        max_prediction_candidates=len(candidates),
    )
    initialized = initialize(
        plan_path=plan_result["plan_path"],
        approval_paths=[approval_result["approval_path"]],
    )
    drained = drain_run(
        run_path=initialized["run_path"],
        worker_id=f"execution-selfcheck-{state['project_id']}",
        config=ExecutionConfig.from_environment(),
    )
    final_run = status(run_path=initialized["run_path"])["run"]
    if final_run["status"] != "completed":
        raise SystemExit(json.dumps(drained, ensure_ascii=False, indent=2))

    planned_tasks = {
        task["task_id"]: task for task in plan_result["plan"]["tasks"]
    }
    task_by_action = {
        planned_tasks[task_id]["action"]: (task_id, value)
        for task_id, value in final_run["tasks"].items()
    }
    prediction_id, prediction_task = task_by_action["evaluate_new_design_candidates"]
    critic_id, critic_task = task_by_action["review_prediction_handoff"]
    prediction_path = Path(prediction_task["outputs"][0]["path"])
    critic_path = Path(critic_task["outputs"][0]["path"])
    prediction = json.loads(prediction_path.read_text(encoding="utf-8"))
    critic = json.loads(critic_path.read_text(encoding="utf-8"))
    category_by_candidate = {
        item["candidate_id"]: category
        for category, items in prediction["categories"].items()
        for item in items
    }
    process_labels = [
        process["label"]
        for receipt in drained["receipts"]
        for process in receipt.get("processes", [])
    ]
    source_hashes_after = {
        name: _sha256(source_data / name) for name in source_hashes_before
    }
    source_files_unchanged = source_hashes_after == source_hashes_before
    summary = {
        "status": "passed" if source_files_unchanged else "failed",
        "selfcheck_kind": "isolated_existing_evidence_execution",
        "project_id": state["project_id"],
        "candidate_ids": candidates,
        "plan_id": plan_result["plan"]["plan_id"],
        "orchestrator_run_id": final_run["run_id"],
        "task_statuses": drained["task_statuses"],
        "task_actions": {
            prediction_id: "evaluate_new_design_candidates",
            critic_id: "review_prediction_handoff",
        },
        "prediction": {
            "handoff_path": str(prediction_path),
            "handoff_sha256": file_sha256(prediction_path),
            "candidate_categories": category_by_candidate,
            "process_labels": process_labels,
            "heavy_predictors_rerun": any(
                label.startswith("prediction_af2") or label.startswith("prediction_enrichment")
                for label in process_labels
            ),
        },
        "critic": {
            "report_path": str(critic_path),
            "report_sha256": file_sha256(critic_path),
            "verdict": critic["verdict"],
            "issue_codes": [item["code"] for item in critic["issues"]],
            "source_prediction_handoff_sha256": (
                critic.get("source") or {}
            ).get("prediction_handoff_sha256"),
        },
        "isolation": {
            "source_data_dir": str(source_data),
            "work_data_dir": str(work_root / "data"),
            "source_files_unchanged": source_files_unchanged,
            "source_sha256_before": source_hashes_before,
            "source_sha256_after": source_hashes_after,
        },
    }
    summary_path = work_root / "execution_selfcheck_summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps({**summary, "summary_path": str(summary_path)}, ensure_ascii=False, indent=2))
    return 0 if source_files_unchanged else 2


if __name__ == "__main__":
    raise SystemExit(main())
