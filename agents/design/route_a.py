"""Route A: RFpeptides free backbone generation -> LigandMPNN -> refold."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from data_layer import CandidateIndex, EvidenceLogger  # noqa: E402

from . import config  # noqa: E402
from .config import DESIGN_PIPELINE_VERSION, OUTPUT_DIR  # noqa: E402
from .manifests import _candidate_from_manifest, _write_manifest  # noqa: E402
from .runtime import _run_ligandmpnn, _run_refold, _run_rfdiff  # noqa: E402
from .service import (  # noqa: E402
    _load_existing_sequences,
    _merge_config,
    _next_candidate_id,
)
from .validation import (  # noqa: E402
    _binder_first_contig,
    _cheap_filter_sequences,
    _infer_binder_chain,
    _infer_cyclization_type,
    _parse_hotspot_residues,
    _pdb_residue_range,
    _ring_closure_check,
)
from project_config import target_slug  # noqa: E402


def design_rfpeptides(target_spec=None, design_config=None):
    """RFpeptides → LigandMPNN → AfCycDesign refold"""
    config = _merge_config(target_spec, design_config)
    route_name = f"route_A_{target_slug(config['target_id'])}"
    batch_id = f"batch_rfpep_{config['target_name']}_s{config['seed']}"
    batch_dir = os.path.join(OUTPUT_DIR, "route_A", batch_id)
    os.makedirs(batch_dir, exist_ok=True)

    with open(os.path.join(batch_dir, "design_config.json"), "w") as f:
        json.dump(config, f, indent=2, default=str)

    candidates = []
    total_gen, total_valid = 0, 0
    t_batch = time.time()
    _hotspots = _parse_hotspot_residues(config.get("hotspots", ""))
    target_range = _pdb_residue_range(config["target_pdb"], config["chain"],
                                      hotspot_residues=_hotspots)
    seen_seqs = _load_existing_sequences() or set()  # cross-batch dedup (None → set())

    # Pass 1: RFdiffusion + LigandMPNN → collect all raw sequences across
    # every backbone so global scoring is not biased by backbone order (P1-2).
    backbone_entries = []  # (bb_path, binder_chain, raw_seqs)

    for L in config["lengths"]:
        n_designs = max(1, config["n"] // len(config["lengths"]))
        backbone_dir = os.path.join(batch_dir, f"backbones_len{L}")
        os.makedirs(backbone_dir, exist_ok=True)
        rfdiff_ok = _run_rfdiff(
            target_pdb=config["target_pdb"], binder_len=L,
            n_designs=n_designs, output_prefix=f"{backbone_dir}/bb",
            contig=_binder_first_contig(
                config["chain"], target_range[0], target_range[1], L
            ),
            seed=config["seed"],
            hotspots=config.get("hotspots"),
            chain=config["chain"])
        if not rfdiff_ok:
            print(f"[Route A] RFdiff 失败 len={L}，跳过")
            continue

        def _bb_sort_key(p):
            try:
                return int(p.stem.split('_')[-1])
            except (ValueError, IndexError):
                return 0  # P1-1: non-standard filename, sort to front
        bb_files = sorted(Path(backbone_dir).glob("bb_*.pdb"), key=_bb_sort_key)
        print(f"[Route A] RFdiff 完成, 找到 {len(bb_files)} 个骨架PDB")
        for bb_path in bb_files[:n_designs]:
            total_gen += 1
            try:
                binder_chain = _infer_binder_chain(str(bb_path), L, receptor_chain=config["chain"])
            except (OSError, UnicodeError, ValueError) as exc:
                EvidenceLogger.error(
                    "design", "rfdiff_binder_chain_invalid",
                    f"{bb_path}: {exc}", recovery="skip ambiguous backbone",
                )
                continue
            mpnn_dir = os.path.join(batch_dir, f"mpnn_{bb_path.stem}")
            os.makedirs(mpnn_dir, exist_ok=True)
            mpnn_seed = (config["seed"] + total_gen) % 2**31
            seqs = _run_ligandmpnn(
                str(bb_path), mpnn_dir, n_seq=8, binder_chain=binder_chain,
                seed=mpnn_seed,
            )
            if not seqs:
                print(f"[Route A] LigandMPNN 返回 0 条序列: {bb_path.name}")
                continue
            backbone_entries.append((bb_path, binder_chain, seqs))

    # Pass 2: global cheap filter — score ALL sequences together so early
    # backbones cannot starve later ones (P1-2).
    all_raw_seqs = []
    bb_lookup = {}  # seq.upper() → [(bb_path, binder_chain), ...]  (P2-1)
    for bb_path, binder_chain, seqs in backbone_entries:
        for s in seqs:
            key = s.upper() if isinstance(s, str) else ""
            if key:
                bb_lookup.setdefault(key, []).append((bb_path, binder_chain))
        all_raw_seqs.extend(seqs)

    filtered = _cheap_filter_sequences(all_raw_seqs, seen_seqs=seen_seqs, top_k=config["n"])
    print(f"[Route A] global cheap filter: {len(all_raw_seqs)}→{len(filtered)} sequences")

    for seq, quality_score in filtered:
        bb_list = bb_lookup.get(seq)
        if not bb_list:
            continue
        # Use the first backbone that produced this sequence; if multiple
        # backbones produced the same sequence, note it in the manifest.
        bb_path, binder_chain = bb_list[0]
        bb_alternatives = [str(bp) for bp, _ in bb_list[1:]] if len(bb_list) > 1 else []
        cid = _next_candidate_id()
        refold_dir = os.path.join(batch_dir, "candidates", cid)
        os.makedirs(refold_dir, exist_ok=True)
        refold_pdb = os.path.join(refold_dir, "refold.pdb")
        plddt = _run_refold(seq, refold_pdb)
        cyclization = _infer_cyclization_type(seq)
        try:
            rc = (
                _ring_closure_check(refold_pdb, cyclization, sequence=seq)
                if os.path.exists(refold_pdb)
                else {"pass": False, "reason": "refold_pdb_missing"}
            )
        except (ValueError, OSError) as exc:
            rc = {"pass": False, "reason": f"closure_check_error: {exc}"}

        if plddt is not None and rc.get("pass"):
            total_valid += 1
            try:
                manifest = _write_manifest(
                    cid, seq, route_name, batch_id, refold_pdb, config,
                    backbone_pdb=str(bb_path), cyclization=cyclization,
                    ring_closure=rc, bb_alternatives=bb_alternatives,
                )
            except ValueError as exc:
                EvidenceLogger.error("design", "manifest_cyclization_mismatch",
                    str(exc), recovery="skip mismatched candidate (P1-7)")
                continue
            candidate = _candidate_from_manifest(
                manifest, plddt,
                notes={"quality_score": round(quality_score, 3)},
            )
            CandidateIndex.add(candidate)
            EvidenceLogger.log("design", "candidate_registered",
                {"candidate": candidate},
                targets=[config["target_id"]], phase="design")
            candidates.append(candidate)
        else:
            print(f"[Route A] refold失败: {cid} pLDDT={plddt} ring_closed={rc.get('pass')}")

    EvidenceLogger.design_batch(route=route_name, n_generated=total_gen,
        n_valid=total_valid, tool_name="rfpeptides_pipeline",
        tool_version=DESIGN_PIPELINE_VERSION,
        duration_sec=round(time.time()-t_batch, 1))
    return candidates
