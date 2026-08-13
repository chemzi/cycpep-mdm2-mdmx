## Context

See `proposal.md` and `specs/workflow/exploration-decision-publication/spec.md`. E3-C deliberately consumes but does not create `exploration_decision`. The existing E2 owners already provide deterministic shortlist computation/publication and Decision build/publication, while `workflow/service.py::_resolve_critic_and_planner` currently proceeds directly from a completed Critic to Planner. The validated Prediction boundary carries the authoritative handoff path, and the formal handoff carries the authoritative threshold digest; the E2 builder rechecks the adjacent threshold snapshot against that digest before publication.

## Goals / Non-Goals

**Goals:**

- Insert one explicit publication boundary in the unified Planner-resolution path after formal Critic completion and before Planner inspection/execution, shared by fresh and resume flows.
- Resolve the exact current Prediction handoff, battery rows, Critic targets, approved config, threshold snapshot, workflow identity, and round once.
- Make shortlist and Decision publication sequentially idempotent so a crash between the two appends can resume without duplicates.
- Keep Planner read-only with respect to Evidence and preserve E3-C as the unique Store-to-Planner selector.

**Non-Goals:**

- Changing shortlist ranking, length policy, E2 schemas, Critic science, Prediction readiness, thresholds, protocols, Design, Execution, transactions, approval, budget, retry, or Launcher diagnostics format.
- Preloading a Decision, copying evidence across runs, modifying an old failed invocation, or treating files as formal completion.
- Adding a new agent, action, generic outcome framework, sidecar, or Store table.

## Decisions

### 1. Put publication at the unified workflow Planner-resolution boundary

Add a small public workflow publication module and call it from the unified `_resolve_planner` path only after `inspect_critic()` is complete. Both the fresh Critic path and the resume path where Critic is already completed pass the same current owner-validated Prediction boundary into `_resolve_planner`, which passes it with the Critic artifact and runtime's injected Store/project context to the publisher. Do not call `self.inspect_prediction()` inside `run_planner`: initial production Prediction may be the bootstrap Execution boundary rather than the launcher-correlated direct Prediction invocation. The returned canonical Decision is not passed directly as a second authority; Planner still resolves it through E3-C's formal Store selector.

Alternative: publish inside Critic. Rejected because Critic owns review/shortlist science but does not own the approved project, Prediction run locator, or closed-loop sequencing. Alternative: publish inside Planner. Rejected because Planner must not query or mutate Evidence.

### 2. Reuse E2 builders and make their writers Store-injectable

Add an optional explicit Store parameter through the existing shortlist writer, Decision builder formal-source validation, and Decision writer, preserving legacy omission behavior. The new publication module computes and validates through the existing public builders and writes through those owners using the same Store already held by `DefaultWorkflowRuntime`; it verifies that Store project identity matches the approved project. This closes project authority without duplicating event construction or policy logic. The injected SQLite path is authoritative and need not refresh the legacy JSONL projection; omitted-Store writer calls preserve existing projection behavior.

Alternative: rely on ambient `EvidenceLogger` while inspecting the injected Store. Rejected because read and write authorities could diverge. Alternative: construct events directly in workflow. Rejected because it duplicates dedicated-writer ownership.

### 3. Treat the validated Prediction locator as the threshold artifact binding

Read `inputs/thresholds.json` only from `handoff_path.parent/inputs/thresholds.json`, where `handoff_path` comes from the current completed Prediction boundary already validated by its owner. This supports both bootstrap Execution and direct Prediction without assuming `run_root` is present. Then let `build_exploration_decision()` recheck its canonical digest against the formal handoff. Battery and handoff rows come from the injected Store, not the filesystem. The Critic report supplies current required targets; the requirement to publish is obtained from a public read-only seam extracted from E3-C's existing recommendation-to-ActionType classifier, and E3-C remains the independent downstream binding check.

Alternative: read ambient State thresholds. Rejected because State can drift after Prediction. Alternative: add threshold contents to a new Evidence format. Rejected as unnecessary schema expansion.

### 4. Share E3-C's iteration classifier

Promote the existing E3-C Critic recommendation classifier to one public read-only workflow function used by both Decision selection and publication. It continues to consume Planner's public recommendation mapping and `ActionType`; the publisher does not maintain a second list or treat any generic `iterate` verdict as sufficient.

Alternative: duplicate the mapping or infer from Critic verdict. Rejected because recommendation semantics and executable Action ownership would drift.

### 5. Reuse exact publications and reject ambiguity

Compute the canonical shortlist payload before writing. Query the injected Store by exact project/workflow/run/round/event type and source event IDs. Zero matches appends through the owner; one exact match reuses it; any conflict or duplicate fails closed. Build the Decision from that exact shortlist and apply the existing source-validating Decision writer, whose canonical identity reuse remains authoritative.

This sequential boundary is crash-safe: a persisted shortlist is diagnostic/formal source Evidence but does not let Planner proceed until the Decision exists. No transaction or rollback layer is added.

## Risks / Trade-offs

- [Crash after shortlist append] → Resume performs exact canonical reuse and continues to Decision publication.
- [Prediction threshold artifact adjacent to the validated handoff is removed after owner completion] → Fail closed before new publication; do not reconstruct it from State.
- [Critic does not request iteration] → Skip Decision publication and preserve the existing non-iterate Planner path.
- [Multiple matching formal rows exist] → Report ambiguity; never select latest or write another row.

## Migration Plan

Deploy only on the PR87 E3 final-integration branch and validate with isolated tests. Old runs and Stores are not migrated. A fresh n=2 Launcher run exercises Round1 Prediction → Critic → publication → Planner → Round2. Rollback reverts the workflow edge and optional writer injection; existing E2 Evidence remains valid and immutable.
