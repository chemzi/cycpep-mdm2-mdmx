"""Transaction adapters wrapping legacy handlers for ExecutionWorker.run (PR36 migration).

These adapters wrap existing ``handler(HandlerContext)`` callables into the
``(TransactionContext, StagingArea) -> ExecutionActionResult`` signature
expected by ``ExecutionWorker.run``, so ``execute_task`` has a single execution
path through the transaction boundary.

- ``make_iterate_design_adapter``: transactional (candidate_updates + staged artifacts)
- ``make_legacy_handler_adapter``: bridge for not-yet-migrated handlers
  (outputs/processes only, empty candidate_updates). MUST NOT call
  ``CandidateIndex.add`` / ``State.save`` / ``EvidenceLogger.log`` — that would
  bypass the transaction boundary again.

Architectural constraint (PR36 migration scope):
    The goal is to make Transaction the single execution path — NOT to delete
    Orchestrator, NOT to rewrite all handlers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .config import ExecutionConfig
from .results import ExecutionActionResult
from .staging import StagingArea
from contracts.transaction import TransactionContext


def make_iterate_design_adapter(packet: dict, config: ExecutionConfig, task_dir: Path):
    """Transactional adapter for iterate_design (PR36 Phase 1).

    Wraps ``iterate_design(HandlerContext)`` into
    ``(TransactionContext, StagingArea) -> ExecutionActionResult``. Stages
    design_pdb + manifest files and double-fills candidate_updates + outputs +
    processes. Reuses the CandidateUpdate records produced by iterate_design
    (which read them via ``CandidateUpdateBatch.from_dict``); the adapter does
    NOT re-read candidate_updates.json or hand-assemble dicts.
    """

    def adapter(context: TransactionContext, staging: StagingArea) -> ExecutionActionResult:
        from .handlers import HandlerContext, iterate_design  # lazy: avoid cycle

        handler_context = HandlerContext(packet=packet, config=config, task_dir=task_dir)
        result = iterate_design(handler_context)
        staged = []
        for cu in result.candidate_updates:
            staged.append(staging.stage_artifact(
                cu.design_pdb_path,
                artifact_id=f"{cu.candidate_id}-design-pdb",
                artifact_type="design_pdb",
            ))
            staged.append(staging.stage_artifact(
                cu.manifest_path,
                artifact_id=f"{cu.candidate_id}-manifest",
                artifact_type="manifest",
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
    handler: Callable, packet: dict, config: ExecutionConfig, task_dir: Path
):
    """Bridge adapter for not-yet-migrated handlers (PR36 Phase 2 target).

    Wraps a legacy ``handler(HandlerContext)`` into the transaction signature.
    Returns outputs/processes from the legacy handler with EMPTY
    candidate_updates / state_updates / artifacts — legacy handler side effects
    are NOT transactional. This is an explicit migration bridge, not a
    long-term architecture.

    The adapter MUST NOT call ``CandidateIndex.add`` / ``State.save`` /
    ``EvidenceLogger.log``; it only forwards the legacy handler's typed result.
    Any formal state mutation by the legacy handler remains a Phase 2 item.
    """

    def adapter(context: TransactionContext, staging: StagingArea) -> ExecutionActionResult:
        from .handlers import HandlerContext  # lazy: avoid cycle

        handler_context = HandlerContext(packet=packet, config=config, task_dir=task_dir)
        result = handler(handler_context)
        if not isinstance(result, ExecutionActionResult):
            raise TypeError(
                f"legacy handler must return ExecutionActionResult, "
                f"got {type(result).__name__}"
            )
        # Forward outputs/processes only. candidate_updates/state_updates/artifacts
        # stay empty: legacy handler side effects are not transactional.
        return ExecutionActionResult(
            outputs=result.outputs,
            processes=result.processes,
        )

    return adapter


def adapter_for(
    action: str, handler: Callable, packet: dict, config: ExecutionConfig, task_dir: Path
):
    """Select the transaction adapter for an action (PR36 migration).

    Single selection point so execute_task holds no business judgment — it
    only creates the transaction, picks the adapter, runs, and notifies the
    Orchestrator. iterate_design uses the transactional adapter; all other
    actions use the legacy bridge adapter until Phase 2 migration.
    """
    if action == "iterate_design":
        return make_iterate_design_adapter(packet, config, task_dir)
    return make_legacy_handler_adapter(handler, packet, config, task_dir)
