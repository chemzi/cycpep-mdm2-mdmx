## 1. Baseline and Control Contract

- [x] 1.1 Characterize target draft approval, caller/default Launcher identity, the post-Initial-Design/pre-Prediction pause, exact approval/resume, project switching, and the current bootstrap estimate/budget conflict.
- [x] 1.2 Define additive safe types for launch, pre-Orchestrator inspection, exact approval, auto ceilings, scoped reads, and structured binding/stale/estimate/ceiling failures without changing Store schemas.

## 2. Minimal Launcher and Control Implementation

- [x] 2.1 Add optional validated caller `launcher_run_id`; same-ID/same-project launch recovers status, conflicting binding fails, and legacy callers remain server-generated.
- [x] 2.2 Add the narrow operator facade that restores exact ProjectContext, validates formal Planner authority, returns a path-free approval view, records explicit approval, and resumes under the existing lock.
- [x] 2.3 Normalize bootstrap task estimates and `budget_request` into one provisional `simple-v1` interpretation with consistent totals/status/calibration metadata; do not add an estimator.
- [x] 2.4 Implement the small web service that applies maximum-slot plus summed proposal/candidate/GPU-minute ceilings only to the first `initial_prediction_bootstrap` gate, consumes the policy, and leaves every later/retry plan for human approval without retrying science.
- [x] 2.5 Add `/api/v2/control/...` routes and cover errors, repeat submission, response-loss recovery, sanitization, and preservation of old routes.

## 3. Scoped Workbench Projection

- [x] 3.1 Add safe immutable `resource_request` and `launcher_run_id`-scoped reads from the bound ProjectContext/SQLite Store while keeping unscoped reads compatible.
- [x] 3.2 Update Python and frontend validators/fixtures for pre-Orchestrator approval, provisional/unavailable estimates, project switching, no-current-run, invalid binding, and stale controls.

## 4. Frontend Launch Ledger

- [x] 4.1 Add the first-tab-load dismissible full-viewport ledger using only existing tokens and scoped selectors; place close/View existing tasks top-right, target first, and Create and launch in the footer.
- [x] 4.2 Add the exact-plan approval card with task resources, provisional/unavailable GPU minutes, totals, calibration, manual ceilings, and an `Auto-approve first GPU gate` option shown only for the bootstrap plan.
- [x] 4.3 Add one compact `New project` button immediately before Refresh; preserve the mounted frame, selection, navigator, inspector, history, and existing content.
- [ ] 4.4 Wire draft/approval, tab-persisted Launcher ID, launch/status, manual resume, auto policy, errors, and scoped refresh without synthetic state.
- [ ] 4.5 Test first-load/dismiss/reopen, repeat recovery, failure preservation, estimates, stale plan, accessibility/focus, and unchanged existing component/layout behavior.

## 5. Documentation and Demo Readiness

- [ ] 5.1 Document the two approval gates, control/scoped routes, estimate status, synchronous recovery, compatibility, and rollback.
- [ ] 5.2 Align the 5-minute script with exact E3 timing; prepare one formal awaiting-approval run and one identified formal downstream Prediction/Critic/Planner run.

## 6. Verification and Review

- [ ] 6.1 Run focused Python control/read-model/Launcher/Planner tests and frontend tests.
- [ ] 6.2 Run full Python, frontend lint/typecheck/build, Architecture Gate, strict OpenSpec, and diff check; record exact results.
- [ ] 6.3 Browser-test 1440×900 placement, close/reopen, unchanged underlying computed styles/geometry/content, approval modes, estimates, failure, scoped refresh, and keyboard focus.
- [ ] 6.4 Run fixed-baseline high-reasoning Standards and Spec reviews, resolve all P0/P1, then OpenSpec verification; do not merge or archive.
