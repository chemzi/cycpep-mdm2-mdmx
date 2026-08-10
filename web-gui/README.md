# CycPep Studio Frontend V2

`web-gui/` is a read-only scientific workbench over the formal browser contract:

```text
GET /api/v2/workbench
```

It presents the current project and validated run/task graph, typed action availability, executions and transactions, candidates, structured Evidence, exploration shortlists, artifacts, protocols, trace linkage, and structured blockers. It does not maintain a workflow state machine or use `/api/v1/snapshot`, `State.phase`, files, SQLite, projections, Evidence counts, or logs as workflow authority.

## Local use

From PowerShell, start the adapter and UI together:

```powershell
.\start-local.ps1
```

The Vite development server proxies `/api/v2` to the local adapter at `127.0.0.1:8765`. For a separately managed deployment, route `/api/v2/workbench` to the adapter through the same-origin reverse proxy.

To start only the UI after the adapter is already running:

```powershell
npm install
npm run dev
```

## Verification

```powershell
npm test
npm run lint
npm run typecheck
npm run build
```

## Contract boundaries

- Shortlist membership is exploratory and never implies `passed: true`.
- `0 / N passed` and `Exploration shortlist` are shown as separate scientific statements.
- Candidate, Evidence, and artifact associations use only formal trace identifiers.
- Missing `source_event_ids` remain opaque unavailable references when their events are outside the bounded response.
- Artifact content is loaded only from an explicit returned `content_link`; otherwise metadata remains visible and content is marked unavailable.
- No start, retry, cancel, approval, SSH, GPU, scheduler, or project-creation control is provided.

See `../docs/web_gui_implementation.md` for the component and data-flow architecture.
