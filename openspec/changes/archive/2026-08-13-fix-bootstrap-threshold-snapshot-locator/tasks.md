## 1. Lock the Real Failure

- [x] 1.1 Add a transaction regression proving successful bootstrap Prediction commits the threshold snapshot as an additional Artifact in the same transaction while the action output inventory remains exactly `prediction_handoff`; Evidence binds only its Artifact ID and canonical digest, while the Store Artifact owns path/file SHA-256.
- [x] 1.2 Add negative regressions proving missing/malformed threshold JSON, canonical digest mismatch, file-byte SHA mismatch, or failed commit/recovery leaves no authoritative threshold Artifact or handoff Evidence.
- [x] 1.3 Add a public bootstrap readiness → E3 publication regression using a promoted handoff whose threshold snapshot is not adjacent and transactional handoff Evidence containing Artifact IDs but no `handoff_path`; current code must reproduce `thresholds_invalid`.

## 2. Implement the Narrow Locator Contract

- [x] 2.1 Revise the strict internal Prediction effects producer/consumer contract to propose the threshold snapshot as an additional Artifact, preserve the sole `prediction_handoff` output role, and validate canonical threshold digest separately from Artifact byte SHA-256.
- [x] 2.2 Bind only `thresholds_artifact_id` plus the existing canonical digest in formal `prediction_handoff_ready` Evidence; resolve the Store-owned path/SHA through typed lookup at bootstrap readiness, project the validated locator into `FormalBoundary`, select transactional handoff Evidence by named handoff Artifact ID, and preserve direct Prediction behavior.
- [x] 2.3 Make E3 publication consume the explicit owner-validated threshold locator and preserve fail-closed behavior with no staging/State fallback.
- [x] 2.4 Keep transaction, retry, Prediction scientific protocol/readiness, Planner policy, Store schema, and old invocations unchanged.

- [x] 2.5 Lock compatibility regressions: bootstrap missing a threshold Artifact locator must not fall back to adjacency, while non-transaction direct Prediction keeps its existing adjacent-snapshot path.

## 3. Verify, Review, and Redeploy

- [x] 3.1 Run focused Prediction transaction/publication/bootstrap/E3 regressions and the original production-shaped repro.
- [x] 3.2 Run the full unittest suite, Architecture Gate, strict OpenSpec, compile checks, and `git diff --check`.
- [x] 3.3 Run independent high-reasoning Spec and Standards reviews; resolve all P0/P1 findings.
- [ ] 3.4 Commit/push, create and merge the narrow PR with `gh` after P0/P1=0, then deploy a fresh exact merged-tree `design.n=2` run and continue full-auto monitoring.
