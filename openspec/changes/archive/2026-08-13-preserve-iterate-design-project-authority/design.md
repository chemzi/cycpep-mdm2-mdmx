## Context

See `proposal.md` for the production failure. In the real Launcher path, `HandlerContext.project_config` is `None`; `execution.handlers.iterate_design()` resolves the approved project from Store-backed `State`, validates `object_sha256(project)` against `project_config_digest`, and owns the multi-job transaction staging. It then launches `agents/design.py` without project context. `agents.design.cli.main()` consequently constructs `Design()` from its legacy default context, while Design-adjacent Candidate ID, Store, and Evidence helpers also resolve their process-scoped project from the ambient selector. The child can therefore observe a different project than the parent even though the parent digest check passed.

The existing public context contract is `core.context.ProjectContext`; the Design facade already accepts it. The existing tests in `test_data_integrity_transactions.py` exercise the real handler/Worker staging seam, while `test_execution.py` exercises the public Design CLI parsing seam.

## Goals / Non-Goals

**Goals:**

- Carry the exact already-validated Store-backed project object into every child job explicitly and through the required legacy process-scoped projection.
- Keep one project authority: the in-memory approved project resolved by Execution.
- Preserve multi-job transaction atomicity and legacy CLI behavior.

**Non-Goals:**

- Changing Planner iteration policy, task scope, proposal counts, approval budgets, retry, scientific algorithms, Prediction, Critic, Launcher, or Store schemas.
- Re-materializing coordinates or mutating the approved project.
- Introducing a generic subprocess-context framework or new integrity scheme.

## Decisions

### Materialize one invocation-local project snapshot after the existing digest gate

Execution will write the resolved project object once under the attempt directory, after the existing `project_config_digest` comparison succeeds, and pass that path to every Design job. The same path will be supplied as the child process's `CYCPEP_PROJECT_CONFIG` value because existing Candidate ID, Store, and Evidence helpers still use that documented compatibility selector. The file is an input snapshot and is not registered as a formal Store artifact or independent authority; argv and environment are two projections of that one object.

This is preferred over passing the original project locator because the production handler resolves its authority from formal State, and that authority may not be the same physical file as the user's original input. It is preferred over embedding JSON in argv because paths avoid command-length and quoting hazards.

No new digest field is introduced. The task already binds `project_config_digest`, the parent validates that binding, and the snapshot is written from the validated object immediately before child launch.

### Extend the Design CLI with an additive explicit project input

`agents.design.cli` will accept `--project-config`. When present it will load a `ProjectContext` through the existing public contract and construct `Design(context)`. When absent it will continue constructing `Design()` exactly as today.

The explicit argument remains the Design scientific-context contract. The process environment points to the same snapshot only as a compatibility projection for existing data-layer helpers; it is not allowed to select a different file. This is preferred over environment-only propagation because the scientific context stays explicit, and over importing Design internals into Execution because Agent boundaries permit only public contracts.

### Reuse the existing transaction and CandidateUpdate seams

The correction changes only the child input. CandidateUpdate staging, result validation, Worker commit, and rollback remain owned by the existing Execution transaction. No second transaction or publication path is added.

## Risks / Trade-offs

- [The snapshot can be confused with formal authority] → Keep it under the attempt input area, do not register it in Store/artifact inventory, and document it as non-authoritative.
- [CLI behavior could change for direct users] → Make the option additive and test omission preserves the legacy `Design()` construction path.
- [Only the first job or only one project-consumer receives the corrected input] → Assert the same snapshot path in argv and child environment for every job, with a deliberately conflicting ambient default.
- [A later job can fail after the first emits an update] → Exercise the real Worker transaction seam and prove no formal Candidate/Evidence publication.

## Migration Plan

1. Deploy the additive CLI and handler handoff together.
2. Do not resume the failed production invocation; it remains immutable and non-retryable.
3. Run a fresh minimal-budget Launcher from the verified feature commit.
4. Rollback is a normal commit revert; no persisted schema or data migration is involved.
