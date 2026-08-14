## 1. Characterization and Contract Tests

- [x] 1.1 Add focused PubMed tests proving PMC text and abstract fallback preserve content through character 30,000 and discard content after character 30,000.
- [x] 1.2 Add focused LLM extraction tests that capture the supplied request content and prove later content up to character 30,000 reaches the model boundary while content after character 30,000 does not.
- [x] 1.3 Add verifier and prompt-contract tests for a whitespace-normalized real quote, a fabricated quote, concise field-addressed evidence, forced input PMID/source type, optional or missing evidence, and `design_insight` remaining an unverified inference.
- [x] 1.4 Add or extend a Design compatibility regression showing an existing-format binder without `source_evidence` still supplies its current `name` and `sequence` behavior.

## 2. Minimal Implementation Before Agent Integration

- [x] 2.1 Change only the three existing per-paper truncation values in `scripts/pubmed_search.py` and `scripts/llm_extract.py` from 8,000/5,000 to 30,000.
- [x] 2.2 Implement the small whitespace-normalized substring verifier and evidence annotation path in `scripts/llm_extract.py`, then run the focused verifier tests before connecting it to `extract_one_paper`.
- [x] 2.3 Update the extraction prompt to request only short verbatim passages, forbid domain-knowledge quotes, allow unsupported factual fields to be null or absent, and classify `design_insight` as inference without quote provenance.
- [x] 2.4 Connect the tested verifier to `extract_one_paper`, preserving every existing binder field while programmatically binding the top-level and evidence PMIDs plus evidence source types to the input paper.

## 3. Focused and Live Validation

- [x] 3.1 Run the new deterministic PubMed and LLM extraction tests after integration and record their results.
- [x] 3.2 Only after deterministic tests pass, run a single-paper smoke against the supplied OpenAI-compatible StepFun endpoint with credentials supplied through environment variables; inspect that output is concise and that program-side verification annotations agree with the supplied text without recording the key.

## 4. Regression and Completion Gates

- [x] 4.1 Run `test_threshold_research.py`, all newly added or existing LLM/Research-focused tests, `test_design.py`, and any directly affected Design compatibility test.
- [x] 4.2 Run the applicable broader unit suite, lint/type checks if configured for the touched Python files, and `scripts/architecture_gate.py`; document any unavailable gate rather than substituting a fabricated result.
- [x] 4.3 Run `openspec validate add-binder-source-evidence --strict` and `git diff --check`, then perform the required strict code review against `integration/data-integrity-transaction` without expanding the change scope.
- [x] 4.4 Confirm no user-facing documentation outside the OpenSpec artifacts requires synchronization, and report changed files, behavior and public data-format impact, tests added, commands/results, and remaining compatibility risk.
