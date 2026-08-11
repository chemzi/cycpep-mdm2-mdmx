## Purpose

Defines bounded single-paper text extraction and verifiable quote provenance for Research binder facts while preserving legacy binder and Design compatibility.

## ADDED Requirements

### Requirement: Binder extraction uses a 30,000-character paper budget
The system SHALL make at most the first 30,000 characters of each PMC full text or PubMed abstract available to the existing single-paper binder LLM extraction call, with the same limit applied at every current truncation boundary.

#### Scenario: Later full-text content remains available
- **WHEN** a PMC paper contains relevant content after character 8,000 and no later than character 30,000
- **THEN** that content is included in the paper content supplied to binder extraction

#### Scenario: Content beyond the budget is excluded
- **WHEN** a paper contains more than 30,000 characters of usable text
- **THEN** binder extraction receives no characters after the first 30,000

#### Scenario: Abstract fallback uses the same budget
- **WHEN** PMC full text is unavailable and a PubMed abstract is used
- **THEN** the abstract is limited to the first 30,000 characters

### Requirement: Factual binder fields may carry short source evidence
For each factual binder field supported by the extraction prompt, the LLM output MAY include a `source_evidence` entry containing the field path and one short, critical quote copied verbatim from the supplied text for that paper. The prompt SHALL forbid quotes derived from domain knowledge, SHALL allow factual fields without direct evidence to be null or absent, and SHALL forbid presenting `design_insight` as a paper quote.

#### Scenario: Direct evidence is available
- **WHEN** the supplied paper text directly supports an extracted sequence, target affinity, or key-residue claim
- **THEN** the LLM is instructed to return only a short supporting passage with the corresponding field path

#### Scenario: Direct evidence is unavailable
- **WHEN** the supplied paper text does not directly support a factual field
- **THEN** the LLM is permitted to leave that field null or absent rather than fabricate a quote

#### Scenario: Design insight is inferred
- **WHEN** the LLM returns `design_insight`
- **THEN** the value is treated as model inference and is not required or permitted to masquerade as verified source quotation

### Requirement: Quote verification is bound to the current paper input
The program SHALL verify each returned evidence quote by whitespace-normalized substring matching against the exact content supplied for the current paper. Each retained evidence entry SHALL contain its field path, quote, program-bound PMID, source type, and `quote_verified`; only quotes that match the current paper content SHALL have `quote_verified` equal to true.

#### Scenario: Exact source passage with whitespace differences
- **WHEN** an evidence quote occurs in the current paper content after applying the established whitespace normalization
- **THEN** the evidence is annotated with the input PMID, the input source type, and `quote_verified: true`

#### Scenario: Fabricated quote
- **WHEN** an evidence quote does not occur in the current paper content after normalization
- **THEN** the evidence is annotated with `quote_verified: false`

#### Scenario: LLM attempts to rewrite provenance
- **WHEN** the LLM returns a different PMID or source type
- **THEN** every evidence entry and the binder result use the PMID and source type bound by the program to the current input paper

### Requirement: Binder output remains backward compatible
The system SHALL preserve all existing binder fields and SHALL treat `source_evidence` as an optional additive field so that legacy binders without it remain usable by current Research and Design consumers.

#### Scenario: Legacy binder reaches Design
- **WHEN** `known_dual_binders` contains an existing-format binder with `name` and `sequence` but no `source_evidence`
- **THEN** current Design binder selection and sequence handling continue without error

#### Scenario: Provenance-enriched binder reaches Design
- **WHEN** `known_dual_binders` contains the existing fields plus `source_evidence`
- **THEN** current Design behavior continues to read the existing name and sequence fields without route changes
