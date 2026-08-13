## Why

Frontend V2 is intentionally read-only, so a presenter cannot currently create a target-driven project, start the real Launcher loop, approve a GPU-bound plan, or see the Planner's existing GPU-time estimate without leaving the workbench. The project demo needs one narrow, truthful control path now, without introducing a second workflow authority or changing scientific behavior.

## What Changes

- Add a browser-facing project launch control that creates and reviews a target bootstrap draft, approves the project, and starts the existing public Workflow Launcher with that approved project artifact.
- Add one narrow Launcher operator-control facade that can inspect the current pre-Orchestrator approval pause, record an operator-requested formal approval in the run's bound ProjectContext, and resume that exact Launcher run. Launcher remains the complete-loop coordinator and does not infer approval.
- Add current-run approval controls that display the exact immutable plan scope, candidate limits, GPU slots, and available GPU-minute estimate, then call the existing Planner approval and Launcher resume contracts.
- Add an opt-in "auto-approve first GPU gate" policy with user-supplied ceilings. It applies only to the E3 bootstrap plan after Initial Design and before heavy Prediction. The adapter may issue one normal plan-bound approval only when every required task and available estimate is within those ceilings; later Critic-driven plans remain explicitly human-approved.
- Extend the workbench with a compact project launch surface and approval card while preserving formal Store/Evidence/Planner/Orchestrator authority and honest unavailable/benchmark-required states.
- Let a browser supply one validated Launcher run identity so a repeated launch submission recovers that same run instead of starting a duplicate; the default Launcher API remains backward compatible.
- Resolve the launched project's exact context for control status and workbench reads so a newly created project never reuses the adapter's previously selected Store.
- Keep bootstrap plan resource requests and their Planner-owned estimate summary consistent. The existing `simple-v1` estimate remains visibly provisional pending benchmark calibration, rather than being presented as calibrated GPU time.
- Add a minimal application-service script/module used by the HTTP adapter so browser route handling stays thin and the same control flow can be exercised deterministically without the UI.
- Do not change Research, Design, Prediction, Critic, Planner algorithms, L1-L7, threshold/calibration semantics, Store schema, action handlers, transaction ownership, or E2/E3 scientific contracts.

## Capabilities

### New Capabilities

- `workflow/browser-project-launch-control`: Target-driven project creation, Launcher start/resume, bounded manual or current-run auto approval, and GPU estimate projection through existing public contracts.

### Modified Capabilities

- `frontend/workbench-ui`: Add the project launch and approval interaction states to the existing truthful workbench while retaining read-model authority and scientific-status honesty.

## Impact

- Affected code: one narrow workflow operator-control facade, `web_api/`, one small workflow-facing application service/script, the bootstrap-plan estimate projection, `web-gui/app/workbench/`, focused Python and frontend tests, and operator/frontend documentation.
- Public interfaces: an optional caller-supplied `launcher_run_id`, additive Launcher control inspection/explicit-approval calls, additive local-control HTTP routes, a `launcher_run_id`-scoped workbench read, and additive browser-safe launch/approval view fields. Existing callers and unscoped V2 reads remain compatible.
- Data format: additive request/response envelopes only. Formal project, plan, approval, run, task, Evidence, Artifact, and transaction formats remain owned by existing contracts.
- Migration: none. Existing read-only workbench responses and Launcher runs remain readable; no existing run is auto-enrolled in approval.
- Legacy paths retained: existing target-bootstrap CLI, Workflow Launcher CLI, explicit approval CLI/Python seam, and read-only `GET /api/v2/workbench` remain supported.
