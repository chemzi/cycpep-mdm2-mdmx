## ADDED Requirements

### Requirement: The workbench provides one target-to-launch surface
On the first load of each browser-tab session, the workbench SHALL offer a dismissible full-viewport new-project sheet that accepts a target identifier, presents the returned project review, and allows an approved project to enter the real Launcher loop without presenting any direct Agent, GPU, scheduler, claim, dispatch, or transaction control. Closing the sheet SHALL reveal the unchanged existing workbench, and one compact `New project` button at the right side of the existing top bar SHALL reopen it.

#### Scenario: Operator opens new project
- **WHEN** the operator selects New project
- **THEN** the full-viewport sheet presents target input, project review, and launch policy as one continuous ledger while the existing workbench remains mounted but inactive beneath it

#### Scenario: Operator views existing work
- **WHEN** the operator closes the initial sheet or selects `View existing tasks`
- **THEN** the sheet closes without changing the current project, selection, refresh state, task graph, candidates, evidence, artifacts, inspector, or history

#### Scenario: Project enters Launcher
- **WHEN** an approved project launch succeeds
- **THEN** the workbench stores the opaque `launcher_run_id` for that tab, switches control and read requests to its bound project, refreshes the formal read model, and displays the returned Launcher status

### Requirement: New controls preserve the existing workbench visual system
The new sheet, top-bar button, and approval card SHALL reuse the existing color, typography, spacing, border, focus, and status tokens. They SHALL NOT change the existing three-column frame, top-bar content order except for the one additive button, navigator, primary workspaces, inspector, history panel, candidate presentation, or scientific-status styling. New CSS selectors SHALL be locally scoped and existing component behavior SHALL remain covered by regression tests.

#### Scenario: Existing task is viewed after the sheet closes
- **WHEN** an operator dismisses the new-project sheet
- **THEN** the previous workbench geometry, content, selection, and visual hierarchy remain unchanged apart from the compact top-bar entry button

#### Scenario: Full-screen sheet is displayed at presentation size
- **WHEN** the page is viewed at 1440×900
- **THEN** the close and `View existing tasks` control are in the top-right safe area, the target input is the dominant first action, the project and policy ledger fits without page scroll, and the primary `Create and launch` action is anchored at the ledger footer without covering content

#### Scenario: Existing responsive layout is narrow
- **WHEN** the viewport is too narrow for the ledger columns
- **THEN** only the new sheet content stacks vertically while the existing workbench responsive rules and component styles remain unchanged

### Requirement: Approval presentation exposes plan resources truthfully
When the current run awaits approval, the workbench SHALL show required tasks, proposal and candidate limits, GPU slots, per-task estimate status and minutes, total estimated GPU minutes, budget status, estimator version, and manual approval using only returned plan/control data. The auto option SHALL be shown only for the first E3 bootstrap gate and SHALL read `Auto-approve first GPU gate` rather than implying authorization for the whole run.

#### Scenario: Estimated GPU time is available
- **WHEN** the current plan returns an estimated GPU duration
- **THEN** the approval card displays the value in GPU-minutes and keeps it associated with the exact plan and task scope

#### Scenario: GPU time is unavailable
- **WHEN** the current plan has no finite normalized estimate
- **THEN** the approval card displays an explicit pending-benchmark state, disables automatic approval, and does not synthesize a duration

#### Scenario: Provisional GPU time is available
- **WHEN** the current plan returns a finite `simple-v1` estimate while benchmark calibration is pending
- **THEN** the card displays the number as a provisional GPU-minute estimate, keeps the pending-calibration statement visible, and allows this-run auto approval only when the operator explicitly chose it and supplied covering ceilings

#### Scenario: Manual approval succeeds
- **WHEN** the operator approves the exact displayed plan within entered ceilings
- **THEN** the workbench reports the plan as approved, refreshes the current run, and does not claim GPU work started until formal status says so

#### Scenario: Automatic ceiling blocks approval
- **WHEN** the control API reports an unavailable estimate, exceeded ceiling, or stale plan
- **THEN** the workbench retains `awaiting_approval`, explains the structured reason, and offers refresh or manual review without widening the limit

#### Scenario: A later plan awaits approval
- **WHEN** a Critic-driven or retry plan awaits approval after the bootstrap policy was consumed
- **THEN** the card offers only explicit manual approval and does not show or reuse the first-gate automatic policy

## MODIFIED Requirements

### Requirement: Read-only UI states remain honest and accessible
The workbench SHALL provide distinguishable loading, empty, partial/blocked, mutation-in-progress, and request-failed states, SHALL preserve the last successfully loaded V2 response during a refresh or control failure with a visible stale/error indication, and SHALL limit mutation controls to target project creation, exact current-plan approval, and Launcher start/resume. It SHALL expose no direct retry, cancel, claim, dispatch, SSH, GPU scheduler, transaction, Agent, or scientific-stage control.

#### Scenario: Initial request is loading
- **WHEN** no successful V2 response has loaded yet
- **THEN** the UI presents a loading state without fake progress, candidate, execution, or scientific data

#### Scenario: Refresh fails after a successful load
- **WHEN** polling or manual refresh fails after a prior successful response
- **THEN** the prior response remains visibly marked stale and the error is shown without mutating or synthesizing its domain state

#### Scenario: Control request is in progress
- **WHEN** project creation, launch, or approval has been submitted and no response has returned
- **THEN** the initiating control is disabled, duplicate submission is prevented, and the UI shows indeterminate activity rather than fabricated workflow progress

#### Scenario: Control request fails after a successful load
- **WHEN** a mutation request returns a validation, review, stale-plan, approval, Launcher, or recovery error
- **THEN** the last successful read model remains visible, the structured failure is shown next to the control, and no domain state is changed locally

#### Scenario: Automatic polling finds a request in flight
- **WHEN** an automatic refresh interval fires before the prior workbench request has completed
- **THEN** the polling tick is skipped without aborting the in-flight request or starting a competing request

#### Scenario: A selected identity leaves a bounded response
- **WHEN** refresh removes the preferred candidate, task, Evidence, or artifact identity from the returned bounded collection
- **THEN** the visible fallback becomes the current selection and the unavailable preferred identity does not reappear automatically if a later response contains it again

#### Scenario: User reviews the read-only workbench
- **WHEN** the V2 page is rendered
- **THEN** only project creation, exact-plan approval, and Launcher continuation controls are offered, and all status, blocker, form, selection, and detail interactions remain keyboard-readable and semantically labelled
