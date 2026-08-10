## 1. Contract and Test Baseline

- [x] 1.1 Add a realistic frozen `frontend.workbench.v2` fixture covering non-linear tasks, action availability and approval, executions and transactions, current/historical/unlinked candidates, structured Evidence, artifacts, protocols, trace, blockers, and bounded collection metadata.
- [x] 1.2 Add the typed V2 envelope, bounded collection, trace, task, execution, transaction, candidate, Evidence/shortlist, artifact, protocol, and blocker domain models without carrying forward the legacy `Snapshot` or fixed Agent types.
- [x] 1.3 Implement and test a focused `/api/v2/workbench` client/parser that accepts the valid and trustworthy-partial contracts, rejects an unsupported/malformed contract, and never falls back to `/api/v1/snapshot`.
- [x] 1.4 Add request-lifecycle tests for initial loading, successful refresh, failed-before-data, and stale-after-refresh-error behavior while preserving the last successful response.

## 2. Workbench and Workflow Views

- [x] 2.1 Introduce the thin Workbench page/composition boundary and shared loading, empty, failure, stale, blocker, and bounded-collection summary components.
- [x] 2.2 Implement the current project/workflow/run shell with formal run status, identifiers, manual/automatic refresh state, and structured global blockers.
- [x] 2.3 Cover no-current-run and `workflow_binding_invalid` trustworthy partial responses so project-scoped data remains visible while workflow/run sections remain explicitly unavailable.
- [x] 2.4 Implement and test a graph-preserving Task/Action view for dynamic dependencies, typed action metadata, availability reason codes, approval, execution gate, status, and task blockers without fixed Agent stages.
- [x] 2.5 Implement and test task execution/transaction detail correlation, including attempts, `not_yet_recorded`, structured failure, committed/failed/rolled-back statuses, and unresolved recovery blockers.

## 3. Scientific Workspace and Provenance

- [x] 3.1 Implement and test the Candidate workspace with identity, returned metrics/status, run relationship, selection, and Evidence/artifact associations made only through formal candidate trace links.
- [x] 3.2 Implement and test the dedicated Exploration Shortlist panel using the frozen real payload, visibly separating `n_passed / n_evaluated passed` from shortlist membership and preserving item, calibration, source-event, and unmapped-metric fields.
- [x] 3.3 Implement and test the structured Evidence timeline/detail with event type, timestamp, agent, round, targets, protocol, run relationship, message, and trace linkage rather than a stdout-style log view.
- [x] 3.4 Implement and test Artifact/Protocol/Trace inspection using opaque identities and formal links, omitting server paths and enabling content/structure viewing only from an explicit returned `content_link`.

## 4. Frontend Migration and Presentation

- [x] 4.1 Compose the domain components into the responsive scientific workbench information architecture while keeping `app/page.tsx` a small composition root.
- [x] 4.2 Redirect the rendered root workbench from legacy snapshot polling to the V2 client and remove or isolate the no-longer-rendered fixed `AGENTS`, `State.phase`, evidence-count workflow inference, log console, project-creation, SSH-control, and GPU-control paths.
- [x] 4.3 Migrate the existing visual tokens, workbench layout, candidate selection, real-artifact viewer, refresh preferences, and honest empty states to semantically named component styles with keyboard-readable controls and labelled status/detail regions.
- [x] 4.4 Replace the stale starter-skeleton rendered HTML test with V2 workbench regression assertions covering the real shell and confirming no fake candidates/progress/execution state, workflow mutation controls, V1 snapshot fallback, or constructed/displayed server-internal artifact path.
- [x] 4.5 Synchronize `web-gui/README.md` and `docs/web_gui_implementation.md` with the delivered V2 read-only UI, retaining explicit contract gaps and non-goals rather than claiming unsupported controls.

## 5. Verification and Review

- [x] 5.1 Run focused typed-client and component tests, then the complete `web-gui` test command, build, lint, and TypeScript check; record exact commands and results.
- [x] 5.2 Run the repository full CPU test suite and `scripts/architecture_gate.py`; confirm no backend behavior, public API, persistence, transaction, or scientific data-format change.
- [x] 5.3 Run strict OpenSpec validation and implementation verification against every requirement and scenario, including the zero-passed shortlist and trustworthy partial-response cases.
- [x] 5.4 Perform Spec and Standards code review, verify the final diff remains within this change, and report any missing backend field as a contract gap without introducing a file/SQLite/log/`State.phase` fallback.
