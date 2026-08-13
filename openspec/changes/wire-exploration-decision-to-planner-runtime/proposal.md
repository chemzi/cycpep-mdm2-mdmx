## Why

E3-A and E3-B can validate, bind, and materialize an explicitly supplied `ExplorationDecision`, but the formal workflow runtime still calls Planner without that input. E3-C must close the existing Store-to-runtime-to-Planner handoff so a Round 1 Decision deterministically governs Round 2 design lengths, while making an expected-but-missing formal Decision fail closed instead of silently reverting to legacy lengths.

## What Changes

- Add a formal, project-scoped resolver that selects the current `prediction_handoff_ready` publication as workflow-identity authority and one validated `exploration_decision` publication from the injected SQLite Store using the current Critic/Prediction/round scope; arbitrary JSON, logs, directory scanning, State projections, derived defaults, and ambient experience are not authorities.
- Extend `agents.planner.service.run()` with explicit Decision handoff inputs and forward the supplied Decision unchanged to `build_plan(exploration_decision=...)`.
- Have `DefaultWorkflowRuntime.run_planner()` use its existing formal Store and Critic artifact context to resolve and pass both the Decision and formal workflow identity for closed-loop iteration planning, injecting the workflow identity only into the invocation-local Planner State copy.
- Define an explicit missing-Decision contract: a formal closed-loop plan that will materialize Round 2 `iterate_design` requires a matching Decision and fails closed when absent; direct legacy callers and bootstrap planning remain explicit no-Decision paths.
- Reuse E3-A `ExplorationDecision.from_dict()` validation/binding and E3-B design-job materialization without duplicating either implementation.
- Add integration coverage proving formal service/runtime Decisions `[12]` and `[10, 12]` reach `iterate_design.design_jobs[*].lengths`, bind plan provenance, remain deterministic, preserve all non-length allocation fields, and reject scope mismatches or required missing Decisions.

## Capabilities

### New Capabilities

- `workflow/planner-exploration-decision-runtime-handoff`: Formal Store-backed retrieval and typed runtime/service handoff of an ExplorationDecision into Planner for closed-loop Round 2 planning.

### Modified Capabilities

None.

## Impact

- **Architectural purpose:** close the one-way Critic/Decision -> workflow runtime -> Planner public-contract boundary; no new executable action is introduced.
- **Affected boundaries:** formal SQLite Evidence query/resolution, `DefaultWorkflowRuntime.run_planner()`, and the public keyword-only `agents.planner.run()` service interface.
- **Public interface:** `agents.planner.service.run()` gains additive keyword-only Decision handoff arguments; existing direct callers remain compatible and are the explicit legacy no-Decision path.
- **Behavior:** formal closed-loop iteration planning fails closed if its required Decision is absent or ambiguous. A matching Decision changes only E3-B length materialization and E3-A provenance/identity.
- **Data format and migration:** no Store schema, Evidence schema, plan schema, project configuration, threshold, protocol, or persistence migration.
- **Legacy/bypass retained:** initial bootstrap planning and direct Planner service calls that do not declare a Decision requirement remain no-Decision compatible. They do not gain ambient Decision discovery.
- **Non-goals:** changing Prediction or Critic scientific policy, thresholds, Design executor behavior, proposal counts, routes, allocation, seeds, Launcher execution, GPU subprocesses, Orchestrator, Execution, or production runtime state.
