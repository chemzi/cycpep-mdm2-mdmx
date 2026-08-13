## 1. Characterize the production display failure

- [x] 1.1 Add a public Workbench regression whose formal Candidate stores `metrics_json` as an object-valued JSON string and only `final_status`, and prove the current response loses metrics/status presentation.
- [x] 1.2 Add a production-shaped temporary real SQLite Store fixture using `commit_transaction()` and real Artifact bytes, with 14 Candidates and more than 100 Evidence and Artifact records, including committed Candidate-bound `prediction_recorded` Evidence, a verified Prediction record inventory, and a structure-bearing Artifact for C0006.
- [x] 1.3 Add fail-closed regressions for a missing, non-committed, transaction-mismatched, candidate-mismatched, or SHA-mismatched Prediction record/inventory without permitting identifier/path inference.

## 2. Implement the read-only Candidate science projection

- [x] 2.1 Introduce a focused Workbench-owned projection helper that normalizes Candidate status and object-valued JSON metrics without changing Store persistence or public Store methods.
- [x] 2.2 Resolve the latest committed Candidate status-owning Prediction event against an unfiltered project-scoped transaction snapshot and verify its record Artifact and inventory through injected Store/file integrity seams; keep the public transaction collection current-run scoped.
- [x] 2.3 Build one-pass per-request indexes and exact per-Candidate Evidence, Artifact, status-owning run, structure descriptors (formal ID/type/inventory role/content availability), shortlist, completeness, and limitation summaries before top-level collection limiting, while omitting all internal paths.
- [x] 2.4 Integrate the additive summary and formally associated Candidate trace into `frontend.workbench.v2` without changing existing collection shapes or `/api/v1` behavior.

## 3. Correct Candidate workspace rendering

- [x] 3.1 Extend the TypeScript domain/client parser for the optional additive Candidate association summary and retain compatibility with payloads that do not provide it.
- [x] 3.2 Update Candidate selectors and detail rendering to receive bounded Evidence/Artifact coverage and show normalized status/metrics, status-owning run relation, exact or explicitly returned association counts, and truncation-aware empty states, including compatibility payloads without the summary.
- [x] 3.3 Recognize formal structure-bearing Design/Prediction Artifact types and render recorded-structure availability separately from browser-safe `content_link` availability.
- [x] 3.4 Add component/browser regressions that render the C0006 production-shaped fixture and audit Candidate, Evidence, Artifact, shortlist, structure, and collection-coverage fields for false absence claims.

## 4. Verify and deploy the narrow change

- [x] 4.1 Run focused Python Workbench/API and TypeScript parser/component/browser tests.
- [x] 4.2 Run the full Python suite, frontend test/type/lint/build gates, Architecture Gate, strict OpenSpec validation, and `git diff --check`.
- [x] 4.3 Obtain high-reasoning implementation Spec and Standards reviews; resolve every P0/P1 while preserving the approved scope.
- [ ] 4.4 Deploy the reviewed feature commit to isolated server ports against a copied or read-only-bound successful Store and use the browser skill on the direct server port to verify the corrected fields without affecting the scientific runtime; repair and repeat gates if browser QA finds a defect.
- [ ] 4.5 After direct-port QA, obtain final high-reasoning P0/P1=0 confirmation, complete all tasks, strict validate and locally archive the OpenSpec change, then use `gh` to create/check/review/merge the PR to `integration` and report the merged commit and browser evidence.
