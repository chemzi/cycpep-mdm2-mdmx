## Why

The completed E3 smoke produced formal Candidate metrics, Prediction records, Evidence, and 808 committed Artifacts, yet Frontend V2 rendered candidates such as C0006 as `unlinked`, `Status: Unavailable`, `No metrics returned`, and `0 artifacts`. The read model is losing or truncating formal scientific relationships at the presentation boundary, making valid production results look absent immediately before the project demonstration.

## What Changes

- Normalize Store-backed Candidate status and JSON-encoded metrics in the Workbench read model so committed scientific fields are returned instead of displayed as unavailable.
- Resolve a Candidate's status-owning run and committed Prediction Artifact relationships only from formal Candidate-bound Evidence and the hash-validated Prediction record Artifact; do not infer linkage from Artifact IDs, paths, sequences, or timestamps.
- Present recorded structure-bearing Artifact types honestly, distinguishing a formally recorded structure from browser-viewable content.
- Make Candidate association counts and shortlist/structure empty states explicitly reflect bounded-response truncation, so “not returned” is never presented as “does not exist.”
- Add a production-shaped browser replay covering 14 Candidates, more than 100 Evidence records, and more than 100 Artifacts, including the C0006 symptom.
- Preserve scientific execution, Store transaction ownership, Launcher/E3 behavior, approval, retry, budgets, and internal server paths unchanged.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `frontend/browser-observability`: Return normalized Candidate scientific fields and formal Candidate-to-run/Artifact relationships without losing truth at bounded collection limits.
- `frontend/workbench-ui`: Present Candidate metrics, statuses, structure availability, associations, and truncation-aware empty states accurately.

## Impact

- Affected boundaries: `web_api/workbench.py`, a read-only committed Artifact reader/hasher seam used to validate Prediction records, Frontend V2 candidate selectors/components, and focused Python/TypeScript/browser tests.
- Public interface: additive fields may be added to `frontend.workbench.v2` Candidate/Artifact views; existing fields and `/api/v1` behavior remain compatible. No scientific or execution API changes.
- Data format and migration: no SQLite schema migration and no backfill. Existing committed smoke data is projected at read time from formal Store Evidence and hash-validated committed Prediction record Artifacts.
- Legacy behavior: genuinely unlinked legacy Candidates and Artifacts remain `unlinked`; malformed or unverifiable formal records fail closed and are not associated.
