## Context

See `proposal.md` for motivation. The production validator in `prediction_pipeline/adapters.py` already owns the configured-executable-to-sibling-Python boundary and returns the observed version to `execution.handlers._observe_prediction_runtime()`. Current tests characterize exact version matching but mock the subprocess result, so they do not catch that the inline probe imports `prodigy` while the supported `prodigy-prot==2.4.0` distribution exposes `prodigy_prot`.

The existing Worker order, typed `ContractError` translation, execution-identity comparison, and transaction failure behavior are correct and remain authoritative.

## Goals / Non-Goals

**Goals:**

- Make the existing validator inspect the supported production package layout through the configured executable's sibling Python.
- Keep exact version enforcement and fail-closed behavior.
- Make unsuccessful probes diagnostically useful with bounded stderr.
- Add a Worker-level regression proving successful validation reaches the next existing scientific seam.

**Non-Goals:**

- No changes to tool discovery, Launcher, Planner, Critic, readiness, protocol values, execution identity shape, Store, transaction, or retry ownership.
- No generic runtime-outcome framework or alternate PRODIGY implementation.
- No compatibility path for unsupported package names or versions.

## Decisions

### Keep validation in the existing adapter seam

`validate_prodigy_runtime(executable, expected_version)` remains the public seam and keeps its signature. It will continue deriving `entrypoint.parent / "python"`, preserving deployment ownership and preventing tasks from selecting a different interpreter.

Alternative considered: move version discovery into Execution configuration. Rejected because configuration owns trusted paths, while the Prediction adapter already owns scientific runtime validation and typed tool failures.

### Use one child probe for import and exact distribution metadata

The child command will import `prodigy_prot` and then print `importlib.metadata.version("prodigy-prot")`. The parent will accept only exit code zero and stdout exactly equal to `expected_version` after surrounding whitespace is removed.

Alternative considered: parse the CLI help/banner or trust metadata without importing the module. Rejected because the CLI has no supported `--version`, and metadata alone does not prove that the scientific Python module is importable.

### Separate probe failure from successful version mismatch

A nonzero child exit represents an unusable runtime, not an observed version mismatch. It will fail through a narrow typed runtime-probe error whose message contains only the existing bounded stderr suffix. Exit zero with a different version continues to use `prodigy_version_mismatch`.

Alternative considered: retain `prodigy_version_mismatch` for both paths. Rejected because the real failure produced `found ''` and hid the actionable import error. No broader error taxonomy is introduced.

### Test the command contract and Worker continuation

Focused adapter tests will inspect the actual child command and simulate supported, mismatched, and import-failure results. Worker coverage will leave the PRODIGY validator real while mocking only the other expensive runtime observers and the next scientific process seam, proving the supported layout is no longer stopped at preflight.

## Risks / Trade-offs

- [Inline probe quoting can regress] → Assert the probe imports `prodigy_prot`, queries the exact distribution name, and contains no `import prodigy` statement.
- [Diagnostic stderr could become unbounded] → Reuse a small fixed suffix consistent with existing subprocess-error patterns.
- [Worker regression could accidentally bypass the validator] → Keep the real
  PRODIGY validator in the Worker path, assert its child-command contract in the
  adapter regression, and mock only the subprocess result plus unrelated expensive
  runtime observers before stopping at the next scientific process seam.

## Migration Plan

Deploy the code fix normally; no data or plan migration is required. Existing failed attempts and transactions remain terminal. After deployment, operators use the existing bootstrap retry-plan contract and obtain a new plan-bound approval. Rollback restores the previous validator code but does not alter any persisted workflow history.
