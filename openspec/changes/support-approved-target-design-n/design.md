## Context

See `proposal.md` for motivation and `specs/workflow/initial-design-proposal-budget/spec.md` for observable behavior. The current shared Design merge path already resolves approved target identity and `design.lengths`, but proposal count is resolved without the selected approved target and therefore falls back to `100` when Launcher calls `materialize_initial_jobs()` without transient overrides. The materializer already copies merged `n`, lengths, and seed into the immutable Initial Design job receipt and later executes exactly those recorded controls.

## Goals / Non-Goals

**Goals:**

- Make approved target `design.n` available through the existing shared Design configuration resolution path.
- Apply one strict, testable precedence and positive-integer validation rule before scientific work.
- Preserve the existing job receipt and execution flow so materialization remains the authority for the controls later executed.

**Non-Goals:**

- Adding a Launcher-only parameter, alternate receipt, or second budget resolver.
- Changing any versioned Design protocol value or scientific expansion/filtering behavior.
- Changing multi-target job selection, Prediction scope/readiness, or PR73 bootstrap orchestration.
- Migrating existing approved project files or historical receipts.

## Decisions

### Pass the selected approved target into the existing proposal-count resolver

The shared merge function will provide the already resolved approved target to proposal-count resolution, matching the existing lengths resolver. This keeps ownership in Design and lets Launcher continue calling `design.merge_config()` without an override.

Alternative considered: read `project_config` or `target.design.n` inside `materialize_initial_jobs()`. Rejected because it duplicates precedence and validation at the Launcher boundary and can make the receipt disagree with later Design execution.

### Validate the selected value without coercion

The resolver will choose the first non-null source by precedence and then require `type(value) is int` (or an equivalent check that explicitly rejects booleans) and `value >= 1`. It will not convert strings or floats with `int()`. This implements the configuration contract directly and prevents silent truncation or acceptance of ambiguous values.

Alternative considered: retain the current `int(value)` coercion. Rejected because it accepts values such as `"3"`, `True`, and `3.7`, contrary to the required integer contract.

Only the selected highest-precedence value is validated. A valid explicit override therefore remains authoritative even if a lower-precedence field is present; lower-precedence configuration is not consulted after resolution.

### Reuse the existing immutable job field

No receipt schema changes are needed. `materialize_initial_jobs()` already writes merged `config["n"]` into job `config.n`, and `run_initial()` passes the recorded value back as explicit `design_config.n` during execution. Tests will prove the approved value reaches this existing seam.

Alternative considered: add approved-budget provenance or an additional Launcher field. Rejected because the requested contract is the resolved proposal count, and new receipt data would broaden format and recovery compatibility scope.

## Risks / Trade-offs

- [Previously tolerated string or fractional overrides become invalid] → Add focused negative tests for every accepted input boundary and document that the contract requires an actual integer; this is intentional validation tightening for `n`, not a general config rewrite.
- [Precedence could diverge between direct Design and Launcher] → Exercise both shared merge resolution and real `materialize_initial_jobs()` in regression tests; do not add a second Launcher resolver.
- [Changing resolver inputs could affect existing callers] → Keep the change private to `agents.design.service`, search all call sites, and preserve the public `Design.merge_config()` signature and returned dictionary shape.

## Migration Plan

No data migration is required. Deploy the shared resolver and regression tests together. Existing approved targets without `design.n` continue resolving to `100`, and historical job receipts remain valid. Rollback restores the previous resolver; approved `design.n` then becomes ignored again without changing stored formats.
