## Context

See `proposal.md` for motivation and `specs/workflow/planner-exploration-decision-runtime-handoff/spec.md` for behavior. The E2 owner already publishes `exploration_decision` through `record_exploration_decision()`, whose dedicated writer validates the contract and its formal source Evidence before appending to SQLite. E3-A makes `build_plan(exploration_decision=...)` the validation/binding boundary, and E3-B consumes only its invocation-local `_frozen_exploration_decision`.

The missing production edge is `DefaultWorkflowRuntime.run_planner()` -> `agents.planner.service.run()`: the runtime owns an injected project Store, but currently passes only the Critic report path and project config. Planner service then calls `build_plan()` without a Decision.

## Goals / Non-Goals

**Goals:**

- Introduce one read-only, Store-scoped retrieval seam owned by workflow composition, not Planner.
- Carry a canonical Decision, the formal Prediction workflow identity, and an explicit required/not-required bit across the runtime/service boundary.
- Use one State snapshot for retrieval scope and plan construction so round/workflow binding cannot drift between the two operations.
- Preserve E3-A as the final binding validator and E3-B as the only materializer.

**Non-Goals:**

- Automatically creating a Decision after Critic, changing the E2 policy, or modifying the dedicated E2 publication writer.
- Adding any new State projection, Store table/schema, sidecar, cache, directory convention, or fallback authority.
- Changing the Launcher state machine, Orchestrator, Execution, Design executor, Prediction, Critic scientific policy, thresholds, protocols, or GPU execution.

## Decisions

### 1. Resolve at the workflow composition boundary

Add a small public workflow handoff module that receives the injected Store, the formal Critic artifact path, the explicit project ID, and the same State snapshot later passed to Planner service. It resolves the unique formal `prediction_handoff_ready` event for the Critic `prediction_run_id`, uses that publication's `workflow_id` as authority, and uses Planner's public recommendation mapping to identify whether the Critic recommendations materialize `iterate_design`. It returns an immutable handoff value containing `workflow_id`, `required`, and either the canonical Decision mapping or `None`.

This keeps dependency direction `workflow -> formal Prediction/E2 publications -> public Decision contract -> Planner service`. Planner never queries Evidence. The alternative of adding Store lookup to `agents.planner.service.run()` was rejected because it would restore ambient Evidence discovery inside Planner and make direct callers context-sensitive.

### 2. Select by formal current-run identity, then let E3-A validate all bindings

The resolver reads only the Critic artifact already selected by `FormalBoundaryInspector`, determines whether its recommendation actions map to `iterate_design`, and queries the injected Store for the unique `agent=prediction`, `event_type=prediction_handoff_ready` event for the Critic Prediction run plus `agent=critic`, `event_type=exploration_decision` for the same project, Prediction run, and current source round. The Prediction event supplies the expected workflow identity. Zero required Decision matches is `exploration_decision_required`; missing or ambiguous Prediction identity and multiple Decision matches fail closed. A selected Decision row is canonicalized with the existing public `ExplorationDecision.from_dict().to_dict()` contract.

The resolver intentionally does not reproduce target/project/applicable-round/target binding logic. It transports the formal Prediction `workflow_id`; E3-A `_bind_exploration_decision` remains the independent validator that the selected Decision agrees with that transported workflow identity and all other Planner inputs.

Alternatives rejected: filesystem or JSON sidecar discovery is not a formal authority; State/diagnostic projections can be stale; choosing the latest matching event is nondeterministic and hides ambiguity.

### 3. Make missing semantics explicit in the Planner service API

Extend keyword-only `run()` with additive `exploration_decision` and `exploration_decision_required` inputs. The service checks only the cross-field presence rule (`required` requires a non-`None` value), then calls `build_plan(..., exploration_decision=exploration_decision)`. It performs no contract parsing and no materialization itself.

Direct callers that omit both arguments retain legacy compatibility. Initial bootstrap uses a separate public planner function and is unaffected. `DefaultWorkflowRuntime` always supplies both values from the formal handoff, making closed-loop intent explicit instead of inferred inside Planner.

### 4. Reuse one State snapshot

`DefaultWorkflowRuntime.run_planner()` loads State once, supplies it to the handoff resolver, copies the returned formal Prediction `workflow_id` into that invocation-local mapping, then passes the same mapping to Planner service. The runtime never calls `State.update` for this identity. This makes the formal Prediction publication—not ambient/default State or `_plan_workflow` derivation—the authority while preventing retrieval from selecting against one round and `build_plan()` validating another.

## Risks / Trade-offs

- **[A formal Decision publication is not yet produced by the Launcher itself]** -> E3-C consumes the existing E2 authority but does not invent or relocate publication. A closed-loop iteration reaches an explicit missing-Decision failure until the E2 owner has published the required event, which is safer than legacy fallback.
- **[Legacy direct service callers can still omit a Decision]** -> Compatibility is deliberate and observable through the explicit default `exploration_decision_required=False`; only `DefaultWorkflowRuntime` declares the formal closed-loop requirement.
- **[Corrupt legacy Store rows may exist]** -> Canonicalize the selected row through the existing public Decision contract and fail closed; do not add hashes beyond the E3-A provenance contract.
- **[State remains the current Planner round authority]** -> Pass one snapshot through retrieval and build. Workflow identity instead comes from the matching formal Prediction publication and is added only to the invocation-local copy; do not persist shadow runtime state.

## Migration Plan

1. Add the resolver and focused unit tests without changing production Planner calls.
2. Add the compatible Planner service keywords and service integration tests.
3. Redirect `DefaultWorkflowRuntime.run_planner()` through the resolver and run workflow/runtime integration tests.
4. Roll back by reverting the runtime redirect and additive service inputs; no persisted schema or data migration is required. Plans already produced remain valid because E3-A provenance fields are already schema-supported.
