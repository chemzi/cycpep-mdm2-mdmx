# Workflow Launcher operator guide

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
2. `<NP_DATA>/launcher_diagnostics`, when `NP_DATA` is set;
3. `<repository>/data/launcher_diagnostics`.

Each report is stored as `<root>/<launcher_run_id>.json` and is updated atomically
under a per-run lock. There is no automatic expiry or cleanup policy. Retain the
file for the required operational recovery and audit period, then remove it only
under the site's normal retention policy.

Removing or editing a diagnostic cannot delete, roll back, complete, or otherwise
change formal workflow data. It can, however, make Launcher `status` or `resume`
fail when the safe project/formal locators can no longer be recovered. Keep
diagnostics access-controlled because the internal report contains local
locators and observations intended for operators, not browsers.

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
| `prediction_recovery_ambiguous`, `prediction_correlation_conflict` | The Prediction start/completion records or exact locator are partial, conflicting, or unverifiable. Do not select another root or rerun Prediction automatically. |
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
