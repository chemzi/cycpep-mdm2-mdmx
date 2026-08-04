"""Route C: binder-template cyclization + deterministic mutation expansion."""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
from pathlib import Path

from data_layer import CandidateIndex, EvidenceLogger  # noqa: E402

from . import config  # noqa: E402
from .config import (  # noqa: E402
    CYCLIZATION_PAIRS,
    DESIGN_PIPELINE_VERSION,
    LINKER_MATRIX,
    OUTPUT_DIR,
    SCAFFOLD_MUTABLE_AA,
)
from .manifests import _candidate_from_manifest, _write_manifest  # noqa: E402
from .runtime import _run_refold, _run_rfdiff  # noqa: E402
from .service import (  # noqa: E402
    _load_existing_sequences,
    _load_target_spec,
    _merge_config,
    _next_candidate_id,
    _require_mdm_reference_route,
)
from .validation import (  # noqa: E402
    _binder_first_contig,
    _describe_cyclize,
    _infer_binder_chain,
    _infer_cyclization_type,
    _parse_hotspot_residues,
    _pdb_residue_range,
    _ring_closure_check,
    _synthesizability_violations,
    _validate_sequence,
)
from project_config import target_slug  # noqa: E402


def _route_c_cyclization_pairs(modality):
    """Return only cyclization chemistries allowed by the project contract.

    Route C can construct terminal-disulfide and head-to-tail templates, but a
    concrete project may support only one of them downstream.  Filtering here
    prevents Design from spending GPU time on a candidate that Prediction is
    contractually unable to ingest.
    """
    normalized = str(modality or "cyclic_peptide").strip().lower().replace("-", "_")
    if normalized in {"head_to_tail_cyclic_peptide", "head_to_tail_amide"}:
        return [("", "")]
    if normalized in {
        "disulfide_cyclic_peptide",
        "cys_cys_disulfide",
        "terminal_disulfide_cyclic_peptide",
    }:
        return [("C", "C")]
    if normalized in {"cyclic_peptide", "generic_cyclic_peptide"}:
        return list(CYCLIZATION_PAIRS)
    raise ValueError(
        f"Route C does not support project modality {modality!r}; supported "
        "modalities are head-to-tail, terminal-disulfide, or generic cyclic peptide"
    )

def _route_c_base_combos(template_seq, allowed_lengths, modality):
    """Build Route C templates that satisfy chemistry, length, and synthesis gates."""
    length_set = {int(length) for length in allowed_lengths}
    combos = []
    for linker in LINKER_MATRIX:
        for cn, cc in _route_c_cyclization_pairs(modality):
            seq = f"{cn}{template_seq}{linker}{cc}"
            if len(seq) not in length_set or not _validate_sequence(seq):
                continue
            violations = _synthesizability_violations(seq)
            # A head-to-tail closure does not require terminal Cys; internal
            # Cys may be chemically valid and is therefore not a hard failure.
            if cn == "" and cc == "":
                violations = [v for v in violations if "stray_cys" not in v]
            if not violations:
                combos.append((seq, _describe_cyclize(cn, cc, linker)))
    return combos

def _route_c_design_references(config, batch_dir, sequences):
    """Generate one independent target-bound RFdiffusion backbone per sequence.

    Route C is sequence/scaffold driven, but Prediction L7 still needs a
    structural hypothesis that was produced independently of the fixed-sequence
    refold.  The returned mapping is keyed by the sequence's stable position in
    *sequences*.  Missing or ambiguous RFdiffusion outputs are omitted so the
    caller can fail closed before registering a candidate.
    """
    indexed_by_length = {}
    for index, (sequence, _description) in enumerate(sequences):
        indexed_by_length.setdefault(len(sequence), []).append(index)

    hotspots = _parse_hotspot_residues(config.get("hotspots", ""))
    target_start, target_end = _pdb_residue_range(
        config["target_pdb"], config["chain"], hotspot_residues=hotspots
    )
    references = {}
    for length, indexes in sorted(indexed_by_length.items()):
        backbone_dir = Path(batch_dir) / f"design_references_len{length}"
        backbone_dir.mkdir(parents=True, exist_ok=True)
        output_prefix = str(backbone_dir / "bb")
        completed = _run_rfdiff(
            target_pdb=config["target_pdb"],
            binder_len=length,
            n_designs=len(indexes),
            output_prefix=output_prefix,
            contig=_binder_first_contig(
                config["chain"], target_start, target_end, length
            ),
            seed=config["seed"],
            hotspots=config.get("hotspots"),
            chain=config["chain"],
        )
        if not completed:
            EvidenceLogger.error(
                "design",
                "route_c_design_reference_generation_failed",
                f"RFdiffusion failed for Route C length {length}",
                recovery="regenerate this Route C length before Prediction",
            )
            continue

        def sort_key(path):
            try:
                return int(path.stem.rsplit("_", 1)[-1])
            except (ValueError, IndexError):
                return -1

        valid_backbones = []
        for backbone_path in sorted(backbone_dir.glob("bb_*.pdb"), key=sort_key):
            try:
                _infer_binder_chain(
                    str(backbone_path), length, receptor_chain=config["chain"]
                )
            except (OSError, UnicodeError, ValueError) as exc:
                EvidenceLogger.error(
                    "design",
                    "route_c_design_reference_invalid",
                    f"{backbone_path}: {exc}",
                    recovery="skip ambiguous Route C reference backbone",
                )
                continue
            valid_backbones.append(backbone_path)

        for index, backbone_path in zip(indexes, valid_backbones):
            references[index] = str(backbone_path)
        if len(valid_backbones) < len(indexes):
            EvidenceLogger.error(
                "design",
                "route_c_design_reference_incomplete",
                {
                    "length": length,
                    "required": len(indexes),
                    "valid": len(valid_backbones),
                },
                recovery="register only candidates with an independent reference",
            )
    return references

def design_atsp_derived(target_spec=None, design_config=None):
    """模板环化：linker × 环化矩阵 + 随机突变扩展 + refold 验证
    ── 适配 Research 产出的任意 binder，不再死绑 ATSP-7041。"""
    config = _merge_config(target_spec, design_config)
    _require_mdm_reference_route("route_C_atsp")
    n = config.get("n", 200)
    seed = config["seed"]  # _merge_config already resolves None → timestamp
    import random
    rng = random.Random(seed)

    route_name = f"route_C_atsp_{target_slug(config['target_id'])}"
    batch_id = f"batch_atsp_{int(time.time())}_s{seed}_{os.urandom(4).hex()}"
    batch_dir = os.path.join(OUTPUT_DIR, "route_C", batch_id)
    os.makedirs(batch_dir, exist_ok=True)

    with open(os.path.join(batch_dir, "design_config.json"), "w") as f:
        json.dump(config, f, indent=2)

    # 从 Research 已知 binder 中选模板：优先 ATSP-7041，否则取第一个有序列的
    spec = _load_target_spec()
    binders = spec.get("known_dual_binders", [])
    template_seq = None
    template_name = None
    fallback = None
    for b in binders:
        seq_candidate = b.get("sequence") or b.get("seq", "")
        if not seq_candidate or not _validate_sequence(seq_candidate):
            continue
        if fallback is None:
            fallback = (seq_candidate, b.get("name", "unknown"))
        if "ATSP" in b.get("name", "").upper():
            template_seq, template_name = seq_candidate, b["name"]
            break
    if template_seq is None and fallback is not None:
        template_seq, template_name = fallback
        EvidenceLogger.log("design", "route_c_fallback_binder", {
            "template": template_name,
            "sequence": template_seq,
            "note": "ATSP-7041 not found in Research output; using first available "
                    "binder as cyclization template",
        })
    if not template_seq:
        EvidenceLogger.error("design", "no_binder_for_route_c",
            "known_dual_binders 中无可用的 binder 序列 — 先跑 Research Agent",
            recovery="确保 Research 产出的 known_dual_binders 包含带 sequence 的条目")
        return []
    # 标准化模板序列：去除小写/修饰符，与 _validate_sequence 的内部归一化一致，
    # 否则 AfCycDesign refold 收到非标准氨基酸会静默失败（P0-3）。
    template_seq = template_seq.upper().replace("-", "").replace("*", "")
    # Route C 序列设计同时遵守项目环化类型和获批长度。这样 Design 不会生成
    # 当前 Prediction 合同无法处理的化学类型，也不会静默忽略 --lengths。
    try:
        allowed_pairs = _route_c_cyclization_pairs(config["modality"])
        base_combos = _route_c_base_combos(
            template_seq,
            config["lengths"],
            config["modality"],
        )
    except ValueError as exc:
        EvidenceLogger.error(
            "design",
            "route_c_unsupported_modality",
            {
                "modality": config.get("modality"),
                "detail": str(exc),
            },
            recovery="select a Route C-supported cyclization modality or add an explicit chemistry adapter",
        )
        return []

    if not base_combos:
        EvidenceLogger.error("design", "route_c_empty",
            {
                "template": template_name,
                "template_length": len(template_seq),
                "allowed_lengths": config["lengths"],
                "modality": config["modality"],
                "attempted_combinations": len(LINKER_MATRIX) * len(allowed_pairs),
                "reason": "all combinations failed length or synthesizability gates",
            },
            recovery="review approved lengths, template sequence, and synthesizability rules")
        return []

    # 第2级：不够 n 则随机突变扩展；基础组合需先按已有序列去重
    # F/W/L 药效团位点保护在 L730 通过 if seq[ix] in "FWL": continue 实现
    seen_seqs = _load_existing_sequences() or set()  # cross-batch dedup (None → set())
    expanded = []
    for s, d in base_combos:
        if s not in seen_seqs:
            seen_seqs.add(s)
            expanded.append((s, d))
    attempts = 0
    while len(expanded) < n and attempts < n * 10:
        attempts += 1
        seq, desc = rng.choice(base_combos)
        aa = rng.choice(SCAFFOLD_MUTABLE_AA)
        off = 1 if seq and seq[0] == "C" else 0
        tail_guard = 1 if seq and seq[-1] == "C" else 0
        max_pos = len(seq) - off - tail_guard  # mutable core length
        if max_pos < 2:
            continue  # nowhere to mutate without breaking a terminal Cys
        pos = rng.randint(1, max_pos)
        ix = off + pos - 1
        # 保护 F/W/L 药效团位点（ATSP-7041 核心锚点）
        if seq[ix] in "FWL":
            continue
        # 同义突变不改变序列，浪费 attempts budget（P1-4）
        if aa == seq[ix]:
            continue
        mutated = seq[:ix] + aa + seq[ix+1:]
        if _validate_sequence(mutated) and mutated not in seen_seqs:
            violations = _synthesizability_violations(mutated)
            # head-to-tail 父本允许内部 Cys（只有 Cys-Cys 环化才严格要求末端 Cys）
            if seq[0] != "C" and seq[-1] != "C":
                violations = [v for v in violations if "stray_cys" not in v]
            if not violations:
                seen_seqs.add(mutated)
                expanded.append((mutated, f"{desc},mut:{ix+1}={aa}"))

    if len(expanded) < n:
        EvidenceLogger.log("design", "route_c_under_target", {
            "target": n,
            "achieved": len(expanded),
            "base_combos": len(base_combos),
            "attempts": attempts,
            "reason": "mutation space exhausted — consider increasing n*10 "
                      "budget or relaxing synthesizability gates",
        })

    selected_sequences = expanded[:n]
    design_references = _route_c_design_references(
        config, batch_dir, selected_sequences
    )

    candidates = []
    total_gen, total_valid = 0, 0
    t_batch = time.time()

    for sequence_index, (seq, desc) in enumerate(selected_sequences):
        backbone_pdb = design_references.get(sequence_index)
        if not backbone_pdb:
            EvidenceLogger.error(
                "design",
                "route_c_design_reference_unavailable",
                {
                    "sequence_index": sequence_index,
                    "sequence_length": len(seq),
                    "sequence_sha256": hashlib.sha256(seq.encode()).hexdigest(),
                },
                recovery="do not register or predict this candidate; regenerate Design",
            )
            continue
        total_gen += 1
        cid = _next_candidate_id()
        refold_dir = os.path.join(batch_dir, "candidates", cid)
        os.makedirs(refold_dir, exist_ok=True)
        refold_pdb = os.path.join(refold_dir, "refold.pdb")
        plddt = _run_refold(seq, refold_pdb)
        cyclization_type = _infer_cyclization_type(seq)
        try:
            rc = (
                _ring_closure_check(refold_pdb, cyclization_type, sequence=seq)
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
                    backbone_pdb=backbone_pdb,
                    cyclization=cyclization_type, ring_closure=rc,
                )
            except ValueError as exc:
                EvidenceLogger.error("design", "manifest_cyclization_mismatch",
                    str(exc), recovery="skip mismatched candidate (P1-7)")
                continue
            candidate = _candidate_from_manifest(manifest, plddt, notes={"design": desc})
            CandidateIndex.add(candidate)
            EvidenceLogger.log("design", "candidate_registered",
                {"candidate": candidate},
                targets=[config["target_id"]], phase="design")
            candidates.append(candidate)
        else:
            EvidenceLogger.error("design", "refold_failed",
                {
                    "candidate_id": cid,
                    "pLDDT": plddt,
                    "cyclization_type": cyclization_type,
                    "ring_closure": rc,
                },
                recovery="skip candidate; inspect ring_closure.reason before rerunning")

    EvidenceLogger.design_batch(route=route_name, n_generated=total_gen,
        n_valid=total_valid, tool_name="atsp_derived",
        tool_version=DESIGN_PIPELINE_VERSION,
        duration_sec=round(time.time()-t_batch, 1))
    return candidates
