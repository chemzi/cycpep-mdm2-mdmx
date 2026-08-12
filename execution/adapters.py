"""Adapters from existing handlers to the transaction worker contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from contracts.transaction import TransactionContext

from .config import ExecutionConfig

from .contracts import ExecutionContractError, validate_output_inventory
from .results import (
    CandidatePatchMutation,
    ExecutionActionResult,
)
from .staging import StagingArea


TRANSACTIONAL_ACTIONS = frozenset({
    "iterate_design",
    "evaluate_new_design_candidates",
    "review_prediction_handoff",
    "propose_threshold_calibration",
})


def _semantic_output_inventory(result: ExecutionActionResult) -> list[dict]:
    return [
        {"role": role, "path": str(path)}
        for role, path in result.outputs
    ]


def _observed_prediction_identity(
    result: ExecutionActionResult, expected: dict
) -> dict:
    observations = [
        item.get("observed_execution_identity")
        for item in result.processes
        if item.get("observed_execution_identity") is not None
    ]
    if len(observations) != 1:
        raise ExecutionContractError(
            "prediction_execution_identity_missing",
            "Prediction result requires one observed runtime identity",
        )
    if observations[0] != expected:
        raise ExecutionContractError(
            "prediction_execution_identity_mismatch",
            "observed Prediction runtime differs from the approved identity",
        )
    return observations[0]


def _prediction_artifacts(
    result: ExecutionActionResult,
    context: TransactionContext,
    staging: StagingArea,
    expected_protocol: dict,
    expected_execution_identity: dict,
    artifact_root: Path,
) -> tuple[list, dict[str, str]]:
    handoff_paths = [
        path for role, path in result.outputs if role == "prediction_handoff"
    ]
    if len(handoff_paths) != 1:
        raise ExecutionContractError(
            "prediction_handoff_invalid", "typed Prediction requires one handoff"
        )
    handoff = json.loads(handoff_paths[0].read_text(encoding="utf-8"))
    if handoff.get("protocol_identity") != expected_protocol:
        raise ExecutionContractError(
            "prediction_protocol_mismatch",
            "Prediction handoff does not match the task protocol identity",
        )
    recorded_identity = handoff.get("execution_identity")
    if recorded_identity != expected_execution_identity:
        raise ExecutionContractError(
            "prediction_execution_identity_mismatch",
            "Prediction handoff conflicts with the approved execution identity",
        )
    staged = []
    committed_inputs: dict[tuple[str, str], tuple[str, str]] = {}
    record_shas: dict[str, str] = {}
    approved_candidates = {
        mutation.candidate_id for mutation in result.candidate_patches
    }
    seen_candidates: set[str] = set()
    input_index = 0
    for entries in (handoff.get("categories") or {}).values():
        for item in entries:
            candidate_id = str(item.get("candidate_id") or "")
            if candidate_id not in approved_candidates or candidate_id in seen_candidates:
                raise ExecutionContractError(
                    "prediction_effects_scope_mismatch",
                    "Prediction handoff exceeds the approved candidate scope",
                )
            seen_candidates.add(candidate_id)
            record_path = Path(str(item.get("record_path") or "")).resolve()
            expected_id = f"{context.transaction_id}-prediction-record-{candidate_id}"
            if item.get("record_artifact_id") != expected_id:
                raise ExecutionContractError(
                    "prediction_record_invalid",
                    f"Prediction record artifact identity mismatch for {candidate_id}",
                )
            if not record_path.is_file():
                raise ExecutionContractError(
                    "prediction_record_invalid",
                    f"Prediction record is missing: {record_path}",
                )
            record = json.loads(record_path.read_text(encoding="utf-8"))
            if record.get("protocol_identity") != expected_protocol:
                raise ExecutionContractError(
                    "prediction_protocol_mismatch",
                    f"Prediction record protocol mismatch for {candidate_id}",
                )
            record_identity = record.get("execution_identity")
            if record_identity != expected_execution_identity:
                raise ExecutionContractError(
                    "prediction_execution_identity_mismatch",
                    f"Prediction record execution identity mismatch for {candidate_id}",
                )
            inventory = record.get("artifact_inventory") or []
            if not isinstance(inventory, list) or any(
                not isinstance(artifact, dict) for artifact in inventory
            ):
                raise ExecutionContractError(
                    "prediction_record_invalid",
                    f"Prediction artifact inventory is invalid for {candidate_id}",
                )
            for artifact in inventory:
                source = Path(str(artifact.get("path") or "")).resolve()
                declared_sha = str(artifact.get("sha256") or "")
                identity = (str(source), declared_sha)
                committed = committed_inputs.get(identity)
                if committed is None:
                    input_index += 1
                    artifact_id = (
                        f"{context.transaction_id}-prediction-input-{input_index:04d}"
                    )
                    staged_input = staging.stage_artifact(
                        source,
                        artifact_id=artifact_id,
                        artifact_type=(
                            f"prediction_input:{artifact.get('role') or 'unknown'}"
                        ),
                    )
                    if staged_input.sha256 != declared_sha:
                        raise ExecutionContractError(
                            "prediction_artifact_invalid",
                            f"Prediction input artifact missing or changed: {source}",
                        )
                    committed_path = str(
                        artifact_root
                        / context.workflow_id
                        / context.task_id
                        / artifact_id
                        / source.name
                    )
                    committed = (artifact_id, committed_path)
                    committed_inputs[identity] = committed
                    staged.append(staged_input)
                artifact["artifact_id"], artifact["path"] = committed
            record_path.write_text(
                json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            staged_record = staging.stage_artifact(
                record_path,
                artifact_id=expected_id,
                artifact_type="prediction_record",
            )
            staged.append(staged_record)
            record_shas[candidate_id] = staged_record.sha256
            item["record_path"] = str(
                artifact_root
                / context.workflow_id
                / context.task_id
                / expected_id
                / record_path.name
            )
            item["record_sha256"] = staged_record.sha256
    if seen_candidates != approved_candidates:
        raise ExecutionContractError(
            "prediction_effects_scope_mismatch",
            "Prediction handoff omits an approved candidate",
        )
    handoff_paths[0].write_text(
        json.dumps(handoff, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return staged, record_shas


def _prediction_committed_effects(
    result: ExecutionActionResult,
    record_shas: dict[str, str],
    execution_identity: dict,
) -> tuple[tuple, tuple]:
    candidate_patches = []
    for mutation in result.candidate_patches:
        patch = dict(mutation.patch)
        if mutation.candidate_id in record_shas and patch.get("metrics_json"):
            metrics = json.loads(patch["metrics_json"])
            prediction = dict(metrics.get("prediction") or {})
            prediction["record_sha256"] = record_shas[mutation.candidate_id]
            metrics["prediction"] = prediction
            patch["metrics_json"] = json.dumps(
                metrics, ensure_ascii=False, separators=(",", ":")
            )
        candidate_patches.append(CandidatePatchMutation(
            candidate_id=mutation.candidate_id,
            patch=patch,
        ))

    evidence_events = []
    for event in result.evidence_events:
        value = dict(event)
        # Prediction effects historically used reserved trace ``run_id`` for
        # their domain run. At the transaction adapter boundary, preserve that
        # identity only as ``prediction_run_id``; Worker supplies formal
        # ``run_id`` from TraceContext.
        domain_run_id = value.pop("run_id", None)
        if domain_run_id is not None:
            existing = value.get("prediction_run_id")
            if existing not in (None, domain_run_id):
                raise ExecutionContractError(
                    "prediction_run_identity_conflict",
                    "Prediction evidence carries conflicting domain run identities",
                )
            value["prediction_run_id"] = domain_run_id
        candidate_id = str(value.get("candidate_id") or "")
        if candidate_id in record_shas:
            if isinstance(value.get("tool_trace"), dict):
                value["tool_trace"] = dict(
                    value["tool_trace"], output_hash=record_shas[candidate_id]
                )
        value["execution_identity"] = execution_identity
        evidence_events.append(value)
    return tuple(candidate_patches), tuple(evidence_events)


def make_transactional_output_adapter(
    handler: Callable,
    packet: dict,
    config: ExecutionConfig,
    task_dir: Path,
    project_config: dict | None,
):
    """Run a typed output handler through semantic validation and staging."""
    def adapter(
        context: TransactionContext, staging: StagingArea
    ) -> ExecutionActionResult:
        from .handlers import HandlerContext

        result = handler(HandlerContext(
            packet=packet,
            config=config,
            task_dir=task_dir,
            project_config=project_config,
            transaction_managed=True,
            transaction_id=context.transaction_id,
        ))
        if not isinstance(result, ExecutionActionResult):
            raise TypeError(
                f"transactional handler must return ExecutionActionResult, "
                f"got {type(result).__name__}"
            )
        validate_output_inventory(
            packet["task"],
            _semantic_output_inventory(result),
            dependency_outputs=packet.get("dependency_outputs") or {},
            approved_project_id=(
                (project_config or {}).get("project_id")
                or (packet.get("trace_context") or {}).get("project_id")
            ),
        )
        additional_staged = []
        record_shas = {}
        is_prediction = packet["task"]["action"] == "evaluate_new_design_candidates"
        if is_prediction:
            observed_identity = _observed_prediction_identity(
                result, packet["task"]["parameters"]["execution_identity"]
            )
            additional_staged, record_shas = _prediction_artifacts(
                result,
                context,
                staging,
                packet["task"]["parameters"]["predictor_protocol"],
                observed_identity,
                config.execution_root / "artifacts",
            )
        staged = [
            staging.stage_artifact(
                path,
                artifact_id=f"{context.transaction_id}-{role}",
                artifact_type=role,
            )
            for role, path in result.outputs
        ]
        staged.extend(additional_staged)
        artifact_ids = [artifact.artifact_id for artifact in staged]
        if is_prediction:
            candidate_patches, evidence_events = _prediction_committed_effects(
                result,
                record_shas,
                observed_identity,
            )
        else:
            candidate_patches = result.candidate_patches
            evidence_events = result.evidence_events
        evidence_events = tuple(
            dict(event, artifact_ids=artifact_ids)
            for event in evidence_events
        )
        return ExecutionActionResult(
            candidate_updates=result.candidate_updates,
            candidate_patches=candidate_patches,
            state_updates=result.state_updates,
            state_appends=result.state_appends,
            artifacts=(*result.artifacts, *staged),
            evidence_events=evidence_events,
            outputs=result.outputs,
            processes=result.processes,
        )

    return adapter


def make_iterate_design_adapter(
    packet: dict,
    config: ExecutionConfig,
    task_dir: Path,
    project_config: dict | None,
):
    def adapter(
        context: TransactionContext, staging: StagingArea
    ) -> ExecutionActionResult:
        from .handlers import HandlerContext, iterate_design

        result = iterate_design(HandlerContext(
            packet=packet,
            config=config,
            task_dir=task_dir,
            project_config=project_config,
        ))
        staged = []
        for candidate in result.candidate_updates:
            candidate_id = str(candidate["candidate_id"])
            for artifact_type, field in (
                ("design_pdb", "design_pdb_path"),
                ("manifest", "manifest_path"),
            ):
                path = str(candidate.get(field) or "")
                if path:
                    staged.append(staging.stage_artifact(
                        path,
                        artifact_id=f"{candidate_id}-{artifact_type}",
                        artifact_type=artifact_type,
                    ))
        for role, path in result.outputs:
            staged.append(staging.stage_artifact(
                path,
                artifact_id=f"{context.transaction_id}-{role}",
                artifact_type=role,
            ))
        return ExecutionActionResult(
            candidate_updates=result.candidate_updates,
            state_updates=result.state_updates,
            artifacts=tuple(staged),
            outputs=result.outputs,
            processes=result.processes,
        )

    return adapter


def make_legacy_handler_adapter(
    handler: Callable,
    packet: dict,
    config: ExecutionConfig,
    task_dir: Path,
    project_config: dict | None,
):
    def adapter(
        context: TransactionContext, staging: StagingArea
    ) -> ExecutionActionResult:
        from .handlers import HandlerContext

        result = handler(HandlerContext(
            packet=packet,
            config=config,
            task_dir=task_dir,
            project_config=project_config,
        ))
        if not isinstance(result, ExecutionActionResult):
            raise TypeError(
                f"legacy handler must return ExecutionActionResult, got {type(result).__name__}"
            )
        return result

    return adapter


def adapter_for(
    action: str,
    handler: Callable,
    packet: dict,
    config: ExecutionConfig,
    task_dir: Path,
    project_config: dict | None,
):
    if action == "iterate_design":
        return make_iterate_design_adapter(
            packet, config, task_dir, project_config
        )
    if action in TRANSACTIONAL_ACTIONS:
        return make_transactional_output_adapter(
            handler, packet, config, task_dir, project_config
        )
    return make_legacy_handler_adapter(
        handler, packet, config, task_dir, project_config
    )
