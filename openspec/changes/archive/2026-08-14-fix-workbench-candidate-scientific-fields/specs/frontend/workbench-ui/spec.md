## MODIFIED Requirements

### Requirement: Candidate workspace preserves formal provenance
The candidate workspace SHALL present Candidate identity, sequence when returned, normalized metrics, status fields, and `current_run`, `historical_run`, or `unlinked` provenance. It SHALL associate Evidence and Artifacts only through returned formal trace identifiers, SHALL label association counts as returned or complete, and SHALL distinguish “not returned because the collection is truncated” from “no formal association exists.”

#### Scenario: A candidate is selected
- **WHEN** a user selects a returned Candidate with committed metrics and final scientific status
- **THEN** the workspace shows its normalized metrics and status and only Evidence and Artifacts whose formal trace linkage identifies that Candidate

#### Scenario: Candidate has a status-owning formal Prediction event
- **WHEN** the displayed final status was committed with a Candidate-bound Prediction event from a formal run
- **THEN** the Candidate run relationship reflects that status-owning run without using sequence, time, filename, or message inference

#### Scenario: Historical and unlinked candidates are present
- **WHEN** project-scoped Candidates span current, historical, and records with no formally established status-owning run
- **THEN** the workspace labels their returned run relationship and does not merge historical or genuinely unlinked data into current-run status

#### Scenario: Candidate associations are truncated
- **WHEN** project Evidence or Artifact totals exceed the returned collection window and the selected Candidate has no matching returned item
- **THEN** the workspace reports that additional associations may be omitted and does not display an unqualified `0 artifacts`, `No returned shortlist`, or `No trace-linked structure` conclusion

#### Scenario: No candidates are returned
- **WHEN** the Candidate collection is empty
- **THEN** the workspace presents an honest empty state without fabricated Candidates, metrics, progress, or molecular structures

### Requirement: Artifact, protocol, and trace views preserve safe identity
The workbench SHALL present Artifact opaque identity, type, role, producer and input provenance, integrity metadata, protocol name/version/integrity identity, and available trace linkage without exposing or reconstructing a server filesystem path. The Candidate workspace SHALL recognize returned structure-bearing Artifact types from the formal Artifact contract and SHALL distinguish “structure Artifact recorded” from “browser-safe structure content available.”

#### Scenario: An artifact is inspected
- **WHEN** an Artifact is associated with a Candidate, task, or execution through formal trace fields
- **THEN** the inspector presents the returned opaque Artifact and provenance data and never displays a server path

#### Scenario: Structure artifact exists without browser content
- **WHEN** a formally Candidate-associated `design_pdb`, post-relax PDB, or prediction PDB Artifact is returned without a supported `content_link`
- **THEN** the Candidate workspace reports the recorded structure Artifact and separately reports browser content as unavailable instead of claiming that no structure Artifact exists

#### Scenario: Artifact content is not explicitly linked
- **WHEN** an Artifact has no supported `content_link`
- **THEN** the UI presents metadata and an unavailable content state rather than constructing a URL from an Artifact identifier or internal path

#### Scenario: A supported artifact content link exists
- **WHEN** an Artifact includes an explicit browser-safe `content_link`
- **THEN** the structure or content viewer may use that returned link while continuing to identify the Artifact by its opaque identity

#### Scenario: The selected artifact identity changes
- **WHEN** the user switches from one linked Artifact to another
- **THEN** the viewer clears the prior structure before showing the new identity as loading and resets the representation control to the default applied to the new model
