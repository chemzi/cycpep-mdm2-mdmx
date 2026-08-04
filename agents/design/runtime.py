"""External scientific tool subprocess layer.

RFdiffusion / LigandMPNN / AfCycDesign invocation, runtime verification, and
refold script generation.  No route logic lives here.
"""

from __future__ import annotations

import hashlib
import math
import os
import subprocess
import shutil
import tempfile
from pathlib import Path

from data_layer import EvidenceLogger  # noqa: E402

from . import config  # noqa: E402
from .config import (  # noqa: E402
    COLABDESIGN_COMMIT,
    COLABDESIGN_DIR,
    COLABDESIGN_PARAMS,
    CUDA_DATA_DIR,
    CYCPEP_PYTHON,
    LIGANDMPNN_CHECKPOINT,
    LIGANDMPNN_DIR,
    LIGANDMPNN_MODEL_TYPE,
    RFDIFF_CONDA,
    RFDIFF_DIR,
    RFDIFF_PYTHON,
    RFDIFF_TIMESTEPS,
    SE3_ROOT,
    _LOCK,
)
from .validation import (  # noqa: E402
    _extract_ligandmpnn_binder_sequence,
    _pdb_chain_residue_layout,
    _pdb_chain_sequences,
    _validate_sequence,
    _verify_fixed_sequence_pdb,
)
from peptide_contract import (  # noqa: E402
    MAX_CYCLIC_PEPTIDE_LENGTH,
    MIN_CYCLIC_PEPTIDE_LENGTH,
)



def _colabdesign_smoke_script():
    """Inline ColabDesign functional smoke test run under CYCPEP_PYTHON."""
    return f"""
import sys, numpy as np
sys.path.insert(0, {COLABDESIGN_DIR!r})
from colabdesign import mk_af_model, clear_mem
model = None
model = mk_af_model(protocol='hallucination', data_dir={COLABDESIGN_PARAMS!r})
model.prep_inputs(length=8)
model.restart(seed=0, seq='AAAAAAAA')
try:
    # Minimal forward pass - proves AF model can actually compute, not just
    # import and initialise (P1 smoke-test enhancement).
    aux = model.predict(
        seq='AAAAAAAA', seed=0, models=[0], num_models=1, num_recycles=1,
        sample_models=False, dropout=False, hard=True, soft=False,
        verbose=False, return_aux=True,
    )
    plddt = np.array(aux['plddt'])
    if not np.isfinite(plddt).all():
        raise RuntimeError(
            f'ColabDesign pLDDT contains non-finite values: '
            f'nan={{np.isnan(plddt).sum()}} inf={{np.isinf(plddt).sum()}}'
        )
    _ = float(np.mean(plddt))
    idx = np.array(model._inputs['residue_index'])
    off = np.array(idx[:, None] - idx[None, :])
    if not np.any(off):
        raise RuntimeError('ColabDesign residue_index offset matrix is all-zero')
finally:
    if model is not None:
        del model
    clear_mem()
print('COLABDESIGN_OFFSET_OK')
"""


def _run_colabdesign_smoke_check(sig):
    """Run the smoke-test subprocess; cache ``sig`` only on success."""
    script = _colabdesign_smoke_script()
    spath = os.path.join(
        tempfile.gettempdir(),
        f"_cd_offset_check_{os.getpid()}.py",
    )
    with open(spath, "w") as f:
        f.write(script)
    try:
        r = subprocess.run([CYCPEP_PYTHON, spath], capture_output=True, text=True,
            timeout=120,
            env={**os.environ,
                 "XLA_FLAGS": f"--xla_gpu_cuda_data_dir={CUDA_DATA_DIR}",
                 "XLA_PYTHON_CLIENT_PREALLOCATE": "false",
                 "XLA_PYTHON_CLIENT_MEM_FRACTION": "0.20"})
        if r.returncode != 0:
            EvidenceLogger.error("design", "colabdesign_offset_check_failed",
                f"exit={r.returncode} stderr={getattr(r, 'stderr', '')[-300:]}")
            return
        if "COLABDESIGN_OFFSET_OK" not in (getattr(r, 'stdout', '') or ""):
            EvidenceLogger.error("design", "colabdesign_offset_check_failed",
                "functional test did not emit success marker")
            return
        config._VERIFIED_RUNTIME_SIGNATURE = sig
    except (subprocess.SubprocessError, OSError) as exc:
        EvidenceLogger.error("design", "colabdesign_offset_check_error", str(exc))
    finally:
        try:
            os.unlink(spath)
        except OSError:
            pass


def _verify_colabdesign_runtime():
    """Functional smoke test: verify ColabDesign can load, forward, and produce
    non-zero residue_index offsets (P2: renamed from _check_colabdesign_loads).

    Runs in a subprocess (ColabDesign needs ``CYCPEP_PYTHON``, not the main
    process interpreter).  On success the module-level signature is set so
    every subsequent refold targeting the *same* environment skips the
    functional gate (P1-3).

    Set ``CYCPEP_SKIP_COLABDESIGN_VERIFY=1`` to bypass the check entirely
    (orchestrator-managed GPU allocation; P1 reviewer feedback).
    """
    # Mutable runtime flags live on config so submodules share one source.
    if os.environ.get("CYCPEP_SKIP_COLABDESIGN_VERIFY") == "1":
        if not config._SKIP_EVIDENCE_LOGGED:
            EvidenceLogger.log("design", "colabdesign_verify_skipped",
                {"reason": "CYCPEP_SKIP_COLABDESIGN_VERIFY=1 - "
                 "GPU allocation managed by orchestrator; "
                 "no pre-flight ColabDesign check will run"})
            config._SKIP_EVIDENCE_LOGGED = True
        return
    sig = (CYCPEP_PYTHON, COLABDESIGN_DIR, COLABDESIGN_PARAMS)
    if config._VERIFIED_RUNTIME_SIGNATURE == sig:
        return
    # Double-checked locking: only one thread may run the GPU subprocess.
    # NOTE: this only serialises within the *same* Python process.  When the
    # orchestrator launches multiple worker processes, use
    # CYCPEP_SKIP_COLABDESIGN_VERIFY=1 with a single pre-flight check instead.
    with _LOCK:
        if config._VERIFIED_RUNTIME_SIGNATURE == sig:
            return
        _run_colabdesign_smoke_check(sig)

def _run_rfdiff(target_pdb, binder_len, n_designs, output_prefix, contig,
                seed=None, hotspots=None, chain="A"):
    """RFdiffusion 子进程。hotspots: 逗号分隔的残基号如 '54,93,96'

    .. note::

        ``seed`` is **intentionally ignored** for RFdiffusion backbone generation
        because the GPU path is non-deterministic at the hardware level.  The seed
        parameter is accepted for API consistency with the rest of the pipeline and
        is only consumed by LigandMPNN and Route C expansion.
    """
    # Deferred log for invalid RFDIFF_TIMESTEPS (P1: no EvidenceLogger at import).
    if config._RFDIFF_TIMESTEPS_INVALID is not None:
        EvidenceLogger.log("design", "invalid_RFDIFF_TIMESTEPS",
            {"value": config._RFDIFF_TIMESTEPS_INVALID, "fallback": 50})
        config._RFDIFF_TIMESTEPS_INVALID = None

    if seed is not None:
        import warnings
        warnings.warn(
            f"RFdiffusion seed={seed} is ignored — GPU non-deterministic backbone",
            stacklevel=2,
        )
    cmd = [
        RFDIFF_PYTHON, f"{RFDIFF_DIR}/scripts/run_inference.py",
        f"inference.input_pdb={target_pdb}",
        "inference.cyclic=True",
        "inference.cyc_chains=a",
        f"inference.num_designs={n_designs}",
        f"inference.output_prefix={output_prefix}",
        f"contigmap.contigs=['{contig}']",
        f"diffuser.T={RFDIFF_TIMESTEPS}",
    ]
    if hotspots:
        # 补链名前缀: "54,93,96" → "'A54','A93','A96'"（Hydra 要求每个残基加引号）
        formatted = ",".join(f"'{chain}{r.strip()}'" for r in hotspots.split(",") if r.strip())
        if formatted:
            cmd.append(f"ppi.hotspot_res=[{formatted}]")
    try:
        _rfdiff_timeout = int(os.environ.get("RFDIFF_TIMEOUT") or "3600")
    except (ValueError, TypeError):
        _rfdiff_timeout = 3600
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=_rfdiff_timeout,
            cwd=RFDIFF_DIR,
            env=_rfdiff_subprocess_env())
        if r.returncode != 0:
            print(f"[RFdiff 失败] exit={r.returncode}")
            print(f"  stderr: {r.stderr[-500:]}")
            EvidenceLogger.error("design", "rfdiff_failed",
                f"exit={r.returncode} stderr={r.stderr[-300:]}")
            _cleanup_partial_rfdiff_output(output_prefix)
            return False
        return True
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        print(f"[RFdiff 异常] {e}")
        EvidenceLogger.error("design", "rfdiff_exception", str(e))
        _cleanup_partial_rfdiff_output(output_prefix)
        return False

def _cleanup_partial_rfdiff_output(output_prefix):
    """Remove incomplete PDB files left by a failed/timed-out RFdiffusion run."""
    prefix_dir = os.path.dirname(output_prefix)
    prefix_name = os.path.basename(output_prefix)
    try:
        for pdb in Path(prefix_dir).glob(f"{prefix_name}_*.pdb"):
            pdb.unlink()
    except OSError:
        pass

def _ligandmpnn_batch_plan(n_seq):
    """Derive (batch_size, number_of_batches) from n_seq and env overrides."""
    try:
        configured_max = int(os.environ.get("LIGANDMPNN_MAX_BATCH_SIZE", "4"))
    except (ValueError, TypeError):
        configured_max = 4
    configured_max = max(1, min(configured_max, 32))
    batch_size = min(max(1, int(n_seq)), configured_max)
    number_of_batches = max(1, (int(n_seq) + batch_size - 1) // batch_size)
    return batch_size, number_of_batches


def _build_ligandmpnn_command(backbone_pdb, output_dir, binder_chain,
                              batch_size, number_of_batches, seed,
                              fixed_residues=None):
    """Assemble the LigandMPNN ``run.py`` command line."""
    cmd = [
        RFDIFF_PYTHON, f"{LIGANDMPNN_DIR}/run.py",
        "--model_type", LIGANDMPNN_MODEL_TYPE,
        f"--checkpoint_protein_mpnn={LIGANDMPNN_CHECKPOINT}",
        f"--pdb_path={backbone_pdb}",
        f"--out_folder={output_dir}",
        f"--batch_size={batch_size}",
        f"--number_of_batches={number_of_batches}",
        "--temperature=0.1", f"--seed={seed}",
        "--fasta_seq_separation=:",
        f"--chains_to_design={binder_chain}",
    ]
    if fixed_residues:
        cmd.append(f"--fixed_residues={fixed_residues}")
    return cmd


def _flush_ligandmpnn_record(seq_buffer, is_generated_record, uses_id_marker,
                             binder_chain, layout, input_sequences, seqs,
                             ref_binder_seq, fa, header_index):
    """Consume one accumulated FASTA record at a header/EOF boundary.

    Returns the (possibly updated) captured reference binder sequence.  The
    generated-record branch appends to ``seqs`` when the sequence passes the
    homopolymer and reference-similarity guards.
    """
    if seq_buffer and is_generated_record:
        try:
            seq = _extract_ligandmpnn_binder_sequence(
                seq_buffer, binder_chain, layout, input_sequences
            )
        except (OSError, UnicodeError, ValueError) as exc:
            EvidenceLogger.error(
                "design", "ligandmpnn_fasta_invalid",
                f"{fa}: {exc}", recovery="skip malformed output",
            )
            seq = None
        if seq is not None:
            # Skip poly-homopolymer (LigandMPNN baseline artifact).
            if len(set(seq)) > 1 and seq not in seqs:
                # Positional-fallback guard: if the generated sequence is
                # nearly identical to the reference complex, the record order
                # may have changed.
                if ref_binder_seq is not None and len(ref_binder_seq) == len(seq):
                    identical = sum(a == b for a, b in zip(ref_binder_seq, seq))
                    similarity = identical / len(seq) if len(seq) > 0 else 0
                    if similarity > 0.8:
                        EvidenceLogger.error(
                            "design", "ligandmpnn_fallback_suspicious",
                            f"{fa}: generated record #{header_index} is "
                            f"{similarity:.0%} identical to reference - "
                            f"positional fallback may have mis-identified "
                            f"the reference complex as a design; "
                            f"sequence SKIPPED to avoid contaminating "
                            f"the candidate pool with a known native sequence",
                            recovery="verify LigandMPNN FASTA header format",
                        )
                        return ref_binder_seq  # P0-1: keep native out of pool
                seqs.append(seq)
    elif seq_buffer and not uses_id_marker and ref_binder_seq is None:
        # Positional fallback: capture reference binder sequence from the
        # first (non-generated) record for later similarity validation.
        try:
            ref_binder_seq = _extract_ligandmpnn_binder_sequence(
                seq_buffer, binder_chain, layout, input_sequences
            )
        except (OSError, UnicodeError, ValueError):
            ref_binder_seq = None
    return ref_binder_seq


def _collect_ligandmpnn_sequences(output_dir, n_seq, binder_chain, layout,
                                  input_sequences, backbone_pdb):
    """Parse generated binder FASTA records from a finished LigandMPNN run."""
    seqs = []
    fa_files = sorted(Path(output_dir).glob("**/*.fa"))
    if len(fa_files) > 1:
        EvidenceLogger.log("design", "ligandmpnn_multiple_fasta", {
            "backbone_pdb": str(backbone_pdb),
            "fasta_count": len(fa_files),
            "fa_files": [str(p) for p in fa_files],
        })
    for fa in fa_files:
        with open(fa) as fh:
            raw_lines = fh.readlines()
        # Detect FASTA header convention: LigandMPNN uses ", id=" markers.
        # If no header contains this marker (e.g. after an upstream format
        # change), fall back to positional heuristics: first record is the
        # reference complex, every subsequent record is a generated design.
        headers = [ln.strip() for ln in raw_lines if ln.strip().startswith(">")]
        uses_id_marker = any(", id=" in h for h in headers)
        if not uses_id_marker and len(headers) > 1:
            EvidenceLogger.log("design", "ligandmpnn_fasta_no_id_marker", {
                "backbone_pdb": str(backbone_pdb),
                "fasta_file": str(fa),
                "header_count": len(headers),
                "fallback": "positional - first record treated as reference, "
                            "subsequent records as generated",
            })
        header_index = 0
        is_generated_record = False
        ref_binder_seq = None  # captured for positional-fallback validation
        seq_buffer = ""
        # Iterate once more at the end to flush the final accumulated sequence.
        lines_iter = iter(raw_lines)
        exhausted = object()
        while True:
            line = next(lines_iter, exhausted)
            if line is exhausted or line.strip().startswith(">"):
                ref_binder_seq = _flush_ligandmpnn_record(
                    seq_buffer, is_generated_record, uses_id_marker,
                    binder_chain, layout, input_sequences, seqs,
                    ref_binder_seq, fa, header_index,
                )
                if line is exhausted:
                    break
                # Start a new record.
                seq_buffer = ""
                line = line.strip()
                header_index += 1
                if uses_id_marker:
                    # ", id=0" (or ", id= 0") -> native reference complex
                    is_generated_record = (
                        ", id=" in line
                        and ",id=0" not in line.replace(" ", "")
                    )
                else:
                    is_generated_record = header_index > 1
                continue
            if is_generated_record:
                seq_buffer += line.strip()
    return seqs[:n_seq]


def _run_ligandmpnn(backbone_pdb, output_dir, n_seq=None, binder_chain=None,
                    fixed_residues=None, seed=42):
    """LigandMPNN subprocess with an explicitly validated binder chain.

    The RFdiffusion output chain labels are discovered from the emitted PDB,
    rather than inferred from the input receptor's chain label.
    fixed_residues: space-separated ``chain+resi`` list, e.g. 'B25 B26 B27';
    those residues stay fixed in LigandMPNN.
    """
    if n_seq is None:
        n_seq = config.DESIGN_PROTOCOL["ligandmpnn"]["n_seq_per_backbone"]

    if LIGANDMPNN_MODEL_TYPE != "protein_mpnn":
        EvidenceLogger.error(
            "design", "unsupported_inverse_folding_model",
            f"LIGANDMPNN_MODEL_TYPE={LIGANDMPNN_MODEL_TYPE!r}; "
            "the validated protein-target workflow requires 'protein_mpnn'",
            recovery="use protein_mpnn or add a separately tested adapter",
        )
        return []
    try:
        layout = _pdb_chain_residue_layout(backbone_pdb)
        input_sequences = _pdb_chain_sequences(backbone_pdb)
    except (OSError, UnicodeError, ValueError) as exc:
        EvidenceLogger.error(
            "design", "ligandmpnn_backbone_invalid", str(exc), recovery="skip"
        )
        return []
    binder_chain = str(binder_chain or "").strip()
    if binder_chain not in layout:
        EvidenceLogger.error(
            "design", "ligandmpnn_binder_chain_missing",
            f"{backbone_pdb}: binder chain {binder_chain!r} is absent",
            recovery="skip",
        )
        return []
    batch_size, number_of_batches = _ligandmpnn_batch_plan(n_seq)
    cmd = _build_ligandmpnn_command(
        backbone_pdb, output_dir, binder_chain, batch_size, number_of_batches,
        seed, fixed_residues=fixed_residues,
    )
    # Wipe the entire output directory so no orphaned file from a previous
    # LigandMPNN run (FASTA, PDB, log, etc.) can be mistaken for new output.
    # LigandMPNN expects a clean or non-existent directory (P1-6).
    # ignore_errors=True already suppresses all OSError; no need for try/except.
    shutil.rmtree(output_dir, ignore_errors=True)
    os.makedirs(output_dir, exist_ok=True)
    try:
        _ligandmpnn_timeout = int(os.environ.get("LIGANDMPNN_TIMEOUT") or "600")
    except (ValueError, TypeError):
        _ligandmpnn_timeout = 600
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=_ligandmpnn_timeout,
            cwd=LIGANDMPNN_DIR,
            env=_rfdiff_subprocess_env())
        if r.returncode != 0:
            print(f"[LigandMPNN failed] exit={r.returncode} stderr={r.stderr[-300:]}")
            return []
        return _collect_ligandmpnn_sequences(
            output_dir, n_seq, binder_chain, layout, input_sequences,
            backbone_pdb,
        )
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        EvidenceLogger.error("design", "ligandmpnn_exception", str(e))
        return []

def _rfdiff_subprocess_env():
    """Reproduce the validated rfdiffusion-design ``activate.d`` runtime."""
    env = dict(os.environ)
    python_version = os.environ.get("RFDIFF_PYTHON_VERSION", "3.10")
    site_packages = f"{RFDIFF_CONDA}/lib/python{python_version}/site-packages"
    python_paths = [SE3_ROOT, RFDIFF_DIR]
    if env.get("PYTHONPATH"):
        python_paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_paths)
    env["DGLBACKEND"] = "pytorch"

    library_paths = [
        f"{RFDIFF_CONDA}/lib",
        f"{site_packages}/torch/lib",
        *(
            f"{site_packages}/nvidia/{package}/lib"
            for package in [
                "cusolver", "cuda_nvrtc", "cuda_runtime", "cublas", "cusparse",
                "nvjitlink", "cuda_cupti", "cufft", "cudnn", "nccl", "curand", "nvtx",
            ]
        ),
    ]
    if env.get("LD_LIBRARY_PATH"):
        library_paths.append(env["LD_LIBRARY_PATH"])
    env["LD_LIBRARY_PATH"] = os.pathsep.join(library_paths)
    return env

def _refold_script_prologue():
    """Generated-script preamble: env setup, commit pin, offset-wiring guard."""
    return f"""
import sys, subprocess, numpy as np
sys.path.insert(0, {COLABDESIGN_DIR!r})
from colabdesign import mk_af_model, clear_mem
from colabdesign.af.alphafold.model import modules as af_modules

head = subprocess.run(
    ['git', '-C', {COLABDESIGN_DIR!r}, 'rev-parse', 'HEAD'],
    capture_output=True, text=True, timeout=30, check=True,
).stdout.strip()
if head != {COLABDESIGN_COMMIT!r}:
    raise RuntimeError(
        'ColabDesign commit mismatch: expected=' + {COLABDESIGN_COMMIT!r}
        + ' observed=' + head
    )
dirty = subprocess.run(
    [
        'git', '-C', {COLABDESIGN_DIR!r}, 'status', '--porcelain',
        '--untracked-files=no'
    ],
    capture_output=True, text=True, timeout=30, check=True,
).stdout.strip()
if dirty:
    raise RuntimeError('tracked ColabDesign sources are modified')
source = open(af_modules.__file__, encoding='utf-8').read()
# Guard: verify the pinned ColabDesign commit still injects cyclic offset
# into the AF2 batch.  If the source-code pattern is absent (e.g. variable
# rename after an upstream refactor), the module-level functional smoke test
# (_verify_colabdesign_runtime) serves as a fallback gate (P1-3).
if '"offset" in batch' not in source and "'offset' in batch" not in source:
    if not {config._VERIFIED_RUNTIME_SIGNATURE is not None}:
        raise RuntimeError(
            'ColabDesign backend does not consume cyclic pairwise offset '
            'and module-level functional verification has not passed - '
            'cyclic geometry may be broken'
        )
"""


def _refold_script_core(sequence, L):
    """Generated-script middle: model setup and cyclic-offset injection."""
    return f"""
model = mk_af_model(protocol='hallucination', data_dir={COLABDESIGN_PARAMS!r})
model.prep_inputs(length={L})
model.restart(seed=0, seq={sequence!r})

i = np.arange({L})
ij = np.stack([i, i+{L}], -1)
offset = i[:,None] - i[None,:]
c_offset = np.abs(ij[:,None,:,None] - ij[None,:,None,:]).min((2,3))
a = c_offset < np.abs(offset)
c_offset[a] = -c_offset[a]
c_offset = c_offset * np.sign(offset)
# Smoke test: verify cyclic offset was actually applied.
# A zero matrix means the ColabDesign cyclic-offset code path was
# not executed, which would silently produce a linear peptide.
if not np.any(c_offset):
    raise RuntimeError(
        'cyclic offset matrix is all-zero - '
        'ColabDesign cyclic geometry was not applied'
    )
idx = np.array(model._inputs['residue_index'])
off = np.array(idx[:,None] - idx[None,:])
off[:{L}, :{L}] = c_offset
model._inputs['offset'] = off
"""


def _refold_script_epilogue(sequence, output_pdb):
    """Generated-script tail: predict, verify drift, persist PDB and pLDDT."""
    return f"""
aux = model.predict(
    seq={sequence!r}, seed=0, models=[0], num_models=1, num_recycles=3,
    sample_models=False, dropout=False, hard=True, soft=False,
    verbose=False, return_aux=True,
)
observed = model.get_seq(get_best=False)
if observed != [{sequence!r}]:
    raise RuntimeError(
        'fixed-sequence refold drift: requested=' + repr([{sequence!r}])
        + ' observed=' + repr(observed)
    )
model.save_pdb({str(output_pdb)!r}, get_best=False, aux=aux)

aa3 = {{
    'ALA':'A','ARG':'R','ASN':'N','ASP':'D','CYS':'C','GLN':'Q','GLU':'E',
    'GLY':'G','HIS':'H','ILE':'I','LEU':'L','LYS':'K','MET':'M','PHE':'F',
    'PRO':'P','SER':'S','THR':'T','TRP':'W','TYR':'Y','VAL':'V'
}}
chains, seen = {{}}, set()
with open({str(output_pdb)!r}) as handle:
    for line in handle:
        if line.startswith('ENDMDL'):
            break
        if not line.startswith('ATOM') or line[12:16].strip() != 'CA':
            continue
        if len(line) < 27:
            continue
        key = (line[21].strip() or '_', line[22:27])
        if key in seen:
            continue
        seen.add(key)
        chains.setdefault(key[0], []).append(aa3.get(line[17:20].strip(), 'X'))
pdb_sequences = {{chain: ''.join(values) for chain, values in chains.items()}}
if len(pdb_sequences) != 1 or list(pdb_sequences.values()) != [{sequence!r}]:
    raise RuntimeError(
        'fixed-sequence PDB mismatch: requested=' + repr({sequence!r})
        + ' observed=' + repr(pdb_sequences)
    )
plddt = float(np.mean(aux['plddt']))
with open({f'{output_pdb}.plddt'!r}, 'w') as pf:
    pf.write(str(plddt))
clear_mem()
"""


def _build_refold_script(sequence, output_pdb):
    """Build a fixed-sequence AfCycDesign prediction script.

    ``design_3stage`` optimizes sequence logits and therefore cannot be used
    for refolding an already designed LigandMPNN sequence.  Prediction uses
    ``predict(seq=...)`` and verifies both ColabDesign's hard sequence and the
    emitted PDB before the manifest is allowed downstream.
    """
    if not _validate_sequence(sequence):
        raise ValueError(
            f"refold sequence must contain {MIN_CYCLIC_PEPTIDE_LENGTH}-"
            f"{MAX_CYCLIC_PEPTIDE_LENGTH} standard amino acids"
        )
    L = len(sequence)
    return "".join([
        _refold_script_prologue(),
        _refold_script_core(sequence, L),
        _refold_script_epilogue(sequence, output_pdb),
    ])

def _refold_script_path(sequence):
    """Temp-script path bound to the caller PID and sequence hash."""
    return os.path.join(
        tempfile.gettempdir(),
        f"refold_{os.getpid()}_{hashlib.sha256(sequence.encode()).hexdigest()[:16]}.py"
    )


def _clear_refold_artifacts(output_pdb, plddt_file):
    """Delete stale outputs so a failed retry can never expose old data."""
    # A failed retry must never inherit a PDB or score produced by an older run.
    # If we cannot guarantee a clean slate we must refuse to proceed, otherwise
    # downstream consumers read stale data and every metric becomes fake.
    for stale_artifact in (output_pdb, plddt_file):
        try:
            os.unlink(stale_artifact)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RuntimeError(
                f"cannot remove stale artifact {stale_artifact!r} - "
                f"refusing to run with potentially contaminated output: {exc}"
            ) from exc


def _run_refold_subprocess(spath, output_pdb, plddt_file, sequence):
    """Execute the generated AfCycDesign script and read the pLDDT score."""
    try:
        _refold_timeout = int(os.environ.get("REFOLD_TIMEOUT") or "1200")
    except (ValueError, TypeError):
        _refold_timeout = 1200
    try:
        r = subprocess.run([CYCPEP_PYTHON, spath], capture_output=True, text=True,
            timeout=_refold_timeout,
            env={**os.environ,
                 "XLA_FLAGS": f"--xla_gpu_cuda_data_dir={CUDA_DATA_DIR}",
                 "XLA_PYTHON_CLIENT_PREALLOCATE": "false",
                 "XLA_PYTHON_CLIENT_MEM_FRACTION": "0.80"})
        if r.returncode != 0:
            EvidenceLogger.error("design", "refold_nonzero",
                f"exit={r.returncode} stderr={r.stderr[-200:]}")
            return None
        if not os.path.isfile(output_pdb) or not os.path.isfile(plddt_file):
            EvidenceLogger.error(
                "design", "refold_artifact_missing",
                f"fixed-sequence refold did not produce {output_pdb} and score",
            )
            return None
        _verify_fixed_sequence_pdb(output_pdb, sequence)
        with open(plddt_file) as pf:
            plddt = float(pf.read().strip())
        if not math.isfinite(plddt):
            raise ValueError(f"refold pLDDT is non-finite: {plddt!r}")
        if plddt < 0.0:
            raise ValueError(f"refold pLDDT is negative: {plddt!r}")
        # ColabDesign may return 0-1 (normalised) or 0-100 (raw AlphaFold)
        # depending on the installed version.  Normalise both to 0-1 so
        # downstream consumers always see a consistent scale (P0-2).
        if plddt > 1.0:
            if plddt > 100.0:
                raise ValueError(f"refold pLDDT out of range: {plddt!r}")
            plddt = plddt / 100.0
        return plddt
    except ValueError as e:
        # Distinguish sequence drift (scientific integrity) from subprocess failures.
        if "sequence" in str(e).lower() or "drift" in str(e).lower() or "mismatch" in str(e).lower():
            EvidenceLogger.error("design", "sequence_drift",
                f"refold PDB sequence diverged from input: {e}")
        else:
            EvidenceLogger.error("design", "refold_exception", str(e))
        return None
    except (subprocess.SubprocessError, OSError, RuntimeError) as e:
        EvidenceLogger.error("design", "refold_exception", str(e))
        return None


def _run_refold(sequence, output_pdb):
    """AfCycDesign refold: fold the fixed sequence as a cyclic peptide.

    Only basic fold verification is done here; the final pLDDT > 0.8 gate is
    owned by the Prediction Agent's L1 layer.
    """
    # Lazily verify ColabDesign cyclic-offset wiring once per process (P1-3).
    if config._VERIFIED_RUNTIME_SIGNATURE is None:
        _verify_colabdesign_runtime()
    script = _build_refold_script(sequence, output_pdb)
    spath = _refold_script_path(sequence)
    plddt_file = f"{output_pdb}.plddt"
    _clear_refold_artifacts(output_pdb, plddt_file)
    with open(spath, "w") as f:
        f.write(script)
    try:
        return _run_refold_subprocess(spath, output_pdb, plddt_file, sequence)
    finally:
        try:
            os.unlink(spath)
        except OSError:
            pass
