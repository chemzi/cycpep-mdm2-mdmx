## Why

E2 now provides an immutable, fully validated `ExplorationDecision`, but Planner has no explicit frozen-input boundary for consuming it. E3-A must bind that Decision into Planner identity and provenance without applying its adjustment or consulting ambient Evidence/history, while preserving byte-for-byte legacy plan identity when no Decision is supplied.

## What Changes

- Add an explicit optional `exploration_decision` input to `build_plan()` and parse it only through the public `ExplorationDecision.from_dict()` contract.
- Validate the Planner handoff bindings for project, workflow, source/applicable rounds, Prediction run, and target scope; mismatches fail closed before plan assembly.
- Canonicalize the validated Decision with `validated_decision.to_dict()` and bind its Decision ID plus `object_sha256(...)` to Planner `input_digest`.
- Inject the canonical Decision only into Planner's local State copy under `_frozen_exploration_decision`; do not call `State.update`, write Evidence, or discover a Decision from ambient history.
- Add Decision ID, canonical Decision SHA-256, and Decision input digest to the Planner source only when an explicit Decision is present.
- Preserve the legacy digest and source shape exactly when the optional input is absent.
- Leave task materialization unchanged: no Decision adjustment is applied to `design_jobs`, proposal counts, lengths, seeds, approvals, orchestration, or execution.

## Capabilities

### New Capabilities

- `workflow/planner-exploration-decision-input`: Explicit, validated, deterministic binding of one frozen E2 ExplorationDecision into Planner provenance and plan identity without applying its adjustment.

### Modified Capabilities

None.

## Impact

- **Architecture:** adds a one-way public Contract-to-Planner handoff; Planner remains pure with respect to formal State/Evidence and creates no new executable action.
- **Code:** limited to Planner plan construction/validation, one narrow `task_builder.py` guard that disables its legacy ambient-experience fallback only when the private frozen-Decision marker is present, an additive Planner plan schema update, focused tests, and optional Planner documentation.
- **Public interface:** `build_plan()` gains one optional keyword-only argument. Existing callers remain compatible.
- **Plan data format:** the Critic-source variant gains three additive source properties only for plans built with an explicit Decision. Decision-absent plans retain their prior source object and digest/ID.
- **Persistence/migration:** none. No Store, SQLite, Evidence, runtime locator, project runtime data, approval, plan, or transaction migration is introduced.
- **Integration target:** the Draft PR targets the shared `e3/closed-loop-runtime` branch created from the frozen baseline. This change does not follow later `integration/data-integrity-transaction` updates; E3 integration synchronizes with integration only after E3-A and E3-B land on the shared branch.
- **Legacy path:** callers that omit `exploration_decision` continue through the existing Planner behavior, including the ambient-experience fallback. The explicit Decision path cannot invoke that fallback or record an applied preference; missing configured lengths use the existing static `[8, 10, 12]` fallback without consulting the Decision adjustment.
- **Non-goals:** applying Decision adjustments, changing design jobs/proposal counts/lengths/seeds, reading or writing formal Evidence, modifying project configuration or approved digests, changing approval/Orchestrator/Launcher/Execution/Prediction, or running scientific subprocesses.
