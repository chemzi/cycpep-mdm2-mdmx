# Workflow Launcher operator guide

## Approval-gated initial Prediction

For new Launcher runs, committed Initial Design produces an immutable
`initial_prediction_bootstrap` plan with exactly one registered
`evaluate_new_design_candidates` task over the complete committed candidate
set. Launcher returns `awaiting_approval`; only plan-bound explicit approval
can initialize Orchestrator and ExecutionWorker. Worker transaction output,
formal Artifact/Evidence, and Prediction-owned readiness must validate before
Critic is reachable.

Runs with `prediction_invocation_started` stay on direct recovery and are not
converted. A terminal bootstrap failure is immutable and ordinary commands
never retry it. After deployment repair, `python -m workflow resume
--launcher-run <id> --retry-bootstrap-prediction` creates a newly approvable
plan over the same Design completion, committed transaction, and exact set. It
does not rerun Research/Design or reuse the old approval. Active, ambiguous, or
transaction-unresolved executions cannot be retried.

Retry additionally requires the complete prior transaction to exist in an
explicitly retryable terminal state and to match the failed project, workflow,
run, task, attempt, action, and formal failure Evidence. Missing, active,
`COMMITTING`, `COMMITTED`, compensation-conflict, unknown, or mismatched
transactions remain immutable blockers.

Plans and receipts record path-independent protocol/tool/model/checkpoint
identity. Executable, repository, cache, checkpoint-location, and output paths
remain internal deployment locators and do not define scientific identity.

The additive resume option `--retry-bootstrap-prediction` is an operator
request, not an automatic retry policy. It is accepted only for the latest
formally terminal failed bootstrap execution with resolved transaction state.
It creates or idempotently recovers a new immutable plan and pauses for a new
plan-bound approval. Repeating the request while that retry plan awaits approval
does not create another plan; supplying the failed plan's approval cannot start
the retry. Claimed, running, partially staged, unresolved, non-failed, and
completed executions cannot be converted into retries.

Workflow Launcher is a thin coordination entry point for an approved project. It
calls the existing Research, initial Design, Prediction, Critic, Planner,
Orchestrator, and Execution Worker contracts. It does not own scientific,
workflow, task, transaction, candidate, or approval state.

## Commands

Run commands from the repository root. Each command writes exactly one
browser-safe JSON document to stdout.

```bash
python -m workflow launch --project projects/approved-project.json
python -m workflow status --launcher-run launcher_0123456789abcdef0123456789abcdef
python -m workflow resume --launcher-run launcher_0123456789abcdef0123456789abcdef
python -m workflow resume --launcher-run launcher_0123456789abcdef0123456789abcdef --approval path/to/approval.json
```

`--approval` may be repeated when the existing approval contract requires more
than one artifact:

```bash
python -m workflow resume \
  --launcher-run launcher_0123456789abcdef0123456789abcdef \
  --approval path/to/approval-a.json \
  --approval path/to/approval-b.json
```

- `launch` validates the current approved project, durably creates its initial
  diagnostic binding, and then advances through the first unproven boundary.
- `status` is read-only. It revalidates referenced formal state and never starts
  scientific work or performs a formal transition.
- `resume` is the only continuation command. It revalidates the original project
  binding and formal owner records before deciding whether a boundary may run.

Save the returned `launcher_run_id`; `status` and `resume` address diagnostics by
that exact opaque identifier and do not search project or run directories.

## Outcomes and exit codes

The JSON `status` is authoritative for how an operator should interpret the
command result; do not treat a zero exit code alone as workflow completion.

| Exit | Possible status | Meaning |
|---:|---|---|
| `0` | `completed`, `completed_required` | The reported formal Orchestrator outcome is complete. |
| `0` | `awaiting_approval` | Intentional human-approval pause; no approval was inferred or created. |
| `0` | `ready`, `running`, `pending` | Successful observation of a non-terminal formal state, including read-only `status`. |
| `2` | `failed` | Invalid input, stale or invalid approval, component failure, or diagnostic persistence failure. Later boundaries were not called. |
| `3` | `blocked` | A formal blocked state or a recovery ambiguity requires operator action. Scientific work is not automatically retried. |

Browser-safe output includes opaque Prediction identities and formal trace IDs.
`formal_trace.run_id` always means the Orchestrator run; it is never the
Prediction run ID. Errors use bounded `code`, `component`, and sanitized
`message` fields rather than tracebacks or complete process output.

## Approval handoff

When launch returns `status: "awaiting_approval"`, use the returned formal
`plan_id`, task references, and normal Planner approval process to obtain an
approval artifact bound to that immutable plan and its budget. Do not edit the
Launcher diagnostic to represent approval.

Continue explicitly with:

```bash
python -m workflow resume --launcher-run <id> --approval <approval.json>
```

The existing approval and Orchestrator contracts validate plan identity, task
scope, content binding, and budget before any task becomes executable. A missing,
stale, or mismatched approval stops execution. Launcher never auto-approves,
widens approval scope, mutates thresholds, or retries until approval appears. A
later plan may legitimately return `awaiting_approval` again.

## Diagnostics: location and retention

The internal diagnostic root is selected in this order:

1. `CYCPEP_LAUNCHER_DIAGNOSTICS`, when set;
2. `<repository>/data/launcher_diagnostics`.

Formal-runtime selectors such as `NP_DATA`, `CYCPEP_DATA_DIR`,
`CYCPEP_EVIDENCE_DIR`, and `CYCPEP_DB_PATH` do not select the diagnostic root.
Changing them between `launch`, `status`, and `resume` therefore cannot hide an
existing launcher run when the explicit diagnostics setting is unchanged.

Each report is stored as `<root>/<launcher_run_id>.json` and is updated atomically
under a per-run lock. Initial creation first writes a directly addressed,
write-once `<root>/<launcher_run_id>.runtime-locator.json` sidecar under the same
lock. The sidecar contains internal location metadata only; it has no workflow
status or transition fields. Every later journal read or write requires the
journal's locator mirror to match this sidecar exactly. A missing, invalid, or
conflicting sidecar blocks continuation instead of falling back to ambient
runtime paths. There is no automatic expiry or cleanup policy. Retain both files
for the required operational recovery and audit period, then remove them only
under the site's normal retention policy.

Removing or editing a diagnostic cannot delete, roll back, complete, or otherwise
change formal workflow data. It can, however, make Launcher `status` or `resume`
fail when the safe project/formal locators can no longer be recovered. Keep
diagnostics access-controlled because the internal report contains local
locators and observations intended for operators, not browsers.

Draft diagnostics created before the sidecar contract remain readable for
troubleshooting, but they cannot be updated or used to resume formal work until
their original runtime locator is available through the supported contract.

The journal is diagnostic only. Fields such as `last_completed_boundary`,
`last_known_formal_status`, mirrored locator metadata, and failure observations
do not authorize a formal transition. Store, Evidence, Agent-owned completion
validators, Transaction recovery, Planner approval, and Orchestrator status win
whenever they disagree with the journal.

## Recovery and blockers

Resume works formal-first. It verifies the original `project_id` and approved
content, checks transaction recovery, and then asks formal boundary owners in
reverse order for the highest uniquely proven completion. It does not infer
completion from `State.phase`, directory contents, stdout, journal claims, or
CandidateIndex existence alone.

Common operator-action blockers include:

| Code or family | Meaning and safe response |
|---|---|
| `launcher_project_binding_changed`, `launcher_approved_content_changed` | The project no longer matches the original launch. Do not reuse the run for changed content; restore/review the original approved input or start a separately approved launch. |
| `research_completion_ambiguous`, `research_correlation_conflict` | Research started but completion is absent, invalid, or non-unique. Inspect the formal Research Evidence; do not rerun automatically. |
| `design_recovery_ambiguous` | Initial Design has a durable start without one valid bound completion, or conflicting formal records. Do not rerun Design/GPU work automatically. |
| `initial_design_no_valid_candidates` | Launcher initial Design finished its required tool calls normally but produced no formally usable candidate. The correlated failure receipt is terminal for this invocation; review the Design outcome and start a newly approved run rather than retrying it in place. |
| `initial_design_scientific_tool_failed` | A required RFdiffusion, LigandMPNN, or refold execution failed on the strict Launcher initial path. This is distinct from a normal zero-result and remains stable across `launch`, `status`, and `resume`. Restore the scientific runtime before starting a newly approved run. |
| `prediction_recovery_ambiguous`, `prediction_correlation_conflict` | The Prediction start/completion records or exact locator are partial, conflicting, or unverifiable. Do not select another root or rerun Prediction automatically. |
| `prediction_execution_incomplete` | The correlated production handoff is structurally coherent, but at least one authoritative record does not satisfy Prediction's own Critic-readiness/evidence contract. Missing required evidence, pending work, and owner-declared non-ready terminal outcomes do not enter Critic. |
| `critic_recovery_ambiguous`, `planner_recovery_ambiguous`, `orchestrator_recovery_ambiguous` | The named owner cannot prove one coherent formal result. Resolve the owner records rather than editing diagnostics. |
| `approval_binding_conflict` | Formal approval records do not bind cleanly to the immutable plan. Obtain a valid approval; do not bypass validation. |
| `transaction_recovery_unresolved` | Transaction ownership or recovery is unresolved. Use the existing transaction/Worker recovery path; Launcher will not execute scientific work meanwhile. |
| `launcher_diagnostic_not_found`, `launcher_diagnostic_invalid`, `launcher_run_locked` | The diagnostic cannot be safely addressed, validated, or locked. Restore operational access or wait for the active coordinator; these errors do not change formal state. |

A durable Research, Design, Prediction, task, or transaction completion may
survive a subsequent diagnostic write failure. On a later explicit `resume`, the
owning formal validator can prove that completion, allowing Launcher to repair
its observation and continue without repeating the scientific action. A durable
start without uniquely valid completion remains blocked: explicit resume is not
an instruction to perform a dangerous automatic retry.

Launcher does not maintain a scientific status table. Prediction owns the
battery-to-status mapping, the authoritative record binding, and the statuses
permitted as Critic input; both handoff generation and Launcher-correlated
validation reuse that one contract. A non-pending status alone is not proof of
scientific readiness.

Critic project binding is intentionally outside this contract. Changes to
`critic_review.project_id`, Critic persistence, Critic transaction handling,
and Critic idempotency are owned by the separate `critic-project-binding`
change.

## Internal locator privacy

Prediction recovery uses a Prediction-owned `prediction_invocation_started`
Evidence receipt as the authority for the original exact
`(prediction_run_root, prediction_run_id)` locator. The diagnostic root value is
only a mirror. Changes to `CYCPEP_PREDICTION_ROOT`, `NP_DATA`, or the diagnostic
copy must not redirect recovery to another run root.

The resolved Prediction run root is internal location metadata. It is excluded
from the browser-safe Launcher JSON result. Frontend and generic Evidence
presentation code must not forward the raw `prediction_invocation_started`
payload or its locator/root fields to browser clients; expose only the approved
opaque Evidence and Prediction IDs. Use the CLI browser-safe projection for
external presentation, not the internal diagnostic report or raw formal receipt.

Launcher performs no automatic code repair, unlimited retry, approval bypass,
threshold mutation, scheduling redesign, or workflow-database/state-machine
replacement.
