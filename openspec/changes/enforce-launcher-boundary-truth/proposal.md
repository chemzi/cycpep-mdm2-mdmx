## Why

The first real Launcher lifecycle exposed two owner-boundary false completions: initial Design can turn swallowed scientific-tool failure into a successful zero-candidate completion, and Prediction can appear complete without satisfying its own Critic-readiness/evidence contract. These contradictions let later boundaries run and make `launch`, `status`, and `resume` disagree about the same formal state.

## What Changes

- Require Launcher initial Design to distinguish typed scientific-tool failure from a normal, successfully executed zero-result. Tool failure writes a correlated tool-failure receipt and MUST NOT be reported as `initial_design_no_valid_candidates`; only a normal empty outcome may use that blocker.
- Require initial Design to publish `design_initial_completion` only for a non-empty, formally referenced candidate set. A normal zero-result writes one correlated terminal failure receipt and does not invoke Prediction.
- Make Launcher-production Prediction reuse the Prediction owner's battery-to-status and Critic-readiness/evidence contract. Missing required evidence, pending work, structurally invalid evidence, or an owner-declared non-ready terminal status blocks before Critic.
- Make `launch`, read-only `status`, and `resume` project the same structured blocker for the same formal state; a persisted failed boundary must not degrade to generic `pending` while the contradiction remains.
- Add focused real-contract regression coverage for the exact E2E failures observed on `launcher_9888b3fb181d4ab9b4295a5e14841905` and `launcher_03a8ecab979a43b6bc56055e16fc9723` without depending on those server artifacts.
- Preserve legacy route call signatures and behavior outside the Launcher initial adapter; no existing formal record is rewritten or backfilled.

## Capabilities

### New Capabilities

- `workflow/launcher-boundary-truth`: Defines the Design and Prediction owner proof required before Launcher may advance and the consistent blocker projection across commands.

### Modified Capabilities

None. The completed Launcher change has not yet been archived into the main spec set, so this change introduces one narrowly scoped capability rather than pretending to modify a nonexistent main capability.

## Impact

- **Behavior:** Scientific-tool failure and normal zero-output are distinguished at Design; Prediction advances only through its owner-defined Critic-readiness/evidence contract.
- **Public interfaces:** Existing CLI arguments and legacy Design route signatures remain compatible. Launcher initial Design gains an additive typed-outcome adapter; structured blocker codes are additive.
- **Data format:** Initial Design adds one correlated `design_initial_failure` receipt for deterministic zero-result or classified scientific-tool failure. Existing completion documents keep their schemas but are emitted or accepted under stricter preconditions.
- **Migration:** No automatic repair, backfill, or reinterpretation of existing runs. Ambiguous pre-change runs remain blocked and auditable.
- **Affected code:** Initial Design route outcome/completion validation, Prediction owner readiness and invocation validation, Launcher formal boundary projection, and their focused tests.
- **Non-goals:** Installing or implementing Boltz, PRODIGY, PyRosetta, or other Prediction executors; registering every Design file as a formal Artifact; purifying CLI progress output; changing scientific thresholds/protocols; or redesigning Store, Planner, Orchestrator, or Worker.
- **Legacy path retained:** Non-Launcher Design and Prediction callers retain their existing public behavior; the stricter readiness gate applies to Launcher-correlated production invocations.
- **Explicit handoff:** `critic_review.project_id`, Critic persistence, Critic transaction handling, Critic idempotency, and all Critic project-binding implementation belong exclusively to the separate `critic-project-binding` change and are not modified here.
