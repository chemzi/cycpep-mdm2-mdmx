## Context

The root `AGENTS.md` already contains the intended OpenSpec workflow and remediation routing, but it is untracked. The referenced `docs/engineering/remediation-strategy.md` is absent. Existing evidence is distributed across `ENGINEERING_STANDARD.md`, `openspec/config.yaml`, README architecture notes, the archived `remove-agent-package-import-path-bypass` change, and focused architecture documents such as `docs/data_integrity_consolidation.md`.

The implementation baseline used for durable claims is `execution/action_registry.py`, `storage/sqlite_store.py`, `execution/worker.py`, `execution/commit_manager.py`, `core/context.py`, and `prediction_pipeline/protocol.py`. The corresponding executable baseline is established by `test_contract_migration.py`, `test_data_integrity_transactions.py`, `test_store_transaction_ownership.py`, `test_execution.py`, `test_project_context.py`, `test_protocol.py`, and `test_architecture_gate.py`.

This change persists governance context only. Existing documentation contains known reality drift, including README wording about root instructions and historical phase status; correcting that drift belongs to the explicitly deferred `documentation-reality-alignment` change.

## Goals / Non-Goals

**Goals:**

- Make the root instructions a tracked, correctly named repository entrypoint.
- Add one durable strategy document that preserves audit-derived direction and decision boundaries across sessions.
- Define a clear ownership split between repository instructions, long-term strategy, and per-change OpenSpec artifacts.
- Make the next-change selection process incremental, evidence-based, and resistant to broad redesign.

**Non-Goals:**

- Changing production code, runtime configuration, tests of business behavior, dependencies, interfaces, or data formats.
- Rewriting README, historical PR status, Web GUI documentation, or other documentation with known drift.
- Declaring a new implementation sequence or recording completion percentages for remediation work.
- Resolving any persistence, transaction, adapter, ProjectContext, large-module, packaging, or scientific-protocol debt.

## Decisions

### 1. Keep `AGENTS.md` concise and route detailed remediation reasoning to one strategy document

`AGENTS.md` remains the mandatory workflow entrypoint: it selects direct versus OpenSpec work, identifies validation expectations, and points remediation work to the strategy. The strategy owns durable direction, priority principles, high-risk boundaries, and change-selection rules.

This avoids turning root instructions into a long architecture report while keeping the strategy discoverable. The rejected alternative is duplicating the same remediation catalog in both files, which would create immediate drift.

The anti-overdefense rule in `AGENTS.md` is intentionally narrow: it prohibits adding unnecessary hash checks merely for defensive completeness, while preserving hash and SHA256 behavior already owned by protocol, artifact, or integrity contracts. The rejected alternative is an absolute prohibition, which would conflict with existing reproducibility and integrity designs.

### 2. Treat the strategy as a decision framework, not a roadmap ledger

The strategy will organize durable content around:

- evidence sources and the current architecture model;
- remediation objectives and dependency direction;
- priority principles for correctness, data integrity, contract boundaries, maintainability, and documentation;
- high-risk areas that require characterization and independent approval;
- rules for selecting the smallest valuable next change;
- explicit non-goals and the boundary with OpenSpec.

It will not contain checkboxes, phase completion tables, PR progress, owners, deadlines, or a mirrored backlog. Those belong nowhere in the strategy; individual OpenSpec changes remain authoritative for concrete work.

The rejected alternative is importing the earlier PR0–PR8 table as a durable roadmap. That table is known to be potentially stale and would mix direction with progress tracking.

### 3. Record only evidence-backed architectural direction

Implementation will reconcile statements against the current repository and existing focused architecture documents. Durable invariants may be recorded, but volatile completion claims and unverified historical assertions will be omitted. Newly observed documentation drift will be reported, not fixed.

This keeps the strategy useful without turning this change into `documentation-reality-alignment` or a new full-repository audit.

### 4. Make scope mechanically reviewable

The implementation diff must be limited to:

- `AGENTS.md`;
- `docs/engineering/remediation-strategy.md`;
- `openspec/changes/persist-repository-remediation-strategy/**`.

No production module, README section, existing architecture document, test, CI configuration, or main spec is changed during implementation. The future archive workflow may sync the approved delta spec into the corresponding main spec after verification and explicit archive approval.

### 5. Preserve compatibility and rollback simplicity

The documents do not alter runtime behavior, interfaces, dependencies, persistence, transactions, or scientific outputs. Before archive, rollback is deletion of the new strategy and restoration or removal of the newly tracked root instructions together with removal of this active change. After archive, rollback uses a dedicated governance change so main specs and archived history remain coherent.

## Risks / Trade-offs

- [Risk] The strategy accidentally becomes a second backlog or progress tracker. → Keep status tables, checklists, owners, dates, and per-change progress out; verify this explicitly during review.
- [Risk] Audit-derived statements repeat stale documentation. → Require source references and distinguish durable invariants from deferred reality-alignment work.
- [Risk] Reviewers interpret listed debt as authorization to modify it. → State that every non-trivial remediation requires its own approved OpenSpec change.
- [Risk] The narrow documentation scope leaves visible README drift. → Preserve it deliberately and route it to `documentation-reality-alignment`.

## Migration Plan

1. Review the existing untracked `AGENTS.md` against the approved governance requirements and add it without expanding its role.
2. Create the remediation strategy from the prior audit and current evidence sources.
3. Verify file scope, internal links, OpenSpec consistency, and absence of production changes.
4. Obtain review and explicit archive approval before syncing the new governance capability into main specs.

Rollback before archive is limited to the two governance documents and this active change; no runtime migration is needed.
