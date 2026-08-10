## 1. Presentation baseline and acceptance coverage

- [x] 1.1 Add focused rendered-workspace tests using the frozen full and invalid-binding V2 fixtures at the 1920x1080 and 1440x900 target viewports.
- [x] 1.2 Add test-first acceptance coverage for the five workspace regions, absence of page-level desktop scrolling, compact no-run/stale/blocker states, and removal of the standalone collection-coverage dashboard.
- [x] 1.3 Add test-first interaction coverage for keyboard navigator selection, selected-subject synchronization, bounded-selection invalidation after refresh, and collapsible/restorable auxiliary panels.
- [x] 1.4 Add test-first semantic coverage proving exploratory shortlist items remain visually distinct from passed candidates and all existing trace-only, artifact-link, and transaction-state rules survive the presentation refactor.

## 2. Workspace composition and UI-only selection

- [x] 2.1 Introduce the identity-only `WorkbenchSelection` presentation model and selectors that resolve every displayed detail from the latest V2 response without storing domain status or associations.
- [x] 2.2 Build the viewport-height `WorkbenchFrame` and compact top context bar for project, run, overall status, refresh, stale state, and global attention indication.
- [x] 2.3 Build the Tasks, Candidates, and Evidence navigator with returned-order selection, compact complete/truncated counts, visible focus, and returned-value-only attention cues.
- [x] 2.4 Route the primary workspace by the current selection while retaining an honest overview and compact no-current-run/invalid-binding presentation.
- [x] 2.5 Build the contextual inspector entry points and detail regions for task, candidate, Evidence, artifact, protocol, trace, and blocker information.
- [x] 2.6 Build the collapsible history dock using only returned timestamps and formal trace or exact task/attempt identities, with a separately labelled untimed lane.
- [x] 2.7 Preserve selected-subject and critical-status visibility when auxiliary panels are collapsed and provide an accessible narrower-layout fallback without horizontal page overflow.

## 3. Scientific and execution presentation

- [x] 3.1 Adapt the task workspace to emphasize returned dependencies, typed action availability, approval, blocker, current execution attempt, and transaction detail without deriving phases or progress.
- [x] 3.2 Adapt the candidate workspace to emphasize structure availability, formal metrics, provenance, and trace-linked Evidence/artifacts without computing new rankings or scientific conclusions.
- [x] 3.3 Refine the Exploration shortlist presentation so `0 / N passed`, shortlist membership, per-item `passed`, desirability, Pareto, reason, top-margin metric, calibration, source event IDs, and unmapped metrics remain complete and semantically distinct.
- [x] 3.4 Adapt Evidence, artifact, protocol, and trace presentation into selection-sensitive primary/inspector/history views while retaining opaque unavailable references and the `content_link` requirement.
- [x] 3.5 Replace full-width blocker and lifecycle panels with compact contextual warnings and inspector detail while preserving returned blocker codes, scopes, attempts, `not_yet_recorded`, failure, rollback, and recovery states.

## 4. Finished light visual identity

- [x] 4.1 Replace the dark dashboard tokens and repeated-card treatment with the approved cool-paper light surface hierarchy, restrained status palette, denser spacing, and typography-led separation.
- [x] 4.2 Bundle and document the selected open-licensed serif and utility fonts locally, apply serif only to scientific identity/hierarchy roles, and retain legible sans/mono and tabular numerals for controls and dense data.
- [x] 4.3 Replace the generic three-node mark with the accessible local cyclic-peptide/paired-target vector identity in the top bar and favicon/metadata touchpoints, with consistent product naming and no architecture slogan.
- [x] 4.4 Complete hover, focus-visible, selected, empty, loading, stale, blocked, failed, unavailable, truncated, collapsed, and reduced-motion states without decorative animation or fake data.
- [x] 4.5 Verify typography, long opaque identifiers, overflow, contrast, and pane hierarchy at both target desktop sizes and the narrower fallback.

## 5. Documentation and verification

- [x] 5.1 Update only the directly related frontend documentation to describe the selection-led workspace, responsive panel behavior, local visual assets, and unchanged V2 authority boundaries.
- [x] 5.2 Run focused presentation, selection, scientific-semantic, accessibility, and lifecycle regression tests, then run the complete `web-gui` test suite, build, lint, and TypeScript check.
- [x] 5.3 Run the full CPU test suite and Architecture Gate and confirm no backend, API contract, scientific calculation, persistence, transaction, mutation, or legacy V1 path changed.
- [x] 5.4 Run strict OpenSpec validation, implementation verification, Spec review, Standards review, and the applicable Web Interface Guidelines review; resolve all findings within the approved scope.
- [x] 5.5 Perform browser visual QA with real no-run data and the frozen populated V2 fixture at 1920x1080 and 1440x900, confirm the experience is complete without page-level desktop scrolling, and run `git diff --check` plus a scope-only diff review.
