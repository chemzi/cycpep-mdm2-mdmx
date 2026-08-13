## Context

The current Rosetta adapter correctly rejects a complex prediction whose head-to-tail C--N distance exceeds 2.0 A. Enrichment currently requires one Rosetta output per complex prediction and aborts on that scientific precondition, so one scientifically invalid model is misreported as an execution-process failure. The failed production attempt stopped in staging and committed no Prediction effects; transaction ownership is already correct.

## Goals / Non-Goals

**Goals:**

- Represent one specific pre-subprocess scientific geometry failure as bound negative model evidence.
- Preserve exact model coverage and a common canonical L3 aggregation cohort.
- Keep batch publication atomic and make the resulting terminal-negative Candidate consumable under existing Critic readiness.

**Non-Goals:**

- Coordinate repair, topology materialization, threshold changes, model dropping, quorum reduction, or a generic outcome framework.
- Changes to Launcher, Planner, Critic, workflow sequencing, approval, budget, retry, Store schema, or transaction ownership.

## Decisions

### Use a concrete rejection collection beside Rosetta outputs

Each target gains `rosetta_rejections`. An entry reuses the prediction identity fields required by Rosetta output coverage and adds a fixed rejection code plus the observed C--N distance and existing protocol limit. The loader builds one identity map over outputs and rejections and enforces XOR exact coverage.

Alternative: catch all `ContractError` values. Rejected because tool/artifact failures must remain task-fatal.

### Keep the original prediction artifact immutable

No scoring pose is generated and no coordinate is changed. The rejected model remains in `complex_predictions`, PRODIGY diagnostics, L2/L5/L6 inputs, inventory, and provenance, but is excluded from every canonical L3 scalar aggregate because it lacks a Rosetta score.

Alternative: materialize a Rosetta-only repaired pose. Rejected because it changes the L3 scientific algorithm and makes Rosetta metrics use different coordinates from PRODIGY and other complex metrics.

### Make rejection an explicit L3 failed gate

Metric collection returns structured rejection evidence separately from missing evidence. Battery evaluation receives the explicit L3 scientific failure and records L3 in `failed_layers`; numeric aggregates are computed only when available and never filled with zero or NaN. This uses the existing `needs_optimization` terminal status and existing Critic readiness contract.

### Bump protocol and artifact compatibility identity

The artifact schema and Prediction protocol version advance together. Existing bundles remain readable only through historical/audit paths and are not executable under the new protocol. No Store migration is required.

### Preserve Worker transaction behavior

Enrichment continues staging all Candidate artifacts before Prediction ingest. Only a fully validated XOR-covered bundle proceeds. Any non-whitelisted failure or later Candidate failure leaves the transaction rolled back; no retry behavior changes.

## Risks / Trade-offs

- [Fewer models contribute numeric L3 aggregates] -> Use one shared eligible cohort for all canonical L3 scalars and force the layer to fail, so reduced sample size cannot improve clearance.
- [A rejection could be mistaken for missing evidence] -> Keep a distinct typed evidence path and regression-test all-rejected and mixed cohorts.
- [Schema compatibility changes] -> Bump protocol identity and require a fresh approved invocation; do not promote partial artifacts from the failed run.

## Migration Plan

1. Deploy the new protocol and schema with fail-closed readers.
2. Keep the failed production invocation and its staged diagnostics immutable.
3. Start a fresh n=2 Launcher invocation from the merged commit and use the formal approval/resume path.
4. Roll back by redeploying the prior commit; no formal data migration or partial artifact promotion is needed.
