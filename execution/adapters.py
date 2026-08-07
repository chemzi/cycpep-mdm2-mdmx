"""Adapters from existing handlers to the transaction worker contract."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from contracts.transaction import TransactionContext

from .config import ExecutionConfig
from .contracts import validate_output_inventory
from .results import ExecutionActionResult
from .staging import StagingArea


TRANSACTIONAL_ACTIONS = frozenset({
    "iterate_design",
    "review_prediction_handoff",
    "propose_threshold_calibration",
})


def _semantic_output_inventory(result: ExecutionActionResult) -> list[dict]:
    return [
        {"role": role, "path": str(path)}
        for role, path in result.outputs
    ]


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
        staged = [
            staging.stage_artifact(
                path,
                artifact_id=f"{context.transaction_id}-{role}",
                artifact_type=role,
            )
            for role, path in result.outputs
        ]
        artifact_ids = [artifact.artifact_id for artifact in staged]
        evidence_events = tuple(
            dict(event, artifact_ids=artifact_ids)
            for event in result.evidence_events
        )
        return ExecutionActionResult(
            candidate_updates=result.candidate_updates,
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
