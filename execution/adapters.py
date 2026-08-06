"""Adapters from existing handlers to the transaction worker contract."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from contracts.transaction import TransactionContext

from .config import ExecutionConfig
from .results import ExecutionActionResult
from .staging import StagingArea


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
    return make_legacy_handler_adapter(
        handler, packet, config, task_dir, project_config
    )
