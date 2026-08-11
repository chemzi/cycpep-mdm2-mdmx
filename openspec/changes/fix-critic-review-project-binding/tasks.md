## 1. Failing Regression Coverage

- [x] 1.1 Add contract tests proving `critic_persistence_effects` copies `source.project_id` and fails closed when the report source lacks a valid project ID.
- [x] 1.2 Add direct Critic persistence tests proving an unbound same-report legacy event does not suppress one new bound event, while a second rerun remains idempotent.
- [x] 1.3 Add transaction-managed handler coverage proving the staged `critic_review` event carries the shared contract's `project_id` without changing transaction effects.
- [x] 1.4 Add project-scoped boundary coverage proving a matching bound event is completed and an otherwise valid cross-project event is not authoritative.

## 2. Minimal Contract Repair

- [x] 2.1 Validate the immutable report source project ID once in `critic_persistence_effects` and include it in the shared Evidence effect without changing the function signature.
- [x] 2.2 Forward the shared `project_id` into the transaction-managed Critic event; do not modify transaction ownership, Store schema, or artifact publication.
- [x] 2.3 Tighten direct Critic idempotency to project ID, prediction run ID, report ID, and report digest so the preserved unbound event remains immutable but cannot suppress a current event.
- [x] 2.4 Update only the existing relevant contract/operator documentation to record the additive `critic_review.project_id` requirement and legacy non-authority rule.

## 3. Verification Gates

- [x] 3.1 Run the focused Critic contract, direct persistence, transactional handler, and workflow boundary regression tests.
- [x] 3.2 Run relevant Store, transaction ownership/recovery, workflow service/status/resume, and Critic correlation test modules.
- [x] 3.3 Run the full Python unittest suite and confirm no public signature or scientific behavior regression.
- [x] 3.4 Run Architecture Gate, strict OpenSpec validation, `git diff --check`, and Spec/Standards review; stop engineering work if all gates pass.

## 4. Preserved E2E Continuation

- [x] 4.1 Deploy only the verified repair to the server validation branch without modifying the preserved run directory or its existing unbound Evidence.
- [x] 4.2 Explicitly resume `launcher_d19de5b80bee453f9eb3ee2ad057126d` with its original isolated runtime locator and verify Research, Design, and Prediction are not repeated.
- [x] 4.3 Verify exactly one new project-bound Critic event is appended, read-only status can prove Critic completion, and Launcher reaches Planner or returns a Planner-owned formal outcome.
- [x] 4.4 Record that missing Prediction artifacts and threshold calibration remain independent blockers; do not implement or bypass them in this change.

## Non-Normative Preserved E2E Acceptance Record

- The hard-coded Launcher run above is acceptance evidence only, not a repository-level normative contract.
- Before resume, formal Store counts were one Research invocation, one Initial Design invocation, one Prediction invocation, and one unbound Critic review. After resume, the first three counts remained unchanged; the old unbound event remained unchanged; exactly one new project-bound Critic review was appended for the same Prediction run and report.
- Read-only Launcher status now proves Critic completion, records one Planner plan, and reports `awaiting_approval` at the approval boundary. Research, Design, and Prediction were not repeated.
- Missing complete Prediction artifacts and threshold calibration remain independent data/scientific blockers. This change did not generate, bypass, or reinterpret them.
- **Future Work / Identified Capability Gap — Automated Structure Selection:** selecting the appropriate experimental structure for a target/site remains an approved-project scientific decision. A future separately approved change should evaluate structure candidates, deterministic constraints, scientific trade-offs, evidence, provenance, and review/approval. This is not a Launcher bug and was not implemented here.
- **Future Work / Identified Capability Gap — Deterministic Structure Materialization:** converting an already selected PDB/chain into controlled, validated, provenance-bound Design input remains a pre-Design engineering capability gap. The preserved E2E used explicitly materialized curated 1YCR/3DAB inputs only to continue main-chain acceptance. A general materializer was not implemented here and requires a separate change/PR.

## 5. Merge-Blocker Contract Closure

- [x] 5.1 Add a negative inspector regression proving an event-bound project cannot authorize a report whose immutable `source.project_id` differs from the inspected project.
- [x] 5.2 Require the Critic inspector to validate immutable report-source project identity without relaxing its project-scoped query or ambiguity policy.
- [x] 5.3 Add a transaction regression proving event/Worker trace project conflict prevents commit, stores no Critic Evidence, and exposes no State mutation.
- [x] 5.4 Reuse `EvidenceEvent` trace conflict semantics during Worker formalization before commit; do not change transaction ownership or Store schema.
- [x] 5.5 Add shared effect regressions for non-empty but formally invalid `source.project_id` values.
- [x] 5.6 Validate `source.project_id` with the shared formal Trace ID validator before returning persistence effects.
- [x] 5.7 Add writer regressions proving `EvidenceLogger.critic_review()` rejects missing/invalid project binding and persists a valid binding.
- [x] 5.8 Add transactional Prediction regressions proving formal `run_id` remains the Orchestrator run, `prediction_run_id` preserves the distinct Prediction identity, formal validation succeeds, and Launcher correlation does not regress.
- [x] 5.9 Normalize only deferred Prediction Evidence identity from reserved payload `run_id` to `prediction_run_id` at the transaction adapter boundary.
- [x] 5.10 Close the legacy writer and incidental field repair, then rerun focused/full Python tests, Architecture Gate, strict OpenSpec, `git diff --check`, and final Spec/Standards review.
