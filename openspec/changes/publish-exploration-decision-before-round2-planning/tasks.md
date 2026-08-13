## 1. Lock the Production Publication Contract

- [x] 1.1 Add focused red regressions proving both fresh and completed-Critic resume paths carry the same owner-validated Prediction boundary into publication before Planner, and a non-iterate Critic does not invent a Decision requirement.
- [x] 1.2 Add resume regressions for exact shortlist reuse after an interrupted publication and exact Decision reuse after complete publication; duplicate/conflicting current events must fail closed.
- [x] 1.3 Add source-authority regressions for injected Store project mismatch, ambient Store divergence, stale approved project revision, missing/invalid threshold artifact adjacent to the validated handoff, incomplete battery coverage, and mismatched workflow/run/round/target bindings; assert Planner is not invoked and no replacement Decision is written.

## 2. Implement the Narrow Publication Edge

- [x] 2.1 Thread an optional explicit project-scoped Store through the existing shortlist writer, Decision builder formal-handoff/source validation, and Decision writer while preserving legacy omission behavior/event formats and documenting that the injected SQLite path is authoritative without a new JSONL projection promise.
- [x] 2.2 Extract E3-C's current recommendation-to-ActionType predicate as one public read-only classifier shared with a workflow publication module; resolve current formal inputs, read thresholds from the owner-validated `handoff_path` locator, compute with existing E2 builders, and perform exact canonical shortlist/Decision inspect-or-publish behavior.
- [x] 2.3 Insert publication in the unified `_resolve_planner` path, pass the current owner-validated Prediction boundary through fresh and completed-Critic resume calls, keep E3-C as the only Store-to-Planner Decision selector, and never call the direct Prediction inspector as a bootstrap substitute.
- [x] 2.4 Preserve Launcher resume/diagnostic behavior and old invocation immutability; do not add a transaction, retry policy, cache, sidecar, action, or scientific-policy table.

## 3. Verify and Integrate

- [x] 3.1 Run focused publication, E2, E3-A/B/C, workflow service, PR83 budget, PR85 publication binding, and PR86 project-authority tests.
- [x] 3.2 Run the full unittest suite, Architecture Gate, strict OpenSpec validation for this change and all three E3 changes, compile checks, and `git diff --check`.
- [x] 3.3 Run independent high-reasoning Spec and Standards reviews; resolve all P0/P1 findings and rerun affected gates.
- [ ] 3.4 Commit and push the verified change on top of PR87, update PR87 with evidence, and merge with `gh` only after both high-reasoning reviews report P0/P1=0.
- [ ] 3.5 Deploy the exact verified branch commit to an isolated server checkout and run a fresh approved `design.n=2` full Launcher flow; use only formal approval requests and leave every old failed invocation immutable.
