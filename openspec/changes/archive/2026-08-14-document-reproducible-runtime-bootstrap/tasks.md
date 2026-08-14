## 1. Characterize the readiness contract

- [x] 1.1 Add focused failing CLI regressions for `doctor --project`, default text and `--json` rendering, READY/NOT READY exit codes, invalid input normalization, unchanged `launch`/`status`/`resume` dispatch, and compatibility with the existing three-field `CommandHandlers` constructor.
- [x] 1.2 Add focused failing service regressions proving explicit approved-project selection, all-required-target coordinate SHA validation including `structure_plan=None`, existing-Store read-only validation versus absent-Store parent readiness with `store_will_initialize_on_launch`, independent failure accumulation, stable check identifiers/statuses, credential redaction, and zero mutation of approved project/formal runtime roots.
- [x] 1.3 Add focused failing runtime regressions that exercise the real public configuration/validator composition seams with controlled host probes, including exact ColabDesign/Boltz/PyRosetta/PRODIGY identities, availability-only RFdiffusion and LigandMPNN checks, protocol-selected checkpoint, GPU visibility, Store preflight, writable-root metadata, and required `OPENAI_API_KEY` under the sole `fresh_full_launcher` profile.
- [x] 1.4 Add WAL-sidecar regressions proving doctor uses immutable validation only for a checkpointed Store, fails closed on non-empty WAL authority, and leaves database/`-wal`/`-shm` bytes and metadata unchanged.

## 2. Implement the read-only doctor

- [x] 2.1 Add the workflow-owned typed doctor report and bounded host-probe interface, keeping result assembly independent from rendering and without introducing a second scientific identity table.
- [x] 2.2 Compose approved project and public coordinate/SHA checks, `ExecutionConfig.from_environment()`, existing-Store read-only or absent-Store parent readiness, Design configuration metadata, and public Prediction runtime validators into the report; extract an Execution-owned public pure required-tool-path validator shared with the existing handler, and do not import private Execution/Design runtime functions.
- [x] 2.3 Add text and JSON renderers that emit no secret values or tracebacks, include actionable ownership/remediation for failures, and return zero only for READY.
- [x] 2.4 Add the additive `workflow doctor` CLI route and prove all pre-existing workflow commands and browser-safe JSON behavior remain unchanged.
- [x] 2.5 Keep ordinary Store readers unchanged while routing only checkpointed doctor preflight through immutable SQLite snapshot validation and rejecting uncheckpointed WAL authority.

## 3. Build the auditable deployment documentation

- [x] 3.1 Inventory external runtime and development dependencies from imports, requirements, protocols, runtime config, validators, and the successfully provisioned server; verify upstream, installation, citation, and license/terms claims against official primary sources.
- [x] 3.2 Add `THIRD_PARTY.md` with required/conditional/development classification, enforced versus observed identity, environment and selector, upstream/install source, citation, license status, and an explicit no-redistribution/authorized-source boundary for PyRosetta.
- [x] 3.3 Add `docs/INSTALLATION.md` covering new GPU-host assumptions, checkout, base dependencies, isolated Design/Prediction environments, RFdiffusion, LigandMPNN, ColabDesign/AF parameters, Boltz/cache/checkpoint, PRODIGY, authorized PyRosetta, CUDA/GPU, project coordinates/approval, SQLite/runtime roots, environment setup, doctor, launch, and owner-based troubleshooting.
- [x] 3.4 Narrowly revise README into a 15-minute orientation plus provisioned-environment run path (`doctor` then `launch`), link the installation and third-party documents, and remove the misleading complete-runtime implication from "Quick Start" without rewriting the architectural overview.
- [x] 3.5 Correct only current operator documents that conflict with the supported Launcher/deployment path; retain dated validation records as explicitly historical material.
- [x] 3.6 Add documentation consistency regressions that bind enforced versions/commits/checkpoint SHA and public environment selectors to production constants/config/protocol sources and validate required internal links and commands.

## 4. Verify, review, and publish without merging

- [x] 4.1 Run doctor focused tests, affected workflow/design/prediction regressions, documentation checks, Python compile checks, and `git diff --check`.
- [x] 4.2 Run the full Python suite, Architecture Gate, and strict OpenSpec validation; record any environment-specific skips without treating them as passes.
- [x] 4.3 Run read-only doctor acceptance against the current provisioned GPU server and approved project, snapshotting the approved project and formal Store/Evidence/artifact/diagnostic/runtime roots before and after; also exercise an isolated fresh target with no SQLite file to prove doctor reports `store_will_initialize_on_launch` without creating it. Confirm READY and zero formal-root mutation; do not claim interpreter/cache directories outside those roots are byte-for-byte unchanged.
- [x] 4.4 Obtain independent high-reasoning Spec and Standards reviews; resolve every P0/P1 finding and rerun affected gates until P0=0 and P1=0.
- [ ] 4.5 Verify and archive `document-reproducible-runtime-bootstrap`, then commit and push the isolated feature branch and use `gh` to open a ready PR against `integration/data-integrity-transaction`; do not merge the PR.
