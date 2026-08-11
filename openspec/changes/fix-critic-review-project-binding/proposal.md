## Why

A real Launcher E2E produced a valid Critic report, but the corresponding `critic_review` formal Evidence omitted `project_id`. The project-scoped Launcher inspector therefore could not prove Critic completion and stopped with `critic_completion_unproven` before Planner, despite the report and its prediction-run binding being valid.

## What Changes

- Require every newly persisted `critic_review` event to carry the report source `project_id` alongside the existing prediction-run and report bindings.
- Apply the same binding rule to both direct Critic persistence and transaction-managed Execution persistence.
- Add regression coverage proving project-scoped inspection recognizes exactly one valid Critic report immediately after persistence and rejects cross-project evidence.
- Require the immutable report source project to agree with the inspected project, reject event/Worker trace conflicts before commit, and close the legacy unbound writer for all new Critic Evidence.
- Apply the incidental contract repair required by strengthened formal Evidence validation: normalize transactional Prediction identity from reserved `run_id` to domain-specific `prediction_run_id`, without changing Prediction behavior or scientific semantics.
- Preserve Launcher fail-closed behavior; do not weaken its project-scoped query or document validation.
- Do not repair or backfill the failed E2E run automatically. After the fix is verified and deployed, continue that preserved run only through the supported explicit `resume` path.

## Capabilities

### New Capabilities

- `workflow/critic-project-binding`: Defines the formal project binding required for new Critic completion Evidence and project-scoped Launcher recovery.

### Modified Capabilities

None. The autonomous Launcher capability is still change-local and has not been archived into the main spec set.

## Impact

- **Architectural purpose:** Close one Critic-to-Launcher formal Evidence contract mismatch; no other boundary semantics change.
- **Affected code:** `contracts/critic.py`, direct Critic persistence, transaction-managed Critic handler assembly, formal Evidence trace conflict validation, the transactional Prediction Evidence adapter, project-scoped Critic inspection, and focused boundary tests.
- **Behavior:** A newly written, valid `critic_review` becomes provable under its owning project immediately after persistence. Missing or mismatched project bindings remain non-authoritative.
- **Public interfaces:** `EvidenceLogger.critic_review()` gains an additive `project_id` argument that is required for new writes; its other arguments and all CLI surfaces are unchanged. The existing open Evidence payload gains required `project_id` for new `critic_review` events.
- **Data format:** Additive top-level `project_id` on new Critic Evidence; transactional Prediction Evidence uses `prediction_run_id` for Prediction identity while top-level `run_id` remains the Orchestrator trace identity.
- **Migration:** No migration, backfill, Store rewrite, or reinterpretation of legacy records. The preserved failed E2E remains blocked until an authorized supported continuation produces/uses current formal state.
- **Legacy path retained:** Legacy Critic records remain readable under existing compatibility logic but cannot authorize a current project-scoped transition without the required binding.
- **Non-goals:** Prediction artifact generation, run generation, readiness/completion semantics, or scientific behavior; threshold calibration; Design/Research changes; Launcher query relaxation; Planner, Orchestrator, transaction ownership/commit semantics, Store schema, scientific protocol, or approval changes. Worker changes are limited to rejecting conflicting pre-commit Evidence trace fields.
