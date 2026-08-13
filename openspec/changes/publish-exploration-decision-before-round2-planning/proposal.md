## Why

E3-C now requires one formal ExplorationDecision before a closed-loop iterate-design plan, but production workflow composition never invokes the existing E2 shortlist/Decision writers. A fresh Launcher therefore completes Round1 scientific work and Critic, then deterministically fails with `exploration_decision_required` despite all required source Evidence being present.

## What Changes

- Add one workflow-owned boundary on the unified Planner-resolution path that receives the already owner-validated current Prediction boundary and materializes the Critic exploration shortlist and ExplorationDecision from current formal evidence on both fresh and resume flows.
- Reuse the existing `exploration_shortlist`, `record_exploration_shortlist`, `build_exploration_decision`, and `record_exploration_decision` owners; do not duplicate scientific policy or Decision validation.
- Inspect before write and reuse only the exact canonical current-run publications, so Launcher resume is deterministic and conflicts fail closed.
- Require the complete formal source set and current approved project/threshold bindings through one explicitly injected Store; unexpected missing, ambiguous, or invalid evidence blocks Planner rather than restoring ambient adaptation.

## Capabilities

### New Capabilities

- `workflow/exploration-decision-publication`: Deterministic production publication of the current-round Critic shortlist and immutable ExplorationDecision before closed-loop Planner execution.

### Modified Capabilities

None.

## Impact

- Production scope is limited to workflow composition/adapters plus a narrow public publication helper around existing E2 owners and focused tests.
- No new agent, action, Store schema, transaction, sidecar, scientific algorithm, threshold, protocol, Prediction executor, Design executor, approval, budget, retry, or readiness behavior is introduced.
- Existing formal Evidence event formats and E2 Decision contract remain unchanged. The injected SQLite Store is authoritative; its explicit path does not add a JSONL projection guarantee, while legacy writer calls retain existing projection behavior. Old failed invocations remain immutable and fresh runs use the new edge.
- Legacy direct Planner callers that explicitly do not require a Decision remain compatible; formal Launcher Round2 planning does not.
