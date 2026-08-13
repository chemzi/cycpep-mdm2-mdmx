## Purpose

Defines the formal production edge that turns completed current-round Prediction and Critic evidence into the immutable ExplorationDecision required for deterministic closed-loop planning.

## ADDED Requirements

### Requirement: Closed-loop workflow publishes the current formal Decision before Planner
After the current Prediction and Critic boundaries are formally complete, a workflow whose Critic recommendations map through the Planner's public classifier to iterate-design SHALL publish one exploration shortlist and one ExplorationDecision before invoking Planner. Fresh coordination and resume from an already completed Critic MUST traverse the same publication boundary. Both publications MUST derive exclusively from the owner-validated current Prediction boundary, formal Prediction handoff, complete battery evidence, Critic scope, approved project configuration, and threshold snapshot already bound by that Prediction invocation.

#### Scenario: Current evidence produces a Decision
- **WHEN** the current Prediction and Critic are complete, the Critic requires iterate-design, and all formal source evidence and bindings are valid
- **THEN** exactly one current-round ExplorationDecision is formally available before Planner is invoked

#### Scenario: Non-iterative plan does not invent a Decision requirement
- **WHEN** the current Critic recommendations do not map to iterate-design
- **THEN** workflow composition does not require or synthesize an ExplorationDecision for that plan

#### Scenario: Resume from completed Critic publishes before Planner
- **WHEN** Launcher resumes with the current Prediction and Critic complete but Planner not started
- **THEN** the unified Planner-resolution path publishes or reuses the required current Decision before invoking Planner

### Requirement: Publication reuses the existing scientific and contract owners
Workflow composition SHALL use the existing exploration shortlist policy, ExplorationDecision builder, dedicated formal writers, and E3-C recommendation-to-action classifier. It MUST NOT copy their ranking, adjustment, policy-envelope, threshold, provenance, canonical identity, or iteration-requirement rules into Launcher, Planner, or a second implementation.

#### Scenario: Scientific policy remains owner-defined
- **WHEN** a Decision is published by the production workflow
- **THEN** its shortlist, adjustment, identity, and provenance validate through the existing public E2 contracts without a workflow-local policy table

### Requirement: Publication is deterministic and resume-safe
Before appending, the publication boundary SHALL inspect the project-scoped Store for the exact current workflow, Prediction run, source round, and canonical source set. An exact canonical shortlist or Decision publication SHALL be reused. Missing publication SHALL be appended once through its owner. Conflicting, duplicate, malformed, stale-revision, or source-incomplete publication MUST fail closed before Planner and MUST NOT be resolved by latest-event selection.

#### Scenario: Resume after shortlist publication
- **WHEN** a prior invocation appended the exact current shortlist but did not append the Decision
- **THEN** resume reuses that shortlist and appends the one canonical Decision without duplicating the shortlist

#### Scenario: Replay after complete publication
- **WHEN** the exact shortlist and Decision are already formally present
- **THEN** replay reuses both event identities and invokes Planner with the same canonical Decision

#### Scenario: Conflicting current publication
- **WHEN** the Store contains multiple or non-canonical current shortlist or Decision publications
- **THEN** workflow composition blocks before Planner without selecting the newest row or writing a replacement

### Requirement: Publication preserves formal authority boundaries
The publication edge and every builder/source-validation/writer operation SHALL use the same explicitly injected project-scoped Store and approved project revision as the Launcher runtime. The Store project identity MUST match the approved project. The threshold snapshot MUST come from `inputs/thresholds.json` adjacent to the handoff path in the already owner-validated current Prediction boundary and MUST match its formal threshold digest. Filesystem artifacts MAY be read only through that validated locator and MUST NOT be treated as a replacement for formal Store evidence. The authoritative SQLite append need not create a JSONL projection; legacy writer calls without explicit Store retain existing projection behavior. Existing failed invocations remain immutable.

#### Scenario: Approved project revision drift
- **WHEN** the current approved project revision differs from the Decision policy envelope or validated Prediction inputs
- **THEN** publication fails closed without appending a shortlist or Decision

#### Scenario: Source evidence is incomplete
- **WHEN** any current handoff, battery, Critic scope, threshold snapshot, or provenance binding required by the E2 contract is missing or invalid
- **THEN** publication fails closed without invoking Planner or falling back to ambient experience
