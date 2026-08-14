"""Fixed handlers for the four Execution v1 semantic actions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from data_layer import CandidateIndex, State
from calibration_baseline import unpublished_calibration_binding
from prediction_pipeline.contracts import PredictionConfig, file_sha256, object_sha256
from prediction_pipeline.execution_identity import (
    PRODIGY_VERSION,
    build_prediction_execution_identity,
    validate_prediction_execution_identity,
)
from prediction_pipeline.protocol import (
    PREDICTION_PROTOCOL,
)
from .prediction_artifact_gate import artifact_bundle_complete as _artifact_bundle_complete

from .config import ExecutionConfig
from .prediction_runtime import validate_required_prediction_tool_paths
from .contracts import (
    EXECUTION_SCHEMA_VERSION,
    EXECUTION_WORKER_VERSION,
    ExecutionContractError,
    validate_task_parameters,
)
from .supervisor import atomic_json, run_process
from .results import (
    ExecutionActionResult,
    StateAppendMutation,
)
from .prediction_effects import (
    load_prediction_transaction_effects,
    typed_prediction_result,
)
from contracts.candidate_update import CandidateUpdateBatch
from contracts.critic import critic_persistence_effects


# Versioned scientific protocol parameters (Engineering Standard section 8):
# seeds / model numbers / recycles and enrichment seed bases are read from
# protocols/prediction_v1.json.  Operational timeouts and tool paths stay in
# ExecutionConfig.
_AF2_PRODIGY_PROTOCOL = PREDICTION_PROTOCOL["parameters"]["af2_prodigy"]
_ENRICHMENT_PROTOCOL = PREDICTION_PROTOCOL["parameters"]["enrichment"]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class HandlerContext:
    packet: dict
    config: ExecutionConfig
    task_dir: Path
    project_config: dict | None = None
    transaction_managed: bool = False
    transaction_id: str | None = None

    def artifact_id_for(self, role: str) -> str:
        """Predict the committed artifact id for one output role.

        Must stay in sync with the adapter's staging naming so handlers can
        reference formal artifact identity in state updates before commit.
        """
        if not self.transaction_id:
            raise ExecutionContractError(
                "transaction_context_required",
                "artifact identity requires a transaction-managed context",
            )
        return f"{self.transaction_id}-{role}"

    @property
    def task(self) -> dict:
        return self.packet["task"]

    @property
    def parameters(self) -> dict:
        return validate_task_parameters(self.task)


HandlerOutcome = ExecutionActionResult


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


def _resolve_project(context: HandlerContext, state: dict) -> dict:
    """Return the approved project config, honouring an explicit injection.

    An injected config is copied so handlers never share a mutable reference
    with the caller.  Planner and Execution must be injected consistently;
    ``iterate_design`` fails closed with ``project_config_drift`` otherwise.
    """
    if context.project_config is not None:
        return dict(context.project_config)
    return state.get("project_config") or State._project_config


def _project_digest(context: HandlerContext) -> str:
    return object_sha256(_resolve_project(context, State.load()))


def _resolve_manifest(raw: str, repo_root: Path) -> Path:
    path = Path(str(raw or "")).expanduser()
    return path.resolve() if path.is_absolute() else (repo_root / path).resolve()


def iterate_design(context: HandlerContext) -> ExecutionActionResult:
    params = context.parameters
    state = State.load()
    project = _resolve_project(context, state)
    if object_sha256(project) != params["project_config_digest"]:
        raise ExecutionContractError(
            "project_config_drift", "approved project config changed after planning"
        )
    project_snapshot = context.task_dir / "inputs" / "approved_project_config.json"
    atomic_json(project_snapshot, project)
    before_rows = CandidateIndex.load()
    before_by_id = {str(row.get("candidate_id")): row for row in before_rows}
    before_digest = object_sha256(before_rows)
    before_snapshot = context.task_dir / "snapshots" / "candidate_index_before.json"
    before_snapshot.parent.mkdir(parents=True, exist_ok=True)
    before_snapshot.write_text(
        json.dumps(before_rows, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    processes = []
    candidate_updates = []
    for index, job in enumerate(params["design_jobs"], start=1):
        updates_path = context.task_dir / "candidate_updates" / f"job_{index:02d}.json"
        argv = [
            context.config.design_python,
            context.config.repo_root / "agents" / "design.py",
            "--route", job["route"],
            "--target", job["target_id"],
            "--n", str(job["proposal_count"]),
            "--lengths", ",".join(str(value) for value in job["lengths"]),
            "--seed", str(job["seed"]),
            "--candidate-updates-path", str(updates_path),
            "--candidate-update-job-id", f"{context.task['task_id']}-job-{index:02d}",
            "--project-config", str(project_snapshot),
        ]
        processes.append(run_process(
            argv,
            cwd=context.config.repo_root,
            logs_dir=context.task_dir / "processes" / f"design_job_{index:02d}",
            timeout_seconds=context.config.design_timeout_seconds,
            environment={"CYCPEP_PROJECT_CONFIG": str(project_snapshot)},
            label=f"iterate_design[{index}]",
        ))
        if not updates_path.is_file():
            raise ExecutionContractError(
                "design_candidate_updates_missing",
                f"Design job {index} did not emit its CandidateUpdate batch",
            )
        batch = CandidateUpdateBatch.from_dict(
            json.loads(updates_path.read_text(encoding="utf-8"))
        )
        candidate_updates.extend(item.to_dict() for item in batch.candidate_updates)

    update_ids = [str(item["candidate_id"]) for item in candidate_updates]
    if len(update_ids) != len(set(update_ids)):
        raise ExecutionContractError(
            "design_candidate_update_duplicate",
            "Design jobs emitted duplicate candidate IDs",
        )
    prepared_updates = [CandidateIndex._prepare_row(item) for item in candidate_updates]
    after_rows = sorted(
        [*before_rows, *prepared_updates],
        key=lambda item: str(item.get("candidate_id") or ""),
    )
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
    new_ids = sorted(update_ids)
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
    after_snapshot = context.task_dir / "snapshots" / "candidate_index_after.json"
    after_snapshot.write_text(
        json.dumps(after_rows, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
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
        "candidate_index_before_snapshot": {
            "path": str(before_snapshot),
            "sha256": file_sha256(before_snapshot),
        },
        "candidate_index_after_snapshot": {
            "path": str(after_snapshot),
            "sha256": file_sha256(after_snapshot),
        },
        "new_candidate_ids": new_ids,
        "candidates": candidates,
        "existing_rows_unchanged": True,
        "completed_at": _utcnow(),
    })
    return ExecutionActionResult(
        candidate_updates=tuple(prepared_updates),
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


def _link_candidate_artifacts(source_dir: Path, staging_root: Path, candidate_id: str) -> None:
    destination = staging_root / candidate_id
    if destination.exists() or destination.is_symlink():
        raise ExecutionContractError(
            "prediction_staging_conflict", f"staging destination exists: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.symlink_to(source_dir.resolve(), target_is_directory=True)


def _observe_prediction_runtime(
    config: ExecutionConfig, expected_identity: dict
) -> tuple[dict, dict]:
    """Observe the existing pinned Prediction runtime before expensive work."""
    from prediction_pipeline.adapters import validate_prodigy_runtime
    from prediction_pipeline.boltz_worker import validate_boltz_runtime
    from prediction_pipeline.colabdesign_worker import validate_colabdesign_runtime
    from prediction_pipeline.rosetta_worker import validate_pyrosetta_runtime

    validate_required_prediction_tool_paths(config)
    try:
        prediction_config = PredictionConfig.from_dict(
            expected_identity.get("prediction_config")
        )
        commit = validate_colabdesign_runtime(
            config.colabdesign_dir,
            expected_commit=prediction_config.colabdesign_commit,
        )
        boltz = validate_boltz_runtime(
            config.boltz_executable,
            config.boltz_checkpoint,
            timeout=min(config.prediction_timeout_seconds, 60),
        )
        pyrosetta = validate_pyrosetta_runtime(config.pyrosetta_python)
        prodigy = validate_prodigy_runtime(
            config.prodigy_executable, PRODIGY_VERSION
        )
        observed = build_prediction_execution_identity(
            prediction_config,
            observations={
                "colabdesign_commit": commit,
                "boltz_version": boltz["version"],
                "boltz_checkpoint_sha256": boltz["checkpoint_sha256"],
                "pyrosetta_version": pyrosetta,
                "prodigy_version": prodigy,
            },
        )
        validate_prediction_execution_identity(observed, expected=expected_identity)
    except ContractError as exc:
        error = ExecutionContractError(exc.code, str(exc))
        error.retryable = True
        raise error from exc
    metadata = {
        "boltz_version": boltz["version"],
        "boltz_checkpoint_sha256": boltz["checkpoint_sha256"],
        "boltz_no_kernels": config.boltz_no_kernels,
        "colabdesign_commit": commit,
        "pyrosetta_version": pyrosetta,
        "prodigy_version": prodigy,
    }
    return observed, metadata


def _prediction_transaction_effects(
    context: HandlerContext,
    path: Path,
    candidate_ids: list[str],
    run_id: str,
    expected_calibration_binding: dict,
) -> dict:
    return load_prediction_transaction_effects(
        path=path,
        candidate_ids=candidate_ids,
        run_id=run_id,
        transaction_id=str(context.transaction_id),
        expected_protocol=context.parameters["predictor_protocol"],
        expected_calibration_binding=expected_calibration_binding,
    )


def _typed_prediction_result(
    effects: dict,
    handoff: Path,
    processes: list[dict],
) -> HandlerOutcome:
    return typed_prediction_result(effects, handoff, processes)


def _prepare_prediction_candidate_artifacts(
    context: HandlerContext,
    candidate_ids: list[str],
    required_targets: list[str],
    execution_identity: dict,
) -> tuple[Path, list[dict], dict]:
    params = context.parameters
    staging_root = context.task_dir / "prediction_artifacts"
    base_root = context.task_dir / "base_prediction_artifacts"
    enrichment_root = context.task_dir / "enriched_prediction_artifacts"
    processes: list[dict] = []
    missing = []
    observed = None
    for candidate_id in candidate_ids:
        existing_dir = context.config.prediction_artifacts_root / candidate_id
        bundle = existing_dir / "artifacts.json"
        if params["reuse_complete_evidence"] and _artifact_bundle_complete(
            bundle, required_targets, execution_identity
        ):
            reused = _json_object(
                bundle.with_name("execution_identity.json"),
                "prediction execution identity",
            )
            if observed not in (None, reused):
                raise ExecutionContractError(
                    "prediction_execution_identity_mismatch",
                    "reused bundles carry different observed execution identities",
                )
            observed = reused
            _link_candidate_artifacts(existing_dir, staging_root, candidate_id)
        else:
            missing.append(candidate_id)
    if missing and params["evidence_mode"] == "ingest_existing":
        raise ExecutionContractError(
            "prediction_artifacts_missing",
            f"complete existing artifacts are unavailable for {missing}",
        )
    if missing:
        observed, generated_processes = _generate_prediction_artifacts(
            context, missing, base_root, enrichment_root,
            required_targets, execution_identity,
        )
        processes.extend(generated_processes)
        for candidate_id in missing:
            _link_candidate_artifacts(
                enrichment_root / candidate_id, staging_root, candidate_id
            )
    if not processes:
        processes.append({
            "label": "prediction_runtime_reuse", "elapsed_seconds": 0.0,
            "observed_execution_identity": observed,
            "runtime_metadata": {"source": "validated_complete_bundle"},
        })
    return staging_root, processes, observed


def _generate_prediction_artifacts(
    context, missing, base_root, enrichment_root, required_targets, expected_identity
):
    observed, metadata = _observe_prediction_runtime(
        context.config, expected_identity
    )
    processes = [{
        "label": "prediction_runtime_preflight", "elapsed_seconds": 0.0,
        "observed_execution_identity": observed, "runtime_metadata": metadata,
    }]
    argv = [
            context.config.prediction_python,
            context.config.repo_root / "scripts" / "run_prediction_predictors.py",
            "--artifacts-root", base_root, "--python", context.config.prediction_python,
            "--data-dir", context.config.colabdesign_params,
            "--colabdesign-dir", context.config.colabdesign_dir,
            "--cuda-data-dir", context.config.cuda_data_dir,
            "--seeds", ",".join(str(v) for v in _AF2_PRODIGY_PROTOCOL["seeds"]),
            "--model-numbers", ",".join(
                str(v) for v in _AF2_PRODIGY_PROTOCOL["model_numbers"]
            ),
            "--num-recycles", str(_AF2_PRODIGY_PROTOCOL["num_recycles"]),
            "--timeout", str(context.config.prediction_timeout_seconds),
            "--prodigy", context.config.prodigy_executable,
    ]
    for candidate_id in missing:
        argv.extend(["--candidate", candidate_id])
    processes.append(run_process(
            argv, cwd=context.config.repo_root,
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
                "--source-bundle", source_bundle, "--output-root", enrichment_root,
                "--boltz", context.config.boltz_executable,
                "--boltz-cache", context.config.boltz_cache,
                "--boltz-checkpoint", context.config.boltz_checkpoint,
                "--prodigy", context.config.prodigy_executable,
                "--pyrosetta-python", context.config.pyrosetta_python,
                "--post-relax-python", context.config.pyrosetta_python,
                "--seed", str(_ENRICHMENT_PROTOCOL["seed_base"] + offset),
                "--post-relax-seed",
                str(_ENRICHMENT_PROTOCOL["post_relax_seed_base"] + offset),
                "--post-relax-repeats", str(_ENRICHMENT_PROTOCOL["post_relax_repeats"]),
                "--timeout", str(context.config.prediction_timeout_seconds),
                "--rosetta-timeout", str(context.config.rosetta_timeout_seconds),
                "--post-relax-timeout", str(context.config.post_relax_timeout_seconds),
        ]
        if context.config.boltz_no_kernels:
            argv.append("--no-kernels")
        processes.append(run_process(
                argv, cwd=context.config.repo_root,
                logs_dir=context.task_dir / "processes" / f"enrich_{candidate_id}",
                timeout_seconds=(context.config.prediction_timeout_seconds
                    + context.config.rosetta_timeout_seconds
                    + context.config.post_relax_timeout_seconds),
                label=f"prediction_enrichment[{candidate_id}]",
        ))
        completed = enrichment_root / candidate_id / "artifacts.json"
        atomic_json(completed.with_name("execution_identity.json"), observed)
        if not _artifact_bundle_complete(completed, required_targets, expected_identity):
            raise ExecutionContractError(
                "prediction_enrichment_incomplete",
                f"enrichment did not create full evidence for {candidate_id}",
            )
    return observed, processes


def evaluate_new_design_candidates(context: HandlerContext) -> HandlerOutcome:
    params = context.parameters
    execution_identity = params["execution_identity"]
    candidate_ids = _prediction_candidate_ids(context)
    state = State.load()
    thresholds = state.get("thresholds") or {}
    expected_calibration_binding = state.get("threshold_calibration_binding")
    if expected_calibration_binding is None:
        expected_calibration_binding = unpublished_calibration_binding(thresholds)
    project = _resolve_project(context, state)
    required_targets = [
        str(item["id"]) for item in project.get("targets", []) if item.get("required", True)
    ]
    staging_root, processes, observed_execution_identity = (
        _prepare_prediction_candidate_artifacts(
            context, candidate_ids, required_targets, execution_identity
        )
    )

    run_id = (
        f"prediction_exec_{context.packet['run_id'].removeprefix('orchestrator_')}"
        f"_{context.task['task_id'].lower()}_a{context.packet['task_attempt']}"
    )
    run_root = context.task_dir / "prediction_runs"
    effects_path = context.task_dir / "prediction_transaction_effects.json"
    if observed_execution_identity is None:
        raise ExecutionContractError(
            "prediction_execution_identity_missing",
            "Prediction runtime did not produce an observed execution identity",
        )
    identity_path = context.task_dir / "observed_execution_identity.json"
    atomic_json(identity_path, observed_execution_identity)
    argv = [
        context.config.prediction_python,
        context.config.repo_root / "agents" / "prediction.py",
        "run",
        "--artifacts-root", staging_root,
        "--run-root", run_root,
        "--run-id", run_id,
        "--execution-identity", identity_path,
    ]
    if context.transaction_managed:
        argv.extend([
            "--effects-output", effects_path,
            "--transaction-id", context.transaction_id,
        ])
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
    if context.transaction_managed:
        effects = _prediction_transaction_effects(
            context, effects_path, candidate_ids, run_id,
            expected_calibration_binding,
        )
        return _typed_prediction_result(effects, handoff, processes)
    return HandlerOutcome(
        outputs=(("prediction_handoff", handoff),),
        processes=tuple(processes),
    )


def review_prediction_handoff(context: HandlerContext) -> HandlerOutcome:
    params = context.parameters
    handoff = _dependency_output(context, "prediction_handoff")
    output = context.task_dir / "outputs" / "critic_report.json"
    if context.transaction_managed:
        from agents.critic import CriticConfig, review

        state = State.load()
        report = review(
            handoff_path=handoff,
            state=state,
            config=CriticConfig(
                min_cohort_for_distribution=params["min_cohort"],
                low_diversity_median_similarity=params["low_diversity_similarity"],
            ),
            project_config=context.project_config,
        )
        atomic_json(output, report)
        state_updates, evidence = critic_persistence_effects(
            report=report,
            report_digest=file_sha256(output),
            state=state,
            report_artifact_id=context.artifact_id_for("critic_report"),
        )
        trace_project_id = (context.packet.get("trace_context") or {}).get(
            "project_id"
        )
        if (
            trace_project_id is not None
            and trace_project_id != evidence["project_id"]
        ):
            raise ExecutionContractError(
                "critic_project_binding_mismatch",
                "Critic report and transaction trace project binding disagree",
            )
        return HandlerOutcome(
            state_updates=state_updates,
            state_appends=(StateAppendMutation(
                key="iteration_history",
                item=evidence["history_entry"],
                identity_path=("summary", "report_id"),
                identity_value=report["report_id"],
            ),),
            evidence_events=({
                "agent": "critic",
                "event_type": "critic_review",
                "phase": "critic",
                **evidence["event_payload"],
            },),
            outputs=(("critic_report", output),),
        )
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
    project = _resolve_project(context, state)
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
    evidence_events = ()
    if context.transaction_managed:
        evidence_events = ({
            "agent": "execution",
            "event_type": "threshold_calibration",
            "phase": "iterate",
            "status": "ready_for_calibration" if controls_available else "pending_controls",
            "requested_threshold_keys": list(requested),
            "applied_to_state": False,
        },)
    return HandlerOutcome(
        evidence_events=evidence_events,
        outputs=(("calibration_proposal", output),),
    )


HANDLERS: dict[str, Callable[[HandlerContext], HandlerOutcome]] = {
    "iterate_design": iterate_design,
    "evaluate_new_design_candidates": evaluate_new_design_candidates,
    "review_prediction_handoff": review_prediction_handoff,
    "propose_threshold_calibration": propose_threshold_calibration,
}
