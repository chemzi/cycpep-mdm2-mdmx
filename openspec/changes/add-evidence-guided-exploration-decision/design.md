## Context

See `proposal.md` for motivation and `specs/evaluation/exploration-decision/spec.md` for observable behavior.

The baseline has two useful pure seams and two unsafe-for-E2 consumption seams:

- `experience.summarize_failures(events=...)` and `suggest_length_preference(...)` implement the intended conservative 5-sample / 70-percent / 30-percent policy.
- `exploration.exploration_shortlist(events=...)` can rank a caller-supplied Evidence set and preserves `passed`; its default path, however, reads cumulative target history.
- `experience.consume_experience_preference()` and `apply_experience_preference()` read cumulative history and are consumed directly by Planner/Design. They remain compatibility paths and are not the E2 authority.
- Transactional Prediction currently normalizes a `run_id` field to `prediction_run_id`, but fresh/cache battery proposal paths do not both supply that field. E2 therefore requires a producer repair before real committed Prediction Evidence can satisfy its run binding. A recorded shortlist can carry the source round and exact source event identifiers.

SQLite-backed Evidence is the formal append-only authority; JSONL is a projection. Existing `object_sha256` is the canonical object-digest implementation. The approved MDM2/MDMX config contains `[8, 10, 12]` under each target's `design.lengths`.

## Goals / Non-Goals

**Goals:**

- Establish one immutable public contract and a pure current-scope builder.
- Make all identity inputs explicit and deterministic.
- Reuse the existing scientific policy without changing its thresholds.
- Persist the complete Decision through the existing Evidence authority.
- Leave a safe, typed handoff for E3 without adding an E3 consumer.

**Non-Goals:**

- Reworking failure aggregation, Pareto ranking, or shortlist selection.
- Replacing or connecting the legacy Planner/Design experience consumers.
- Creating a Planner action, handler, transaction owner, State projection, JSON authority, CLI, UI, or next-round workflow.
- Changing project files, threshold values, protocol parameters, candidate pass state, or Prediction artifacts.

## Decisions

### D1. Separate the immutable contract from the decision service

Add a frozen contract under `contracts/` with strict construction and round-trip serialization. Add a focused top-level evaluation service that scopes/normalizes inputs, calls existing statistics, builds the contract, and optionally records it. This keeps validation, scientific decision logic, and I/O from accumulating in one giant function.

Alternative considered: extend `experience.py`. Rejected because that module also owns legacy direct-consumption and best-effort Evidence behavior; mixing the formal E2 authority into it would obscure the boundary and invite implicit full-history reads.

### D2. Require callers to supply current formal inputs explicitly

The pure builder accepts the selected battery Evidence rows, one recorded shortlist row, the current candidate identifiers and handoff identity, source round, normalized approved project config, threshold snapshot, and expected protocol identity. It never calls `EvidenceLogger.get_all()` to decide what evidence counts.

Exactly one battery row must exist for every candidate in the bound current handoff: battery candidate IDs must equal handoff candidate IDs as sets. Missing, extra, or duplicate candidate verdicts fail closed. Every row must match the project/workflow/workflow-run and `prediction_run_id`, carry the expected targets/protocol identity, and appear in the shortlist's exact `source_event_ids`. The shortlist envelope must match the workflow trace and source round. This combination is the current-round proof even though older `battery_evaluated` rows did not historically carry an Evidence-envelope round.

Fresh Prediction records the battery proposal with its canonical Prediction run identity. A cache hit reconstructs the same proposal from the immutable cached record's candidate/battery fields before the transaction is committed. The Execution Prediction boundary requires exactly one battery event for every handoff candidate and rejects missing, extra, or duplicate verdicts before commit.

Formal provenance additionally freezes `battery_evaluated` as `prediction/evaluate`, `prediction_handoff_ready` as `prediction/evaluate`, and `exploration_shortlist` as `critic/critic`. Agent and phase are included in their semantic projections and are rechecked both at build and before Decision append.

Alternative considered: query the ledger by target and take the latest rows. Rejected because it silently joins rounds and makes unrelated history an undeclared decision input.

### D3. Normalize the approved envelope before statistics

Resolve `design.lengths` for every requested target and require approved project review (`status=approved` and matching approved/content binding). Store `allowed_lengths_by_target`; use the intersection as `effective_allowed_lengths`. All selected current-run battery lengths must be in the corresponding effective envelope. `baseline_policy_weights` assigns one relative integer share to every effective allowed length.

An adjustment uses the existing conservative rule over a numerically sorted length summary and narrows `proposed_policy_weights` to the selected best length with one integer share. A no-adjustment Decision copies the baseline weights. These fields describe relative policy allocation only; they do not claim actual Round 1 sampling, proposal counts, a Planner budget, or an execution budget. E3 alone may combine relative weights with an approved budget to materialize counts.

Alternative considered: emit concrete candidate counts such as 1/2/9. Rejected because assigning a total budget and materializing counts belongs to E3 Planner consumption.

### D4. Bind identity to semantic snapshots, not caller order

Build one canonical input object containing:

- normalized projections of every selected battery event, sorted by event ID;
- the normalized shortlist projection and its event ID;
- project/workflow/run, prediction-run, handoff, candidate, target, and round bindings;
- normalized per-target/effective allowed lengths and project approval binding;
- the canonical threshold snapshot digest and expected protocol identity.
- the complete versioned conservative length-policy identity and parameters.

Use the repository's existing `object_sha256` once for the policy envelope, threshold snapshot, and complete input object. `decision_id` is `exploration_decision_` plus the complete decision-input digest. The event's own append ID and timestamp are deliberately excluded. Decision-relevant Evidence payload changes therefore change identity even if a test fixture reuses an event ID.

Alternative considered: derive identity only from source event IDs. Rejected because it would not detect mutated fixtures or a broken non-immutable source backend.

### D5. Record the complete contract as formal Evidence

Add `exploration_decision` to the Evidence event allowlist and add a narrow writer that:

1. serializes and revalidates the immutable Decision;
2. reads the formal ledger only to verify that every referenced event exists and still matches the bound projections;
3. appends the complete Decision payload with canonical trace/round/target envelope fields.

Before appending, the writer also checks existing `exploration_decision` rows by `decision_id`. An identical canonical payload returns the existing formal event ID without another append; a different payload under the same ID raises a contract error. This is deliberately sequential retry idempotency only—no database unique index, concurrent serialization, or transaction refactor is introduced.

`EvidenceLogger.log` rejects `exploration_decision`; the dedicated writer is the only supported append path and performs source, shortlist, and Prediction handoff verification before reaching the Store. This closes the generic-writer bypass without redesigning Evidence infrastructure.

The Decision object and formal Evidence payload are the E2 artifact. No sidecar file or State projection becomes authority. Build/validation happens before append; a failed append propagates and produces no separate completion claim.

Alternative considered: write a JSON Decision artifact first. Rejected because the task forbids JSON workflow authority and E2 has no transaction owner that could atomically register such an artifact with a later consumer.

### D6. Preserve shortlist/pass objects by construction

The service treats source rows and shortlist data as immutable inputs, copies only normalized projections, and emits no pass or threshold mutation. A zero-hard-pass shortlist is valid. Tests patch Design and Planner entry points to fail if called and compare source structures before/after.

### D7. Public/data compatibility is additive

Existing pre-E2 public function signatures do not change. Before PR merge, the new E2 builder boundary replaces caller-declared handoff identifiers/candidate IDs with one formal `prediction_handoff_ready` event. Prediction battery/handoff rows gain additive canonical threshold and candidate-scope fields. Legacy `experience_applied` remains readable and its direct Planner/Design behavior remains unchanged until an explicit E3 migration decides how the Decision is consumed.

### D8. Bind handoff and thresholds to Prediction authority

Reuse `prediction_handoff_ready` as the formal handoff authority. Prediction records its sorted candidate IDs and the canonical threshold digest on that event, and records the same digest on each `battery_evaluated` event. The immutable handoff document records the same digest. E2 receives the formal handoff event, derives `prediction_handoff_id`, candidate IDs, Prediction run, protocol, and threshold identity from it, and later verifies that projection against the ledger before formal Decision append. Caller-supplied candidate IDs are removed from the builder boundary.

Canonical threshold identity is the repository canonical object digest of the exact Prediction-owned threshold snapshot consumed by the evaluator. Prediction and E2 both call the same identity helper. Every selected battery row, the handoff authority, and `ExplorationDecision.threshold_digest` must match it.

This identity work does not migrate Prediction scientific evaluation semantics. Prediction continues evaluating the caller-provided threshold snapshot as before; the provenance digest is computed separately. Any alias/duplicate-key semantic migration is deferred to its own approved change.

### D9. Validate embedded policy versions without the current singleton

Maintain a frozen registry keyed by `(policy_name, policy_version)`. Each entry binds both frozen parameters and its evaluator algorithm. Contract restoration selects the embedded definition and runs the same summary, support, status, reason, and adjustment validation with that version's parameters and algorithm. Changing the default policy only affects newly built Decisions; recorded V1 Decisions remain fully validated and sequentially retryable. Unknown versions fail closed, and the embedded parameter definition remains part of semantic identity.

## Risks / Trade-offs

- **[Legacy direct consumers still bypass the new Decision]** → Keep that debt explicit; do not mix E3 migration into E2. E3 must select and bind the formal Decision before changing Planner/Design behavior.
- **[Old non-transactional battery events lack `prediction_run_id`]** → Fail closed for E2 rather than treating target-only history as current-round evidence.
- **[A multi-target envelope can become empty]** → Fail closed and require project/config resolution; never widen one target's approval.
- **[Existing preference helper has order-sensitive tie behavior]** → Normalize the summary by numeric length before calling it and cover ties deterministically without changing the 5/70/30 scientific rule.
- **[Evidence verification scans the ledger]** → Accept the bounded MVP cost; computation still uses only declared IDs, and a future indexed Store lookup can replace the read without changing the contract.
- **[PredictionPipeline is already above the preferred core-file size]** → Cache reconstruction and invalid-input battery authority remain at the existing Prediction record owner seam; moving them requires the broad Prediction refactor forbidden by PR #71. Treat decomposition as separate remediation work.

## Migration Plan

1. Ship the additive contract, builder, Evidence event type/writer, and tests with no automatic caller.
2. Existing workflows continue unchanged; rollback removes only the additive code/event capability. Previously appended events remain valid immutable history and are harmless to older readers.
3. E3 may later consume a validated `ExplorationDecision` through its own approved OpenSpec change and transaction ownership. Planner/Execution integration is explicitly not part of this rollout.
