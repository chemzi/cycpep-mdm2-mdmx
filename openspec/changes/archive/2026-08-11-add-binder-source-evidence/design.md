## Context

See `proposal.md` for motivation and `specs/research/binder-literature-extraction/spec.md` for observable requirements. The current path truncates PMC body text to 8,000 characters, truncates abstract fallback to 8,000, and truncates the selected paper content again to 5,000 before a single LLM call. `extract_one_paper` already forces the result PMID from the input paper. `scripts/threshold_research.py::_normalize_evidence` establishes the repository pattern of collapsing whitespace, stripping, and case-folding before substring verification.

The current binder output is a plain additive JSON object consumed through `known_binders` / `known_dual_binders`; Design reads existing properties such as `name` and `sequence` and does not require a provenance schema migration.

## Goals / Non-Goals

**Goals:**

- Apply one 30,000-character constant value at the three existing binder truncation points without changing the one-paper/one-call flow.
- Constrain the model to concise, field-addressed, verbatim evidence and validate every returned quote locally against exactly the truncated content sent for that PMID.
- Add provenance as optional nested data while preserving program ownership of PMID and source type.
- Establish deterministic tests of truncation, prompt/output processing, verification, and compatibility before a live StepFun smoke test is used to observe model behavior.

**Non-Goals:**

- No chunking, retrieval, embeddings, vector database, retry call, or additional LLM call.
- No Research architecture refactor, shared Evidence subsystem, or persistence migration.
- No threshold calibration, threshold default, Prediction, Planner, Launcher, Store/Transaction, or Design route change.
- No attempt to make an inferred `design_insight` source-verifiable.

## Decisions

### Keep provenance inside the existing binder object

`source_evidence` remains an optional list on each extracted binder. Each normalized entry has `field`, `quote`, `pmid`, `source_type`, and `quote_verified`. This is an additive extension at the point where the claim already lives and does not introduce a second Evidence lifecycle.

Alternative considered: writing binder quotes into the formal Evidence store. Rejected because it expands transaction and architecture scope and is unnecessary for the requested local provenance.

### Validate against the exact LLM input slice

The extraction function will retain the 30,000-character `content` slice used to build the request and use that same value for verification after JSON parsing. Program-side normalization will follow the threshold-research behavior: collapse all whitespace runs to a single space, trim, and case-fold, then require the normalized quote to be a substring of normalized content. The verifier will annotate both successful and unsuccessful LLM evidence instead of silently promoting or dropping an unverified claim.

This tolerates line wrapping and capitalization differences while still requiring the LLM's words and values to occur contiguously in the supplied paper. It does not add fuzzy semantic matching, punctuation rewriting, or token similarity that could accept invented evidence.

Alternative considered: exact raw-string matching. Rejected because XML paragraph extraction and model output can change harmless whitespace. Fuzzy matching was rejected because it weakens the provenance guarantee.

The one-line whitespace normalizer deliberately remains local even though it mirrors `threshold_research.py`. Importing that script's private helper would couple the two flows, while extracting a shared validation or Evidence utility would modify the threshold path and expand this approved change. This bounded duplication is therefore accepted for this change and can only be revisited in a separately approved refactor.

### Keep provenance ownership outside the model

The prompt asks the model only for a short `field` and `quote`. Regardless of any model-provided provenance keys, the program overwrites the top-level PMID and emits the input paper's PMID and `source_type` on every evidence entry. This extends the existing forced-PMID rule and avoids trusting the model for identity.

### Make inference semantics explicit in the prompt and verifier contract

The prompt will state that `design_insight` is a model inference, must be concise, and must not receive a `source_evidence` quote. The verifier only evaluates returned evidence quotes; it does not require `design_insight` to be verified. No extra model call or new top-level inference field is needed.

### Test deterministic behavior before live model behavior

First add tests that mock the model boundary and cover the exact 30,000-character request slice, verified and fabricated quotes, forced PMID/source type, concise prompt constraints, and absence of provenance on legacy binders. Only after those tests pass should the existing agent call path be wired to the verifier and exercised with the supplied StepFun-compatible endpoint. The API key stays in the process environment and is never committed or printed.

The live smoke is observational because hosted model output can vary; deterministic mocked tests remain the regression gate.

## Risks / Trade-offs

- [Risk] A 30,000-character input increases token usage and may approach a selected model's context limit. → Keep one bounded call, preserve the existing model selection, and fail through the current API error path rather than adding retries or chunking.
- [Risk] Whitespace-normalized substring matching can reject a substantively correct paraphrase. → This is intentional: provenance requires a quote, while unsupported fields may remain null or absent.
- [Risk] Case-folding is slightly broader than byte-for-byte quotation. → It matches the existing threshold verification pattern and retains contiguous source-text matching; tests will cover whitespace robustness and fabricated text rejection.
- [Risk] Existing consumers may serialize or ignore unknown fields differently. → Keep `source_evidence` optional and run current Research/threshold/Design regressions, including legacy binders without the new field.

## Migration Plan

No stored-data migration is required. Deploy the additive output and prompt/verifier changes together. Rollback consists of reverting the three limits and optional output processing; binders already containing `source_evidence` remain readable by consumers that ignore unknown fields.
