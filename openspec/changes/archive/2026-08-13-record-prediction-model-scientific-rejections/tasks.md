## 1. Red regressions and contract

- [x] 1.1 Add a real mixed-cohort regression using the observed 1.295/2.570/1.615 A geometry pattern; prove current code fails at the model-level precondition.
- [x] 1.2 Add artifact-loader regressions for XOR exact coverage, bound identity, duplicate/missing/overlap rejection, and all-rejected coverage.
- [x] 1.3 Add Worker regression proving an unexpected later tool failure remains task-fatal and publishes no formal Candidate or Evidence effects.

## 2. Typed rejection publication

- [x] 2.1 Add the concrete `rosetta_rejections` artifact entry and bump the Prediction artifact/protocol compatibility identity without changing scientific thresholds or tool parameters.
- [x] 2.2 Convert only `rosetta_cyclic_bond_open` at the enrichment call site into a bound typed rejection; keep every other Rosetta/runtime failure task-fatal.
- [x] 2.3 Validate `rosetta_outputs XOR rosetta_rejections` exact-once coverage before bundle publication and reuse, and include rejections in inventory/provenance.

## 3. Evaluation semantics

- [x] 3.1 Build one Rosetta-eligible model cohort for canonical L3 PRODIGY, SC, and dSASA aggregation while retaining rejected-model PRODIGY as diagnostic provenance.
- [x] 3.2 Make any typed rejection explicitly fail L3 and produce `needs_optimization` with L3 in `failed_layers`, including the all-rejected/no-numeric-aggregate case.
- [x] 3.3 Add ingest/transaction regressions proving complete mixed negative evidence commits atomically and remains consumable by the existing Critic readiness contract without readiness changes.

## 4. Verification and deployment

- [x] 4.1 Run focused Prediction/enrichment/Worker tests, the full suite, Architecture Gate, strict OpenSpec validation, and `git diff --check`.
- [x] 4.2 Obtain independent high-reasoning Spec and Standards reviews and resolve all P0/P1 findings without scope expansion.
- [x] 4.3 Archive the change, commit/push, create and merge the PR with `gh`, deploy the merged integration commit, and start a fresh n=2 full-auto Launcher smoke without modifying or retrying the failed invocation.
