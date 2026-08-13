## Why

The real Launcher reached a committed Prediction result and then failed the Critic-driven `iterate_design` task because Execution validated the approved project in its parent process but launched the Design CLI without that project authority. The child therefore loaded the bundled default config, which has no formal `structure.coordinate_path`, and incorrectly reported a materialization blocker even though the approved project already carried valid coordinate bindings.

## What Changes

- Preserve the exact Planner-bound approved project config across the Execution-to-Design subprocess boundary for every `iterate_design` job, using one snapshot as both the explicit CLI input and the legacy process-environment projection.
- Make the Design CLI accept an explicit project-config input and construct its existing `Design` facade from the resulting `ProjectContext`.
- Materialize one invocation-local, non-authoritative project snapshot only after the existing digest check, then pass that snapshot to every job in the invocation.
- Add regressions at the public Design CLI seam and the real `iterate_design` handler seam, including multi-job reuse, digest drift fail-closed behavior, and failed-invocation non-publication.
- Keep the failed production invocation immutable; validation will use a fresh Launcher run.
- Do not change Planner recommendations, proposal counts, budgets, approval semantics, retry, Candidate/Store schema, scientific protocol, Prediction, Critic, or Launcher sequencing.

## Capabilities

### New Capabilities

- `execution/iterate-design-project-authority`: Defines how an approved project authority is preserved from an executable `iterate_design` task into each Design subprocess.

### Modified Capabilities

None.

## Impact

- Production code: `execution/handlers.py` and `agents/design/cli.py` only.
- Tests: focused CLI and production-shaped Execution handler regressions with Store-backed project resolution and a deliberately different ambient default; no scientific-tool mocks presented as end-to-end evidence.
- Public interfaces: no Python API removal or signature change. The internal Design command line gains an additive `--project-config` option; legacy invocations without it keep their existing default behavior.
- Data formats and migrations: no Store, Evidence, plan, approval, task, result, or Candidate schema change; no migration.
- Legacy path: direct/legacy Design CLI invocations that omit `--project-config` continue to resolve the existing environment/default project config.
