## Why

Frontend V2 now presents the formal workbench read model correctly, but its current dark, vertically stacked monitoring-dashboard presentation prevents users from understanding the project, run, scientific candidates, evidence, and execution health within a desktop demo glance. This change refines only presentation and information architecture so the truthful read model feels like a complete professional scientific workbench rather than an engineering observability page.

## What Changes

- Replace the long document stack with a desktop-first workspace: compact global bar, tabbed navigator, selection-led primary workspace, context-sensitive inspector, and collapsible evidence/execution timeline.
- Translate internal engineering labels into concise scientific work language while retaining contract codes and opaque identifiers in advanced detail.
- Move collection counts beside their owning navigator sections and emphasize truncation only when data is actually omitted.
- Present blockers as a compact global indicator plus contextual badges and inspector detail instead of a full-width engineering panel.
- Establish a light, cool-neutral visual system with restrained scientific status colors, serif-led typography, denser spacing, fewer borders, and complete hover/focus/selected/empty/stale states.
- Replace the current generic three-node mark and related metadata artwork with a cohesive vector identity based on a cyclic-peptide loop and paired target interaction; keep the asset local and accessible.
- Preserve all existing scientific and architecture truth: `/api/v2/workbench` remains the only authority, shortlist membership never implies passing, associations remain trace-only, transaction attempts remain distinct, and artifact content still requires `content_link`.
- Add desktop visual/interaction regression coverage at 1920×1080 and a narrower 1440-wide workspace, plus targeted accessibility and semantic tests.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `frontend/workbench-ui`: Add observable requirements for a selection-led desktop workspace, user-facing scientific language, compact contextual disclosure, a light serif-led visual identity, an original domain-specific logo, and accessible panel behavior without changing backend or scientific semantics.

## Impact

- Affected code is limited to `web-gui/` presentation components, styles, local visual/font assets, frontend tests, and directly related frontend documentation/OpenSpec artifacts.
- `GET /api/v2/workbench`, backend services, Store, Orchestrator, Action Registry, transactions, scientific calculations, thresholds, exploration logic, and Evidence contracts are unchanged.
- No public API or scientific data-format migration is introduced. Existing typed client/domain parsing remains authoritative.
- The rendered legacy path remains absent: this change does not restore V1 snapshot, fixed-Agent, log-derived, filesystem-derived, or mutation-control behavior.
- No new workflow/planning owner or runtime UI framework is introduced. Small presentation dependencies such as a split-pane primitive require explicit justification before implementation; CSS Grid and existing React state are preferred.
