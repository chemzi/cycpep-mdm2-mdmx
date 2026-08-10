## Purpose

Defines the behavior contract for building RFdiffusion binder-first contigs from PDB receptors that may contain unmodeled residue gaps.

## ADDED Requirements

### Requirement: Receptor contigs never reference unmodeled residues
The system SHALL emit the fixed receptor of a binder-first contig as one range per modeled residue segment. Any gap > 1 between consecutive modeled residue numbers SHALL split the receptor into separate segments joined by `/` on the same output chain, followed by the `/0` chain break.

#### Scenario: Fully modeled receptor produces a single segment
- **WHEN** the receptor chain has consecutive residue numbers with no gap
- **THEN** the contig is identical to the legacy single-segment form (`10-10 A25-109/0`)

#### Scenario: Unmodeled loop splits the receptor into segments
- **WHEN** the receptor chain omits residues (e.g. residue 69 of CXCR4 22XC)
- **THEN** the contig lists only modeled ranges (`10-10 C26-68/C70-228/C235-306/0`) and never names the missing residues

### Requirement: Hotspot-constrained windows keep single-segment validation
When binding-site hotspot residues are provided, the system SHALL validate that every hotspot exists in the PDB and lies within a single contiguous segment, and SHALL use that validated single segment as the receptor window (legacy `_pdb_residue_range` semantics).

#### Scenario: Hotspot inside an unmodeled gap is rejected
- **WHEN** a hotspot residue number is absent from the PDB
- **THEN** the system raises `ValueError` and does not generate a contig

### Requirement: Invalid segment inputs are rejected
The system SHALL reject an empty residue set, a reversed segment, an invalid chain id, or an out-of-range binder length with `ValueError`.
