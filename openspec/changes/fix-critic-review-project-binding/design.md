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
- Do not generate Prediction artifacts, change Prediction run generation, readiness/completion decisions, or scientific behavior, calibrate thresholds, or reinterpret pending-only Prediction as scientific completion.
- Do not change Planner, Orchestrator, transaction ownership/commit semantics, Store schema, or scientific protocols. Worker changes are limited to validating event/trace agreement before formalization.
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

### 5. Bind the immutable report source to the inspected project

After resolving and reading the immutable report, the Critic inspector will require `report.source.project_id` to equal the inspected `project_id` in addition to the existing Evidence query, prediction-run, report identity, and digest checks. A mismatch marks the candidate record invalid and therefore fails closed under the existing ambiguity outcome; the inspector query and recovery policy remain unchanged.

### 6. Reuse Evidence trace conflict semantics before transaction commit

Transactional Evidence formalization will construct an `EvidenceEvent` with the Worker `TraceContext` while retaining event-supplied trace keys in the payload passed to that contract. `EvidenceEvent.to_dict()` already rejects a payload trace field that disagrees with its trace context, so the Worker will reuse that behavior before Store commit rather than silently applying `{**event, **trace}` precedence or defining another identifier rule. The failure occurs before formal commit and therefore preserves existing transaction rollback/state atomicity.

### 7. Validate report project identity with the formal Trace ID contract

`critic_persistence_effects()` will validate `source.project_id` through the shared trace identifier validator before constructing State, history, or Evidence effects. It will not copy or restate the trace regex. This preserves the immutable report source as the sole identity authority while ensuring direct persistence cannot mutate formal State before a later writer rejects an invalid identifier.

### 8. Close the legacy Critic writer for new events

`EvidenceLogger.critic_review()` will require `project_id` for every new call and include it in the event. The generic `EvidenceLogger.log()` boundary will apply the shared Trace ID validation whenever `event_type == "critic_review"`, so neither the convenience method nor a direct generic call can create new unbound Critic Evidence. Direct Critic remains on the shared effect contract and generic logger seam. Historical unbound rows remain readable through legacy Store ingestion and are neither rewritten nor backfilled.

### 9. Keep Prediction identity out of the formal trace namespace

Strengthened Worker validation exposes a pre-existing collision in deferred Prediction Evidence: the Prediction domain run identity is emitted as payload `run_id`, while formal `TraceContext.run_id` is the distinct Orchestrator run identity. The transaction Evidence adapter will normalize only that payload field to `prediction_run_id` before the event reaches Worker formalization. Worker will continue validating every `TRACE_KEYS` field without agent-specific exceptions; committed top-level `run_id` remains the Orchestrator trace and `prediction_run_id` preserves the Prediction identity.

Writing both keys, teaching Worker to skip Prediction conflicts, or making readers infer which identity a `run_id` represents were rejected because each preserves the ambiguity. Prediction run generation, artifacts, readiness/completion logic, and Launcher inspection remain unchanged.

## Risks / Trade-offs

- **[Risk] Existing tests construct Critic reports without `source.project_id`.** → Update only fixtures that exercise new persistence; keep pure report-reading compatibility tests unchanged where persistence is not invoked.
- **[Risk] A retry could append duplicate events.** → Define current idempotency with the minimal full binding and test two consecutive reruns.
- **[Risk] The preserved run also contains missing Prediction evidence.** → Resume acceptance proves only that Critic completion becomes visible and Planner is reached or returns its own formal outcome; it does not claim scientific completion.
- **[Risk] Worker trace validation accidentally changes transaction ownership.** → Reuse `EvidenceEvent` conflict behavior before commit and assert no Critic event or State mutation on mismatch; do not change Store/commit ownership.
- **[Risk] Correcting the deferred Prediction field could alter Prediction behavior.** → Normalize only the transaction Evidence payload at its adapter boundary and characterize both identities plus existing Launcher correlation; leave manifests, run directories, scientific logic, and completion readers unchanged.

## Migration Plan

1. Deploy the additive producer fix after focused and full verification.
2. Preserve the failed E2E directory and its old unbound Evidence unchanged.
3. Run explicit `workflow resume` against the same launcher ID and isolated runtime locator.
4. Verify Research, Design, and Prediction are not repeated; verify one new project-bound Critic event is present and Launcher advances to Planner or a Planner-owned outcome.
5. Roll back the code if verification fails; do not delete or edit either Evidence event. No schema rollback is required.
