## Why

The repository's current "Quick Start" installs only the base Python dependencies, while a real NovaPeptide run also depends on approved coordinates, project-scoped storage, GPU runtimes, pinned scientific repositories, models, checkpoints, and licensed software. The recently completed real Launcher smoke gives us a verified deployment to turn into an auditable operator contract instead of leaving new users to infer a server setup from scattered or historical documents.

## What Changes

- Refocus `README.md` on a 15-minute project orientation and the exact commands for running an environment that is already provisioned.
- Add `docs/INSTALLATION.md` for bootstrapping a new GPU machine from zero, including required environments, configuration selectors, licensed-package boundaries, verification, and handoff to the supported Launcher command.
- Add `THIRD_PARTY.md` as the auditable inventory of required, conditional, and development-only external projects, distributions, model parameters, checkpoints, pinned versions or commits, upstream sources, citation guidance, and license/terms status.
- Add the read-only `python -m workflow doctor --project <approved-project.json>` command. It derives expected identities from the same project, protocol, environment configuration, and scientific runtime validators used by production; reports each required, conditional, or skipped check; prints `READY` only when launch prerequisites are satisfied; and exits non-zero otherwise.
- Correct current operator documentation that still presents base dependency installation or historical validation records as sufficient guidance for launching the present workflow.
- Do not install or download tools, mutate project/runtime state, start or resume workflows, weaken production validation, or change any scientific protocol.

## Capabilities

### New Capabilities

- `workflow/runtime-readiness-doctor`: Defines a project-scoped, fail-closed, read-only preflight for approved configuration, coordinates, storage, scientific tool identities, GPU visibility, writable roots, and conditionally required Research credentials.

### Modified Capabilities

None.

## Impact

- Public interface: one additive `doctor` subcommand under `python -m workflow`; existing `launch`, `status`, and `resume` behavior remains unchanged.
- Documentation: targeted revisions to `README.md` and current operator guidance, plus new `docs/INSTALLATION.md` and `THIRD_PARTY.md`; historical validation records remain explicitly historical rather than being rewritten as current authority.
- Code: a small workflow-owned doctor service/result projection and CLI dispatch, reusing existing project approval, coordinate integrity, execution configuration, protocol identity, and scientific runtime validators.
- Data and migration: no Store schema, Evidence, artifact, project configuration, protocol, or migration changes. Doctor is read-only and does not create formal state.
- Dependencies: no new scientific dependency is introduced; the third-party manifest documents the identities already required by production.
