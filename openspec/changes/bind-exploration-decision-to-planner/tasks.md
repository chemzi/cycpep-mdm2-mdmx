## 1. Characterization and Contract Boundary

- [x] 1.1 Add focused isolated Planner fixtures that construct contract-valid ExplorationDecisions and capture the frozen-baseline Decision-absent source, input digest, plan ID, and task surfaces.
- [x] 1.2 Add failing focused tests for valid binding, deterministic replay, a different valid Decision changing plan identity, canonical source provenance, and local-only frozen-State injection.
- [x] 1.3 Add failing mismatch tests for project, workflow, source round, applicable round, Prediction run, and target scope, plus invalid-contract rejection and a positive reordered-equivalent target-scope regression that proves task input order is unchanged.
- [x] 1.4 Add failing regressions proving Decision absence preserves legacy shape/identity, ambient Evidence/history is not consulted, no State/Evidence persistence occurs, and Decision changes do not alter tasks/budgets/approvals/execution/proposal counts/lengths/seeds.
- [x] 1.5 Strengthen characterization with the frozen pre-change digest formula/plan ID, direct schema complete/partial provenance validation, real explicit-workflow import coverage, and missing-length ambient-experience guards.

## 2. Minimal Planner Implementation

- [x] 2.1 Add the optional keyword-only `exploration_decision` input to `build_plan()` and restore it exclusively through `ExplorationDecision.from_dict()`.
- [x] 2.2 Implement focused Planner handoff validation for project/workflow/source/applicable-round/Prediction-run/target bindings and inject the canonical payload only into the invocation-local State copy.
- [x] 2.3 Compute the canonical Decision SHA with `object_sha256(validated_decision.to_dict())` and conditionally bind Decision ID plus SHA into Planner input identity without changing the absent path.
- [x] 2.4 Conditionally emit the three Decision provenance fields in Critic-source plans and make the Planner plan schema accept them only as an all-or-none additive group.
- [x] 2.5 Update `contracts/plan.py` or `agents/planner/validation.py` only if focused contract validation proves necessary; otherwise leave them unchanged. Do not modify any forbidden module.
- [x] 2.6 Move handoff validation into `agents/planner/validation.py` and add the authorized narrow `task_builder.py` marker guard so explicit Decision builds cannot read/write ambient experience while legacy builds remain unchanged.
- [x] 2.7 Clear any caller-supplied reserved marker before explicit Decision validation/injection and add a Decision-absent regression proving ambient State cannot suppress the legacy experience path.

## 3. Verification and Documentation

- [x] 3.1 Run the focused E3-A Planner tests using only unittest fixtures/temporary directories and resolve every failure without relaxing assertions.
- [x] 3.2 Run the full `python -m unittest discover -b` suite without Launcher/Worker/scientific execution and record the exact pass/skip result.
- [x] 3.3 Synchronize only the latest `e3/closed-loop-runtime` HEAD into the feature branch immediately before final review/merge readiness, then rerun focused tests, the full unittest suite, Architecture Gate, strict OpenSpec validation, relevant compile checks, and `git diff --check`; do not rebase or follow moving `integration/data-integrity-transaction`.
- [x] 3.4 Inspect the final changed-file list and diff to prove no forbidden file, approved project config/digest, Store/runtime artifact, or production checkout was touched.
- [x] 3.5 Synchronize `docs/planner_agent.md` only if the optional public input cannot be understood from code/schema/tests; otherwise document why no doc edit is required.

## 4. Independent Review and Draft PR

- [x] 4.1 Run independent high-reasoning Spec review against this OpenSpec change; resolve all P0/P1 findings and rerun affected gates.
- [x] 4.2 Run independent high-reasoning Standards review against `ENGINEERING_STANDARD.md` and repository instructions; resolve all P0/P1 findings and rerun affected gates.
- [x] 4.3 Commit the verified narrow diff, push `chemzi/e3-bind-exploration-decision-to-planner`, and create a Draft PR against `e3/closed-loop-runtime` without merging or deploying it.
- [x] 4.4 Report base SHA, head SHA, changed files, every verification result, forbidden-file status, and Draft PR URL.
