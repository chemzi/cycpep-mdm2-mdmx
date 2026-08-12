## Purpose

Defines an immutable, auditable decision that converts explicitly scoped Prediction and Exploration Evidence into one constrained next-round length-allocation recommendation without granting scientific clearance or executing the recommendation.

## ADDED Requirements

### Requirement: Immutable ExplorationDecision contract
The system SHALL expose an immutable, versioned `ExplorationDecision` contract containing at least: `schema_version`, `decision_id`, `decision_input_digest`, `project_id`, `workflow_id`, workflow `run_id`, `source_round`, `applies_to_round`, `prediction_run_id`, current candidate/handoff scope, `target_ids`, `source_event_ids`, `shortlist_event_id`, `failure_summary`, `adjustment`, `evidence_support`, `policy_envelope_digest`, `threshold_digest`, `protocol_identity`, `decision_status`, and `reason`. `applies_to_round` SHALL equal `source_round + 1`. The only valid statuses SHALL be `adjustment` and `no_adjustment`.

#### Scenario: Valid Decision is immutable and complete
- **WHEN** a Decision is built from valid scoped inputs
- **THEN** every required field is present, collection fields cannot be mutated through the contract, and serializing then restoring the contract preserves the complete adjustment, reason, and provenance

#### Scenario: Invalid Decision fails closed
- **WHEN** a caller supplies an invalid status, a non-next target round, an empty identity field, an adjustment outside the policy envelope, or internally inconsistent provenance
- **THEN** contract construction fails and no valid Decision is returned

### Requirement: Deterministic Decision identity
The system SHALL derive `decision_input_digest` with the repository's canonical object-digest facility from normalized decision inputs comprising the selected source Evidence semantics, shortlist semantics, approved policy envelope, threshold snapshot identity, and protocol identity. `decision_id` SHALL be derived solely from that digest and SHALL NOT depend on time, randomness, an event append identifier, or free-form model output.

#### Scenario: Same formal inputs reproduce identity
- **WHEN** the same source Evidence, shortlist, approved envelope, threshold snapshot, and protocol identity are provided in a different input order
- **THEN** the Decision content, `decision_input_digest`, and `decision_id` are identical

#### Scenario: Source Evidence semantics change
- **WHEN** a decision-relevant field in selected source Evidence changes while other inputs remain fixed
- **THEN** `decision_input_digest` and `decision_id` change

#### Scenario: Unrelated history changes
- **WHEN** historical Evidence outside the declared current prediction run and candidate scope is added, removed, or changed
- **THEN** the current-round Decision content and identity do not change

### Requirement: Approved length policy envelope
The system SHALL interpret each selected target's approved `design.lengths` as that target's allowed length set. A multi-target Decision SHALL use only lengths approved for every selected target, record the normalized per-target envelope and effective allowed set in its policy support, and bind them through `policy_envelope_digest`. The Decision SHALL only preserve or narrow this effective set; it SHALL never introduce another length. `baseline_policy_weights` and `proposed_policy_weights` SHALL be relative integer weights, not actual prior-round sampling counts, proposal counts, or an E3 execution budget.

#### Scenario: MDM2 and MDMX approved lengths constrain output
- **WHEN** each selected target is approved for lengths `[8, 10, 12]`
- **THEN** `baseline_policy_weights` contain only `8`, `10`, and `12`, and `proposed_policy_weights` and preferred lengths contain no other value

#### Scenario: Multi-target envelopes differ
- **WHEN** selected targets have different approved length sets with a non-empty intersection
- **THEN** the Decision can use only their intersection and records each target's original allowed set

#### Scenario: Project is not approved or has no common allowed length
- **WHEN** project approval is absent/inconsistent or selected target envelopes have no common allowed length
- **THEN** Decision creation fails closed

### Requirement: One adaptive knob with conservative policy
The only adaptive field the Decision SHALL change is peptide length allocation expressed as relative policy weights. The current policy SHALL compare allowed lengths having at least five current-scope evaluations; it SHALL produce `adjustment` only when a worst length has failure rate at least 70 percent and a better length has failure rate at most 30 percent. The adjustment SHALL record baseline and proposed policy weights, preferred lengths, deterministic reason text, per-length support counts/rates, and the source Evidence identifiers. Ties SHALL resolve deterministically by normalized length order without changing the sample or failure-rate thresholds.

#### Scenario: Insufficient evidence is auditable
- **WHEN** fewer than two allowed lengths meet the minimum sample requirement or the failure-rate boundary is not met
- **THEN** the system returns a deterministic `no_adjustment` Decision whose proposed policy weights equal its baseline policy weights and whose reason/support explain why no change was justified

#### Scenario: Sufficient evidence narrows allocation
- **WHEN** at least five evaluations at length 8 have a failure rate of at least 70 percent and at least five evaluations at length 12 have a failure rate of at most 30 percent
- **THEN** the system returns an `adjustment` Decision preferring length 12, removes length 8 from the proposed allocation, and records the compared statistics and source Evidence

#### Scenario: No other adaptive parameter changes
- **WHEN** any Decision is built
- **THEN** it contains no adjustment for targets, routes, model parameters, thresholds, calibration, Prediction policy, Planner budget, or scientific strategy text

### Requirement: Explicit current-round Evidence scope
Decision creation SHALL require an explicit `prediction_run_id`, workflow trace binding, `source_round`, current candidate identifiers, and current Prediction handoff identity. Exactly one primary `battery_evaluated` event SHALL exist for every candidate in the bound current handoff: the battery candidate-ID set SHALL equal the handoff candidate-ID set. Primary Evidence SHALL match the declared project/workflow/workflow-run, prediction run, target scope, and protocol identity. The referenced shortlist SHALL match `source_round` and SHALL consume exactly the selected battery event identifiers. A missing, extra, duplicate, cross-run, cross-candidate, or conflicting scope SHALL fail closed.

#### Scenario: Current prediction run and handoff match
- **WHEN** every selected battery event belongs to the declared prediction run and current candidate/handoff scope and the shortlist names exactly those events
- **THEN** only those events contribute to failure statistics and Decision identity

#### Scenario: Prediction run or candidate scope mismatch
- **WHEN** battery candidate IDs are not exactly equal to handoff candidate IDs, any candidate has zero or multiple current verdicts, or any selected event names another `prediction_run_id`
- **THEN** Decision creation fails closed

#### Scenario: Historical evidence is not an implicit input
- **WHEN** the formal ledger contains older matching-target battery events not listed by the current shortlist
- **THEN** those events do not contribute to statistics or identity

### Requirement: Shortlist and scientific pass remain distinct
The system SHALL accept a valid current-round exploration shortlist even when zero of N candidates scientifically passed. It SHALL preserve every shortlist item's original `passed` value and SHALL NOT modify pass fields, threshold values, Prediction records, or shortlist Evidence.

#### Scenario: Zero hard-pass shortlist yields a Decision
- **WHEN** a valid shortlist is produced from current-scope Evidence with `n_passed = 0` and all shortlist items have `passed = false`
- **THEN** Decision creation succeeds if all other inputs are valid and every shortlist item's `passed` value remains false

#### Scenario: Decision cannot grant scientific clearance
- **WHEN** a Decision recommends a preferred length or returns `no_adjustment`
- **THEN** neither the Decision nor its Evidence represents any candidate as scientifically passed or changes a threshold

### Requirement: Formal exploration_decision Evidence
After a Decision validates, the system SHALL be able to append one `exploration_decision` event through the existing Store-backed Evidence authority. Before append, every `source_event_id` and the `shortlist_event_id` SHALL resolve to matching formal Evidence. The event SHALL carry the complete serialized Decision, including baseline/proposed policy weights, reason, support statistics, policy/threshold/protocol identity, decision status, and provenance, while project/workflow/run/round/targets use the canonical Evidence envelope. Sequential writer retries with the same `decision_id` and identical canonical Decision payload SHALL return/reuse the existing formal event without appending a duplicate. The same `decision_id` with a different canonical payload SHALL fail closed. A failed validation or append SHALL NOT create a completion claim elsewhere.

#### Scenario: Formal Evidence recovers the Decision
- **WHEN** a valid Decision is appended successfully
- **THEN** reading the `exploration_decision` event is sufficient to restore and revalidate the complete Decision without a JSON sidecar or State projection

#### Scenario: Referenced source event is absent
- **WHEN** any referenced source or shortlist event does not exist in formal Evidence or does not match the Decision-bound semantics
- **THEN** append fails and no `exploration_decision` event is written

#### Scenario: Sequential retry reuses formal event
- **WHEN** the writer is called twice sequentially with the same `decision_id` and identical canonical Decision payload
- **THEN** the second call returns the first formal event identifier and no duplicate event is appended

#### Scenario: Decision identity collides with different payload
- **WHEN** formal Evidence already contains a `decision_id` but its canonical Decision payload differs from the new payload
- **THEN** the writer fails closed and appends no event

#### Scenario: Evidence append fails
- **WHEN** the formal Store rejects the append
- **THEN** the caller receives the failure and no State or file is treated as Decision authority

### Requirement: E2 stops before planning or execution
Decision construction and persistence SHALL be side-effect free with respect to Design, Planner, Orchestrator, Execution, CandidateIndex, State, thresholds, and Prediction artifacts. The capability SHALL expose no Planner action, execution handler, automatic consumer, or next-round launch.

#### Scenario: Decision creation has no downstream invocation
- **WHEN** a Decision is built or recorded
- **THEN** Design and Planner entry points are not called, no execution task is registered, and no candidate generation or next-round execution begins
