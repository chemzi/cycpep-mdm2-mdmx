## 1. Characterization and Focused Regressions

- [x] 1.1 Add adapter regressions proving the child probe uses the configured executable's sibling Python, imports `prodigy_prot`, reads `prodigy-prot==2.4.0`, and does not require a `prodigy` module.
- [x] 1.2 Add fail-closed adapter regressions for a `2.3.x` distribution and for a nonzero import/metadata probe whose bounded stderr identifies the failure.
- [x] 1.3 Add a Worker preflight regression that keeps the real PRODIGY validator, passes the supported package layout, and proves execution reaches the next existing scientific process seam.

## 2. Production Validator Repair

- [x] 2.1 Update only `validate_prodigy_runtime()` to run the supported import and metadata probe, retain exact version enforcement, and preserve bounded stderr for typed probe failures.
- [x] 2.2 Confirm no public signature, readiness rule, protocol/identity field, persistence schema, transaction behavior, retry behavior, or prohibited component changes are introduced.

## 3. Verification and Review

- [x] 3.1 Run the focused PRODIGY adapter and Worker preflight tests.
- [x] 3.2 Run the full unittest suite and any repository lint/type checks applicable to the changed Python files.
- [x] 3.3 Run `python scripts/architecture_gate.py` and confirm it passes without baseline expansion.
- [x] 3.4 Run `openspec validate fix-production-prodigy-runtime-validator --strict` and `git diff --check`.
- [x] 3.5 Perform strict Standards and Spec code review; stop only when P0/P1 findings are zero.
