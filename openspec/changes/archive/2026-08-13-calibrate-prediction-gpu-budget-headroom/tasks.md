## 1. Calibrated Prediction estimate

- [x] 1.1 Add public bootstrap-plan and approval regressions that fail at 22 and prove the default n=2 estimate is 30 GPU-slot wall minutes, a ceiling below 30 is rejected, 30 is admitted, and an explicit estimator override still changes the immutable plan ID.
- [x] 1.2 Change only the existing Prediction-specific Planner configuration default from 11 to 15; preserve positional API compatibility, other action estimates, and all admission/completion logic.
- [x] 1.3 Preserve completion authority with a focused regression proving an admitted actual 22.338975-minute outcome fits a 30-minute ceiling while an actual usage above 30 remains `gpu_minutes_exceeded`.

## 2. Verification and delivery

- [x] 2.1 Run focused Planner and approval-admission regressions, the full unittest suite, Architecture Gate, strict OpenSpec validation, and `git diff --check`.
- [x] 2.2 Complete independent high-reasoning Spec and Standards reviews, resolve every P0/P1, and archive/sync the verified change.
- [x] 2.3 Commit, push, create and merge the PR with `gh`; deploy the merged integration commit and start a fresh fully automatic n=2 Launcher smoke without modifying the failed 22-minute invocation.
