"""Fixed handlers for the four Execution v1 semantic actions."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import data_layer
from data_layer import CandidateIndex, State
from prediction_pipeline.contracts import file_sha256, object_sha256

from .config import ExecutionConfig
from .contracts import (
    EXECUTION_SCHEMA_VERSION,
    EXECUTION_WORKER_VERSION,
    ExecutionContractError,
    validate_task_parameters,
)
from .supervisor import atomic_json, run_process


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class HandlerContext:
    packet: dict
    config: ExecutionConfig
    task_dir: Path

    @property
    def task(self) -> dict:
        return self.packet["task"]

    @property
    def parameters(self) -> dict:
        return validate_task_parameters(self.task)


@dataclass(frozen=True)
class HandlerOutcome:
    outputs: tuple[tuple[str, Path], ...]
    processes: tuple[dict, ...] = ()

    @property
    def elapsed_seconds(self) -> float:
        return sum(float(item.get("elapsed_seconds") or 0.0) for item in self.processes)


def _dependency_output(context: HandlerContext, role: str) -> Path:
    matches = []
    dependencies = context.packet.get("dependency_outputs") or {}
    for outputs in dependencies.values():
        for item in outputs or []:
            if item.get("role") == role:
                path = Path(str(item.get("path") or "")).expanduser().resolve()
                if not path.is_file() or file_sha256(path) != item.get("sha256"):
                    raise ExecutionContractError(
                        "dependency_output_changed", f"dependency {role} is missing or changed"
                    )
                matches.append(path)
    if len(matches) != 1:
        raise ExecutionContractError(
            "dependency_output_ambiguous",
            f"expected one dependency output role={role}; found {matches}",
        )
    return matches[0]


def _json_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionContractError(f"{label}_invalid", f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise ExecutionContractError(f"{label}_invalid", f"{label} must be a JSON object")
    return value


def _project_digest() -> str:
    state = State.load()
    project = state.get("project_config") or State._project_config
    return object_sha256(project)


def _resolve_manifest(raw: str, repo_root: Path) -> Path:
    path = Path(str(raw or "")).expanduser()
    return path.resolve() if path.is_absolute() else (repo_root / path).resolve()


def iterate_design(context: HandlerContext) -> HandlerOutcome:
    params = context.parameters
    state = State.load()
    project = state.get("project_config") or State._project_config
    if object_sha256(project) != params["project_config_digest"]:
        raise ExecutionContractError(
            "project_config_drift", "approved project config changed after planning"
        )
    before_rows = CandidateIndex.load()
    before_by_id = {str(row.get("candidate_id")): row for row in before_rows}
    before_digest = object_sha256(before_rows)
    processes = []
    for index, job in enumerate(params["design_jobs"], start=1):
        argv = [
            context.config.design_python,
            context.config.repo_root / "agents" / "design.py",
            "--route", job["route"],
            "--target", job["target_id"],
            "--n", str(job["proposal_count"]),
            "--lengths", ",".join(str(value) for value in job["lengths"]),
            "--seed", str(job["seed"]),
        ]
        processes.append(run_process(
            argv,
            cwd=context.config.repo_root,
            logs_dir=context.task_dir / "processes" / f"design_job_{index:02d}",
            timeout_seconds=context.config.design_timeout_seconds,
            label=f"iterate_design[{index}]",
        ))

    after_rows = CandidateIndex.load()
    after_by_id = {str(row.get("candidate_id")): row for row in after_rows}
    changed = sorted(
        candidate_id for candidate_id, row in before_by_id.items()
        if after_by_id.get(candidate_id) != row
    )
    if changed:
        raise ExecutionContractError(
            "candidate_index_existing_row_changed",
            f"Design modified existing candidate rows: {changed}",
        )
    new_ids = sorted(set(after_by_id) - set(before_by_id))
    limit = int(context.task["resource_request"]["candidate_limit"])
    if len(new_ids) > limit:
        raise ExecutionContractError(
            "design_candidate_limit_exceeded",
            f"Design registered {len(new_ids)} candidates; task limit is {limit}",
        )
    candidates = []
    for candidate_id in new_ids:
        row = after_by_id[candidate_id]
        manifest = _resolve_manifest(str(row.get("manifest_path") or ""), context.config.repo_root)
        if not manifest.is_file():
            raise ExecutionContractError(
                "design_manifest_missing", f"new candidate {candidate_id} lacks manifest: {manifest}"
            )
        candidates.append({
            "candidate_id": candidate_id,
            "sequence": row.get("sequence"),
            "source_route": row.get("source_route"),
            "manifest_path": str(manifest),
            "manifest_sha256": file_sha256(manifest),
            "design_pdb_path": row.get("design_pdb_path"),
            "design_pdb_hash": row.get("design_pdb_hash"),
            "backbone_pdb": row.get("backbone_pdb"),
        })
    result_path = context.task_dir / "outputs" / "design_task_result.json"
    atomic_json(result_path, {
        "schema_version": EXECUTION_SCHEMA_VERSION,
        "execution_worker_version": EXECUTION_WORKER_VERSION,
        "action": context.task["action"],
        "task_id": context.task["task_id"],
        "project_id": project.get("project_id"),
        "project_config_digest": object_sha256(project),
        "jobs": params["design_jobs"],
        "candidate_index_before_sha256": before_digest,
        "candidate_index_after_sha256": object_sha256(after_rows),
        "new_candidate_ids": new_ids,
        "candidates": candidates,
        "existing_rows_unchanged": True,
        "completed_at": _utcnow(),
    })
    return HandlerOutcome(
        outputs=(("design_result", result_path),),
        processes=tuple(processes),
    )


def _prediction_candidate_ids(context: HandlerContext) -> list[str]:
    explicit = list((context.task.get("candidate_scope") or {}).get("candidate_ids") or [])
    from_task = (context.task.get("candidate_scope") or {}).get("from_task_id")
    if from_task:
        design_result = _json_object(
            _dependency_output(context, "design_result"), "design_result"
        )
        upstream = list(design_result.get("new_candidate_ids") or [])
        if explicit and explicit != upstream:
            raise ExecutionContractError(
                "prediction_candidate_scope_mismatch",
                "explicit candidate scope differs from Design output",
            )
        explicit = upstream
    if not explicit:
        raise ExecutionContractError(
            "prediction_candidate_scope_empty", "Prediction task has no candidates"
        )
    limit = int(context.task["resource_request"]["candidate_limit"])
    if len(explicit) > limit:
        raise ExecutionContractError(
            "prediction_candidate_limit_exceeded",
            f"Prediction received {len(explicit)} candidates; task limit is {limit}",
        )
    return explicit


def _artifact_bundle_complete(path: Path, required_targets: list[str]) -> bool:
    if not path.is_file():
        return False
    try:
        raw = _json_object(path, "artifact_bundle")
        global_values = raw.get("global") or {}
        if not global_values.get("monomer_predictions"):
            return False
        if not global_values.get("post_relax_pdb") or not global_values.get("post_relax_metadata"):
            return False
        targets = raw.get("targets") or {}
        for target_id in required_targets:
            values = targets.get(target_id) or {}
            predictions = values.get("complex_predictions") or []
            if len(predictions) < 4:
                return False
            if len(values.get("prodigy_outputs") or []) != len(predictions):
                return False
            if len(values.get("rosetta_outputs") or []) != len(predictions):
                return False
    except ExecutionContractError:
        return False
    return True


def _link_candidate_artifacts(source_dir: Path, staging_root: Path, candidate_id: str) -> None:
    destination = staging_root / candidate_id
    if destination.exists() or destination.is_symlink():
        raise ExecutionContractError(
            "prediction_staging_conflict", f"staging destination exists: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.symlink_to(source_dir.resolve(), target_is_directory=True)


def _require_prediction_tools(config: ExecutionConfig) -> None:
    required = {
        "boltz_executable": config.boltz_executable,
        "boltz_cache": config.boltz_cache,
        "boltz_checkpoint": config.boltz_checkpoint,
        "prodigy_executable": config.prodigy_executable,
        "pyrosetta_python": config.pyrosetta_python,
    }
    missing = sorted(name for name, path in required.items() if path is None or not path.exists())
    if missing:
        raise ExecutionContractError(
            "prediction_toolchain_incomplete",
            f"full Prediction requires configured tools: {missing}",
        )


def evaluate_new_design_candidates(context: HandlerContext) -> HandlerOutcome:
    params = context.parameters
    candidate_ids = _prediction_candidate_ids(context)
    state = State.load()
    project = state.get("project_config") or State._project_config
    required_targets = [
        str(item["id"]) for item in project.get("targets", []) if item.get("required", True)
    ]
    staging_root = context.task_dir / "prediction_artifacts"
    base_root = context.task_dir / "base_prediction_artifacts"
    enrichment_root = context.task_dir / "enriched_prediction_artifacts"
    processes = []
    missing = []
    for candidate_id in candidate_ids:
        existing_dir = context.config.prediction_artifacts_root / candidate_id
        bundle = existing_dir / "artifacts.json"
        if params["reuse_complete_evidence"] and _artifact_bundle_complete(bundle, required_targets):
            _link_candidate_artifacts(existing_dir, staging_root, candidate_id)
        else:
            missing.append(candidate_id)

    if missing and params["evidence_mode"] == "ingest_existing":
        raise ExecutionContractError(
            "prediction_artifacts_missing",
            f"complete existing artifacts are unavailable for {missing}",
        )
    if missing:
        _require_prediction_tools(context.config)
        argv = [
            context.config.prediction_python,
            context.config.repo_root / "scripts" / "run_prediction_predictors.py",
            "--artifacts-root", base_root,
            "--python", context.config.prediction_python,
            "--data-dir", context.config.colabdesign_params,
            "--colabdesign-dir", context.config.colabdesign_dir,
            "--cuda-data-dir", context.config.cuda_data_dir,
            "--seeds", "0,1,2",
            "--model-numbers", "0,1,2",
            "--num-recycles", "3",
            "--timeout", str(context.config.prediction_timeout_seconds),
            "--prodigy", context.config.prodigy_executable,
        ]
        for candidate_id in missing:
            argv.extend(["--candidate", candidate_id])
        processes.append(run_process(
            argv,
            cwd=context.config.repo_root,
            logs_dir=context.task_dir / "processes" / "af2_prodigy",
            timeout_seconds=max(
                context.config.prediction_timeout_seconds,
                context.config.prediction_timeout_seconds * len(missing),
            ),
            label="prediction_af2_prodigy",
        ))
        for offset, candidate_id in enumerate(missing):
            source_bundle = base_root / candidate_id / "artifacts.json"
            argv = [
                context.config.core_python,
                context.config.repo_root / "scripts" / "enrich_prediction_evidence.py",
                "--source-bundle", source_bundle,
                "--output-root", enrichment_root,
                "--boltz", context.config.boltz_executable,
                "--boltz-cache", context.config.boltz_cache,
                "--boltz-checkpoint", context.config.boltz_checkpoint,
                "--prodigy", context.config.prodigy_executable,
                "--pyrosetta-python", context.config.pyrosetta_python,
                "--post-relax-python", context.config.pyrosetta_python,
                "--seed", str(101 + offset),
                "--post-relax-seed", str(20260802 + offset),
                "--post-relax-repeats", "3",
                "--timeout", str(context.config.prediction_timeout_seconds),
                "--rosetta-timeout", str(context.config.rosetta_timeout_seconds),
                "--post-relax-timeout", str(context.config.post_relax_timeout_seconds),
            ]
            processes.append(run_process(
                argv,
                cwd=context.config.repo_root,
                logs_dir=context.task_dir / "processes" / f"enrich_{candidate_id}",
                timeout_seconds=(
                    context.config.prediction_timeout_seconds
                    + context.config.rosetta_timeout_seconds
                    + context.config.post_relax_timeout_seconds
                ),
                label=f"prediction_enrichment[{candidate_id}]",
            ))
            completed_bundle = enrichment_root / candidate_id / "artifacts.json"
            if not _artifact_bundle_complete(completed_bundle, required_targets):
                raise ExecutionContractError(
                    "prediction_enrichment_incomplete",
                    f"enrichment did not create full evidence for {candidate_id}",
                )
            _link_candidate_artifacts(completed_bundle.parent, staging_root, candidate_id)

    run_id = (
        f"prediction_exec_{context.packet['run_id'].removeprefix('orchestrator_')}"
        f"_{context.task['task_id'].lower()}_a{context.packet['task_attempt']}"
    )
    run_root = context.task_dir / "prediction_runs"
    argv = [
        context.config.prediction_python,
        context.config.repo_root / "agents" / "prediction.py",
        "run",
        "--artifacts-root", staging_root,
        "--run-root", run_root,
        "--run-id", run_id,
    ]
    for candidate_id in candidate_ids:
        argv.extend(["--candidate", candidate_id])
    processes.append(run_process(
        argv,
        cwd=context.config.repo_root,
        logs_dir=context.task_dir / "processes" / "prediction_ingest",
        timeout_seconds=max(300, context.config.prediction_timeout_seconds),
        label="prediction_ingest",
    ))
    handoff = run_root / run_id / "prediction_handoff.json"
    if not handoff.is_file():
        raise ExecutionContractError(
            "prediction_handoff_missing", f"Prediction did not write handoff: {handoff}"
        )
    return HandlerOutcome(
        outputs=(("prediction_handoff", handoff),),
        processes=tuple(processes),
    )


def review_prediction_handoff(context: HandlerContext) -> HandlerOutcome:
    params = context.parameters
    handoff = _dependency_output(context, "prediction_handoff")
    output = context.task_dir / "outputs" / "critic_report.json"
    process = run_process(
        [
            context.config.core_python,
            context.config.repo_root / "agents" / "critic.py",
            "review",
            "--handoff", handoff,
            "--output", output,
            "--min-cohort", str(params["min_cohort"]),
            "--low-diversity-similarity", str(params["low_diversity_similarity"]),
        ],
        cwd=context.config.repo_root,
        logs_dir=context.task_dir / "processes" / "critic",
        timeout_seconds=900,
        label="critic_review",
    )
    return HandlerOutcome(
        outputs=(("critic_report", output),),
        processes=(process,),
    )


def propose_threshold_calibration(context: HandlerContext) -> HandlerOutcome:
    params = context.parameters
    state = State.load()
    project = state.get("project_config") or State._project_config
    thresholds = state.get("thresholds") or {}
    requested = params["threshold_keys"]
    snapshot = {
        key: thresholds.get(key.split(":", 1)[0])
        for key in requested
    }
    control_path = context.config.control_data_path
    controls_available = bool(control_path and control_path.is_file())
    output = context.task_dir / "outputs" / "threshold_calibration_proposal.json"
    atomic_json(output, {
        "schema_version": EXECUTION_SCHEMA_VERSION,
        "execution_worker_version": EXECUTION_WORKER_VERSION,
        "action": context.task["action"],
        "task_id": context.task["task_id"],
        "project_id": project.get("project_id"),
        "status": "ready_for_calibration" if controls_available else "pending_controls",
        "requested_threshold_keys": requested,
        "current_thresholds": snapshot,
        "control_data": {
            "path": str(control_path) if control_path else None,
            "sha256": file_sha256(control_path) if controls_available else None,
            "available": controls_available,
        },
        "control_requirements": {
            "same_protocol_required": True,
            "minimum_positive_controls": 3,
            "minimum_negative_controls": 10,
            "maximum_false_positive_rate": 0.05,
            "minimum_positive_recall": 0.50,
        },
        "next_action": (
            "run_reviewed_offline_calibration"
            if controls_available else "collect_same_protocol_positive_negative_controls"
        ),
        "applied_to_state": False,
        "created_at": _utcnow(),
    })
    return HandlerOutcome(outputs=(("calibration_proposal", output),))


HANDLERS: dict[str, Callable[[HandlerContext], HandlerOutcome]] = {
    "iterate_design": iterate_design,
    "evaluate_new_design_candidates": evaluate_new_design_candidates,
    "review_prediction_handoff": review_prediction_handoff,
    "propose_threshold_calibration": propose_threshold_calibration,
}
