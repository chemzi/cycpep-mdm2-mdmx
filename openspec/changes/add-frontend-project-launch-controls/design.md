## Context

See `proposal.md` and the two delta specs. The repository already has target draft/project approval in `target_bootstrap.py`, the complete-loop service in `workflow.service`, formal plan approval in `agents.planner.record_approval`, and browser-safe Store observation in `web_api.workbench.WorkbenchReader`.

Two current boundaries require explicit treatment. E3 returns `awaiting_approval` after Initial Design but before Orchestrator initialization, while WorkbenchReader currently discovers a plan only through `State.orchestrator.run_path`. The adapter also captures one Store at startup, so an unscoped read after launching a different project can still observe the old project. Launcher diagnostics may locate the exact approved project/runtime context but are non-authoritative; Planner Artifact/Evidence and the project SQLite Store remain formal truth.

Bootstrap plan construction also declares `budget_request.gpu_minutes_status=benchmark_required` and then computes finite per-task `simple-v1` estimates. This change must normalize those views rather than ask the frontend to choose an authority.

## Goals / Non-Goals

**Goals:**

- Compose existing project bootstrap, Launcher, Planner approval, and read-model seams through minimal public control interfaces.
- Expose the real post-Initial-Design/pre-heavy-Prediction approval pause.
- Surface Planner-owned estimates with provisional/unavailable status.
- Recover a repeated launch through the same caller-supplied Launcher identity without a request database or hash-derived identity.
- Preserve the existing workbench content, geometry, visual tokens, and scientific-status styling.

**Non-Goals:**

- No new workflow engine, queue, scheduler, SSH control, GPU process control, task retry, transaction mutation, or shadow authority.
- No inferred approval inside ordinary Launcher launch/status/resume and no relaxation of Planner or Orchestrator validation.
- No change to target discovery, scientific algorithms, E2 ExplorationDecision, E3 scientific execution, thresholds, calibration algorithms, or Store schema.
- No general multi-project dashboard or cross-run approval policy.

## Decisions

### 1. Keep HTTP thin and add one narrow Launcher operator-control facade

Add one browser application module under `web_api/` and one small public operator-control facade under `workflow/`. Route handlers only parse and delegate. The workflow facade reuses Launcher's locked diagnostic session, locator restoration, explicit ProjectContext binding, runtime construction, and formal Planner inspection. It exposes browser-safe inspection of the pre-Orchestrator approval request plus an operator-explicit `record approval and resume` operation.

The explicit operation calls the existing Planner approval seam inside the exact bound context and supplies its artifact to the existing resume path. Ordinary launch/status/resume still create or infer no approval. Diagnostics only address the original context; validated Planner Artifact/Evidence, approval Artifact/Evidence, Orchestrator, transaction, and Store remain authoritative.

Alternative rejected: reading private runtime helpers directly from HTTP or teaching React the Python lifecycle would create boundary coupling.

### 2. Keep project review and execution approval separate

Project draft approval proves approved project content; later Planner approval proves exact task and budget authorization. The UI names them separately. Launch accepts only the exact approved draft artifact. It does not claim project approval authorized GPU execution.

### 3. Recover launch submission with one caller-supplied Launcher identity

Before POSTing launch, the browser creates a random UUID with the platform API, formats it into the valid `launcher_<32 hex>` namespace, and stores it with the in-flight form in tab `sessionStorage`. `launch_project` gains an optional `launcher_run_id`; omission preserves current server-generated behavior.

If the same ID already has a diagnostic record, launch does not initialize again. It validates that the diagnostic locator resolves to the same approved project ID/content binding and returns current status; a mismatch is a binding conflict. The existing durable diagnostic create remains the create-before-side-effects guard. This closes response-loss retry without a new persistence file, deterministic hash, or shadow workflow state.

### 4. Keep auto approval request-scoped and explicit

The launch request may carry a policy bound to the same `launcher_run_id` and restricted to `source.kind=initial_prediction_bootstrap`. At the first E3 `awaiting_approval`, the web service requests the formal plan projection from the operator facade, requires finite normalized estimates for required GPU tasks, and compares:

- maximum requested concurrent GPU slots;
- summed design proposals;
- summed Prediction candidates;
- summed estimated GPU minutes.

Only when every user ceiling covers that exact bootstrap plan does it request one normal plan-bound approval and resume. The policy is then consumed. Every later Critic-driven or retry plan stops at formal human approval, even if its resources would fit the original ceilings. Any missing estimate, stale binding, ceiling breach, blocker, or failure also stops. It never retries failed scientific work.

### 5. Normalize the existing provisional estimate once in Planner

Keep `_compute_plan_metadata` as the sole estimator. After bootstrap tasks receive `simple-v1` estimates, synchronize `budget_request.gpu_minutes`, status, total, estimator version, and calibration status from those tasks. A finite value is `estimated` and `provisional` while benchmark calibration remains pending. If no finite value exists, both task and budget views remain unavailable and auto approval is disabled.

This is a consistency repair to the bootstrap projection needed by the one automatic gate, not a new scientific algorithm. Manual `record_approval` behavior remains unchanged. Before both manual and automatic browser approval, the operator facade compares the displayed estimate with the submitted GPU-minute ceiling; `record_approval` then performs its existing formal scope and budget validation. No approval schema changes.

### 6. Add launcher-scoped projections, not frontend inference

Operator-control status returns a safe `approval_control` view before Orchestrator exists: plan ID/digest, required task IDs, resources, and Planner-owned estimate/budget/calibration fields. It omits plan paths and machine locators. Approval echoes the displayed plan ID/digest; the facade re-inspects under its lock before writing.

Extend `_task_view` with the same browser-safe `resource_request` after Orchestrator initialization. Unscoped `GET /api/v2/workbench` preserves current behavior. With `?launcher_run_id=...`, the server restores the bound ProjectContext, obtains that context's SQLite backend inside `bind_project_context`, constructs WorkbenchReader, and releases the binding after the read. The UI never totals resources or infers readiness from labels.

An externally owned Orchestrator may return `running` before its formal Prediction completion is visible. Status GET remains read-only. When a later poll returns `pending` at Critic or Planner, the browser calls the narrow `POST .../continue` seam; that seam delegates only to ordinary Launcher resume with no approval paths. Launcher then stops at the next gate, terminal outcome, or blocker. This avoids both mutation-on-GET and a silent second-round stall.

### 7. Use synchronous calls with honest indeterminate state

The threaded local adapter can keep serving reads while launch advances to a formal pause or outcome. The submitting button is disabled and the UI shows indeterminate activity, never fake progress. The tab-persisted Launcher ID supports status/recovery if the HTTP response is lost. No background-command state machine is introduced.

### 8. Preserve the visual system with a dismissible laboratory launch ledger

The operator is a cyclic-peptide design scientist; the new surface's single job is to move one reviewed target into a budgeted run. Reuse existing tokens exactly: canvas `#eef2f4`, raised white `#ffffff`, ink `#182126`, selection blue `#245f7a`, exploratory amber `#9a6223`, STIX Two Text, IBM Plex Sans, and IBM Plex Mono.

On the first load of each browser-tab session, New project opens as a full-viewport sheet above the already mounted workbench. It does not reflow or replace the existing three-column frame. The sheet uses the current canvas, raised surface, divider, shadow, type, status, and focus tokens.

Placement is fixed:

- close icon and `View existing tasks` occupy the top-right safe area;
- target input is the first-focused, dominant control;
- project review is the middle ledger region;
- manual/this-run-auto policy is right-hand at 1440×900 and stacks only inside the sheet on narrow screens;
- `Create and launch` is right-aligned at the ledger footer and disabled until gates/ceilings are valid;
- after dismissal, the only permanent addition is a compact `New project` button in the existing top-bar action group immediately before Refresh.

No global token, WorkbenchFrame grid, navigator, primary view, inspector, history, candidate, Evidence, artifact, transaction, or shortlist component is restyled. New selectors use a `launch-` prefix. The underlying read model and selection stay mounted and unchanged.

```text
+-------------------------- full viewport -------------------------+
| New project                    [View existing tasks] [close]     |
| TARGET             PROJECT REVIEW          LAUNCH POLICY         |
| [MDM2__________] -> approved facts       -> manual / this-run auto|
| [Resolve target]                           GPU min [60] candidates|
|                                             [Create and launch]   |
+------------------------------------------------------------------+
| Awaiting approval: planner_<id>                                  |
| exact task / scope       slots     provisional estimate   ceiling|
| evaluate candidates       1              42 min           <= 60 |
|                                      [Approve and continue]      |
+------------------------------------------------------------------+
```

A numbered generic wizard would look interchangeable with onboarding software; a replacement landing page would hide the workbench. The domain ledger gives first-time presenters an obvious start while preserving immediate access to existing work. Motion is limited to connector activation and an indeterminate request marker, respecting reduced motion.

## Risks / Trade-offs

- [A launch response may time out] → Persist the caller-generated Launcher ID before submission; retry/status uses that exact durable Launcher identity.
- [Auto approval may look like a bypass] → Require explicit per-run opt-in and ceilings, create a standard approval artifact, expose approver/justification, and stop on uncertainty.
- [Scoped binding may leak the prior project] → Restore/bind the exact context under the existing process lock and test alternating project reads.
- [Estimate may be mistaken for calibration] → Label `simple-v1` provisional and keep pending benchmark calibration visible in plan, API, UI, and narration.
- [New UI may dilute the existing aesthetic] → Add only the token-reusing sheet and one top-bar button; verify unchanged underlying DOM/computed style/geometry/content.
- [The worktree has unrelated user changes] → Limit edits to declared surfaces and review the fixed diff.

## Migration Plan

1. Characterize caller/default Launcher identity, project draft, E3 pause, explicit approval, estimate conflict, and scoped/unscoped reads.
2. Add optional Launcher identity and operator facade; normalize bootstrap budget/estimate projection; prove legacy compatibility.
3. Add `/api/v2/control/...` service/routes; preserve `/api/v1` and unscoped V2 behavior.
4. Add launcher-scoped workbench/resource projections and strict client validation.
5. Add the dismissible launch ledger and regression tests; browser-verify it and the unchanged workbench at 1440×900.
6. Rollback removes new UI/control/scoped interfaces and the caller-ID option. Existing server-generated IDs, projects, runs, approvals, Store data, and CLIs remain valid; published immutable plans remain history.
