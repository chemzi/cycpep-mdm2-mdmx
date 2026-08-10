# CycPep Studio Frontend V2 implementation

更新时间：2026-08-10

## 1. Authority and purpose

Frontend V2 is a read-only presentation of `GET /api/v2/workbench`. The backend read model joins formal Orchestrator/Plan, Action Catalog/Registry, Store, Candidate, Evidence, Artifact, Protocol, Trace, execution, and transaction data. The browser does not repeat that join and does not own workflow state.

```text
Project
→ validated Workflow / Run
→ dynamic Task / typed Action
→ Execution / Transaction
→ Store-backed Candidate / Evidence / Artifact / Protocol / Trace
```

The legacy `/api/v1/snapshot` routes remain backend compatibility interfaces for other clients, but the root V2 UI does not consume them. PR #26 remains UX inspiration only and contributes no controller, state model, API contract, or implementation dependency.

## 2. Component and dependency direction

```text
app/page.tsx
  → workbench/workbench-page.tsx
      → client.ts + use-workbench.ts
      → WorkbenchWorkspace
          → WorkbenchTopBar
          → WorkbenchNavigator
          → PrimaryWorkspace
          → WorkbenchInspector
          → WorkbenchHistory
              → existing Task / Candidate / Evidence / Artifact renderers
```

`page.tsx` is only the root composition entry. The HTTP client validates the required `frontend.workbench.v2` envelope and bounded collections. Components receive typed domain views; they do not fetch backend state or infer task transitions.

Frontend-local state is limited to one identity-only `WorkbenchSelection`, auxiliary-panel presentation, request lifecycle, and the auto-refresh preference. Selection contains only an opaque returned task/candidate/Evidence/artifact identity; every detail is resolved again from the latest bounded response. Request lifecycle values (`initial-loading`, `ready`, `refreshing`, `stale-after-error`, `failed-before-data`) describe the HTTP observation, not workflow state.

## 3. Information architecture

- Compact context bar: current project, workflow/run identifiers, returned run status, refresh/stale state, and attention count.
- Navigator: returned-order Tasks, Candidates, and Evidence with their own `returned / total / truncated` coverage and one selected subject.
- Primary workspace: the selected task, candidate, Evidence item, artifact, or truthful overview; it does not render all collections as one long page.
- Inspector: formal identity, protocol, trace linkage, content availability, and full structured blocker detail for the current context.
- History dock: returned timestamped Evidence/transaction records plus a separately labelled untimed lane for attempts or transactions without formal timestamps.

At 1920×1080 and 1440×900, the application frame is viewport-height and its panes scroll internally. At the narrower desktop target, navigator/inspector/history space is reduced before the primary scientific workspace is compressed. Below the desktop threshold, auxiliary panes become reachable stacked regions without horizontal page overflow.

The default presentation is a light cool-neutral scientific workspace. Product and scientific identity use a locally bundled serif; controls and dense data use local sans/mono faces. The cyclic-peptide/paired-target mark, fonts, and license notices are local assets and introduce no runtime font CDN dependency.

## 4. Scientific truthfulness

For `exploration_shortlist`, the UI renders the backend values without recalculation:

- `n_passed / n_evaluated`;
- shortlist item `candidate_id`, `passed`, `desirability`, `pareto_front`, `reason`, and `top_margin_metric`;
- calibration counts;
- `source_event_ids`;
- `unmapped_metrics`.

A shortlist may be non-empty while `n_passed` is zero. Non-passing shortlist items use exploratory presentation and are never promoted to passed. Threshold, desirability, Pareto-front, and pass calculations remain backend scientific responsibilities.

Candidate-specific Evidence and artifacts are associated only when `trace.candidate_id` matches. Text, sequence, agent, timestamps, filenames, and ordering are not association heuristics. A `source_event_id` outside the current bounded Evidence response remains an opaque ID labelled unavailable; the UI does not query a legacy source to fill it.

## 5. Artifact and structure boundary

Artifact presentation uses opaque identity, type, role, integrity identity, producer/input provenance, protocol, run relation, and trace. Server paths are neither accepted nor displayed.

Structure content loads only from an explicit browser-safe `content_link` returned in the formal artifact view. If the field is absent, artifact metadata remains available and the viewer says that content is unavailable. The browser never constructs `/api/v1/artifacts/{id}/coordinates` or another URL from an artifact ID.

This conditional content availability is the current explicit contract gap; it does not block the observability UI and is not bypassed.

## 6. Partial, empty, and failure behavior

- No current run: project-scoped collections remain visible and workflow/task areas show an explicit no-run state.
- Invalid binding: the HTTP 200 trustworthy partial response is rendered; workflow/run are unavailable, current-run collections remain empty, and `workflow_binding_invalid` stays prominent.
- Initial request failure: no fake workbench data is shown.
- Refresh failure after success: the last successful response remains visible and is marked stale with the error.
- Truncated collection: returned and total counts remain visible.
- Unknown returned status/reason: displayed as returned, without mapping to a frontend phase.

## 7. Explicit non-goals

Frontend V2 does not provide start/retry/cancel, approval mutation, workflow dispatch, project creation, SSH control, GPU control, scheduler control, threshold calibration, Tournament/Pareto computation, or transaction mutation. It does not read SQLite, JSON/CSV/JSONL projections, files, or logs, and it does not parse Evidence messages into state.

## 8. Verification

Frontend fixtures freeze both a full response and an invalid-binding partial response. Tests cover typed parsing, the exact V2 endpoint, request lifecycle, dynamic tasks, execution/transaction correlation, candidate trace associations, structured Evidence, zero-passed shortlist semantics, missing source events, explicit artifact content links, identity-only selection, panel controls, and the 1920×1080 / 1440×900 workspace contracts.

Run:

```powershell
cd web-gui
npm test
npm run lint
npm run typecheck
npm run build
```
