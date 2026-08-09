# Repository Remediation Strategy

## Purpose and authority

This document preserves the repository's long-term engineering direction so remediation can continue incrementally without restarting a full audit. It is a decision framework, not a roadmap, backlog, task list, or progress tracker.

The root [`AGENTS.md`](../../AGENTS.md) selects the development workflow. [`ENGINEERING_STANDARD.md`](../../ENGINEERING_STANDARD.md) defines mandatory engineering constraints. For every non-trivial remediation unit, its OpenSpec proposal, specs, design, and tasks are the only authoritative sources for scope, requirements, implementation decisions, approval, and progress.

When current evidence invalidates a durable statement here, update this document through a separately scoped governance or documentation change. Do not silently retain stale assumptions and do not use this document to report the completion state of individual changes.

## Evidence model

Governance decisions should prefer current implementation and executable validation over historical phase labels. Useful focused evidence includes:

- [`openspec/config.yaml`](../../openspec/config.yaml) for repository-wide architectural invariants and validation expectations;
- [`execution/action_registry.py`](../../execution/action_registry.py) and the execution contracts for the Planner-to-Execution closed world;
- [`storage/sqlite_store.py`](../../storage/sqlite_store.py), [`execution/worker.py`](../../execution/worker.py), and [`execution/commit_manager.py`](../../execution/commit_manager.py) for persistence and commit ownership;
- [`core/context.py`](../../core/context.py) for explicit project context;
- [`prediction_pipeline/protocol.py`](../../prediction_pipeline/protocol.py) and `core/protocol/` for versioned scientific configuration;
- [`docs/data_integrity_consolidation.md`](../data_integrity_consolidation.md) for focused data-integrity decisions and explicitly retained limitations;
- Architecture Gate and regression tests for enforceable boundaries and preserved behavior.

README and historical validation documents remain useful orientation, but claims that affect remediation scope must be checked against current code, tests, and OpenSpec artifacts. Documentation-reality alignment is a separate concern from structural remediation.

## Durable architecture direction

### Public contracts and executable actions

Agents communicate through public contracts rather than private implementation details. Planner actions, Orchestrator dispatch, Execution handlers, and completion validation form one closed world: an action cannot be treated as executable unless a real registered handler and compatible result path exist.

Future work should deepen these public seams instead of adding parallel registries, duplicated contracts, or direct cross-Agent implementation dependencies.

### Persistence and transaction ownership

State, CandidateIndex, Evidence, and run metadata have distinct responsibilities behind the shared Store boundary. SQLite is the formal data authority; human-readable files are compatibility projections rather than an independent write authority.

Execution owns the formal transaction lifecycle: isolated staging, validation, commit, publication, completion, compensation, and recovery must agree. A failed task must not expose partially committed formal state. Future adapter migration should produce typed transaction effects rather than bypassing commit ownership.

Projection performance may be improved only while preserving database authority and one-way projection semantics. Convenience must not reintroduce read-modify-write concurrency hazards or automatic reverse synchronization.

### Project context and process isolation

Project-scoped behavior flows through explicit `ProjectContext` injection. New import-time project globals, process-wide path mutation, shadow state, and caller-order dependencies are prohibited. Remaining migrations should narrow global compatibility seams without changing public behavior in the same change.

### Scientific protocols

Scientific parameters belong in versioned protocol or configuration contracts, not as handler-local constants. Changes to algorithms, thresholds, model settings, or experimental interpretation are behavior changes and must be separated from architectural refactors, with explicit scientific validation and compatibility decisions.

### Module depth and dependency direction

Large or highly coupled modules should be decomposed only around proven responsibility seams. Prefer characterization, extraction behind a stable interface, caller redirection, verification, and then removal of the old path. Do not move files merely to improve directory appearance, introduce a second package layout, or split functions mechanically without reducing conceptual load.

Shared infrastructure dependencies point inward toward contracts, domain rules, context, storage, and execution abstractions. Web, workers, CLIs, and scientific adapters remain boundary consumers; they must not become alternate owners of formal state or shared contracts.

## Audit-derived problem families

The following are durable areas for future evidence gathering and change selection, not an ordered backlog or a statement of implementation status:

- legacy Prediction, Critic, and calibration adapters whose outputs should converge on typed transaction effects;
- persistence, compensation, recovery, and projection paths where ownership or failure semantics may still be implicit;
- residual project-global or import-order coupling outside already enforced package-initializer rules;
- oversized or multi-responsibility Agent and pipeline modules with weak public seams;
- duplicated validation, path, serialization, contract, or atomic-write infrastructure;
- scientific settings that remain outside versioned protocols;
- test discovery or fixtures whose import manipulation differs from supported runtime imports;
- documentation claims that no longer match current implementation.

Listing a problem family does not authorize its implementation. Each concrete non-trivial change requires fresh evidence, its own approved OpenSpec artifacts, and validation proportional to its risk.

## Prioritization principles

Choose remediation work using these principles, in order:

1. Protect correctness and formal data integrity before improving aesthetics or convenience.
2. Close executable-contract, persistence, and transaction gaps before decomposing modules that depend on those boundaries.
3. Prefer a small boundary violation with a reproducible characterization over a broad category such as "clean up imports" or "refactor agents."
4. Preserve public interfaces, CLI behavior, scientific semantics, and data formats unless the approved change explicitly governs their migration.
5. Prefer changes that can be independently reviewed, validated, reverted, and archived.
6. Avoid overlapping changes in shared contracts, Store ownership, transaction semantics, or protocol definitions.
7. Treat documentation-only reality alignment as an independent change rather than attaching it to structural work.

## High-risk boundaries

Changes in these areas require narrow evidence, explicit design decisions, and focused recovery or compatibility validation:

- formal Store authority, candidate identity, migrations, projections, and concurrent writes;
- Execution staging, commit, compensation, crash recovery, retry, and Orchestrator completion;
- Action Registry membership and Planner readiness semantics;
- public contracts, schemas, CLI entrypoints, persisted data formats, and migration behavior;
- ProjectContext lifetime and multi-project isolation;
- scientific algorithms, thresholds, protocols, external tools, and GPU-dependent behavior;
- worker, web, packaging, and scientific-adapter startup paths that cross process or environment boundaries.

Do not combine multiple high-risk boundaries merely because they are near each other in the call graph. If a shared contract decision is unresolved, resolve it in one approved change before parallel implementation begins.

## Selecting the next change

A suitable next remediation unit has a concrete observed violation, a bounded ownership seam, a behavior-preserving target unless behavior change is explicitly approved, and focused evidence capable of proving both the problem and the result.

Use the following classification:

- Direct work is limited to obvious, reversible hygiene with no behavioral or architectural effect.
- OpenSpec work covers structural cleanup, contract changes, persistence or transaction work, dependency-boundary enforcement, substantial testing gates, and documentation governance with durable requirements.
- High-risk work is deferred when transaction ownership, migration behavior, public compatibility, or scientific semantics cannot yet be specified and validated independently.

After a remediation change is verified and archived, reassess only the remaining known debt affected by that result. Select the smallest high-value problem supported by current evidence; do not restart a full-repository redesign and do not create a parallel remediation tracker.

## Explicit non-goals

This strategy does not define release status, phase completion, PR sequencing, ownership assignments, deadlines, or implementation checklists. It does not declare README, historical PR notes, Web GUI documentation, or other existing documents accurate. Those claims must be handled by a separate `documentation-reality-alignment` change.

This strategy also does not authorize production modifications. Authorization and scope come from the approved OpenSpec change for the specific work being performed.
