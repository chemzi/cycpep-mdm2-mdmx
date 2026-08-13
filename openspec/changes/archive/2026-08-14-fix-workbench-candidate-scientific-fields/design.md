## Context

See `proposal.md` for the production symptom. The current Workbench reader projects Candidate rows, Evidence, and Artifacts independently and applies the global collection limit after projection. Candidate payloads may retain `metrics_json` as a JSON string and may expose only `final_status`; Candidate rows also do not carry the Prediction run or Artifact inventory that produced that status. Formal `prediction_recorded` Evidence does carry the Candidate, transaction, run, and Prediction record Artifact identity, and the committed Prediction record declares its Artifact inventory. The browser currently ignores that formal join path and treats a missing item in the first 100 returned records as non-existence.

The change crosses the Python read model and the TypeScript Candidate workspace, but remains read-only. SQLite Store/Evidence and committed Artifact bytes remain authoritative. Internal filesystem paths may be used by the server to verify an Artifact, but may never be serialized to the browser.

## Goals / Non-Goals

**Goals:**

- Project an accurate Candidate status, metrics, status-owning run, formal Artifact associations, and structure availability from existing committed data.
- Preserve bounded top-level collections while giving each returned Candidate an exact, truncation-independent association summary.
- Fail closed when a claimed Prediction record or inventory cannot be verified, and explain the limitation without guessing.
- Exercise the public Workbench API and rendered Candidate workspace with a production-shaped 14-Candidate replay whose Evidence and Artifact collections exceed 100 records.

**Non-Goals:**

- No Store schema migration, backfill, Candidate mutation, or compatibility-projection read.
- No new Artifact content-serving endpoint; this change only distinguishes recorded structure metadata from browser-safe content availability.
- No changes to Launcher, E3, Planner, Orchestrator, Execution, Prediction, scientific protocol, approval, retry, or budget behavior.
- No repair of an operational Workbench `502`; the browser acceptance environment must first expose the existing API on a direct server port.

## Decisions

### 1. Build a read-only Candidate science projection before limiting collections

Introduce a focused Workbench-owned helper module that receives the Store-backed Candidate, Evidence, Artifact, and transaction records and produces:

- normalized Candidate metrics and status;
- the formal status-owning run relationship;
- a per-Candidate association summary containing exact Evidence and Artifact totals, associated opaque Artifact identities, structure-bearing identities, shortlist relationships, completeness, and typed limitations; and
- read-model-only Candidate trace fields on formally associated Artifact views.

`WorkbenchReader.read()` will build this index from all matching project records before applying `DEFAULT_COLLECTION_LIMIT` to top-level collections. Candidate projection uses an independent project-scoped transaction snapshot (no run filter), while the existing top-level transaction collection remains current-run scoped. The existing collection shapes and limit remain compatible; the additive Candidate summary prevents a bounded top-level window from silently changing scientific meaning.

Alternative rejected: raising the global limit. The completed smoke already produced hundreds of Artifacts, so a larger fixed limit merely moves the correctness failure.

Alternative rejected: changing `SQLiteStore.list()` or Candidate persistence. The normalization is presentation-specific, and changing the shared Store API would expand risk to scientific callers.

### 2. Join Candidate Artifacts through committed Prediction authority only

For a Candidate, the projector selects the latest committed Candidate-bound `prediction_recorded` Evidence using formal event order. It resolves that Evidence transaction through the project-scoped transaction snapshot, including for historical Candidates and when no current run exists. It requires:

1. a non-empty transaction and Prediction record Artifact identity;
2. a `COMMITTED` transaction that owns that Artifact;
3. a Store Artifact row whose transaction/task trace matches the Evidence;
4. Artifact bytes whose SHA-256 matches the Store row;
5. a Prediction record whose Candidate identity matches the Evidence Candidate; and
6. inventory Artifact identities that are registered in the same transaction and match their declared SHA-256.

The record Artifact and verified inventory become the Candidate's formal Artifact associations. The Candidate summary contains exact Evidence and Artifact totals, the status-owning run identity/relation, shortlist relationships, completeness/limitations, and compact structure descriptors containing `artifact_id`, Store `artifact_type`, formal inventory `role`, and supported `content_link` availability. Any failed condition produces a typed read-model limitation and no guessed association. The helper reads the committed Artifact internally through an injected file reader/hasher seam so tests remain deterministic; serialized views still omit paths.

Alternative rejected: infer association from Artifact ID prefixes, filenames, directories, sequence, or timestamps. Those values are not formal Candidate authority.

Alternative rejected: copy `candidate_id` into every persisted Artifact. That requires a schema/data migration and duplicates authority already recorded transactionally.

### 3. Normalize only representations already present in formal Candidate data

When `metrics` is an object it remains authoritative. Otherwise an object-valued JSON string in `metrics_json` is decoded. Malformed or non-object JSON is not surfaced as metrics and produces a typed Candidate limitation. Public `status` prefers a non-empty stored status and otherwise falls back to the committed `final_status`; both original fields remain available for compatibility.

No scientific value is synthesized, recalculated, or converted to a pass. This is representation normalization at the browser boundary.

### 4. Treat structure availability as two independent facts

The server marks a Candidate-associated Artifact as structure-bearing from formal Store `artifact_type` values: `design_pdb`, `prediction_input:global.post_relax_pdb`, `prediction_input:global.design_reference_pdb`, and any other `prediction_input:<role>` whose formal inventory role ends in `.pdb`. Prediction inventory role is retained in the compact structure descriptor; the classifier never uses a filesystem suffix or assumes Store persists a separate role column. The UI reports that a structure Artifact is recorded even when it has no `content_link`, and separately reports whether browser-safe content is available.

Alternative rejected: construct a URL from an opaque Artifact ID or expose the internal path. Content delivery is a separate capability and security boundary.

### 5. Make browser rendering consume the Candidate summary, not only the returned global slices

TypeScript parsing gains optional additive Candidate association fields. Existing payloads without them remain valid and keep the legacy returned-window behavior, but `CandidateWorkspace` receives the bounded Evidence and Artifact collection coverage as well as their items, labels those counts as returned, and respects truncation. When the summary is present, Candidate detail uses its exact totals, formal run relation, structure descriptors, shortlist relationships, and limitations. Empty-state text is qualified as complete, truncated, or unavailable rather than making an unqualified absence claim.

### 6. Verify through the public API and a direct server port

Python tests will exercise `WorkbenchReader` and the public `/api/v2/workbench` envelope. TypeScript tests will exercise `parseWorkbenchEnvelope` and render the Candidate workspace. The production-shaped fixture uses a temporary real `SQLiteStore.commit_transaction()` and real temporary Artifact bytes, includes 14 Candidates, more than 100 Evidence records, more than 100 Artifacts, and a C0006-like Candidate with string metrics, final status, committed Prediction Evidence, and a structure-bearing inventory Artifact. Small `FakeStore` tests remain for isolated malformed-boundary cases.

After deterministic gates pass, the feature branch will be deployed in an isolated frontend/API process that reads a copied or read-only-bound successful Store. Browser QA will navigate directly to the server port, not a domain, and inspect Candidate, Evidence, Artifact, structure, shortlist, and truncation fields. The active scientific runtime and authoritative Store will not be mutated.

## Risks / Trade-offs

- [Reading committed record bytes adds I/O to the Workbench request] → Build one-pass Evidence/Artifact/transaction indexes and verify each referenced Artifact identity at most once per request. Do not scan Artifact directories or add a persistent cache.
- [Older Candidates may lack transactional Prediction Evidence] → Preserve `unlinked` and expose no invented association; compatibility remains explicit.
- [A malformed record could make the entire Workbench unavailable] → Contain integrity failure to that Candidate as a typed limitation; only unexpected Store/API failures remain request failures.
- [Per-Candidate summaries add response size] → Include opaque identities and counts only, not duplicate full Evidence/Artifact objects or file contents.
- [Current direct-port deployment returns HTTP 502] → Treat API/store binding health as an acceptance precondition and diagnose it operationally without broadening this field-correction change.

## Migration Plan

1. Add characterization regressions and the read-only projection helper.
2. Add the backward-compatible Workbench response fields and frontend parser support.
3. Update Candidate rendering and run focused, full, type/build, Architecture Gate, strict OpenSpec, and diff checks.
4. Deploy the reviewed branch to isolated server ports against read-only successful-run data and perform browser QA; fix and repeat deterministic/high review gates if QA finds a defect.
5. After browser QA and final P0/P1=0 reviews, archive the change, create/check/merge the PR, and roll back if needed by reverting the additive read-model/UI commit. No persisted data or schema rollback is required.
