## Why

Launcher Initial Design currently ignores the proposal budget already carried by an approved target at `design.n`: `_resolve_proposal_count()` only considers transient call-time overrides and otherwise fixes every materialized job at the legacy default of 100. Approved projects therefore cannot durably select a smaller or larger Initial Design proposal count without an out-of-band Launcher override.

## What Changes

- Allow an approved target's optional `design.n` to supply the Initial Design proposal count.
- Resolve proposal count with one precedence rule: explicit `design_config.n`, then `target_spec.n`, then approved `target.design.n`, then the legacy default `100`.
- Require every selected proposal count to be an integer greater than or equal to one and reject invalid values before scientific execution.
- Keep `materialize_initial_jobs()` recording the resolved `n` in the existing immutable Initial Design job receipt; do not add a Launcher-specific override or receipt field.
- Preserve exactly equivalent behavior for approved projects that omit `design.n`.
- Leave Design protocol parameters, RFdiffusion, LigandMPNN sequence expansion, cheap filtering, Prediction scope, and the PR73 bootstrap contract unchanged.

## Capabilities

### New Capabilities

- `workflow/initial-design-proposal-budget`: Defines approved-project proposal-budget resolution, validation, compatibility, and immutable Launcher job recording for Initial Design.

### Modified Capabilities

None.

## Impact

- Affected implementation: `agents/design/service.py` proposal-count resolution and the existing Initial Design materialization path that consumes its merged configuration.
- Affected tests: Design configuration precedence/validation and Launcher Initial Design job materialization.
- Public interface impact: no function signature changes; approved target configuration gains an optional `design.n` input.
- Data-format impact: no new receipt fields or migration. Existing receipts continue recording the resolved integer `n`; legacy approved projects still materialize `n=100`.
- Dependencies and scientific protocols: unchanged.
