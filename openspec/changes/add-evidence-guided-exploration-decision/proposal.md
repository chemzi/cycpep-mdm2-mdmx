## Why

Prediction evidence, conservative failure statistics, and exploration shortlist ranking already exist, but the workflow has no immutable, current-round object that records what the evidence recommends changing next and why. Without that boundary, accumulated historical evidence can influence legacy Planner/Design consumers directly, while no deterministic Decision identity or complete provenance exists for a future E3 consumer.

## What Changes

- Add an immutable `ExplorationDecision` contract that binds one prediction run, candidate handoff scope, source and target rounds, source Evidence, shortlist Evidence, approved project envelope, threshold identity, and protocol identity.
- Add a deterministic decision builder that reuses existing failure aggregation and conservative length policy only over explicitly supplied current-round Evidence.
- Treat approved `project.targets[*].design.lengths` as the allowed length envelope; the Decision may only narrow relative policy weights inside it and may never expand it. These weights are not actual round sampling or proposal counts.
- Require exactly one current `battery_evaluated` event for every candidate in the bound Prediction handoff; missing, extra, or duplicate candidate evidence fails closed.
- Produce an auditable `no_adjustment` Decision when current-round evidence does not satisfy the existing minimum-sample and failure-rate rule.
- Append a formal `exploration_decision` Evidence event through the existing Store-backed Evidence authority after the Decision validates successfully, with sequential retry idempotency by `decision_id` and canonical payload.
- Make the dedicated writer the only supported formal append path for `exploration_decision`; the generic Evidence writer rejects this event type so source verification cannot be bypassed.
- Bind the Decision to the existing formal `prediction_handoff_ready` authority, deriving the candidate set, Prediction run, protocol, and canonical threshold identity from that handoff rather than caller claims.
- Propagate one canonical normalized threshold digest through Prediction battery/handoff Evidence into the Decision, and include the versioned conservative policy identity in Decision identity.
- Preserve scientific pass, thresholds, Prediction records, and exploration shortlist semantics unchanged.
- Leave legacy `experience_applied` and its direct Planner/Design consumption path in place for compatibility; E2 does not connect the new Decision to Planner, Design, Orchestrator, or Execution.

## Capabilities

### New Capabilities

- `evaluation/exploration-decision`: Evidence-backed, deterministic, current-round length-allocation Decisions inside an approved project envelope, with formal Evidence persistence and provenance.

### Modified Capabilities

None. The existing `evaluation/exploration-shortlist` requirements and scientific-pass distinction are unchanged.

## Impact

- **Architecture:** introduces a public evaluation contract/builder between Prediction/Exploration Evidence and a future E3 consumer; it does not introduce an executable action.
- **Code:** expected additive changes in a contract module, a focused decision service, the Evidence event allowlist/writer, and tests. Existing statistics and shortlist functions are reused through explicit event inputs.
- **Public interfaces/data:** adds an immutable Python contract and additive `exploration_decision` Evidence payload/event type. No existing signature or stored row is migrated.
- **Persistence:** SQLite-backed Evidence remains the formal authority; JSONL remains only its projection. Decision creation and Evidence append are ordered so failure cannot be represented as completed.
- **Dependencies:** no new runtime dependency and no LLM call.
- **Non-goals:** calibration, threshold changes, Planner/Execution integration, Design invocation, budget allocation, route/model tuning, transaction refactoring, later-round execution, UI, Launcher, and all E3+ work.
