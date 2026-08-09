## 1. Characterize the Import Seam

- [x] 1.1 Add Architecture Gate tests that report direct import-path mutation in package initializers while ignoring clean initializers and standalone entrypoints.
- [x] 1.2 Add fresh-process compatibility tests that import Critic, Planner, and Orchestrator public names without changing the caller's import search path and exercise each retained CLI shim's help command.
- [x] 1.3 Add focused fresh-process regression coverage for direct `prediction_pipeline.protocol` import-path stability and existing public protocol names.

## 2. Remove and Prevent the Legacy Bypass

- [x] 2.1 Remove only the repository-root import bootstrap from the three affected Agent package initializers, preserving their relative imports, public names, and `__all__` contracts.
- [x] 2.2 Remove only the repository-root `sys.path` bootstrap from `prediction_pipeline/protocol.py`, preserving `ROOT`, protocol loading, public names, and scientific behavior.
- [x] 2.3 Implement the focused package-initializer Architecture Gate check, integrate it with reporting and baseline comparison, and keep its accepted baseline empty.

## 3. Synchronize Engineering Documentation

- [x] 3.1 Update the Architecture Gate check inventory and README validation section to document package initializer import-path enforcement without changing unrelated architecture guidance.

## 4. Verify the Change

- [x] 4.1 Run the focused Architecture Gate test module and the retained Critic, Planner, and Orchestrator CLI compatibility checks.
- [x] 4.2 Run `python scripts/architecture_gate.py --baseline architecture_baseline.json` and confirm no new or baselined violations for the new rule.
- [x] 4.3 Run the full CPU suite with `python -m unittest discover` and confirm existing business behavior remains green.
- [x] 4.4 Run strict OpenSpec validation and OpenSpec implementation verification, then confirm code, tests, documentation, and change artifacts agree before review.
