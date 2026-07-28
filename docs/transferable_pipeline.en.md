# Transferable Cyclic-Peptide Binder Pipeline

MDM2/MDMX is the bundled reference project and regression target, not a
hard-coded assumption in the evaluator. A new project starts from a minimal
gene symbol, UniProt accession, or PDB ID and is expanded into a reviewable
configuration before any research or design job may run.

## Bootstrap and human approval gate

Configure an OpenAI-compatible LLM endpoint. The default model is
`step-3.7-flash`, and `LLM_MODEL` can override it.

```powershell
$env:OPENAI_API_KEY='...'
$env:OPENAI_BASE_URL='https://api.stepfun.com/v1'
$env:LLM_MODEL='step-3.7-flash'
```

Create and inspect a draft:

```powershell
python -m target_bootstrap draft --identifier P12345 --type uniprot --output projects/new_target.draft.json
python -m target_bootstrap show --draft projects/new_target.draft.json
```

Apply a JSON Merge Patch and approve it:

```powershell
python -m target_bootstrap edit --draft projects/new_target.draft.json --patch review_patch.json
python -m target_bootstrap approve --draft projects/new_target.draft.json --output projects/new_target.json
$env:CYCPEP_PROJECT_CONFIG=(Resolve-Path projects/new_target.json)
python -m agents.research
```

The corresponding service-layer APIs are
`TargetBootstrapper.create_draft()`, `edit_draft()`, `approve_draft()`, and
`assert_project_approved()`. Approval stores a digest of the reviewed content.
Changing the target, epitope, structure, or any other project field invalidates
that approval and forces another review.

Identifier ambiguity is blocking. Missing epitope evidence or structure quality
is reported as a visible warning. A user may approve a draft with warnings for
additional Research, but Design also has a stricter structure-readiness gate.

## Evidence-constrained LLM enrichment

The LLM receives database-resolved target metadata and explicit evidence IDs.
It is instructed to preserve unknowns rather than invent residues, binders,
affinities, structures, or citations. JSON Mode is enabled so the response can
be validated and merged deterministically. An LLM failure does not fabricate a
fallback answer: it leaves a reviewable draft with `llm_status=failed`.

## Structure resolution and conformational quality

`structure_resolution.resolve_project_structures()` implements an
experimental-first policy:

- RCSB experimental structures are graded A/B/C from resolution and available
  complex metadata.
- AlphaFold DB is queried only when no usable experimental structure is found;
  predicted structures are graded from pLDDT/PAE availability.
- Predicted or C-grade structures are marked as requiring ensemble validation.
- Missing structures return `prediction_required` and an explicit next action.
- Usable coordinates alone do not make a target design-ready. The binding-site
  residues, their review status, and the target chain must also be confirmed.

The `ExperimentalStructureProvider` and `PredictedStructureProvider` protocols
allow a future local AlphaFold 3, Boltz, or other prediction backend to be added
without changing the bootstrap, approval, or Design contracts. The current
implementation discovers and grades public structures; it does not submit an
expensive local prediction job.

The UI-facing resource model, REST-adapter contract, request/response examples,
and review/run state machine are documented in
[frontend_api_contract.md](frontend_api_contract.md).

## Thresholds for novel targets

When literature does not provide a defensible cutoff, use
`threshold_calibration.calibrate_threshold()` with positive and negative
controls produced by the same tool version and protocol. The calibrator selects
a cutoff under an empirical false-positive-rate constraint and records the
protocol hash, sample sizes, observed FPR, and recall.

A threshold derived only from negative controls means “better than the observed
background”; it must not be presented as experimental affinity. Provisional
thresholds may support funnel triage but cannot produce final
`competition_clearance`.

## Decision semantics

- `triage_status` controls the development funnel. Physical failures become
  `invalid`; incomplete soft evidence becomes `needs_more_evidence`; candidates
  below provisional soft thresholds become `needs_optimization`.
- `metric_clearance` reports numeric threshold passage.
- `competition_clearance` additionally requires auditable or calibrated
  threshold evidence across every required layer.
- Pareto ranking is applied only after clearance, followed by diversity
  selection under the experimental budget.

## AutoDL A100 validation

An isolated server smoke test was completed on 2026-07-28 with an NVIDIA A100
40 GB GPU:

- UniProt `Q00987` → RCSB → `step-3.7-flash` JSON Mode → draft completed.
- Draft edit, approval, digest validation, and tamper rejection passed.
- Coordinates without a reviewed target chain and epitope remained blocked from
  Design.
- A minimal MDM2/1YCR ColabDesign run generated one valid 8-residue candidate
  and PDB in about 105 seconds.
- All 180 existing regression checks and 5 new bootstrap/structure tests passed.

The live run uncovered and fixed two environment-specific defects: slow serial
RCSB metadata requests are now capped and parallelized, and Design launches its
child process with `sys.executable` so it uses the same Conda/CUDA environment.

## Secret handling

- Supply API keys only through environment variables or a protected secret
  manager.
- Never embed keys in diagnostic scripts, project configurations, logs, drafts,
  or evidence records.
- Revoke and rotate a key immediately if it appears in a script, chat, or shell
  log.
- `.env` is ignored by Git, but a platform secret manager is preferred in
  production.

Install `requirements.txt` in an isolated virtual environment. NumPy is capped
below 2.0 until the complete scientific stack is built against the NumPy 2.x
ABI.
