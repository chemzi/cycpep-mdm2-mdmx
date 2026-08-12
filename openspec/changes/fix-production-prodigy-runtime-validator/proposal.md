## Why

A real approved Launcher execution proved that production Prediction rejects the supported `prodigy-prot==2.4.0` deployment before science starts because its runtime validator imports the nonexistent `prodigy` module instead of the distribution's `prodigy_prot` module. The validator must recognize the formal package layout while continuing to fail closed on missing, broken, or mismatched runtimes.

## What Changes

- Validate PRODIGY through the configured executable's sibling Python by importing `prodigy_prot` and reading `importlib.metadata.version("prodigy-prot")`.
- Require an exact match with the protocol-owned expected version (`2.4.0` for the active production identity).
- Preserve fail-closed behavior for import or metadata probe failures and include bounded stderr in the typed error; preserve fail-closed version mismatch behavior.
- Add focused regressions for the supported package layout, absence of the fictional `prodigy` module, version mismatch, import failure despite matching metadata, and Worker preflight reaching the next scientific seam.
- Do not change Launcher, Planner, Critic, Prediction readiness, scientific protocol, execution identity, Store schema, transactions, or retry behavior.

## Capabilities

### New Capabilities

- `execution/prediction-runtime-validation`: Defines production validation of the supported PRODIGY Python distribution before Prediction science begins.

### Modified Capabilities

None.

## Impact

- Affected production code: `prediction_pipeline/adapters.py::validate_prodigy_runtime()` only.
- Affected tests: focused Prediction execution-identity/runtime and Worker preflight coverage.
- Public interfaces: no signature or caller change.
- Data formats and Store schema: no change.
- Scientific protocol and execution identity: no change; the validator enforces the existing expected version.
- Migration: none. Existing failed attempts, approvals, transactions, and retry contracts remain immutable and unchanged.
- Legacy behavior retained: unsupported or broken PRODIGY installations continue to be rejected before scientific execution.
