"""Route A: RFpeptides free backbone generation -> LigandMPNN -> refold."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from data_layer import CandidateIndex, EvidenceLogger  # noqa: E402

from . import config  # noqa: E402
from .candidates import _collect_raw_sequences, _register_refolded_candidate  # noqa: E402
from .config import DESIGN_PIPELINE_VERSION, DESIGN_PROTOCOL, DesignContext  # noqa: E402
from .runtime import _run_ligandmpnn, _run_rfdiff  # noqa: E402
from .service import (  # noqa: E402
    _load_existing_sequences,
    _merge_config,
    _next_candidate_id,
)
from .validation import (  # noqa: E402
    _binder_first_contig,
    _cheap_filter_sequences,
    _infer_binder_chain,
    _parse_hotspot_residues,
    _pdb_residue_range,
)
from project_config import target_slug  # noqa: E402


def _route_a_generate_backbones(config, batch_dir):
    """Pass 1: RFdiffusion + LigandMPNN per length; returns (backbone_entries, total_gen).

    Collects every raw sequence so the global cheap filter can score them
    together (P1-2) instead of biasing results by backbone order.
    """
    hotspots = _parse_hotspot_residues(config.get("hotspots", ""))
    target_range = _pdb_residue_range(
        config["target_pdb"], config["chain"], hotspot_residues=hotspots
    )
    backbone_entries = []  # (bb_path, binder_chain, raw_seqs)
    total_gen = 0
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
            print(f"[Route A] RFdiff \u5931\u8d25 len={L}\uff0c\u8df3\u8fc7")
            continue

        def _bb_sort_key(p):
            try:
                return int(p.stem.split('_')[-1])
            except (ValueError, IndexError):
                return 0  # P1-1: non-standard filename, sort to front
        bb_files = sorted(Path(backbone_dir).glob("bb_*.pdb"), key=_bb_sort_key)
        print(f"[Route A] RFdiff \u5b8c\u6210, \u627e\u5230 {len(bb_files)} \u4e2a\u9aa8\u67b6PDB")
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
                str(bb_path), mpnn_dir, n_seq=DESIGN_PROTOCOL["parameters"]["ligandmpnn"]["n_seq_per_backbone"],
                binder_chain=binder_chain,
                seed=mpnn_seed,
            )
            if not seqs:
                print(f"[Route A] LigandMPNN \u8fd4\u56de 0 \u6761\u5e8f\u5217 {bb_path.name}")
                continue
            backbone_entries.append((bb_path, binder_chain, seqs))
    return backbone_entries, total_gen

def design_rfpeptides(target_spec=None, design_config=None, context=None):
    """RFpeptides \u2192 LigandMPNN \u2192 AfCycDesign refold"""
    ctx = context if context is not None else DesignContext.default()
    # B3: 失败经验库闭环——上一轮淘汰原因驱动本轮长度偏好（证据不足时不调整）
    from experience import apply_experience_preference  # noqa: E402
    design_config, _hint = apply_experience_preference(design_config)
    config = _merge_config(target_spec, design_config, project_config=ctx.project_config)
    route_name = f"route_A_{target_slug(config['target_id'])}"
    batch_id = f"batch_rfpep_{config['target_name']}_s{config['seed']}"
    batch_dir = os.path.join(str(ctx.output_dir), "route_A", batch_id)
    os.makedirs(batch_dir, exist_ok=True)

    with open(os.path.join(batch_dir, "design_config.json"), "w") as f:
        json.dump(config, f, indent=2, default=str)

    candidates = []
    total_gen, total_valid = 0, 0
    t_batch = time.time()
    seen_seqs = _load_existing_sequences() or set()  # cross-batch dedup (None \u2192 set())

    # Pass 1: RFdiffusion + LigandMPNN \u2192 collect all raw sequences across
    # every backbone so global scoring is not biased by backbone order (P1-2).
    backbone_entries, total_gen = _route_a_generate_backbones(config, batch_dir)

    # Pass 2: global cheap filter \u2014 score ALL sequences together so early
    # backbones cannot starve later ones (P1-2).
    all_raw_seqs, bb_lookup = _collect_raw_sequences(backbone_entries)

    filtered = _cheap_filter_sequences(all_raw_seqs, seen_seqs=seen_seqs, top_k=config["n"])
    print(f"[Route A] global cheap filter: {len(all_raw_seqs)}\u2192{len(filtered)} sequences")

    for seq, quality_score in filtered:
        bb_list = bb_lookup.get(seq)
        if not bb_list:
            continue
        # Use the first backbone that produced this sequence; if multiple
        # backbones produced the same sequence, note it in the manifest.
        bb_path, binder_chain = bb_list[0]
        bb_alternatives = [str(bp) for bp, _ in bb_list[1:]] if len(bb_list) > 1 else []
        cid = _next_candidate_id()
        registration = _register_refolded_candidate(
            candidate_id=cid, sequence=seq, config=config,
            batch_dir=batch_dir, route_name=route_name, batch_id=batch_id,
            backbone_pdb=str(bb_path), bb_alternatives=bb_alternatives,
            notes={"quality_score": round(quality_score, 3)},
        )
        if registration.candidate is not None:
            total_valid += 1
            candidates.append(registration.candidate)
        else:
            print(f"[Route A] refold\u5931\u8d25: {cid} pLDDT={registration.plddt} "
                  f"ring_closed={registration.ring_closure.get('pass')}")

    EvidenceLogger.design_batch(route=route_name, n_generated=total_gen,
        n_valid=total_valid, tool_name="rfpeptides_pipeline",
        tool_version=DESIGN_PIPELINE_VERSION,
        duration_sec=round(time.time()-t_batch, 1))
    return candidates
