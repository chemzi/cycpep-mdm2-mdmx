## 1. Characterize the formal handoff

- [x] 1.1 Add focused failing tests for unique Store-backed Prediction workflow identity and Decision resolution, required missing/ambiguous/invalid publication failures, and explicit legacy/bootstrap no-Decision behavior.
- [x] 1.2 Add formal Planner service/runtime integration fixtures that publish Prediction trace Evidence and Decisions through their dedicated formal paths, do not preseed `state["workflow_id"]`, and exercise the real Critic artifact -> runtime -> service path.

## 2. Implement Decision retrieval and service wiring

- [x] 2.1 Add the immutable workflow Decision handoff value and resolver over the injected project Store, current formal Critic artifact, unique formal Prediction workflow identity, and one Planner State snapshot.
- [x] 2.2 Extend Planner service `run()` with additive explicit Decision/required inputs, enforce required presence, and forward the Decision to `build_plan()` without duplicating E3-A validation or E3-B materialization.
- [x] 2.3 Update `DefaultWorkflowRuntime.run_planner()` to resolve the formal handoff, inject its workflow identity only into the invocation-local State snapshot, and pass that snapshot, Decision, and requirement flag to Planner service.

## 3. Prove behavior and compatibility

- [x] 3.1 Prove Decisions `[12]` and `[10, 12]` materialize through the formal service/runtime path and bind Decision ID, canonical SHA-256, and input digest in plan source.
- [x] 3.2 Prove project/workflow/round/Prediction-run/target mismatches, missing/ambiguous formal Prediction workflow identity, and formal Round 2 required-missing Decisions fail closed before plan persistence.
- [x] 3.3 Prove Planner service does not read ambient experience/Evidence or derive a workflow identity for Decision discovery; bootstrap/direct legacy no-Decision paths retain their explicit contracts.
- [x] 3.4 Prove proposal count, routes, target allocation, seeds, approvals, and unrelated task fields remain unchanged, and repeated identical formal inputs are deterministic.

## 4. Verification and review

- [x] 4.1 Run the new E3-C focused tests plus `test_planner_exploration_decision.py` and `test_planner_decision_materialization.py`.
- [x] 4.2 Run the full unittest suite, repository Architecture Gate, strict OpenSpec validation, compileall, and `git diff --check` using the repository's actual commands.
- [ ] 4.3 Complete independent high-reasoning Spec and Standards reviews, resolve all P0/P1 findings, rerun affected gates, and record final changed files and residual risks.
- [x] 4.4 Update only documentation required by the public Planner service/runtime handoff and mark OpenSpec tasks complete from verified evidence.
