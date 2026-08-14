## Context

See `proposal.md` for motivation. The current README accurately warns that `requirements.txt` is incomplete, but its "Quick Start" heading and missing supported launch sequence still create the wrong expectation. Runtime knowledge is split across `ExecutionConfig.from_environment()`, Design configuration and strict preflights, Prediction validators, protocol JSON, project approval/coordinate validation, and historical server notes. Duplicating these values into a second validator would immediately recreate the drift this change is meant to remove.

The existing workflow CLI is a thin adapter over workflow-owned services. `launch`, `status`, and `resume` emit a single browser-safe JSON document. Doctor is operator-facing, must expose actionable paths and observations, and must remain strictly read-only.

## Goals / Non-Goals

**Goals:**

- Make one supported path obvious for a provisioned machine and one complete path available for a new GPU machine.
- Reuse production configuration and validation ownership so a green doctor meaningfully predicts whether Launcher can start its required scientific runtime.
- State precisely which identities are enforced, merely observed, restricted by license, or still require team confirmation.
- Keep checks bounded, deterministic, secret-safe, testable on CPU with injected probes, and useful on the deployed GPU host.

**Non-Goals:**

- Rewriting the README, redesigning Launcher, or automatically running doctor inside launch.
- Installing packages, cloning repositories, downloading weights, repairing configuration, approving projects, or starting/resuming work.
- Adding a new scientific-runtime manifest, Store/Evidence format, protocol version, hash requirement, or retry/readiness rule.
- Claiming repository commits or license rights that are not supported by production code, the deployed environment, or official upstream material.

## Decisions

### 1. Add a workflow-owned read-only doctor service and thin CLI branch

The implementation will add a small workflow-owned module that returns a typed report containing stable check IDs, categories, requirement level, status, observation, and remediation. `workflow.cli` will add `doctor --project PATH [--json]`; existing commands and their default JSON output remain untouched. The exported three-field `CommandHandlers` construction remains compatible: doctor uses a separate optional injected handler/dispatch path rather than adding a new required dataclass field or generalizing `LauncherCommandResult`. Doctor defaults to a concise human checklist because its primary consumer is an operator at a terminal, while `--json` supports CI and deployment automation.

Alternative considered: a standalone `scripts/doctor.py`. Rejected because the supported start command already lives under `python -m workflow`, and a second entry point would create another configuration and error-normalization boundary.

### 2. Compose existing authorities instead of copying validation rules

Project loading uses the explicit `--project` path, normalization, and `assert_project_approved`. For every required target doctor calls public `assert_target_structure_ready`, then independently reads the approved `structure.coordinate_path` and `coordinate_sha256` and verifies bytes through public `core.integrity.file_sha256`; this closes the approved legacy `structure_plan=None` case without importing `agents.design.service._resolve_coordinate_artifact`.

Execution paths come from `ExecutionConfig.from_environment()`. Prediction checks call the public ColabDesign, Boltz, PyRosetta, and PRODIGY validators. The pure required-path checks currently embedded in Execution's private `_require_prediction_tools` will be extracted into an Execution-owned public, side-effect-free validator used by both the handler and doctor; doctor will not cross-import that private symbol. RFdiffusion and LigandMPNN checks use Design configuration plus executable/repository/checkpoint file metadata only. Doctor MUST NOT import Design private `_run_*`/`_verify_*` seams, run the functional ColabDesign smoke, create its temporary script, or claim exact RFdiffusion/LigandMPNN repository identity.

Where a production seam only establishes file/repository availability, doctor may report an observed Git HEAD or filename for diagnosis but labels it `observed`, not `verified`. It will not introduce new commit or SHA gates. This keeps formal identity ownership in current code/protocols while making gaps visible in `THIRD_PARTY.md`.

Alternative considered: parse `THIRD_PARTY.md` as a machine-readable lockfile. Rejected because prose is not a safe runtime authority and the user did not request a new deployment-manifest contract.

### 3. Separate pure check orchestration from host probes

The doctor service will run independent check functions through injected host probes for command execution, environment lookup, path metadata, read-only Store opening, GPU discovery, and write-capability inspection. Production defaults use bounded subprocesses and filesystem metadata; tests supply deterministic fakes. A failed check is accumulated rather than aborting unrelated safe checks, while malformed project input is reported once as the root authority failure. Python import/version probes may populate interpreter caches outside formal roots, so the no-mutation guarantee applies to the approved project and configured Store/Evidence/diagnostic/artifact/runtime roots rather than claiming that the operating system changes no bytes anywhere.

Write capability is evaluated from an existing path or nearest existing parent and OS access metadata. Doctor does not create a probe file or directory. An existing SQLite file must pass `validate_storage_backend`; an absent fresh database is not opened or created and instead passes only when its explicit target and nearest existing parent are usable. GPU visibility uses a bounded `nvidia-smi` query and records model/driver/memory observations without running CUDA kernels.

Alternative considered: perform a tiny write and GPU tensor allocation. Rejected because doctor is a preflight, not a scientific smoke, and must leave no artifacts or GPU work behind.

### 4. Treat fresh Launcher Research credentials as one explicit profile

This change supports exactly one profile, `fresh_full_launcher`, so `OPENAI_API_KEY` is required and reported by name only. The implementation never returns its value. `skipped` remains a general result status for genuinely inapplicable checks, but this change adds no profile selector and no non-LLM Research profile.

Alternative considered: make the key only a warning because parts of Research can degrade without it. Rejected because the requested operator contract is readiness for the full demonstrated workflow, not readiness for a degraded Research run.

### 5. Documentation has three non-overlapping owners

- README: architecture orientation that fits a short first read, the provisioned-environment run sequence, and links.
- `docs/INSTALLATION.md`: ordered machine provisioning, configuration, validation, launch, and troubleshooting.
- `THIRD_PARTY.md`: dependency audit table and component-specific identity/license/citation notes.

Current operational docs that directly conflict with the supported CLI or deployment boundary will be corrected and linked. Dated validation reports remain historical records and receive at most a clear historical marker; they are not rewritten to resemble current authority.

Third-party upstream, license, and citation claims will be verified from official repositories, package metadata, documentation, or vendor terms. Restricted artifacts such as PyRosetta are documented as locally authorized and not redistributed. Unknown facts remain explicitly unresolved.

### 6. Verification covers contract, documentation, and a real deployed preflight

Focused tests will prove CLI dispatch/format/exit codes, no-side-effect orchestration, explicit project selection, failure accumulation, secret redaction, production-validator handoff, and legacy command compatibility. Documentation tests will ensure the supported commands, environment selectors, and production identity constants cannot silently drift from the inventory. The final acceptance includes CPU full-suite gates and one read-only doctor run against the provisioned server project; it does not start Launcher or modify the server.

## Risks / Trade-offs

- [A green preflight is mistaken for scientific success] → Documentation states that doctor validates deployment readiness, not model quality or end-to-end completion.
- [A host filesystem reports writable but a later atomic write still fails] → Report this as a permission preflight, retain all production write/transaction checks, and avoid claiming stronger proof.
- [Third-party licenses or citations drift] → Link official sources, record the verification date/status, and label team-specific authorization rather than copying binaries or making legal claims.
- [Production validators are expensive or import GPU libraries] → Use their bounded metadata/import probes only; do not invoke model inference or allocate a CUDA workload.
- [Doctor and docs become another identity table] → Tests bind enforced versions/commits/checkpoint SHA to their production constants and protocols; observed-only values remain clearly non-authoritative.
- [Human and JSON renderers diverge] → Render both from the same typed report and test equivalent status/check IDs.

## Migration Plan

1. Add focused doctor tests and implement the read-only report/service and additive CLI route.
2. Verify the provisioned server's current paths and observed identities without changing it.
3. Write `THIRD_PARTY.md`, `docs/INSTALLATION.md`, and the narrow README/operator-guide edits from production sources and official upstream sources.
4. Run focused tests, full suite, Architecture Gate, strict OpenSpec, documentation/link checks, and independent high-reasoning Spec/Standards review.
5. Archive the completed change locally, commit and push the feature branch, and open a ready PR with `gh`; do not merge it.

Rollback is removal of the additive doctor branch/module/tests and the documentation changes. No runtime data migration or deployment rollback is required.
