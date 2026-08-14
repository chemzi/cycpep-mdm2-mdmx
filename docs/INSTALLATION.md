# Production runtime installation

This guide bootstraps a new Linux NVIDIA GPU host for the current
`fresh_full_launcher` profile. It is an operator sequence, not a lockfile:
[THIRD_PARTY.md](../THIRD_PARTY.md), production configuration, versioned
protocols, and runtime validators own the enforced identities. Never copy an
observed path from another server and present it as a production pin.

## 1. Host and checkout

Provision an x86-64 Linux host with an NVIDIA GPU, a working vendor driver,
enough local storage for model parameters and runtime artifacts, Git, and a
Python environment manager. Install CUDA using the NVIDIA guide linked from
the third-party inventory. Confirm the driver sees the GPU before installing
model runtimes:

```bash
nvidia-smi
git clone https://github.com/chemzi/cycpep-mdm2-mdmx.git
cd cycpep-mdm2-mdmx
python -m venv /opt/novapeptide/envs/core
source /opt/novapeptide/envs/core/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The final paths may live elsewhere. Keep environments, model caches, runtime
data, and the repository in controlled directories with enough free space.
`requirements.txt` deliberately does not install the GPU scientific stack.

## 2. Isolated Design runtime

Create a separate Design environment and follow each official upstream
installation procedure in [THIRD_PARTY.md](../THIRD_PARTY.md):

1. Install RFdiffusion and its dependencies in an isolated environment. Fetch
   its officially distributed model weights and retain upstream notices.
2. Clone LigandMPNN and run its official model-parameter installer. The current
   Design protocol selects `protein_mpnn` and
   `model_params/proteinmpnn_v_48_020.pt`; the repository revision and
   checkpoint digest are availability observations, not invented pins.
3. Clone ColabDesign, checkout the exact production commit listed in
   `THIRD_PARTY.md`, leave the tracked checkout clean, and download the official
   AlphaFold parameters.

The repository setup portion is intentionally explicit while environment
package commands remain governed by each upstream's current installation guide:

```bash
git clone https://github.com/RosettaCommons/RFdiffusion.git \
  /opt/novapeptide/tools/RFdiffusion
git clone https://github.com/dauparas/LigandMPNN.git \
  /opt/novapeptide/tools/LigandMPNN
(cd /opt/novapeptide/tools/LigandMPNN && bash get_model_params.sh)
git clone https://github.com/sokrypton/ColabDesign.git \
  /opt/novapeptide/tools/ColabDesign
git -C /opt/novapeptide/tools/ColabDesign checkout \
  094e2cb3603dee7d99846e0977736bd943c830c2
git -C /opt/novapeptide/tools/ColabDesign status --short
```

The last command must be empty after installation. Download RFdiffusion weights
and AlphaFold parameters from the official sources linked in
`THIRD_PARTY.md`; do not commit or redistribute those files from this repository.

Configure only runtime locations. Scientific values remain in
`protocols/design_v1.json`:

```bash
export CYCPEP_CONDA=/opt/novapeptide/envs/design
export CYCPEP_PYTHON="$CYCPEP_CONDA/bin/python"
export RFDIFF_CONDA=/opt/novapeptide/envs/rfdiffusion
export RFDIFF_PYTHON="$RFDIFF_CONDA/bin/python"
# Override only when this environment uses another Python minor version.
export RFDIFF_PYTHON_VERSION=3.10
export RFDIFF_DIR=/opt/novapeptide/tools/RFdiffusion
export LIGANDMPNN_DIR=/opt/novapeptide/tools/LigandMPNN
export COLABDESIGN_DIR=/opt/novapeptide/tools/ColabDesign
export COLABDESIGN_PARAMS=/opt/novapeptide/models/alphafold
export SE3_ROOT="$RFDIFF_DIR/env/SE3Transformer"
export CUDA_DATA_DIR=/usr/local/cuda
export CYCPEP_DESIGN_AGENT_PYTHON="$CYCPEP_PYTHON"
```

Do not add an environment override for the LigandMPNN model/checkpoint or other
scientific parameters: the versioned Design protocol is authoritative.

## 3. Isolated Prediction runtimes

Create a Prediction environment that can run the checked-out ColabDesign and
load the configured AlphaFold parameters. Install the exact PRODIGY
distribution from the official source. Create a separate Boltz environment,
install the exact distribution listed in `THIRD_PARTY.md`, populate its cache
through the official download path, and select the checkpoint whose bytes pass
the production validator.

PyRosetta is a legal and deployment boundary. Obtain the repository-compatible
quarterly package only from an authorized PyRosetta channel and confirm that
the team's use is covered by the applicable license. Install it in a dedicated
environment and retain the package/version record. Do not mirror the wheel,
publish credentials, substitute ordinary Rosetta output, mock the import, or
skip InterfaceAnalyzer/post-relax to make doctor pass.

Install the public Prediction packages in their isolated environments:

```bash
/opt/novapeptide/envs/prediction/bin/python -m pip install 'prodigy-prot==2.4.0'
/opt/novapeptide/envs/boltz/bin/python -m pip install 'boltz[cuda]==2.2.1'
```

The required PyRosetta distribution version is
`2026.29+releasequarterly.80a0635615`. The required Boltz checkpoint SHA-256 is
`090e82ac8c92f5e943fa1b39e7410a44027bea7243c0bbb3caa67a77fc1428e1`;
verify the officially obtained file before selecting it. These are existing
production identities, not additional defensive checks.

```bash
export CYCPEP_PREDICTION_PYTHON=/opt/novapeptide/envs/prediction/bin/python
export CYCPEP_PRODIGY_EXECUTABLE=/opt/novapeptide/envs/prediction/bin/prodigy
export CYCPEP_BOLTZ_EXECUTABLE=/opt/novapeptide/envs/boltz/bin/boltz
export CYCPEP_BOLTZ_CACHE=/opt/novapeptide/models/boltz
export CYCPEP_BOLTZ_CHECKPOINT=/opt/novapeptide/models/boltz/boltz2_conf.ckpt
export CYCPEP_PYROSETTA_PYTHON=/opt/novapeptide/envs/pyrosetta/bin/python
export XLA_CUDA_DIR=/usr/local/cuda
```

The Prediction validator uses the configured executable/interpreter and fails
closed on a missing tool, wrong distribution version, dirty/wrong ColabDesign
checkout, or wrong Boltz checkpoint digest. `CYCPEP_BOLTZ_NO_KERNELS=1` is only
for a deliberately provisioned supported Boltz deployment; it is not a bypass
for a broken GPU stack.

## 4. Formal runtime roots and credentials

Create writable, access-controlled roots. Use absolute paths and keep the same
selectors for `launch`, `status`, and `resume`:

```bash
export NP_DATA=/srv/novapeptide/runtime
export CYCPEP_DATA_DIR=/srv/novapeptide/runtime/data
export CYCPEP_EVIDENCE_DIR=/srv/novapeptide/runtime/evidence
export CYCPEP_DB_PATH=/srv/novapeptide/runtime/store/project.sqlite3
export CYCPEP_EXECUTION_ROOT=/srv/novapeptide/runtime/execution
export CYCPEP_PREDICTION_ROOT=/srv/novapeptide/runtime/prediction-runs
export CYCPEP_PREDICTION_ARTIFACTS=/srv/novapeptide/runtime/prediction-artifacts
export CYCPEP_DESIGN_ROOT=/srv/novapeptide/runtime/designs
export CYCPEP_LAUNCHER_DIAGNOSTICS=/srv/novapeptide/diagnostics
export CYCPEP_EXECUTION_PYTHON=/opt/novapeptide/envs/core/bin/python
```

The Store file may be absent before the first launch; its parent must already
exist and be writable. Doctor reports `store_will_initialize_on_launch` without
creating the database. Do not manually seed SQLite or treat diagnostics and
filesystem artifacts as formal workflow state.

`CYCPEP_CONTROL_DATA` is an optional selector for a deployment-owned control-data
artifact used by calibration-capable paths. It is not universally required by
`fresh_full_launcher`, and it does not define a scientific identity. Configure
it only when the selected project/protocol calls for that controlled artifact:

```bash
# Optional; omit when no approved control-data artifact applies.
export CYCPEP_CONTROL_DATA=/srv/novapeptide/controls/approved-control-data.json
```

The file's format, provenance, and approval remain owned by the applicable
calibration contract; this installation guide does not infer them from a path.

For the `fresh_full_launcher` Research step, inject `OPENAI_API_KEY` through the
site's secret manager. `OPENAI_BASE_URL` and `LLM_MODEL` may select an authorized
compatible endpoint/model. Never put credential values in this repository,
shell history, doctor output, or logs.

## 5. Project coordinates and approval

Create and review a draft with the supported bootstrap CLI:

```bash
python -m target_bootstrap draft --identifier P12345 --type uniprot \
  --output projects/new-target.draft.json
python -m target_bootstrap show --draft projects/new-target.draft.json
```

Selecting a PDB record is not sufficient. Use the controlled project-management
API/UI (the `materialize_draft_coordinates()` boundary) to download coordinates
into the managed target root, validate PDB/chain/target binding, and record the
coordinate SHA-256 and provenance. Review the resulting draft, then approve it:

```bash
python -m target_bootstrap approve \
  --draft projects/new-target.draft.json \
  --output projects/new-target.json
export CYCPEP_PROJECT_CONFIG="$PWD/projects/new-target.json"
```

Do not hand-write `coordinate_path`, copy an unverified PDB into place, or edit
an approved project. Any content change invalidates the approval and requires
review and approval again. See the [transferable-project guide](./transferable_pipeline.md)
and [frontend project contract](./frontend_api_contract.md).

## 6. Doctor, then launch

Run doctor from the repository root under the core environment. It is read-only,
checks every required target coordinate/SHA, formal Store readiness, writable
roots, configured interpreters and tools, exact Prediction identities, GPU
visibility, and the Research credential:

```bash
python -m workflow doctor --project "$CYCPEP_PROJECT_CONFIG"
# Optional machine-readable report:
python -m workflow doctor --project "$CYCPEP_PROJECT_CONFIG" --json
```

Do not launch unless doctor prints `READY` and returns exit code 0. Doctor
success proves deployment readiness, not candidate quality or scientific
clearance.

```bash
python -m workflow launch --project "$CYCPEP_PROJECT_CONFIG"
```

Save the returned `launcher_run_id`. Follow the
[Launcher operator guide](./workflow_launcher.md) for status, approval-bound
resume, immutable failures, and recovery. Launcher never infers approval.

## 7. Troubleshooting by owner

Doctor accumulates independent failures; fix them by the reported owner and
rerun the same command:

| Owner | Typical failure | Correct response |
|---|---|---|
| Project | Approval, target identity, coordinate, chain, or SHA invalid | Rematerialize through the controlled boundary, review, and re-approve. |
| Host/runtime | GPU invisible, CUDA missing, Python or root unavailable | Repair the driver/environment/path or permissions; do not weaken readiness. |
| Design | RFdiffusion, LigandMPNN, selected checkpoint, or ColabDesign unavailable | Reinstall from the official source and restore configured paths. |
| Prediction | ColabDesign, Boltz, PRODIGY, PyRosetta, model/cache/checkpoint identity failure | Restore the exact production dependency in its isolated environment. |
| Data | Existing Store fails read-only preflight or the new Store parent is unwritable | Repair/migrate under the data owner; do not edit formal rows to force success. |
| Research | `OPENAI_API_KEY` missing | Inject an authorized secret outside the repository. |

Treat the first fail-closed observation as the blocker. Do not mock a required
scientific tool, reuse an incompatible artifact, relax Prediction readiness,
or automatically retry an immutable failed invocation.
