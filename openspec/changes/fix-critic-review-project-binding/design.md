## Context

See `proposal.md` for motivation. The real E2E produced a valid report and `critic_review`, but `contracts/critic.py` only copied `prediction_run_id` and report fields into the Evidence payload. `workflow.boundaries.FormalStateInspector.critic()` intentionally queries by project before validating the prediction-run and immutable document bindings, so the unbound event is invisible. The same effect contract feeds direct Critic persistence and the transaction-managed handler, but the latter assembles its event field-by-field.

The preserved run also constrains recovery: direct Critic persistence currently treats any event with the same `report_id` as idempotent. Merely adding `project_id` to future payloads would therefore leave the failed run permanently blocked because its old unbound event would suppress a new authoritative event.

## Goals / Non-Goals

**Goals:**

- Establish one project identity source: the already immutable Critic report `source.project_id`.
- Make direct and transaction-managed Critic Evidence carry the same project binding.
- Preserve append-only history while allowing explicit resume to replace an unbound observation with a new authoritative event.
- Prove the fix at the public contract and formal inspector seams before resuming the server run.

**Non-Goals:**

- Do not change Launcher queries, recovery ordering, blocker policy, or CLI output.
- Do not generate Prediction artifacts, calibrate thresholds, or reinterpret pending-only Prediction as scientific completion.
- Do not change Planner, Orchestrator, Worker, transaction ownership, Store schema, or scientific protocols.
- Do not mutate, delete, or backfill the preserved unbound event.

## Decisions

### 1. Derive project identity only from the immutable report source

`critic_persistence_effects()` will validate `report["source"]["project_id"]` as a non-empty project identifier and copy it into the returned Evidence effect. Callers will not accept ambient project configuration as an alternate authority.

This keeps the Evidence bound to the document it attests. Passing another project argument was rejected because two identity inputs could disagree and would widen the public function signature unnecessarily.

### 2. Keep one shared effect contract, update both publishers

Direct `agents.critic.report.run()` already forwards the effect payload generically, so it gains the binding from the shared contract. The transaction-managed Critic handler will add only `project_id` from that same effect when assembling its typed event; transaction staging and commit behavior remain unchanged.

Duplicating project derivation in each caller was rejected because it would recreate the contract mismatch this change is closing.

### 3. Tighten direct idempotency to the full current binding

The direct persistence guard will recognize an existing current event only when project ID, prediction run ID, report ID, and report digest all match. An old event missing project ID will not be modified or counted as current, so an explicit rerun can append one authoritative event. Once that event exists, repeated reruns remain idempotent.

Treating every matching `report_id` as authoritative was rejected because it preserves the observed blocker; rewriting the old event was rejected because formal Evidence is append-only.

### 4. Leave Launcher fail-closed inspection unchanged

The inspector's project-scoped query and immutable-document validation are the desired consumer contract. Tests will demonstrate that the corrected producer satisfies it and that another project's otherwise valid event is ignored.

Relaxing the inspector to accept projectless events was rejected because it would turn ambiguous legacy data into transition authority.

## Risks / Trade-offs

- **[Risk] Existing tests construct Critic reports without `source.project_id`.** → Update only fixtures that exercise new persistence; keep pure report-reading compatibility tests unchanged where persistence is not invoked.
- **[Risk] A retry could append duplicate events.** → Define current idempotency with the minimal full binding and test two consecutive reruns.
- **[Risk] The preserved run also contains missing Prediction evidence.** → Resume acceptance proves only that Critic completion becomes visible and Planner is reached or returns its own formal outcome; it does not claim scientific completion.

## Migration Plan

1. Deploy the additive producer fix after focused and full verification.
2. Preserve the failed E2E directory and its old unbound Evidence unchanged.
3. Run explicit `workflow resume` against the same launcher ID and isolated runtime locator.
4. Verify Research, Design, and Prediction are not repeated; verify one new project-bound Critic event is present and Launcher advances to Planner or a Planner-owned outcome.
5. Roll back the code if verification fails; do not delete or edit either Evidence event. No schema rollback is required.
