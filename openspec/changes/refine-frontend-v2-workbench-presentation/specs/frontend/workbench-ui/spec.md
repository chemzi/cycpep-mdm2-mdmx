## ADDED Requirements

### Requirement: The workbench uses a selection-led desktop workspace
The workbench SHALL organize the read model into a compact global context bar, a navigator, a selection-sensitive primary workspace, a contextual inspector, and a collapsible history area instead of presenting every domain collection as consecutive full-width sections.

#### Scenario: A project is viewed on a desktop display
- **WHEN** the workbench is rendered at a viewport at least 1440 pixels wide and 900 pixels high
- **THEN** the initial viewport simultaneously exposes project and run status, the navigator, the selected primary subject, the inspector entry point, the blocker indicator, and the history entry point without requiring page-level vertical scrolling

#### Scenario: A task is selected
- **WHEN** the user selects a returned task in the navigator
- **THEN** the primary workspace emphasizes its returned dependency/action/execution information and the inspector exposes its formal metadata, blockers, transactions, protocol, and trace links without deriving a workflow stage

#### Scenario: A candidate is selected
- **WHEN** the user selects a returned candidate in the navigator
- **THEN** the primary workspace emphasizes its structure availability, returned metrics, provenance, and shortlist/pass relationship while the inspector exposes only formally trace-linked Evidence and artifacts

#### Scenario: No current run exists
- **WHEN** the response contains `no_current_run`
- **THEN** the top context presents a compact user-facing no-active-run state and keeps project-scoped navigation available without reserving a large empty workflow panel

### Requirement: The navigator owns object selection and collection coverage
The navigator SHALL provide keyboard-operable Tasks, Candidates, and Evidence views with an unambiguous selected state, SHALL preserve returned collection order, and SHALL present collection coverage beside the owning view rather than in a separate coverage dashboard.

#### Scenario: A navigator collection is complete
- **WHEN** a bounded collection reports `truncated: false`
- **THEN** its navigator label presents a compact returned count without elevating coverage metadata into a primary workbench panel

#### Scenario: A navigator collection is truncated
- **WHEN** a bounded collection reports `truncated: true`
- **THEN** its navigator label visibly presents `returned / total` and an omission indicator without implying that absent records do not exist

#### Scenario: Candidate attention is indicated
- **WHEN** candidates include returned pass, shortlist, provenance, or blocker information
- **THEN** the navigator may use those returned values to help users locate candidates but does not compute a new rank, pass result, desirability, or Pareto conclusion

### Requirement: Inspector and history preserve formal context
The inspector SHALL disclose details for the current UI selection, and the history area SHALL present returned Evidence, attempts, and transactions as provenance and lifecycle records without becoming a frontend workflow state machine.

#### Scenario: Contextual detail is inspected
- **WHEN** a task, candidate, Evidence item, artifact, or history record is selected
- **THEN** the inspector presents the corresponding returned detail and formal trace links without associating records by text, sequence, filename, Agent name, path, or temporal proximity

#### Scenario: Timestamped records appear in history
- **WHEN** Evidence or transaction records include formal timestamps
- **THEN** the history may order those records by their returned timestamps and labels each record by its real event or lifecycle type

#### Scenario: A record has no formal timestamp
- **WHEN** an execution attempt or other returned record lacks a formal timestamp
- **THEN** the history presents it in an explicitly untimed or identity-grouped lane and does not invent a timestamp or chronological position

#### Scenario: Prior and current attempts are present
- **WHEN** a selected task has a current execution attempt and transactions from prior attempts
- **THEN** the history keeps the current attempt distinct, preserves every returned prior transaction, and retains `not_yet_recorded`, committed, failed, rolled-back, and unresolved-recovery semantics

### Requirement: Presentation language serves scientific work
Primary labels and status messages SHALL use concise user-facing scientific work language, while internal contract codes, scopes, opaque identifiers, and coverage mechanics remain available as secondary or advanced detail when they are useful for provenance or diagnosis.

#### Scenario: A blocker is present
- **WHEN** the response contains a structured blocker
- **THEN** the top bar or affected navigator item presents a compact human-readable warning and the inspector provides its full returned code, scope, identifiers, and summary

#### Scenario: Refresh fails after data was loaded
- **WHEN** the workbench is showing a stale last-good response
- **THEN** a compact persistent status treatment identifies the refresh failure without displacing the primary workspace or obscuring the stale-data condition

#### Scenario: Internal observability metadata is available
- **WHEN** the workbench renders engineering concepts such as bounded collections or structured blocker codes
- **THEN** it does not use those implementation terms as dominant page headings and instead places them with their owning object or advanced detail

### Requirement: The visual identity is light, editorial, and domain-specific
The default workbench presentation SHALL use a light or cool-neutral surface hierarchy, serif-led typography with legible utility typography for dense data, restrained semantic color, and an original local vector identity related to cyclic-peptide and target interaction rather than a generic monitoring-dashboard aesthetic.

#### Scenario: The default desktop theme is rendered
- **WHEN** the workbench opens without a stored theme preference
- **THEN** large surfaces are light or cool-neutral rather than near-black, hierarchy is established primarily through typography, spacing, and surface elevation, and borders are reserved for meaningful separation

#### Scenario: The product identity is rendered
- **WHEN** the global bar and page metadata are shown
- **THEN** they use the new accessible cyclic-peptide interaction mark and consistent product naming without the old three-node mark or architecture slogan

#### Scenario: Scientific statuses are compared
- **WHEN** passed, exploratory shortlist, unavailable, blocked, failed, committed, or rolled-back states appear together
- **THEN** each uses distinguishable text and restrained semantic styling, and a shortlist item with `passed: false` never receives passed or success styling

### Requirement: Workspace panels remain accessible and resilient
Navigator tabs, selections, inspector tabs, timeline controls, and panel collapse or resize controls SHALL be semantic, keyboard-operable, visibly focused, and usable across the prioritized desktop sizes without hiding truthful empty, partial, blocked, failed, or stale states.

#### Scenario: The workspace is used by keyboard
- **WHEN** a user tabs through navigator, primary workspace, inspector, refresh controls, and history controls
- **THEN** focus order follows the visual reading order, every interactive control has an accessible name and visible focus state, and selection does not depend on pointer-only interaction

#### Scenario: The inspector or history is collapsed
- **WHEN** a user collapses an auxiliary panel
- **THEN** the selected subject and critical status remain visible and the panel can be restored through a labelled keyboard-operable control

#### Scenario: The viewport narrows below the desktop workspace threshold
- **WHEN** the layout cannot retain all desktop panes at usable widths
- **THEN** secondary panes become tabs, drawers, or stacked regions while all formal data and states remain reachable without horizontal page overflow
