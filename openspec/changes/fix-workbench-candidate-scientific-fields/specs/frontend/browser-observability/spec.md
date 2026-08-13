## MODIFIED Requirements

### Requirement: Browser observability uses a versioned read model
The system SHALL expose a versioned read-only workbench response for the current project containing project identity, current workflow and run identity, tasks, typed actions, executions and transactions, candidates, evidence, artifacts, protocol provenance, trace identifiers, and blockers when those records exist.

The `project` view SHALL be current-project scoped. The `workflow`, `run`, `tasks`, `executions`, and `transactions` views SHALL be scoped only to the validated current workflow/run. Project-scoped `candidates`, `evidence`, and `artifacts` SHALL preserve their available formal trace linkage and SHALL distinguish current-run records from historical-run or unlinked project records. Candidate views SHALL normalize committed status and JSON-encoded metrics into their public typed fields. Every bounded collection SHALL report `total` as the number of matching formal records before limiting, `returned` as the number of returned items, and `truncated` as whether `returned` is less than `total`; Candidate-specific association projections SHALL also report whether their Evidence, Artifact, or shortlist results are complete or limited by the returned window.

#### Scenario: A current run exists
- **WHEN** a browser requests the Frontend V2 workbench read model for a project with a current Orchestrator run
- **THEN** the response identifies the project, workflow, run, tasks, actions, observable executions and transactions, candidates, evidence, artifacts, protocol bindings, trace relationships, and blockers using opaque identifiers

#### Scenario: No current run exists
- **WHEN** a browser requests the workbench read model for a valid current project that has no current Orchestrator run
- **THEN** the response returns the project and available Store-backed collections with an explicit no-current-run state and does not synthesize a workflow stage

#### Scenario: Project history spans multiple runs
- **WHEN** project-scoped candidates, evidence, or artifacts include records from the current run, historical runs, or records without a formal run link
- **THEN** each record preserves its available trace identifiers and is identified as current-run, historical-run, or unlinked without being merged into current workflow/run state

#### Scenario: Candidate metrics are JSON encoded in formal storage
- **WHEN** a committed Candidate contains object-valued metrics encoded in its Store payload and a final scientific status
- **THEN** the Candidate view returns typed metrics and a non-empty status presentation without changing the stored Candidate

#### Scenario: A bounded collection reaches its response limit
- **WHEN** a bounded workbench collection has more matching formal records than its response limit
- **THEN** `total` reports the pre-limit count, `returned` reports the item count in the response, `truncated` is true exactly when `returned` is less than `total`, and Candidate detail does not equate omitted associations with non-existence

### Requirement: Formal Store data remains authoritative
The read model SHALL obtain candidates, evidence, artifact metadata, recorded transaction metadata, and any Candidate-to-Artifact projection through the formal Store seam and committed Artifact contract. It SHALL NOT read JSON, CSV, or JSONL compatibility projections as an independent authority. A committed Prediction record Artifact MAY be read only after its Store identity and SHA-256 are verified, and only to project its declared Candidate identity and committed artifact inventory.

#### Scenario: Projection disagrees with SQLite
- **WHEN** a compatibility projection differs from formal Store data
- **THEN** the browser response reflects the Store and does not merge or reverse-synchronize projection content

#### Scenario: Browser requests observability data
- **WHEN** the browser obtains the workbench response
- **THEN** it receives serialized domain views rather than database access, SQL details, table names, raw persistence rows, or server paths

#### Scenario: Prediction record integrity cannot be established
- **WHEN** a Candidate-bound Prediction record reference is absent, does not identify a committed Artifact in the same formal transaction, or its bytes do not match the Store SHA-256
- **THEN** the read model does not associate that record or its inventory with the Candidate and returns a structured integrity limitation without guessing from an identifier or path

### Requirement: Provenance and artifacts are safe browser contracts
The response SHALL expose protocol identity, trace identifiers, Evidence relationships, Candidate associations, and Artifact metadata needed for provenance while treating server paths as internal and Artifact identifiers as opaque. Candidate-to-Artifact association SHALL follow an explicit Candidate-bound formal Evidence reference and a verified committed Prediction record inventory; it SHALL NOT be inferred from Artifact identifier prefixes, paths, sequences, timestamps, or display text.

#### Scenario: Artifact metadata contains an internal path
- **WHEN** a formal Artifact record includes a server filesystem path
- **THEN** the response omits the path and exposes only opaque identity, type, provenance, integrity metadata, producer identity, Candidate identity when formally established, and an explicitly supported content link if one exists

#### Scenario: Protocol-bound scientific output is shown
- **WHEN** a task, Artifact, Candidate, or Evidence record carries a protocol binding
- **THEN** the response preserves its protocol name, version, and required integrity identity without substituting the currently active protocol

#### Scenario: Evidence is correlated
- **WHEN** Evidence carries workflow, run, task, attempt, transaction, Candidate, Artifact, or parent-event trace fields
- **THEN** the response preserves those identifiers so the browser can present provenance without parsing message text

#### Scenario: Candidate-bound Prediction record is committed
- **WHEN** committed Candidate-bound Evidence names a Prediction record Artifact whose verified inventory names other committed Artifacts
- **THEN** the response associates the record and inventory Artifacts with that Candidate and preserves each Artifact's own formal run and transaction trace

#### Scenario: Exploration shortlist evidence is presented
- **WHEN** an `exploration_shortlist` evidence event is returned to the browser
- **THEN** its `k`, `n_evaluated`, `n_passed`, `shortlist`, `calibration`, `source_event_ids`, and additive `unmapped_metrics` fields are preserved so the browser can render the scientific shortlist, while payload fields from other Evidence event types are not generically exposed
