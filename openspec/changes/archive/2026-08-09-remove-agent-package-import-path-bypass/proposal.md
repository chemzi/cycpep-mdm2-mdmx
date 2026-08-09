## Why

The `agents.critic`, `agents.planner`, and `agents.orchestrator` package initializers contain process-wide Python import search-path bootstraps. Fresh-process characterization additionally proves that their shared `prediction_pipeline.protocol` dependency performs the same mutation transitively, so removing only the initializer blocks does not satisfy the public Agent import contract.

## What Changes

- Remove repository-root import-path mutation from the three affected Agent package initializers while preserving their existing public re-exports.
- Remove only the equivalent repository-root import-path bootstrap from `prediction_pipeline/protocol.py`, preserving its protocol path resolution and public names.
- Add a focused Architecture Gate rule that rejects import search-path mutation from package initializer files.
- Add characterization tests proving that supported Agent and Prediction protocol imports still work and do not change the caller's import search path.
- Update Architecture Gate documentation to describe the new rule.
- Keep root-level legacy CLI shims and explicit external-tool path setup unchanged; they are outside this change.

There is no intended change to scientific algorithms, workflow behavior, public Python imports, command-line behavior, persistence, transaction semantics, or data formats. No migration is required.

## Capabilities

### New Capabilities

- `engineering/package-import-integrity`: Defines side-effect-free Agent package initialization and CI enforcement against package-level import-path bypasses.

### Modified Capabilities

None. The repository has no existing OpenSpec capabilities to modify.

## Impact

- Affected implementation: `agents/critic/__init__.py`, `agents/planner/__init__.py`, `agents/orchestrator/__init__.py`, and the equivalent bootstrap in `prediction_pipeline/protocol.py`.
- Affected engineering tooling: `scripts/architecture_gate.py`, `test_architecture_gate.py`, and Architecture Gate documentation.
- Public interfaces: unchanged; existing imports and re-exported names remain supported.
- Dependencies and packaging: unchanged; this change does not introduce package installation or a `src/` layout.
- Remaining legacy paths: root-level Agent CLI shims may still bootstrap the repository root, and scientific-tool adapters may still configure isolated external-tool import paths where required.

## Non-goals

- Removing or redesigning legacy CLI entrypoints.
- Eliminating every import-path adjustment in scripts, workers, web startup, or external scientific-tool adapters.
- Reorganizing Agent packages or changing their public exports.
- Changing Action Contract, persistence, transaction, ProjectContext, scientific protocol contents or identity, or scientific behavior.
