## Context

See `proposal.md` for motivation and `specs/workflow/launcher-boundary-truth/spec.md` for observable behavior. The current seams are already narrow:

- `agents/design/initial.py` owns the start/completion receipts and recovery validation, but `run_initial()` writes completion for an empty route result. Route A currently converts RFdiffusion failure to `False`, LigandMPNN failure to `[]`, and refold failure to `None`, so the outer empty list does not prove normal scientific exhaustion.
- `agents/prediction_contract.py` owns Launcher correlation and recovery. The Prediction pipeline already owns battery-to-status semantics, authoritative record references, and the declared Critic-input status set, but the invocation validator currently proves only file/correlation structure and then returns `completed`.
- `workflow/service.py` already coordinates exclusively through `FormalBoundary` inspections. The inconsistency appears when an invocation raises after diagnostics have recorded failure but its formal validator later describes a different state.

The change must preserve Store ownership, explicit `ProjectContext`, existing hash/provenance contracts, public non-Launcher paths, and read-only status semantics. It must not install or emulate absent scientific executors.

## Goals / Non-Goals

**Goals:**

- Make each owning agent publish or classify one unambiguous formal state before Launcher advances.
- Keep the implementation localized to existing owner contracts and boundary adapters.
- Preserve exact-once recovery: known deterministic outcomes are durable; unknown crashes remain ambiguous and are never retried automatically.
- Prevent a failed Initial Design invocation from leaking any formally published candidate or authoritative candidate-registration Evidence.
- Cover the observed swallowed-tool-failure, normal-zero-output, pending, missing-evidence, and non-ready-status shapes with repository-local fixtures.

**Non-Goals:**

- Do not add Prediction executors, fabricate L1-L7 outputs, or change scientific thresholds.
- Do not redesign the generic Prediction/Critic feedback loop outside Launcher production invocations.
- Do not modify `critic_review.project_id`, Critic persistence, Critic transaction handling, or Critic idempotency; those belong exclusively to `critic-project-binding`.
- Do not introduce a second workflow state store, server-artifact dependency, automatic backfill, or broad CLI cleanup.
- Do not make Design artifact registration comprehensive; candidate existence remains the minimum advancement proof for this change.

## Decisions

### 1. Launcher initial Design uses a typed route outcome

Keep the legacy `design_rfpeptides()` signature and list return unchanged. Add a Launcher-initial adapter over the same Route A core that enables strict tool-failure propagation and returns a typed owner outcome. The strict path converts RFdiffusion, LigandMPNN, and refold execution failures plus failed required output postconditions into one Design-owned typed exception/outcome instead of their legacy `False`/`[]`/`None` fallbacks. Required postconditions cover expected RFdiffusion backbone count and parseability, binder-chain discovery, availability of the configured LigandMPNN entrypoint/checkpoint/model, and at least one parseable generated LigandMPNN sequence per required backbone. Only after those generation postconditions succeed may scientific filtering, quality, deduplication, or closure rejection yield a normal empty outcome.

Add `design_initial_failure` beside the existing start and completion events. It carries the existing correlation fields and jobs plus one of `initial_design_no_valid_candidates` or `initial_design_scientific_tool_failed`. The validator reads start, completion, and failure receipts together and returns:

- `completed` only for exactly one valid start, exactly one valid completion, no failure, and a non-empty referenced candidate set;
- `blocked` with the recorded Design-owned blocker for exactly one valid start and exactly one matching classified failure;
- the existing recovery/correlation blocker for missing, duplicate, conflicting, or malformed receipts.

`run_initial()` writes and validates the correct failure receipt before raising the typed Design contract error. Only classified owner tool failures receive the tool-failure receipt. Arbitrary exceptions continue to leave `started_without_completion`, because guessing their meaning would destroy recovery truth.

Alternative considered: inspect recent generic EvidenceLogger errors after a returned `[]`. Rejected because those events are not a typed, invocation-scoped route outcome and can be stale or unrelated. Alternative considered: make all legacy Route A calls raise. Rejected because it broadens the public behavior change beyond Launcher initial Design.

### 2. Initial Design candidate publication reuses CandidateUpdate and Store commit

The strict route receives an invocation-owned CandidateUpdate staging collection. Candidate materialization may write diagnostic PDB/manifest files, but candidate publication appends a typed `CandidateUpdate` to that collection instead of calling CandidateIndex or EvidenceLogger. All jobs in one Initial Design invocation share the same collection. If any later candidate or job raises a classified tool/output failure, the collection is discarded and the existing correlated failure receipt is appended; no candidate row or `candidate_registered` event is authoritative.

After every job succeeds, Initial Design validates that returned candidates and staged CandidateUpdates have the same unique identities. It then calls the existing Store transaction effect seam once with those candidate updates and the correlated `design_initial_completion` event. The Store atomically creates candidate rows, its existing authoritative `candidate_registered` events, and completion. Recovery validates that completion and candidate-registration events share that transaction. Compatibility projections remain non-authoritative.

Alternative considered: delete already-published candidates on failure. Rejected because compensating direct writes is not atomic. Alternative considered: add a new Initial Design transaction framework. Rejected because CandidateUpdate and Store commit already define the required effect and atomicity boundary.

### 3. Prediction exposes and reuses one owner readiness contract

Extract the battery-to-status decision from the pipeline mixin into a public Prediction-owned contract and have the pipeline continue using it. Define the owner Critic-readiness set beside that contract, and have handoff generation and Launcher invocation validation consume the same definition. This is one scientific vocabulary owned by Prediction, not a copy in Launcher or service.

After current correlation and snapshot checks, invocation validation loads every authoritative record referenced by the handoff, verifies its declared digest, candidate/run identity, category/status, exact candidate-set coverage, and recomputes its status from the persisted battery using the owner contract. It returns `completed` only when every recomputed status is in the owner Critic-readiness set. Missing required evidence naturally recomputes to `prediction_pending`; pending or other well-formed non-ready statuses return `prediction_execution_incomplete`. A claimed status that contradicts its battery, a missing record, or broken evidence binding remains an integrity/recovery blocker rather than a scientific-incomplete classification.

The pipeline may continue writing `prediction_handoff_ready` for generic consumers, and Critic may continue understanding pending records outside the Launcher transition. The semantic tightening lives in the Launcher-correlated owner validator, not in the generic pipeline or Critic parser.

Alternative considered: treat every non-pending status as ready. Rejected because `invalid` is terminal but explicitly not Critic input, and status text alone does not prove required evidence. Alternative considered: parse status strings in `workflow/service.py`. Rejected because it duplicates scientific policy outside the owner.

### 4. Launcher remains a projector, not a second validator

Do not add scientific status parsing or special-case error history in `workflow/service.py`. Boundary adapters translate the owner validation results into `FormalBoundary(status="blocked", error=...)`; `_advance`, `_block`, and `_block_or_invalid` continue using those results. This makes launch/status/resume converge naturally and keeps diagnostics a recoverable mirror.

Only minimal service tests are added to prove no downstream call and stable command projection. Detailed state-shape tests stay with Design, Prediction, and Critic owners.

Alternative considered: preserve the last diagnostic exception and replay it from status. Rejected because diagnostics are explicitly non-authoritative and may be stale or fail to persist.

### 5. Compatibility is forward-only and fail-closed

No existing events are rewritten. An old empty Design completion is ambiguous, and an old Prediction handoff is re-evaluated through the owner readiness/evidence contract. Rollback restores the former readers/writers; new Design failure events are additive and harmless to older readers. Critic project-binding migration is neither read nor written by this change.

## Risks / Trade-offs

- [Some existing Launcher runs stop earlier after upgrade] → This is intentional fail-closed behavior; expose the precise owning blocker and leave records unchanged for audit.
- [A legitimate feedback workflow uses pending Prediction records] → Scope readiness enforcement to Launcher-correlated production invocation validation; retain generic handoff persistence.
- [Failure receipt persistence itself fails] → Preserve the durable start and return the existing recovery ambiguity on later inspection; never claim the zero-result blocker without its receipt.
- [Record-status vocabulary evolves] → Keep battery classification, handoff declaration, and Launcher validation on one Prediction-owned public contract.
- [Strict Design path changes legacy behavior accidentally] → Characterize the existing list-returning route and make strict tool propagation opt-in only through the Launcher initial adapter.
- [Candidate files survive a failed invocation] → Treat them as non-authoritative diagnostics; formal Candidate Store and candidate-registration Evidence remain empty because staged effects never commit.

## Migration Plan

1. Characterize swallowed Route A tool failures, normal empty output, existing legacy route behavior, and current Prediction status/readiness semantics.
2. Implement strict Launcher initial Design outcome propagation plus correlated failure persistence and validation.
3. Extract and reuse the Prediction owner status/readiness contract, then enforce authoritative-record evidence in invocation validation.
4. Add service-level blocker-parity/no-downstream-call tests, update Launcher documentation, and run repository verification gates.
5. Deploy owner readers/writers together. Existing runs are not backfilled; rerun a project only through the normal approved workflow.

Rollback is code-only: restore the former validators and writers. Do not delete new events or edit historical runs. Any run that advanced under the stricter readers remains valid under the former behavior.
