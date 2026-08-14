## Why

Research binder extraction currently discards usable later-paper text at 5,000 or 8,000 characters and can only trace extracted fields to a PMID, not to a verifiable passage in that paper. This change raises the single-paper text budget and adds quote-level provenance without changing the Research architecture or downstream Design behavior.

## What Changes

- Raise all three binder-literature per-paper truncation limits in `scripts/pubmed_search.py` and `scripts/llm_extract.py` to 30,000 characters.
- Extend the binder LLM contract with optional `source_evidence` entries for extracted factual fields, with short verbatim quotes from the supplied paper text.
- Verify each returned quote against the current PMID's supplied content using whitespace-normalized substring matching, and annotate evidence with the program-bound PMID, source type, and verification result.
- Keep `design_insight` explicitly classified as model inference rather than paper quotation.
- Preserve all existing binder fields and compatibility with legacy binders that omit `source_evidence`.
- Add characterization and regression tests before exercising the extraction agent against the supplied OpenAI-compatible StepFun endpoint.
- Do not add chunking, RAG, vector storage, extra LLM calls, dependencies, a second Evidence system, threshold changes, or changes to Prediction, Planner, Launcher, Store/Transaction, or Design routing.

## Capabilities

### New Capabilities

- `research/binder-literature-extraction`: Defines the per-paper input budget, quote provenance contract, program-side quote verification, inference labeling, and legacy compatibility for Research binder extraction.

### Modified Capabilities

None.

## Impact

- Affected implementation: `scripts/pubmed_search.py` and `scripts/llm_extract.py` only, plus narrowly related tests.
- Public function signatures remain unchanged. The binder JSON format gains an optional additive `source_evidence` field; no migration is required because existing consumers continue reading the existing fields.
- The LLM receives up to 30,000 characters per paper in the existing single-call flow. No new runtime dependency or additional model call is introduced.
- Existing threshold evidence remains the sole reference pattern for whitespace-normalized quote verification; this change does not alter threshold calibration or create a parallel persisted Evidence subsystem.
