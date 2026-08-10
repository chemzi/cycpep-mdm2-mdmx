## Context

See `proposal.md` for motivation. This design is based on `integration/data-integrity-transaction` at `b05c67f` and the current implementation rather than historical phase labels.

### Current reusable seams

| Boundary | Reusable public seam | Formal output/authority | Audit notes |
|---|---|---|---|
| Project | `ProjectContext.load`, `assert_project_approved` | approved project contract and Store project state | Approval is content-bound and already fails on stale approval. |
| Research | `agents.research.run(project_config=...)` | State plus append-only Evidence | The call accepts explicit project configuration and returns its scientific result. |
| Design routes | `Design(ProjectContext)` and `design_rfpeptides`, `design_motif_guided`, `design_atsp_derived` | CandidateIndex, manifests, Evidence | Public route calls exist and own scientific behavior. Route A is generic; Routes B/C contain project-specific constraints. |
| Prediction | `agents.prediction.run(..., run_id=..., resume=..., project_config=...)` | Prediction run manifest/records/handoff, State, Evidence; transactional effects when invoked by Execution | Explicit run identity and resume validation already exist. |
| Critic | `agents.critic.run(handoff_path=..., project_config=...)` | immutable Critic report, State, Evidence | Report identity is bound to Prediction input. |
| Planner | `agents.planner.run(critic_report_path=...)` | immutable execution plan, State, Evidence | Idempotently derives a plan from a real Critic report. |
| Approval | `record_approval` and existing validation used by Orchestrator | immutable approval artifact | Scope, plan binding, and budgets are already authoritative. |
| Orchestrator | `initialize`, `status` | orchestrator run and task state | Initialization is idempotent; status refreshes task/run state from the bound plan and approvals. |
| Execution | `drain_run` and `execute_task` | Worker receipts, Store transactions, Orchestrator task state | Ready tasks execute in task order through registered handlers and transaction recovery. |
| Store/Evidence/Trace/Transaction | `get_storage_backend`, Store query methods, `TraceContext`, transaction records, `RecoveryManager` through Worker | SQLite-backed formal data and append-only Evidence | These remain the only formal authorities. |

`scripts/run_execution_selfcheck.py` confirms the reusable downstream composition, but its generated Critic bootstrap, isolated copied data, automatic selfcheck approval, and source-file checks are test-only behavior and are not dependencies of this design.

Relevant executable baselines include `test_target_bootstrap.py`, `test_research*.py`, `test_design*.py`, `test_prediction*.py`, `test_critic.py`, `test_planner.py`, `test_orchestrator.py`, `test_execution*.py`, `test_data_integrity_transactions.py`, `test_recovery_hardening.py`, `test_store_transaction_ownership.py`, and `test_architecture_gate.py`.

### Actual missing seams

1. **No authoritative initial Design invocation.** Public route methods execute science and write candidates, but there is no public `run_initial` contract that owns route/job materialization, exposes an invocation ID, returns formal candidate/artifact/evidence references, or records an unambiguous completion boundary. The CLI's `route=all` behavior is not a safe project-generic policy for a Launcher.
2. **No durable correlated Research completion.** Research writes State and Evidence but does not bind an invocation to `launcher_run_id` plus approved project content, return its Evidence IDs, or durably distinguish completion from a crash after partial Research side effects.
3. **No common pre-Orchestrator boundary inspector.** Prediction, Critic, and Planner artifacts can be validated individually, but there is no public read-only seam that resolves a launcher correlation to a unique completed pre-Orchestrator boundary. The Launcher must compose explicit public queries; it must not scan directories or read `State.phase`.
4. **No Launcher diagnostic contract or CLI.** There is no safe place to record call observations and recovery blockers without overloading formal workflow state.

The change will add only the narrow contracts above. It will not move state ownership or replace an Agent implementation.

## Goals / Non-Goals

**Goals:**

- Provide a single launch/status/resume entry point with deterministic correlation and structured outcomes.
- Persist the initial launcher/project binding before any scientific side effect.
- Make every continuation decision from validated formal state.
- Make a report-write failure after a completed formal boundary recoverable without repeating that boundary.
- Preserve existing Planner approval and Execution transaction behavior.
- Make ambiguity visible as a stable blocker rather than an automatic retry.

**Non-Goals:**

- Making every legacy pre-Planner Agent operation transactional in this change.
- Defining a new workflow graph, scheduler, task model, retry policy, scientific route policy, or database.
- Supporting concurrent Launcher processes for the same `launcher_run_id`; a per-report exclusive lock is sufficient for v1.
- Changing scientific algorithms, thresholds, protocol values, Planner recommendations, or approval budgets.
- Treating the diagnostic journal as necessary for formal workflow correctness.

## Decisions

### 1. Put coordination in a new inward-facing `workflow` package

`workflow` will contain a small application service, CLI adapter, diagnostic serializer, error mapping, and read-only boundary resolver. Dependency direction is:

```text
workflow CLI
  -> launcher application service
     -> public Project/Agent/Planner/Orchestrator/Execution/Store contracts
     -> diagnostic report writer
```

Agent, Store, Planner, Orchestrator, and Execution modules never import `workflow`. The package contains no handlers, scheduling loop beyond the fixed one-pass boundary sequence, or formal transition tables.

Alternative considered: extend `scripts/run_execution_selfcheck.py`. Rejected because its isolation, synthetic Critic report, and automatic approval are explicitly unsuitable for production coordination.

### 2. Add Design-owned initial invocation and Research correlation receipts

The Design package will expose a narrow public initial invocation seam. Before calling it, Launcher reconstructs `design_invocation_id` from the durable launcher UUID using `launcher_<uuid-payload> -> design_initial_<uuid-payload>`; this is a reversible namespace mapping, not a hash. Design owns materializing deterministic initial jobs from the approved project and versioned Design configuration. Before any scientific, GPU, candidate, or artifact side effect, it appends `design_initial_invocation_started` through the existing `EvidenceLogger`/Store boundary, binding `design_invocation_id`, `launcher_run_id`, `project_id`, `approved_content_binding`, and the materialized job/config identity. It then invokes only supported public route logic and appends `design_initial_completion`. The completion payload binds the same correlation fields plus job descriptions, candidate IDs, Artifact IDs, and prior Evidence IDs.

The Design-owned public validator accepts the reconstructed `design_invocation_id` and expected project binding, queries the exact correlated start/completion Evidence through `Store.query`, validates their bindings and referenced candidates/artifacts/evidence, and returns `not_started`, `started_without_completion`, `completed`, or a structured conflicting blocker. Only absence of the Design-owned start event is `not_started`; a start without a valid completion is ambiguous and cannot authorize rerun. Launcher does not inspect CandidateIndex, directories, timestamps, or journal completion claims to decide Design recovery. A durable completion followed by launcher bookkeeping failure is therefore recoverable: resume reconstructs the same ID, validates completion, skips Design/GPU work, repairs diagnostics, and advances to Prediction.

For v1, Design owns the safe generic route policy. It must not call MDM-specific Routes B/C for arbitrary projects. If the approved project and current Design contract cannot produce an unambiguous supported initial job set, the seam returns a structured `initial_design_contract_gap` blocker before GPU work. Scientific defaults remain in the versioned Design protocol and approved project; no Launcher defaults are introduced.

Research will add an explicit public `run_with_receipt(..., correlation=...)` wrapper. Its correlation contract contains `research_invocation_id`, `launcher_run_id`, `project_id`, and the existing approved-content identity. Before calling the existing Research implementation, the wrapper appends `research_invocation_started` through the existing `EvidenceLogger`/Store boundary; this event must be durable before any Research side effect. After the implementation's formal outputs are committed, the wrapper appends `research_completion_receipt` and returns the existing scientific result plus the receipt event ID. The additive payloads carry the correlation bindings, and the completion payload also references the Research Evidence IDs. A Research-owned public validator reads both events through `Store.query`, validates their payloads and referenced Evidence, and returns `not_started`, `started_without_completion`, or `completed` to the boundary resolver. The existing `run(...)` signature and return value remain unchanged.

If no correlated start event exists, formal Evidence proves Research did not start and resume may invoke it. If the start event exists but the correlated completion receipt is absent, the boundary resolver returns `research_completion_ambiguous`; it neither reruns Research nor advances. If the receipt is durable but the later launcher journal update failed, resume validates the receipt and safely skips Research.

These start/completion events use the existing `EvidenceEvent` envelope and Store persistence contract; they add event types and payload schemas only. Existing readers remain compatible because unrelated event types are ignored, and no new table, ledger, artifact type, or storage authority is introduced. The initial Design events are correlation/recovery receipts, not task state. If the Design start event exists but its completion event is absent, recovery is ambiguous and fails closed rather than rerunning Design.

Alternative considered: have Launcher call each Design route and infer completion from CandidateIndex or directories. Rejected because it duplicates route policy and cannot distinguish a complete batch from a crash after partial candidate publication.

### 3. Use one fixed coordination sequence, not a persisted state machine

The application service attempts these boundaries in order:

```text
project approval validation
initial diagnostic persistence
Research receipt
initial Design receipt
Prediction handoff
Critic report
Planner plan
approval pause or Orchestrator initialize
ExecutionWorker drain
Orchestrator status
```

At each boundary it first asks the boundary resolver whether the formal authority proves completion for the same approved project and correlated inputs. If yes, it records an observation and advances. If no formal work exists, it calls the public seam once. If formal work is partial, conflicting, or recovery-unresolved, it stops with a blocker.

The diagnostic field `last_completed_boundary` is only a convenience observation permitted by the requirements. It never decides which call is legal. There is no persisted `pending/ready/running/succeeded` model for stages.

Alternative considered: store a launcher workflow row with stage transitions. Rejected as a second workflow database/state machine.

### 4. Correlate without replacing formal trace identity

`launcher_run_id` is a UUID-based diagnostic correlation ID, not a `workflow_id`. Pre-Planner receipts carry it as correlation metadata. Once Planner returns the formal `workflow_id`, all later diagnostics copy the formal `TraceContext` identifiers exactly. The Launcher does not derive substitute plan, run, task, attempt, or transaction IDs.

Prediction uses two explicitly named identities: `prediction_invocation_id` correlates the Launcher call, and `prediction_run_id` is the existing Prediction pipeline run identity passed through `agents.prediction.run(run_id=...)`. Both are deterministically reconstructed from the UUID payload of the already durable `launcher_run_id`:

```text
launcher_<uuid-payload>
  -> prediction_invocation_<uuid-payload>
  -> prediction_<uuid-payload>
```

This is a reversible namespace mapping, not a new hash or integrity authority. Before calling Prediction or allowing it to create a run directory, Launcher resolves the configured Prediction run root and forms the exact locator `(resolved_run_root, prediction_run_id)`. A narrow Prediction-owned pre-invocation seam then appends `prediction_invocation_started` through the existing Store-backed Evidence boundary, binding the exact locator, `prediction_invocation_id`, `prediction_run_id`, `launcher_run_id`, `project_id`, and approved-content/config/candidate inputs. That receipt must be durable before any Prediction or run-directory side effect; persistence failure returns non-zero and Prediction is not called. The internal diagnostic copies the locator for operator observability, but the Prediction-owned receipt is the recovery locator authority. The locator remains location metadata: it selects the exact formal run to inspect but cannot prove scientific completion.

Prediction records `prediction_invocation_id` additively in its existing run manifest, handoff, and completion Evidence; `prediction_run_id` remains the existing manifest/run-directory identity. Both values are non-null and unequal. For legacy/non-Launcher calls, the correlation argument is absent and `_run_manifest()` omits every Launcher-only key entirely. It must not add keys with `null` values because current resume compares observed and expected manifests with strict equality. Launcher-correlated fields are mandatory only when correlation is supplied.

Prediction owns a public read-only recovery validator. Given the reconstructed identities and expected project/config/candidate bindings, it first queries the exact `prediction_invocation_started` receipt through Store. Absence of that receipt is `not_started`; a valid receipt supplies the only authoritative original locator for resume. The validator opens only that locator without consulting current ambient root selection or enumerating directories, then validates the existing run manifest, input snapshot, handoff, and completion Evidence as one coherent formal result. A start receipt without a coherent completion is `started_without_completion` and fails closed. Diagnostic locator metadata may mirror the receipt for observability but never authorizes `not_started`, selects an alternative root, or satisfies completion validation.

Neither Prediction identity is written into `TraceContext.run_id`. The formal trace `run_id` is reserved exclusively for the Orchestrator run created after plan approval and remains `null` before Orchestrator initialization. The strict namespace contract is `prediction_invocation_id != prediction_run_id != formal_trace.run_id`; after Orchestrator initialization all three remain distinct. Existing Prediction digest validation remains responsible for project/config/candidate identity, and the approved-content binding reuses the existing project approval contract value. The Launcher computes no new integrity digest.

Existing protocol and artifact digest fields remain untouched because they are part of current formal contracts. The Launcher adds no new hash or integrity mechanism.

### 5. Diagnostic report is an atomic observation journal

The diagnostics root is resolved without knowing a project: `CYCPEP_LAUNCHER_DIAGNOSTICS` when configured, otherwise `<NP_DATA>/launcher_diagnostics`, otherwise the repository runtime data root's `launcher_diagnostics` directory. A report is addressed directly by its validated opaque ID; `status` and `resume` never enumerate project directories.

Default internal location shape:

```text
<configured launcher diagnostics root>/<launcher_run_id>.json
```

Version 1 shape:

```json
{
  "schema_version": 1,
  "launcher_run_id": "launcher_<uuid>",
  "project_id": "...",
  "approved_content_binding": "...",
  "prediction_invocation_id": "prediction_invocation_<opaque>",
  "prediction_run_id": "prediction_<opaque>",
  "prediction_run_locator": {"root": "<internal>", "run_id": "prediction_<opaque>"},
  "created_at": "...",
  "updated_at": "...",
  "last_completed_boundary": "planner",
  "calls": [
    {
      "boundary": "planner",
      "component": "agents.planner",
      "started_at": "...",
      "completed_at": "...",
      "input_refs": [{"kind": "critic_report", "id": "..."}],
      "output_refs": [{"kind": "plan", "id": "..."}],
      "formal_trace": {"workflow_id": "...", "run_id": null, "plan_id": "..."},
      "observed_formal_status": "awaiting_approval"
    }
  ],
  "failure": null
}
```

The internal report mirrors the exact resolved Prediction run locator for diagnosis, but resume obtains and validates it from the Prediction-owned Store-backed start receipt. Browser-facing output excludes its internal root and exposes opaque IDs and safe relative roles only. The report locator is not recovery, workflow, or scientific authority. The report never stores secrets, environment dumps, full stdout/stderr, tracebacks, candidate payloads, task transition state, or transaction effects. Writes use the repository's existing atomic JSON infrastructure without adding a new hash.

After project approval validation, the Launcher allocates `launcher_run_id` and must durably create the initial report containing `project_id`, the current approved-content binding, and the safe project locator before invoking Research. The report writer is injected so tests can fail both this initial write and later writes. Initial-write failure performs no scientific call; a later report-write failure becomes a non-zero launcher failure but never triggers formal rollback.

### 6. Failure model is boundary-scoped and fail-fast

All errors are normalized to:

```json
{
  "code": "stable_machine_code",
  "component": "research|design|prediction|critic|planner|approval|orchestrator|execution|transaction|launcher",
  "message": "bounded sanitized message"
}
```

Known contract errors retain their existing `code`. Unexpected exceptions use the exception class only as a fallback code, are attributed to the active component, and terminate the launch. The CLI never emits raw tracebacks or complete process output in its JSON contract.

Exit policy:

- `0`: `completed`, `completed_required`, or the intentional `awaiting_approval` pause;
- `2`: invalid input, stale approval, Agent/Planner/Orchestrator/Worker failure, or diagnostic persistence failure;
- `3`: formal `blocked` outcome or an ambiguity/recovery blocker that requires operator action.

No exception handler continues to a later boundary. Existing committed data is left to its owner. The Launcher does not compensate or mutate formal state.

### 7. Resume algorithm is formal-first

`resume` performs the following read/decision sequence:

1. Resolve the diagnostic report by `launcher_run_id` and acquire its exclusive report lock. The report supplies only project and artifact locators plus the original `project_id` and approved-content binding.
2. Reload the project from the durable safe locator, re-run the current approval validation, and compare both `project_id` and approved-content identity with the initial durable binding. A mismatch returns a structured blocker before any formal recovery or scientific action.
3. Query formal transaction recovery before any Worker action. If recovery reports unresolved transactions or marker errors relevant to the run, return `transaction_recovery_unresolved` and do not execute science.
4. If a formal Orchestrator run is referenced, call `status`. A formal terminal or approval status is returned directly. A ready run may be passed to `drain_run`; a claimed/ambiguous run is not re-claimed by Launcher logic.
5. If a plan exists without a valid supplied/formally loaded approval, return `awaiting_approval`. If `--approval` is supplied, let existing approval and Orchestrator contracts validate it.
6. Otherwise resolve Planner, Critic, Prediction, initial Design, and Research receipts in reverse order using their public validators and correlation/project/input bindings. Choose the next boundary only from the highest uniquely proven formal completion.
7. If a boundary has partial or conflicting formal records, return a structured blocker. Do not use journal completion claims, `State.phase`, directory enumeration, stdout parsing, or GPU rerun as a fallback.
8. Append repaired observations after formal verification. Repeating resume on the same formal state is read-only/idempotent.

For initial Design, resume reconstructs `design_invocation_id` and invokes the Design-owned validator. No start receipt permits the first invocation; a valid completion skips Design/GPU and advances to Prediction; a start without completion or any conflicting, multiply bound, or unverifiable result fails closed.

For Prediction, resume reconstructs both identities from `launcher_run_id` and invokes the Prediction-owned recovery validator. It never trusts the diagnostic locator or re-resolves the root from current environment variables. Absence of a Store-backed start receipt permits the first Prediction invocation, which must first persist a new receipt for the resolved locator; a valid start receipt supplies the original locator, and a valid completed result skips Prediction and advances to Critic even when the post-Prediction diagnostic write never succeeded. A start without coherent completion, conflicting receipts, or an unverifiable locator returns `prediction_recovery_ambiguous` with non-zero exit and invokes neither Prediction nor Critic.

For the required committed-transaction/bookkeeping case, the transaction, task closure, and Orchestrator run status are already formal. Resume observes them, skips the action, and continues. If the transaction committed but Orchestrator closure is not safely resolved, existing recovery owns compensation/reconciliation; Launcher stops until that contract returns clean.

### 8. CLI contract is one JSON document per command

Commands:

```text
python -m workflow launch --project <approved-project.json>
python -m workflow status --launcher-run <id>
python -m workflow resume --launcher-run <id> [--approval <approval.json>]...
```

Common browser-safe output fields are:

```json
{
  "schema_version": 1,
  "status": "completed|completed_required|awaiting_approval|blocked|failed|ready|running|pending",
  "launcher_run_id": "...",
  "project_id": "...",
  "approved_content_binding": "...",
  "boundary": "...",
  "prediction_invocation_id": null,
  "prediction_run_id": null,
  "formal_trace": {
    "workflow_id": null,
    "run_id": null,
    "plan_id": null,
    "task_id": null,
    "attempt_id": null,
    "transaction_id": null
  },
  "evidence_ids": [],
  "artifact_ids": [],
  "last_known_formal_status": null,
  "error": null
}
```

`status` is strictly read-only. `resume` is the only continuation command and approval artifacts are explicit inputs. Human-readable logs, if any, go to stderr in bounded form so stdout stays machine-readable.

The `formal_trace.run_id` field always means Orchestrator run identity. Prediction identities are never aliased into it.

### 9. Outcome mapping delegates to Orchestrator

After initialization, Launcher never determines success from receipt count or absence of ready tasks. It calls Orchestrator `status` and returns the formal run status (`completed`, `completed_required`, `failed`, `blocked`, `awaiting_approval`, `ready`, `running`, or `pending`) plus the existing task-status counts. `drain_run` is called only for a formally ready run. A later approval requirement is surfaced as `awaiting_approval` and is not auto-approved.

### 10. Recovery authority is ordered upstream-first

Pre-Orchestrator continuation is resolved in causal order, not by searching for the furthest downstream artifact. Launcher first invokes the Prediction-owned validator for the deterministic launcher correlation. A blocked, conflicting, or `started_without_completion` result returns immediately and Critic and Planner are neither inspected nor invoked. A `not_started` result permits only Research/Design/Prediction recovery followed by a fresh Prediction validation. Critic becomes observable only after Prediction is formally `completed`, and Planner becomes observable only after Critic is formally `completed`. This ordering makes upstream ambiguity authoritative over stale downstream history.

### 11. Diagnostic updates are non-destructive observations

`DiagnosticReport.with_observation` merges observations into the report and preserves the existing `failure` and `failed_boundary`. Formal trace helpers merge with the accumulated `FormalTrace` rather than constructing a partial replacement, so run/task/attempt/transaction identifiers survive later plan observations. A separate explicit `clear_failure` operation is available only to the application service after the relevant owner-side validator proves the formerly failed boundary is resolved. Ordinary status reads and diagnostic repair never clear failure implicitly.

### 12. Status uses an owner-side read-only recovery inspector

Transaction/Store infrastructure exposes the minimum public read-only inspection needed to report whether recovery is clean for the referenced formal run. It reads existing transaction, marker, compensation, and owner-liveness state without invoking `recover_pending`, claiming a task, writing a marker, or changing transaction state. `status` uses this inspector and returns `transaction_recovery_unresolved` with available transaction identifiers when unresolved. `resume` may use the existing formal mutating recovery contract immediately before Worker continuation; Launcher does not duplicate transaction policy.

The inspector composes Store transaction rows with durable recovery markers, owner-liveness leases, and Orchestrator closure probes. A live owner is reported as active rather than unresolved; a stale marker whose Store transaction and Orchestrator closure are already formally complete is clean; DB-only unresolved transaction states and incomplete compensation remain blockers. Marker parsing and status categories are shared with mutating recovery so the two owner paths cannot drift.

### 13. Critic and Research correlation is filtered before artifact validation

New `critic_review` Evidence binds the source `prediction_run_id`. The formal boundary inspector filters explicit current-run events before opening any report artifact. Legacy events without this field remain compatible: they are considered only when the referenced report can be uniquely and safely validated as sourced from the current Prediction run. Unrelated legacy history, including broken artifacts, cannot poison the current run; a legacy record that might belong to the current run but cannot be verified fails closed. Multiple or broken explicit records for the current run also fail closed.

Research validation queries Evidence with the expected `project_id`, agent, and event type and accepts referenced Research Evidence IDs only from that same project-scoped set. A completion receipt cannot borrow `research_targets` or other formal Research Evidence from another project.

### 14. Runtime paths come from one explicit ProjectContext contract

Launcher constructs or loads the same official `ProjectContext` and resolved `ProjectPaths` used by Data Layer and Agent execution. The resolved data, Evidence, and database paths are supplied through that context once and temporarily bound only through the existing process-scoped compatibility adapter, which restores prior globals on every exit. Launcher does not independently parse environment variables, infer paths from the current directory, or silently fall back to repository data. Conflicting explicit runtime inputs fail before formal writes.

`ProjectPaths` owns an explicit `database_path` alongside its directories. The official runtime constructor resolves documented environment inputs into an immutable `ProjectContext` before Launcher coordination; after construction, Data Layer receives that exact database path and does not re-read ambient selectors. The compatibility adapter remains a temporary bridge for legacy public Agent seams, not a second resolver.

## Risks / Trade-offs

- **[Pre-Planner stages are not all transaction-owned]** → Add immutable correlated receipts and fail closed on partial ambiguity; do not claim transactional guarantees those Agents do not currently provide.
- **[The new Design initial seam could grow into policy duplication]** → Keep job/route materialization inside Design, use approved project plus versioned protocol only, and make unsupported ambiguity a contract gap.
- **[Report write failure can leave observations stale]** → Formal-first resume reconstructs the boundary; diagnostics never trigger rollback or rerun.
- **[A report contains local locators useful for resume]** → Separate internal diagnostics from browser-safe projection and test path/secret/stdout redaction.
- **[Concurrent resume calls could duplicate coordination attempts]** → Use a per-launcher-run exclusive diagnostic lock; formal Agent and Orchestrator idempotency remain the ultimate protection.
- **[Remote/GPU failures are expensive]** → No automatic retry loop; return structured failure or blocker and require explicit resume after formal recovery is clean.
- **[Public optional correlation parameters affect broad modules]** → Preserve defaults, search all callers, add compatibility tests, and keep changes narrow.

## Migration Plan

1. Add additive receipt/correlation seams and their characterization tests without redirecting legacy callers.
2. Add diagnostic schema, serializer, sanitizer, and formal boundary resolver.
3. Add Launcher application service and CLI behind the new `workflow` module.
4. Run focused failure/recovery/idempotency tests, existing Agent and Execution suites, the full applicable suite, lint/type checks if configured, and Architecture Gate.
5. Document the CLI and the diagnostic/non-authority boundary.

Rollback removes the `workflow` package and additive optional seams. Existing formal artifacts and Store data require no migration and remain usable through existing Agent/Orchestrator interfaces. Diagnostic JSON files may be retained for audit or deleted without affecting workflow data.
