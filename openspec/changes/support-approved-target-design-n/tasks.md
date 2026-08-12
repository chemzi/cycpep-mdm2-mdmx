## 1. Proposal-count contract tests

- [x] 1.1 Add focused shared-Design regressions proving absent approved `design.n` resolves to `100`, approved `design.n=3` resolves to `3`, and explicit `design_config.n` plus `target_spec.n` follow the specified precedence.
- [x] 1.2 Add focused validation regressions proving `0`, booleans, strings, fractional values, and other non-integers are rejected before scientific execution.
- [x] 1.3 Add a real Launcher Initial Design materialization regression proving an approved target with `design.n=3` records `config.n=3` in the existing immutable job receipt while legacy receipt fields remain unchanged.

## 2. Shared Design implementation

- [x] 2.1 Update the existing shared proposal-count resolver to consume the selected approved target, apply explicit-config → target-spec → approved-target → legacy-default precedence, and strictly validate a positive integer.
- [x] 2.2 Keep `materialize_initial_jobs()` on the shared merge path and confirm no Launcher override, receipt-field change, protocol edit, scientific-parameter change, Prediction change, or bootstrap-contract change is introduced.

## 3. Verification and review

- [x] 3.1 Run the focused Design and Launcher Initial Design test suites and resolve only failures caused by this change.
- [x] 3.2 Run the full Python test suite and applicable compile/lint/type checks exposed by the repository.
- [x] 3.3 Run `scripts/architecture_gate.py`, `openspec validate support-approved-target-design-n --strict`, and `git diff --check`.
- [x] 3.4 Perform strict Spec and Engineering Standards code review against latest `integration/data-integrity-transaction`; stop when P0/P1 findings are zero without expanding scope.
