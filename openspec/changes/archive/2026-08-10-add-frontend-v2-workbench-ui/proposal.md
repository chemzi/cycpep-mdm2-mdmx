## Why

The current `web-gui/` presents a fixed four-Agent workflow and derives status from the legacy `/api/v1/snapshot` projection, so it cannot truthfully represent the Planner task graph, typed action availability, execution attempts, transactions, or Store-backed provenance now exposed by `/api/v2/workbench`. Frontend V2 needs a read-only scientific workbench that renders the formal browser observability contract without becoming a second workflow authority.

## What Changes

- Replace the rendered fixed Research → Design → Prediction → Critic rail with a dynamic task/action graph sourced only from `/api/v2/workbench`.
- Add a typed, read-only workbench client and frontend domain model for the versioned success envelope, bounded collections, project/run context, tasks, executions, transactions, candidates, evidence, artifacts, protocols, trace links, and structured blockers.
- Validate the required nested browser contract at the HTTP boundary so malformed domain records become a controlled contract failure instead of a render-time crash.
- Recompose the existing page into domain-oriented workbench shell, task graph, candidate, exploration-shortlist, evidence, execution/transaction, artifact/protocol, and trace components with explicit loading, empty, blocked, partial, and failed states.
- Present project-scoped candidate, evidence, and artifact history with its formal `current_run`, `historical_run`, or `unlinked` provenance instead of merging it into current-run status.
- Present `exploration_shortlist` as exploratory scientific evidence, keeping `0 / N passed` distinct from shortlist membership and never treating a shortlisted candidate as passed unless its `passed` field is true.
- Stop the main Frontend V2 workbench from consuming `/api/v1/snapshot`, `State.phase`, fixed Agent order, evidence counts, log text, or browser-side filesystem/SQLite data as workflow authority.
- Preserve the useful workbench layout, candidate selection, real-artifact structure viewer, refresh settings, and honest empty states where they can be driven by the V2 contract.
- Preserve transaction history across retries and keep refresh, selection, and structure-viewer transitions aligned with the identity currently shown.
- Keep the UI read-only in this change; no workflow, execution, project, scheduler, SSH, threshold, Tournament, or Pareto mutation/computation is added.

## Capabilities

### New Capabilities

- `frontend/workbench-ui`: Defines the truthful read-only Frontend V2 workbench presentation, component boundaries, scientific shortlist semantics, provenance views, and state handling over `/api/v2/workbench`.

### Modified Capabilities

None. The established `frontend/browser-observability` backend contract is consumed without changing its requirements.

## Impact

- Affected frontend: `web-gui/app/`, frontend tests, and only the minimum supporting `web-gui/` configuration needed for typed UI testing.
- Public backend interfaces and data formats: unchanged; the UI consumes `GET /api/v2/workbench` and does not modify `/api/v1` or `/api/v2` behavior.
- User-visible migration: the root workbench view changes from the legacy fixed-Agent snapshot presentation to the dynamic V2 read model.
- Dependencies: no new runtime dependency is assumed; any proposed dependency must be justified in design and kept within frontend scope.
- Legacy paths remaining: backend `/api/v1` compatibility routes remain available for existing clients. Project creation, SSH control, and coordinate-content delivery are not redesigned here; unsupported V2 functions remain absent rather than being reconstructed from legacy projections.
