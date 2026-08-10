## Context

See `proposal.md` for motivation. The current production composition in `web-gui/app/workbench/workbench-page.tsx` renders coverage, task/candidate, shortlist, Evidence, and artifact sections as a long page. `workbench-shell.tsx` reserves full-width regions for run context and blockers, while `globals.css` uses a near-black green palette, repeated bordered cards, and engineering-facing headings. A live 1920×1080 review confirmed that even the no-run state requires scrolling before Evidence and Artifact content becomes reachable.

The typed `/api/v2/workbench` client, request lifecycle, bounded selection behavior, scientific selectors, structure loading, and domain contracts are already verified and remain the authority. Presentation may coordinate UI selection, tabs, collapse, and pane sizes, but it may not derive workflow or scientific state.

Design research used two user-level aids:

- Anthropic `frontend-design`: drove the domain-specific aesthetic, deliberate typography, restrained signature element, plain user-facing copy, and rejection of generic AI dashboard patterns.
- Vercel `web-design-guidelines`: supplies the implementation review checklist for semantics, focus, keyboard operation, reduced motion, typography, long content, responsive layout, and accessible controls.

PR #26 is used only as a spatial reference: its three-column work area, prominent structure stage, inspector, and bottom dock demonstrate useful information density. Its V1 snapshot, fixed Agent rail, logs, execution controls, and old state assumptions are not reused.

## Goals / Non-Goals

**Goals:**

- Make project/run health, important task/candidate context, scientific distinction, and execution history understandable in one desktop viewport.
- Establish one coherent, finished light visual identity suited to a scientific instrument rather than an admin or hacker dashboard.
- Keep existing typed domain components deep and reusable while replacing the page-level stacking composition.
- Preserve honest empty, invalid-binding, stale, truncated, unavailable, failure, rollback, and recovery states.
- Make UI selection and panel controls semantic, keyboard-friendly, and testable.

**Non-Goals:**

- No changes to `/api/v2/workbench`, backend, Store, Orchestrator, transaction, scientific computation, thresholds, exploration, or Evidence contracts.
- No workflow mutations, start/retry/cancel, project creation, SSH/GPU control, or reconstructed frontend workflow state.
- No PR #26 component/code transplant, fixed Agent pipeline, snapshot model, or stdout log console.
- No dark-theme implementation in the first pass. Light is the intentional default; a future theme change requires its own evidence and scope.
- No large design-system dependency, routing redesign, or general `web-gui` framework migration.

## Decisions

### 1. Use a stable five-region desktop workspace

The primary layout is a viewport-height CSS Grid, optimized first for 1920×1080 and remaining usable at 1440×900:

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ Logo  Project · Run · Status              blocker · stale · refresh        │ 56
├───────────────┬──────────────────────────────────────────┬───────────────────┤
│ Navigator     │ Primary workspace                        │ Inspector         │
│ Tasks         │                                          │ Overview / Trace  │
│ Candidates    │ Candidate: structure + metrics           │ Evidence          │
│ Evidence      │ Task: graph + execution                  │ Artifact/Protocol │
│ counts/badges │ Evidence: scientific detail              │ Blocker detail    │
├───────────────┴──────────────────────────────────────────┴───────────────────┤
│ History  Evidence · Attempts · Transactions · Recovery        [collapse]   │
└──────────────────────────────────────────────────────────────────────────────┘
```

Nominal columns are 248px / minmax(560px, 1fr) / 344px. The history dock opens around 210px and collapses to a labelled 36px rail. At narrower desktop widths the inspector may overlay or become a tabbed drawer before the navigator is sacrificed. Below the desktop threshold all regions remain reachable as stacked/tabbed surfaces.

Alternative considered: refine the existing card grid. Rejected because it cannot meet simultaneous-context and no-page-scroll acceptance without recreating panes indirectly.

### 2. Introduce one UI-only selection model

`WorkbenchSelection` is a discriminated UI reference (`task`, `candidate`, `evidence`, `artifact`, or `overview`) containing only a returned opaque identity. It coordinates navigator, primary workspace, inspector, and history focus. It does not store status, phase, pass, execution, or association truth; every render resolves the identity against the latest bounded response and existing formal selectors.

On refresh, the established bounded-selection rule applies: an absent selection falls back visibly and does not resurrect later. Cross-domain links may change selection only when a formal trace identifier exists.

Alternative considered: retain independent selected IDs in every section. Rejected because it permits contradictory simultaneous contexts and makes the inspector ambiguous.

### 3. Keep current domain logic, refactor composition boundaries

Retain with limited presentation adaptation:

- typed client/parser, domain types, request lifecycle, `useWorkbench`, and bounded selection logic;
- scientific selectors and trace-only association rules;
- structure-viewer loading/identity isolation;
- task graph, execution/transaction, shortlist, Evidence, artifact/protocol/trace content renderers.

Refactor or replace:

- `WorkbenchShell` becomes compact `WorkbenchTopBar` plus workspace frame;
- `WorkbenchPage` becomes composition/selection orchestration, not a large renderer;
- Task/Candidate/Evidence list portions move into a shared `WorkbenchNavigator`;
- content details become selection-led `PrimaryWorkspace` and `WorkbenchInspector` views;
- Evidence chronology and transaction history move into `WorkbenchHistoryDock`;
- full-width collection coverage and blocker regions are removed from the primary hierarchy;
- global CSS is divided into tokens/layout/domain component sections if that can be done without introducing another styling system.

Proposed component hierarchy:

```text
WorkbenchPage
└─ WorkbenchFrame
   ├─ WorkbenchTopBar
   ├─ WorkbenchNavigator
   │  ├─ TaskNavigator
   │  ├─ CandidateNavigator
   │  └─ EvidenceNavigator
   ├─ PrimaryWorkspace
   │  ├─ OverviewWorkspace
   │  ├─ TaskWorkspace
   │  ├─ CandidateWorkspace
   │  └─ EvidenceWorkspace
   ├─ WorkbenchInspector
   │  ├─ SelectionOverview
   │  ├─ ProvenanceInspector
   │  └─ BlockerInspector
   └─ WorkbenchHistoryDock
      ├─ EvidenceHistory
      ├─ AttemptHistory
      └─ TransactionHistory
```

### 4. Treat history as a projection, never a workflow model

History combines only presentation records already returned by the V2 response. Evidence and transactions may be ordered by their formal timestamps. Executions without timestamps are placed in an explicitly untimed attempt lane keyed by task/attempt identity. Filtering history by the current selection uses formal trace or exact task/attempt identifiers only.

The dock never creates stages, completion percentages, causal links, or inferred associations. Source events outside the bounded response remain opaque unavailable references.

### 5. Use progressive disclosure for engineering detail

Primary copy uses scientific work language:

- `No active run` rather than the contract code as a headline;
- `Needs attention` with a concise returned summary rather than `Structured blockers`;
- counts attached to Tasks/Candidates/Evidence rather than `Collection coverage`;
- compact `Data may be out of date` treatment for stale-last-good responses.

Contract codes, scopes, identifiers, returned/total values, and integrity detail remain visible in the inspector. This is a presentation mapping only; text is sourced from returned facts and stable UI copy, not inferred from logs or internal storage.

### 6. Adopt a cool-paper scientific visual system

The design avoids both the current black/green console and the common AI-default cream/terracotta editorial page.

Initial token direction:

- canvas `#EEF2F4` (cool lab-paper gray)
- surface `#F8FAFA`
- raised surface `#FFFFFF`
- ink `#182126`
- secondary ink `#657078`
- divider `#D7DEE2`
- focus/selection `#245F7A` (spectral blue)
- exploratory `#A66722` (amber, never success)
- passed `#2F6B50` (reserved green)
- failure `#A64B3F`

Typography uses a locally bundled, open-licensed serif associated with scientific publishing (preferred: STIX Two Text) for product name, workspace titles, and selected scientific identity. A neutral humanist sans (preferred: IBM Plex Sans) serves controls and body copy; IBM Plex Mono or an equivalent local mono serves opaque IDs and numeric traces. Fonts must be local with `font-display: swap`, not fetched from a runtime CDN. Numeric comparisons use tabular figures.

The single signature element is a new monoline vector mark: a cyclic-peptide loop crossing a paired target axis, readable at favicon and 24–32px toolbar sizes. It replaces the three glowing dots and does not use gradients, glow, molecule clip-art, or decorative pseudo-data. A text alternative is supplied through the surrounding product label; decorative SVG paths are hidden from assistive technology.

### 7. Use restrained interaction and no decorative motion

Tabs, selections, panel collapse, and optional resize handles use semantic buttons and visible `:focus-visible` treatment. Hover and selection increase contrast. If pane resize is implemented, it uses pointer and keyboard controls with bounded widths and an accessible separator; otherwise fixed responsive CSS Grid sizes satisfy the first delivery. Motion is limited to short opacity/transform transitions and respects `prefers-reduced-motion`.

### 8. Verify the rendered workspace, not just source strings

Focused tests retain frozen V2 fixtures and add observable assertions for:

- one-screen desktop region hierarchy at 1920×1080 and 1440×900;
- navigator selection updating primary and inspector context;
- compact no-run, blocker, stale, and truncated states;
- passed versus exploratory shortlist visual semantics;
- timestamped versus untimed history behavior and prior-attempt truth;
- trace-only associations and content-link-only structure loading;
- keyboard focus, accessible names, landmark/headings, collapse restoration, and no horizontal overflow.

Browser screenshots at both desktop sizes are required during implementation review. Snapshot images are review evidence, not a second product specification.

## Risks / Trade-offs

- [Risk] Dense panes can become cramped at 1440px → Use minimum widths, collapse inspector/history before compressing scientific content, and test long identifiers and empty states.
- [Risk] A unified selection model could accidentally become domain authority → Store identity only, resolve every detail from the current V2 response, and test refresh invalidation.
- [Risk] A combined history can imply false chronology → Order only timestamped records and render untimestamped attempts in a separately labelled lane.
- [Risk] Serif typography can reduce dense-data readability → Limit serif to identity/hierarchy roles; use sans/mono for controls, tables, codes, and metrics.
- [Risk] New local fonts and logo increase asset surface → Keep assets small, open-licensed, local, and covered by metadata/render tests; no runtime network dependency.
- [Risk] Visual polish work expands into component rewrite → Preserve typed client/selectors and refactor only composition and presentation seams covered by this delta.
- [Risk] Current empty backend data underrepresents populated layouts → Review both real no-run data and the frozen full V2 fixture at target viewports.

## Migration Plan

1. Add presentation fixtures/tests and the new token/logo/font assets without changing the rendered composition.
2. Introduce the UI-only selection model and frame components behind the existing V2 page boundary.
3. Move navigator, primary, inspector, and history presentation incrementally while reusing formal domain renderers.
4. Replace the old shell/coverage/full-width sections after equivalent truthful states are covered.
5. Update directly related frontend documentation, run browser review at both target sizes, and perform full repository verification.

Rollback is a normal revert of this frontend-only change. No persisted application state, API, or data migration is involved; local panel preferences must tolerate absence after rollback.
