## Why

The first complete real Launcher run proved that Initial Design can atomically publish real candidates while the next Launcher step still calls the ingest/evaluate-only `agents.prediction.run()` seam against an empty artifact root. PR68 correctly blocks those `prediction_pending` records before Critic, but the production lifecycle has no formal, approval-gated bootstrap path that can invoke the existing `evaluate_new_design_candidates` Execution handler to materialize the required scientific evidence.

## What Changes

- Add a Planner-owned bootstrap Prediction plan variant for the single pre-Critic condition: a correlated Initial Design transaction is `COMMITTED`, its exact candidate set is authoritative, and no Critic-ready Prediction completion exists.
- Bind the immutable bootstrap plan and its approval request to the approved project, Launcher/Design completion, committed transaction, exact candidate set, active Prediction protocol, action `evaluate_new_design_candidates`, and declared execution budget.
- Bind the invocation, plan, and execution receipts to auditable scientific protocol/configuration identity, including Prediction protocol version, ColabDesign commit, Boltz and PyRosetta versions, model/checkpoint identity, and configuration digest. Treat machine-specific executable, cache, checkpoint, repository, and output paths only as execution locators; absolute paths do not define scientific identity.
- Apply that shared execution identity contract to every newly generated `evaluate_new_design_candidates` task, including later Critic-driven plans, so the reused handler has one contract rather than a bootstrap-only variant.
- Change Launcher production coordination after Initial Design so it creates or recovers that bootstrap plan, returns `awaiting_approval` before scientific execution, and initializes the existing Orchestrator/ExecutionWorker path only after explicit valid approval.
- Reuse the registered `evaluate_new_design_candidates` handler as the only scientific executor. Launcher will not invoke ColabDesign, Boltz, PRODIGY, Rosetta, post-relax, or another Prediction materializer directly.
- After Worker completion, recover Prediction only from the approved plan, Orchestrator run/task/attempt, committed transaction, formal Artifact/Evidence, and validated task output. Preserve `run_id` as Orchestrator identity and `prediction_run_id` as Prediction domain identity.
- Require the Prediction owner readiness validator to report `completed` before Launcher invokes Critic. Any `prediction_pending` candidate remains `prediction_execution_incomplete` and blocks downstream work.
- Make `launch`, `status`, and `resume` project the same bootstrap-plan, approval, execution, recovery, and readiness state; completed expensive work is not repeated, and unknown scientific execution is never retried automatically.
- Add an operator-explicit retry contract for a formally failed bootstrap execution: preserve the failed plan/run/task/transaction, create a new immutable retry plan from the same Initial Design completion, committed Design transaction, and exact candidate set, require a new approval, and reuse the normal Orchestrator/Worker path without rerunning Research or Initial Design.
- Preserve the existing direct Prediction ingest API for non-Launcher callers and preserve old Launcher runs under their existing immutable direct-invocation evidence. No historical run is rewritten or promoted into the new bootstrap path.
- Close the PR review gaps without changing the bootstrap architecture: derive observed execution identity from actual runtime/tool/model/config observation, preserve Python virtual-environment entrypoint semantics, validate installed PyRosetta and PRODIGY versions, require exact-scope formal Prediction record Artifacts plus transaction-bound record and handoff-ready Evidence during recovery, and permit explicit retry only from a fully correlated terminal transaction that explicitly allows retry.

## Capabilities

### New Capabilities

- `workflow/approval-gated-initial-prediction-execution`: Defines the Planner-owned immutable bootstrap plan, approval and execution lifecycle, formal Prediction recovery, and Critic advancement gate between committed Initial Design and the normal Critic-driven workflow.

### Modified Capabilities

None. The autonomous Launcher and boundary-truth changes are not yet archived as main capability specs, so this change introduces one standalone workflow capability rather than modifying an absent main spec.

## Impact

- **Behavior:** A new Launcher run with committed Initial Design candidates and no Critic-ready Prediction pauses at `awaiting_approval` instead of directly producing pending ingest records. Valid approval drives the existing Worker handler; Critic remains unreachable until owner readiness completes.
- **Public interfaces:** Planner gains an additive formal bootstrap-plan entry point/plan variant, and Launcher resume gains an explicit bootstrap-Prediction retry request for a terminal failed execution. Newly generated Prediction execution tasks gain a required path-independent execution identity. `agents.prediction.run()` and `PredictionPipeline` gain an optional observed-execution-identity input used by the transaction-managed Worker path; existing callers remain compatible and the direct/non-Launcher default remains unchanged. Existing Critic-driven plan source semantics and scientific behavior remain unchanged.
- **Data formats:** The Planner plan contract gains a versioned bootstrap source variant bound to Initial Design completion and committed candidate scope, plus a protocol/configuration identity that is independent of deployment paths. Expected identity is plan authority; observed identity is independently runtime-derived and required in formal handoff/record output. Existing plan, approval, task, transaction, Artifact, Evidence, and Prediction record formats are reused wherever possible; no Store schema changes.
- **Migration:** Forward-only. Existing direct Launcher Prediction receipts, completed historical plans, and failed runs remain immutable and continue to recover through their current contract. An unstarted historical Prediction task that lacks the new execution identity is not rewritten and cannot begin a new scientific invocation after upgrade; it must be regenerated from its immutable source under the current Planner contract. Only new Launcher runs without a direct Prediction start receipt may enter the bootstrap path.
- **Affected production areas:** Planner plan building/validation and schema, approval-compatible plan validation, Launcher service/runtime/boundary recovery, and formal task-output-to-Prediction correlation. The registered Execution action and scientific tool implementations are reused rather than copied.
- **Non-goals:** No L1-L7, `CRITIC_READY_STATUSES`, Critic, threshold, Store schema, scientific executor, candidate-selection, or automatic-approval changes; no candidate-scope reduction; no modification of the preserved failed Launcher run; no unrelated P2 cleanup.
- **Legacy path retained:** Direct/non-Launcher Prediction ingestion remains supported. It is no longer the new-Launcher pre-Critic execution path, and it cannot bypass the bootstrap approval contract.
