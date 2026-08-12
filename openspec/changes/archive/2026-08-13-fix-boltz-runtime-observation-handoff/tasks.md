## 1. Characterization and merge-blocking regressions

- [x] 1.1 Add a focused failing regression proving successful runtime observations survive real Boltz environment preparation.
- [x] 1.2 Add a focused failing regression through public `run_boltz_prediction()` proving the validated identity reaches final metadata while command checkpoint and protocol parameters stay unchanged.

## 2. Narrow production repair

- [x] 2.1 Wire the existing validator result through `prediction_pipeline/boltz_worker.py` without changing signatures, schemas, or scientific policy.
- [x] 2.2 Add a compact enrichment-boundary regression that proves a successful Boltz result reaches the next existing scientific seam.

## 3. Verification

- [x] 3.1 Run the focused Boltz handoff regressions and applicable Prediction regression suites.
- [x] 3.2 Run the full unittest suite, Architecture Gate, strict OpenSpec validation, Python compilation, and `git diff --check`.
- [x] 3.3 Perform strict Spec and Engineering Standards review; stop when P0/P1 are zero and report any remaining out-of-scope risks separately.
