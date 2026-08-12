## 1. Versioned CalibrationBaseline contract

- [x] 1.1 Add focused red tests for explicit `simulation_only`/`approved_real` authority, synthetic provenance rejection under approved-real authority, canonical digest traceability, deterministic publication identity, and unchanged calibrator rules.
- [x] 1.2 Implement the small public baseline contract that builds/validates deterministic scientific and publication bindings while reusing the existing Prediction protocol and integrity helpers.
- [x] 1.3 Build the deterministic simulation fixture through the existing calibrator and prove that algorithmic `calibrated` status remains machine-distinct from `calibration_authority=simulation_only`.

## 2. Atomic SQLite publication

- [x] 2.1 Add one additive calibration-publication operation to the public Store contract and the narrow data-layer delegation.
- [x] 2.2 Implement one SQLite transaction that enforces natural-ID consistency, registers the deterministic artifact/digest, replaces thresholds plus active binding, and appends formal publication Evidence atomically.
- [x] 2.3 Add regressions for successful publication, exact replay idempotency, same-ID different-content failure, injected partial-write rollback, prior-authority preservation, artifact lookup, and `state.json` projection independence.

## 3. Prediction consumption proof

- [x] 3.1 Validate the Store-owned binding before Prediction construction against publication ID, project approval, active protocol/scoring identity, threshold snapshot, artifact row/content, and calibration authority.
- [x] 3.2 Propagate the validated binding unchanged into cache identity, run manifest/summary/handoff, candidate record/metadata, and formal scoring/record Evidence.
- [x] 3.3 Add the full simulation controls → existing calibrator → simulation artifact → atomic Store → Prediction acceptance test plus dataset/protocol/threshold/artifact tamper and simulation-to-approved-real mismatch regressions.

## 4. Documentation and merge gates

- [x] 4.1 Update calibration documentation with the two-axis status/authority model, deterministic publication/idempotency contract, SQLite authority, complete simulation trace, and unchanged real MDM provenance-only limitation.
- [x] 4.2 Run focused calibration-baseline, Store, Prediction protocol/pipeline/effects, transaction, and projection tests and record exact results.
- [x] 4.3 Run the full Python suite, configured lint/type checks, Architecture Gate, strict OpenSpec validate/verify, and `git diff --check`; confirm no UI, Launcher, Planner, Execution, Research workflow, Store schema, project config, control asset, E2+, or unrelated changes.
- [x] 4.4 Run fixed-point Spec, Standards, and Strict code review; resolve in-scope findings and require the repository merge score of at least 85 before reporting merge-ready.

## 5. PR #72 review remediation

- [x] 5.1 Add calibrator audit input identities and regression proving Dataset B cannot publish with Audit/Threshold A.
- [x] 5.2 Enforce Prediction-owned protocol/scoring identities and reject approved-real publication without an externally approved scored-dataset digest.
- [x] 5.3 Restrict idempotency to complete current-active replay; reject A → B → replay A and incomplete-authority replay without mutation.
- [x] 5.4 Re-run the simulation-only lifecycle and existing rollback/artifact/threshold tamper regressions.
- [x] 5.5 Run focused/full tests, Architecture Gate, strict OpenSpec, diff check, and fixed-point review; require P1=0 before merge-ready.

## 6. PR #72 latest authority-seam remediation

- [x] 6.1 Make approved-real unavailable throughout E1 and add post-approval review-injection regressions.
- [x] 6.2 Bind scored dataset and audit to the Prediction-owned scoring implementation; enforce approved project status and target subset.
- [x] 6.3 Introduce a validated formal binding value and reject direct Pipeline calibrated claims from missing or plain-dict authority before writes.
- [x] 6.4 Validate publication Evidence payload equality on idempotent replay and preserve active State on corruption.
- [x] 6.5 Re-run lifecycle/tamper/rollback regressions, focused/full suites, Architecture Gate, strict OpenSpec, diff check, and fixed-point reviews; require P0=0 and P1=0.
