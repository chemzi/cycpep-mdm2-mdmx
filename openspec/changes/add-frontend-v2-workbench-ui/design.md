## Context

See `proposal.md` for motivation. The current `web-gui/app/page.tsx` is a 22 KB client component that owns network access, polling, settings, fixed Agent state, candidate selection, structure loading, inspectors, mutation dialogs, and most rendering. It consumes `/api/v1/snapshot`; its `Snapshot.state.phase`, constant `AGENTS`, evidence-by-phase counts, log console, seven-layer assumptions, SSH dialog, and project-draft dialog are not valid Frontend V2 workflow sources.

The usable baseline is narrower: the three-region scientific workbench layout, project header, candidate selection interaction, real-artifact-only structure viewer, manual/automatic refresh preference, visual tokens, responsive breakpoints, and explicit no-data presentation. `web_api/workbench.py`, `test_workbench.py`, and `test_web_api.py` establish the only V2 response shape this UI may consume. PR #26 remains product inspiration only.

The current `web-gui/tests/rendered-html.test.mjs` still asserts a disposable starter loading page and `react-loading-skeleton` files that do not describe the checked-in workbench. It is not a characterization contract for this migration and must be replaced or rewritten with V2 workbench assertions rather than preserved as a competing UI baseline.

## Goals / Non-Goals

**Goals:**

- Make `/api/v2/workbench` the sole workflow-facing frontend dependency.
- Keep `page.tsx` as a composition root and move data access and domain presentation into focused modules.
- Preserve backend terminology and trace linkage instead of defining a frontend workflow state machine.
- Make scientific limitations—especially zero passed versus exploratory shortlist, unavailable calibration, unmapped metrics, partial binding, and truncated collections—visible.
- Establish testable client parsing and domain-oriented components before visual polish.

**Non-Goals:**

- No backend endpoint, Store, Orchestrator, transaction, protocol, threshold, Tournament, Pareto, or scientific computation change.
- No start/retry/cancel, approval mutation, project creation, SSH control, GPU control, or scheduler UI.
- No direct browser access to SQLite, files, projections, logs, or internal paths.
- No PR #26 code transplant, fixed Agent pipeline, snapshot state adapter, or broad redesign of `web-gui` tooling.
- No promise of structure rendering when the formal artifact record lacks a supported `content_link`; that remains a visible contract gap, not a reason to reconstruct a legacy URL.

## Decisions

### 1. Use a thin composition root and domain-oriented modules

`app/page.tsx` will compose a client-side `WorkbenchPage` and no longer contain the response schema or every domain renderer. The intended dependency direction is:

```text
page.tsx
  → workbench client/hook
  → WorkbenchShell
      → ProjectRunHeader + BlockerSummary
      → TaskGraph
      → CandidateWorkspace
          → CandidateList + CandidateDetail + StructureViewer
          → ExplorationShortlist
      → EvidenceTimeline + EvidenceDetail
      → ExecutionTransactionPanel
      → ArtifactProtocolTraceInspector
      → shared CollectionSummary / LoadingState / EmptyState / FailureState
```

Domain components receive typed view data and selection callbacks; they do not fetch backend data or infer formal state. This keeps the client seam deep and prevents another monolithic page. Alternative rejected: minimally patching the existing `page.tsx`, because that would retain coupled legacy state and continue growing the oversized module.

### 2. Define a narrow typed client at the HTTP boundary

The client model will mirror the V2 envelope rather than legacy UI types:

```text
ApiEnvelope<WorkbenchReadModel>
BoundedCollection<T> = { scope, total, returned, truncated, items }
WorkbenchReadModel = {
  schema_version, project, workflow, run,
  tasks, executions, transactions,
  candidates, evidence, artifacts, protocols,
  trace, blockers
}
```

The domain types include `TraceLink`, `RunRelation`, `TaskView`, `ExecutionView`, `TransactionView`, `CandidateView`, `EvidenceView`, `ExplorationShortlistEvidence`, `ArtifactView`, `ProtocolView`, and `BlockerView`. Status and reason-code fields remain extensible strings so a new backend status is displayed rather than collapsed into an invented frontend enum. The parser validates the required envelope, schema version, nullable workflow/run, bounded-collection structure, and the required nested fields that rendered components consume for project, task/action, execution, transaction, candidate, Evidence, artifact, protocol, trace, blocker, and shortlist records. Optional/extensible fields remain permissive, but a missing or wrongly typed required rendered field becomes a controlled contract error rather than an unchecked cast or render-time crash. It does not add validation for fields the UI does not consume or impossible cases.

The default client target is the exact `/api/v2/workbench` route. A configurable same-origin/API-origin prefix may be retained, but it is not an API-version base that appends V1 paths. Alternative rejected: reusing the existing generic `api<T>` cast, because it trusts arbitrary JSON and encodes the old base/path convention.

### 3. Keep server-returned state and frontend view state separate

The hook owns request lifecycle only: `initial-loading`, `ready`, `refreshing`, `stale-after-error`, and `failed-before-data`. The backend response remains immutable input. Frontend-local state is limited to selections, expanded details, tabs, and refresh preferences; it never stores a workflow phase, progress percentage, pass decision, or transaction transition.

On refresh failure, the last successful response remains on screen with an explicit stale/error marker. Automatic polling skips a tick while a request is already in flight rather than aborting and restarting that request; this prevents a slow endpoint from being starved by its own interval. On invalid run/plan binding, the HTTP 200 response is treated as valid partial data: project-scoped collections render, workflow/run/task/execution/transaction areas render unavailable states, and `workflow_binding_invalid` remains visible. Alternative rejected: clearing all data or throwing on any blocker, because blockers are part of the successful observability contract.

Selection state follows the currently returned bounded collection. If a preferred identity is absent after refresh, the visible fallback becomes the new selection and the stale preferred identity is cleared, so a later response cannot resurrect a choice the user no longer sees.

### 4. Present a graph without introducing a browser state machine

The first version uses a graph-preserving task list/cards view: each task shows its ID, action, returned status, approval, execution gate, availability and reason codes, plus explicit incoming dependency links. Layout order may be stable for readability but must not imply a fixed semantic phase sequence. A task detail panel correlates the matching execution, transactions, and blockers by formal IDs.

No graph/layout runtime dependency is required for the MVP. Alternative rejected: a custom staged pipeline or Agent-name swimlane, because either would reconstruct backend state; a heavy graph library is also unnecessary for the first read-only slice.

### 5. Join project-scoped records only through formal trace linkage

Candidate selection filters associated evidence and artifacts using returned `trace.candidate_id`; task/execution/transaction correlation uses formal identifiers. The current execution is correlated to its exact `task_id` and `attempt_id`, while every transaction returned for the selected task remains visible as transaction history. A prior attempt transaction is never attached to the current attempt, but it is also never hidden merely because a retry has advanced the current execution attempt. `run_relation` is displayed as returned. Records without a formal link stay project-level or explicitly unlinked and are never attached by matching text, sequence, agent, timestamps, or filesystem names.

Collection headers expose `returned / total` and truncation. The UI must not imply that a visible subset is complete. Alternative rejected: client heuristics to recover missing associations, because they would create shadow provenance.

### 6. Give exploration shortlist a dedicated scientific panel

The panel has two separate semantic blocks:

1. `Passed`: a prominent `n_passed / n_evaluated passed` summary.
2. `Exploration shortlist`: a separately titled list of returned shortlist items.

Each item shows `passed` literally, with non-passing items using neutral exploratory styling; it also shows desirability, Pareto-front flag, reason, and top-margin metric. Calibration counts and unmapped metrics are visible limitations, and source event IDs link back to the Evidence detail when present in the loaded collection. The UI performs no threshold, desirability, pass, or Pareto calculation.

### 7. Evidence is an inspectable domain record, not a log console

The project-level timeline uses event type and timestamp as its primary scan fields, with agent, round, targets, run relation, protocol, and trace fields in detail. Candidate and task contexts can filter only by formal trace IDs. The existing bottom “运行日志” representation is removed or isolated from the V2 route; message text is supplemental and never parsed for status.

### 8. Artifact and structure behavior follows explicit content links

Artifact cards display opaque ID, type, role, integrity identity, producer/input lineage, protocol, and trace. They never display or accept a server path. `StructureViewer` may be retained after changing its input from `candidate.artifact_id` plus a constructed V1 URL to an explicitly returned artifact `content_link`. With no supported link, it shows metadata and an honest unavailable state. When the selected artifact identity changes, the viewer clears the prior model before entering loading and resets its representation control to the default used for the new model; old scientific content is never displayed beneath the new artifact identity. This prevents the UI from silently depending on an undocumented artifact route or presenting stale structure content.

### 9. Testing is contract-first and frontend-scoped

Tests will freeze a realistic V2 fixture covering non-linear tasks, unavailable action and approval, current/historical/unlinked candidates, structured evidence, `n_passed: 0` with a non-empty shortlist, all shortlist item fields, calibration counts, unmapped metrics, `not_yet_recorded`, current-attempt correlation plus prior-attempt transaction history, failure, rollback/recovery blocker, artifact/protocol/trace, structure identity switching, truncation, selection invalidation, no-run, invalid-binding partial response, nested malformed records, request failure, refresh staleness, and slow in-flight polling.

The existing build and lint gates remain. A minimal DOM component test harness may be added as development-only tooling if the current stack cannot exercise interactions; it must not become a runtime dependency. Tests SHALL assert semantics and accessible labels rather than brittle pixel/layout snapshots.

The stale starter-skeleton rendered HTML test will be replaced with tests for the actual V2 shell and its honest initial state; no second preview page or parallel frontend contract will be introduced.

## UI Information Architecture

- Header: project identity, workflow/run identity, formal run status, refresh/stale state.
- Global blocker band: structured blocker summary with scope and linked identity.
- Left/main workflow region: dynamic task/action graph and selected task execution/transaction detail.
- Candidate workspace: candidate browser, metric/provenance detail, associated evidence/artifacts, optional real structure content.
- Scientific results panel: exploration shortlist with passed summary and calibration/mapping limitations.
- Provenance inspector: Evidence timeline/detail, Artifact/Protocol detail, and Trace linkage.
- Collection metadata: returned/total/truncated indicators adjacent to every bounded list.

## Risks / Trade-offs

- [The current integration branch does not yet contain PR #52's synced main browser-observability spec] → Treat the merged PR #49 implementation and active delta as the local contract; do not copy PR #52 artifacts into this change. Rebase after PR #52 merges if needed.
- [Some artifacts may not expose `content_link`] → Show a precise unavailable state and report the contract gap; do not construct a V1 coordinate URL.
- [Backend statuses and reason codes may expand] → Render returned strings with neutral fallback labels rather than a closed frontend state machine.
- [Project collections are bounded] → Display counts and truncation and avoid “all” language when `truncated` is true.
- [The current CSS is tightly coupled to legacy class names] → Reuse tokens and broad layout concepts, but migrate styles alongside focused components rather than retaining semantic names such as `agent-flow`.
- [Polling can hide failures or starve a slow request] → Preserve last good data with an explicit stale/error state, skip automatic ticks while a request is in flight, and keep manual refresh visible.

## Migration Plan

1. Add typed client/domain types and frozen V2 fixtures with parsing tests.
2. Add request lifecycle handling and shell states without removing the existing page until the new composition renders the contract fixture.
3. Introduce domain components incrementally, beginning with shell/blockers and task graph, then candidate/shortlist, provenance, and execution/artifact views.
4. Redirect the root page to the V2 composition and remove or isolate the no-longer-rendered snapshot/fixed-Agent/control code.
5. Run focused frontend tests, build, lint/typecheck, full CPU suite, Architecture Gate, OpenSpec verification, and code review.

Rollback is a single frontend change reversal: the backend V2 endpoint and `/api/v1` compatibility routes are unchanged. No persisted frontend workflow state or data migration is introduced.
