## 1. Contract and current-scope characterization

- [x] 1.1 Add focused characterization tests proving a scoped `0/N passed` shortlist is still produced and retains `passed=false`, and proving the existing conservative policy is five samples / 70-percent worst / 30-percent better.
- [x] 1.2 Implement the frozen, versioned `ExplorationDecision` contract with strict field, round, allocation-envelope, provenance, status, and round-trip validation.

## 2. Deterministic constrained Decision builder

- [x] 2.1 Implement approved per-target length-envelope normalization, multi-target intersection, deterministic relative `baseline_policy_weights`, and fail-closed project/candidate length validation without representing actual proposal counts.
- [x] 2.2 Implement explicit current prediction-run, workflow trace, target, protocol, source-event, and shortlist-round/source validation; require exactly one battery verdict per bound handoff candidate and exact battery/handoff candidate-ID set equality, with missing/extra/duplicate failure and no full-history decision query.
- [x] 2.3 Reuse the existing failure aggregation and 5/70/30 policy over normalized scoped events to produce either a length-only adjustment or an auditable `no_adjustment`, including support statistics and deterministic reason.
- [x] 2.4 Build canonical policy, threshold, and complete semantic input digests with the existing repository utility, then derive a stable `decision_id` independent of time, randomness, event append ID, and input order.

## 3. Formal Evidence authority

- [x] 3.1 Add the additive `exploration_decision` event contract and a narrow Store-backed writer that verifies every source/shortlist event, sequentially reuses an existing identical `decision_id` event, rejects same-ID/different-payload collisions, and otherwise appends the complete Decision through the canonical Evidence envelope.
- [x] 3.2 Prove failed validation or Store append produces no Decision Evidence, State projection, JSON authority, CandidateIndex mutation, or completion claim.

## 4. Acceptance and boundary coverage

- [x] 4.1 Add focused tests for deterministic `no_adjustment`, deterministic sufficient-evidence adjustment, `[8,10,12]` envelope containment, source mutation identity change, unrelated history stability, and round/run/candidate mismatch failure.
- [x] 4.2 Add provenance/persistence round-trip tests proving all source IDs exist, the formal Decision Evidence fully restores baseline/proposed policy weights and provenance, identical sequential retries reuse one event, and same-ID/different-payload calls fail closed.
- [x] 4.3 Add non-interaction tests proving threshold/pass/source inputs remain unchanged and Decision creation/recording does not call Design, Planner, Orchestrator, Execution, or register an executable action.

## 5. Verification and review

- [x] 5.1 Run focused E2 and relevant experience/exploration/Evidence regression tests, then the full Python test suite.
- [x] 5.2 Run the Architecture Gate, strict OpenSpec validation, and `git diff --check`; synchronize public contract/docstrings without adding a parallel specification.
- [x] 5.3 Run separate Standards and Spec code reviews, resolve all merge blockers within E2 scope, and confirm no E3/E4/E5 integration or scientific-policy change entered the diff.

## 6. PR 71 merge-ready provenance repair

- [x] 6.1 Add a shared canonical identity helper for the exact Prediction-consumed threshold snapshot and propagate it through actual Prediction battery and `prediction_handoff_ready` Evidence without changing threshold science.
- [x] 6.2 Replace caller-declared handoff candidate scope with the existing formal Prediction handoff authority; bind project/workflow/run, Prediction run, protocol, candidates, and threshold identity.
- [x] 6.3 Reject generic `exploration_decision` appends and preserve one dedicated source-validating writer with sequential idempotency.
- [x] 6.4 Include complete conservative policy identity in Decision semantic identity and add the six requested regressions.
- [x] 6.5 Run focused/full tests, Architecture Gate, strict OpenSpec validation, diff/compile/configured static gates, then independent Standards and Spec reviews; resolve all P0/P1 findings within frozen E2 scope.

## 7. PR 71 real-producer and historical-policy repair

- [x] 7.1 Emit canonical `prediction_run_id` on real fresh and cache-reconstructed transactional battery proposals; enforce exactly-one battery candidate coverage at the Execution Prediction boundary.
- [x] 7.2 Add a real Store-backed transactional Prediction-to-E2 integration test plus interrupted-commit/cache-resume recovery coverage.
- [x] 7.3 Replace current-singleton policy restoration with a frozen versioned parameter-and-algorithm registry and prove V1 restore/retry after an algorithmically distinct V2 default plus distinct V2 identity.
- [x] 7.4 Freeze battery, handoff, and shortlist owner/phase in build, semantic projections, and formal append revalidation; add wrong-owner/phase and post-build tamper regressions.
- [x] 7.5 Preserve raw Prediction threshold evaluation behavior while computing provenance identity separately; add alias/duplicate-key scientific pass/fail characterization.
- [x] 7.6 Run focused/full suites, Architecture Gate, strict OpenSpec, diff/compile/static gates, and independent Standards/Spec reviews; require P0=0 and P1=0.
