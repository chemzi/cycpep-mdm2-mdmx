## Context

See `proposal.md` for motivation and `specs/engineering/package-import-integrity/spec.md` for observable requirements.

PR6 split Critic, Planner, and Orchestrator into packages while retaining root-level single-file CLI shims. Each affected package initializer still imports `sys` and `Path`, computes the repository root, and conditionally inserts it into `sys.path` before loading package modules. Normal imports already require the repository root (or an installed package) to be discoverable before Python can find these initializers, so the initializer cannot repair the condition needed to locate itself.

After removing those three initializer blocks, the approved fresh-process test still observed the repository root being inserted while importing `agents.critic`. A traced call stack identifies `prediction_pipeline/protocol.py` as the remaining writer. All three affected Agent packages reach it through their existing Prediction imports. The module's bootstrap is equivalent, while its `ROOT` value is also legitimately used to locate `protocols/prediction_v1.json`; only the `sys.path` mutation is in scope.

The supported public imports are exercised throughout the current CPU suite. The current baseline is 426 passing tests with 4 skips, and `scripts/architecture_gate.py` reports zero violations. The gate scans Python ASTs but has no rule for package initializer import-path mutation.

## Goals / Non-Goals

**Goals:**

- Make the three affected Agent package initializers side-effect-free with respect to the caller's import search path.
- Remove the proven transitive import-path side effect from `prediction_pipeline.protocol` without changing protocol loading or identity.
- Preserve each initializer as the existing public interface and re-export seam.
- Add a narrow, zero-baseline-debt Architecture Gate rule that prevents this bypass from returning.
- Verify supported package and CLI imports without exercising scientific tools or changing business state.

**Non-Goals:**

- General Python packaging, editable installation, or repository layout changes.
- Removal of root-level Agent CLI shims.
- Rules for standalone scripts, worker launchers, web startup, or external scientific-tool adapters.
- Expanding the Architecture Gate rule beyond package initializers without separate evidence and design.
- Changes to package exports, scientific behavior, persistence, transaction handling, or runtime project configuration.

## Decisions

### 1. Keep the public package seam and remove only its bootstrap side effect

Delete the `sys`/`Path` bootstrap block from the Critic, Planner, and Orchestrator package initializers. Leave their relative imports, public names, and `__all__` definitions unchanged.

This preserves a deep module seam: callers continue to learn the same Agent package interface, while repository-location knowledge remains outside the package implementation.

**Alternative considered:** remove the package initializers or make callers import submodules directly. Rejected because that would enlarge the caller-facing interface and create a compatibility change unrelated to this governance step.

### 2. Preserve entrypoint-owned bootstrapping

The root-level `agents/critic.py`, `agents/planner.py`, and `agents/orchestrator.py` files remain compatibility CLI adapters. They may prepare their execution environment before importing the package because they are entrypoints, not package initializers. The change will verify that their existing help command still delegates successfully but will not redesign them.

**Alternative considered:** remove all repository-root path insertion in the same change. Rejected because workers, scripts, web startup, and scientific-tool adapters have different invocation and dependency constraints; combining them would make the change broad and behavior-sensitive.

### 3. Remove the proven transitive bootstrap without changing protocol behavior

Remove `sys` and the conditional `sys.path` insertion from `prediction_pipeline/protocol.py`. Preserve `Path`, `ROOT`, protocol file selection, parsing, validation, constants, and public names unchanged.

This module is included only because the approved fresh-process characterization demonstrates that it violates the same Agent import requirement after the three initializer blocks are removed. Directly importing the protocol module in a fresh process will provide focused regression coverage in addition to the Agent-level transitive test.

**Alternative considered:** broaden the static gate to every non-entrypoint Python module. Rejected because the repository contains worker, web, script, and scientific-adapter path setup with distinct constraints; the current evidence supports this one transitive module, not a general classification policy.

### 4. Add a focused AST rule for package initializers

Extend the existing pure-standard-library Architecture Gate with one check whose scan surface is repository `__init__.py` files. The check detects direct `sys.path` mutation syntax, including mutating method calls and direct assignment/deletion forms. It will report the repository-relative file and mutation form.

The rule intentionally does not attempt data-flow analysis, alias resolution, or reflective mutation detection. The confirmed violations use direct syntax, and a simple rule is easier to review and less prone to false positives.

The rule remains limited to `__init__.py`. The `prediction_pipeline.protocol` regression is enforced through the focused fresh-process test rather than expanding the gate without an independently specified module-classification rule.

The check will join the existing `CHECK_ORDER` and baseline machinery. Because implementation removes all in-scope violations, `architecture_baseline.json` will contain an empty entry for the new check rather than accepting legacy debt.

**Alternative considered:** use text matching. Rejected because comments and strings could create false positives, while AST matching is already the gate's established convention.

### 5. Test both detection and runtime compatibility at the existing gate surface

Add focused tests to `test_architecture_gate.py`:

- fixture initializers containing direct insert/append or assignment are reported;
- equivalent code in a non-initializer entrypoint is outside this rule;
- a clean initializer is not reported;
- a fresh subprocess imports the three affected packages, checks representative public names, and confirms `sys.path` is unchanged after each import;
- a fresh subprocess imports `prediction_pipeline.protocol` directly, checks representative public names, and confirms `sys.path` is unchanged;
- each retained CLI shim's help invocation succeeds.

A subprocess is used for import characterization so module-cache state from test discovery cannot hide import-time behavior. Tests assert only supported interface behavior, not internal import order.

**Alternative considered:** inspect source text to prove the bootstrap is absent. Rejected because runtime characterization better protects the interface and side-effect contract through future internal refactors.

### 6. Documentation follows the enforced gate

Update the Architecture Gate's built-in check list and README validation section to name the new package initializer rule. Do not expand ENGINEERING_STANDARD or create a separate governance document.

## Risks / Trade-offs

- **[Risk] An undocumented caller executes a package initializer from an unsupported path and relied on the mutation.** → Preserve documented root-level CLI shims, test supported invocation forms, and treat unsupported direct execution as outside the package interface.
- **[Risk] The gate grows an overly broad import policy.** → Restrict scanning to `__init__.py` and direct `sys.path` mutation; leave other contexts for separate evidence-backed changes.
- **[Risk] Import tests pass because an earlier import changed process state.** → Run characterization in a fresh subprocess and compare the path around each import.
- **[Trade-off] Some import-path bootstraps remain.** → This is deliberate scope control; they belong to distinct entrypoint or external-adapter seams and require separate review.
- **[Risk] Removing the protocol bootstrap changes scientific protocol resolution.** → Keep `ROOT` and all protocol loading logic unchanged, then run protocol and full regression tests.

## Migration Plan

1. Add failing gate and import characterization tests for the confirmed initializer behavior.
2. Remove the three initializer bootstrap blocks without altering public re-exports.
3. Remove only the equivalent `sys.path` bootstrap from `prediction_pipeline/protocol.py`, preserving its protocol path and public contract.
4. Register the new zero-debt initializer gate check and update its documentation and baseline shape.
5. Run focused architecture/import/protocol tests, the Architecture Gate, and the full CPU test discovery suite.

Rollback is a normal revert of this change. No database, artifact, protocol, or configuration migration is involved.
