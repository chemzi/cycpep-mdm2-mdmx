"""Pure scientific validation helpers for the Design Agent.

No external tool subprocesses run here.  Functions may emit audit records via
``EvidenceLogger`` but must not mutate CandidateIndex or State.
"""

from __future__ import annotations

import math
from pathlib import Path

from data_layer import EvidenceLogger  # noqa: E402

from . import config  # noqa: E402
from .config import (  # noqa: E402
    CHEAP_FILTER_MAX_KEEP,
    CLOSURE_GEOMETRY,
    HYDROPHOBIC,
    NEG_CHARGED,
    POS_CHARGED,
    SCAFFOLD_MUTABLE_AA,
)
from peptide_contract import (  # noqa: E402
    MAX_CYCLIC_PEPTIDE_LENGTH,
    MIN_CYCLIC_PEPTIDE_LENGTH,
    STANDARD_AMINO_ACIDS,
    supported_length_message,
)


def _cheap_filter_sequences(seqs, seen_seqs=None, top_k=CHEAP_FILTER_MAX_KEEP):
    """
    便宜预筛（无 GPU）：合成可行性 + 基本理化性质。
    返回 top_k 条最优序列，格式 [(seq, score), ...]。
    选中的序列会回写到 ``seen_seqs`` 供跨 backbone 去重。
    """
    if seen_seqs is None:
        seen_seqs = set()
    seqs = list(seqs or [])
    scored = []
    violation_counts = {}
    for seq in seqs:
        if not isinstance(seq, str) or not seq:
            continue
        if seq.upper() in seen_seqs:
            continue
        violations = _synthesizability_violations(seq)
        if violations:
            for v_reason in violations:
                # Normalize dynamic keys like "stray_cys_at_[1,3]" to "stray_cys"
                base_reason = v_reason.split("_at_")[0]
                violation_counts[base_reason] = violation_counts.get(base_reason, 0) + 1
            continue  # 硬淘汰
        score = _sequence_quality_score(seq)
        scored.append((seq.upper(), score))
    scored.sort(key=lambda x: (x[1], x[0]), reverse=True)
    result = scored[:top_k]
    if not result and seqs:
        EvidenceLogger.log("design", "cheap_filter_empty", {
            "total": len(seqs),
            "already_seen": sum(1 for s in seqs if isinstance(s, str) and s.upper() in seen_seqs),
            "top_k": top_k,
            "violation_distribution": violation_counts,
        })
    for seq, _score in result:
        seen_seqs.add(seq)
    return result

def _synthesizability_violations(seq):
    """
    检查 Kickoff 定义的可合成性规则。返回违规列表，空列表 = 通过。
    - 聚集：连续 >4 个疏水氨基酸
    - 游离 Cys：不在 N/C 端的 Cys
    - 氧化：Met / Trp（软警告，不硬淘汰）
    - 脱酰胺：Asn-Gly
    - Asp-Pro 断裂
    """
    if not seq:
        return ["empty_sequence"]
    seq = seq.upper()  # P1-2: normalise case for all downstream checks
    v = []
    # 连续疏水（线性扫描）
    run = 0
    for aa in seq:
        if aa in HYDROPHOBIC:
            run += 1
        else:
            run = 0
        if run > 4:
            v.append("aggregation")
            break
    # 环化交界面：N端和C端环化后相邻，检查跨边界的连续疏水
    if "aggregation" not in v:
        tail_run = 0
        for aa in reversed(seq):
            if aa in HYDROPHOBIC:
                tail_run += 1
            else:
                break
        head_run = 0
        for aa in seq:
            if aa in HYDROPHOBIC:
                head_run += 1
            else:
                break
        if tail_run + head_run > 4:
            v.append("aggregation")
    # 游离 Cys（不在首尾）— 收集全部位置
    stray_positions = [
        i for i, aa in enumerate(seq)
        if aa == "C" and i not in (0, len(seq) - 1)
    ]
    if stray_positions:
        v.append(f"stray_cys_at_{stray_positions}")
    # Asn-Gly 脱酰胺
    for i in range(len(seq) - 1):
        if seq[i:i+2] == "NG":
            v.append("deamidation_NG")
            break
    # Asp-Pro 断裂（环肽中 N→C 也检查）
    for i in range(len(seq) - 1):
        if seq[i:i+2] == "DP":
            v.append("dp_cleavage")
            break
    # 环化连接 bond：C-term(seq[-1]) → N-term(seq[0])
    junction = seq[-1] + seq[0]
    if junction == "NG":
        v.append("deamidation_NG_cyclic")
    if junction == "DP":
        v.append("dp_cleavage_cyclic")
    return v

def _sequence_quality_score(seq):
    """
    序列质量评分（越高越好），基于：
    - 疏水/亲水平衡（0.3-0.7 区间最优）
    - 净电荷适中（-1 到 +1 最优）
    - 氨基酸多样性
    """
    L = len(seq)
    seq = seq.upper()  # normalise case for all downstream checks
    h = sum(1 for aa in seq if aa in HYDROPHOBIC) / max(L, 1)
    pos = sum(1 for aa in seq if aa in POS_CHARGED)
    neg = sum(1 for aa in seq if aa in NEG_CHARGED)
    net = (pos - neg) / max(L, 1)
    diversity = len(set(seq)) / max(L, 1)

    # 疏水平衡分：离 0.5 越近越好
    h_score = 1.0 - abs(h - 0.5) * 2
    # 电荷分：离 0 越近越好
    c_score = 1.0 - min(abs(net) * 5, 1.0)
    # 多样性分：越高越好（但 >0.4 就很好）
    d_score = min(diversity / 0.5, 1.0)

    total = h_score * 0.4 + c_score * 0.3 + d_score * 0.3
    # Met/Trp 氧化风险：每残基扣 0.15，上限 0.30（软惩罚，不硬淘汰）
    mw_count = sum(1 for aa in seq if aa in "MW")
    total -= min(mw_count, 2) * 0.15
    return max(total, 0.0)

def _binder_first_contig(target_chain, target_start, target_end, binder_len):
    """Build the RFdiffusion macrocyclic-binder contig in official chain order.

    RFdiffusion assigns the first contig segment to internal chain ``a``.
    Because ``inference.cyc_chains=a`` is used below, the generated binder must
    be the first segment and the fixed receptor must follow it.
    """
    chain = str(target_chain or "").strip()
    if len(chain) != 1 or not chain.isalpha() or not chain.isupper():
        raise ValueError(
            f"target chain must be a single uppercase PDB chain ID, got {target_chain!r}"
        )
    start, end, length = int(target_start), int(target_end), int(binder_len)
    if start > end:
        raise ValueError(f"target residue range is reversed: {start}-{end}")
    if not MIN_CYCLIC_PEPTIDE_LENGTH <= length <= MAX_CYCLIC_PEPTIDE_LENGTH:
        raise ValueError(
            f"{supported_length_message('binder')}, got {length}"
        )
    return f"{length}-{length} {chain}{start}-{end}/0"

def _canonical_cyclization_type(cyclization, sequence=None):
    """Return the stable manifest value while accepting legacy descriptions."""
    raw = str(cyclization or "").strip()
    if not raw:
        return _infer_cyclization_type(sequence or "")
    normalized = raw.lower().replace("_", "-")
    if "cys-cys-disulfide" in normalized:
        return "Cys-Cys_disulfide"
    if "head-to-tail-amide" in normalized:
        return "head-to-tail_amide"
    raise ValueError(f"unsupported cyclization type: {cyclization!r}")

def _infer_cyclization_type(sequence):
    """Infer the existing Design convention for routes without an explicit mode."""
    sequence = str(sequence or "").strip().upper()
    if not _validate_sequence(sequence):
        raise ValueError("cannot infer cyclization from an invalid sequence")
    if sequence.startswith("C") and sequence.endswith("C"):
        return "Cys-Cys_disulfide"
    return "head-to-tail_amide"

def _first_model_residues(pdb_path):
    """Parse canonical protein atoms from the first PDB model, fail closed."""
    chains, residue_lookup = {}, {}
    with open(pdb_path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            record = line[0:6].strip()
            if record == "ENDMDL":
                break
            if record != "ATOM":
                continue
            if len(line) < 54:
                raise ValueError("short_atom_line")
            altloc = line[16:17].strip()
            if altloc not in {"", "A"}:
                continue
            chain = line[21:22].strip() or "_"
            residue_number = line[22:26].strip()
            insertion_code = line[26:27].strip()
            residue_name = line[17:20].strip().upper()
            atom_name = line[12:16].strip().upper()
            if not residue_number:
                raise ValueError("blank_residue_identifier")
            try:
                coordinate = tuple(
                    float(line[start:end])
                    for start, end in ((30, 38), (38, 46), (46, 54))
                )
            except (OSError, UnicodeError, ValueError) as exc:
                raise ValueError("invalid_atom_coordinate") from exc
            if not all(math.isfinite(value) for value in coordinate):
                raise ValueError("nonfinite_atom_coordinate")
            residue_id = (chain, residue_number, insertion_code)
            residue = residue_lookup.get(residue_id)
            if residue is None:
                residue = {
                    "chain": chain,
                    "number": residue_number,
                    "insertion_code": insertion_code,
                    "name": residue_name,
                    "atoms": {},
                }
                residue_lookup[residue_id] = residue
                chains.setdefault(chain, []).append(residue)
            elif residue["name"] != residue_name:
                raise ValueError("conflicting_residue_name")
            residue["atoms"].setdefault(atom_name, coordinate)
    if not chains:
        raise ValueError("no_protein_atoms")
    return chains

def _ring_closure_check(pdb_path, cyclization_type, sequence=None):
    """Check the actual prospective covalent atoms; never use terminal CA atoms.

    This is a pre-relax geometric compatibility gate.  It records the observed
    bond distance and both the screening and ideal ranges; it does not claim
    that a coordinate file contains a chemically instantiated covalent bond.
    """
    try:
        canonical = _canonical_cyclization_type(
            cyclization_type, sequence=sequence
        )
    except (OSError, UnicodeError, ValueError) as exc:
        return {
            "pass": False,
            "reason": "unsupported_cyclization",
            "detail": str(exc),
        }
    criterion = CLOSURE_GEOMETRY.get(canonical)
    if criterion is None:  # P2-1: new cyclization type missing from geometry table
        return {
            "pass": False,
            "reason": "unsupported_cyclization",
            "detail": f"no closure geometry defined for {canonical!r}",
        }
    base = {
        "pass": False,
        "assessment": "pre_relax_geometry_compatibility",
        "cyclization_type": canonical,
        "atom_1": criterion["atom_1"],
        "atom_2": criterion["atom_2"],
        "screen_range_angstrom": list(criterion["screen_range_angstrom"]),
        "ideal_range_angstrom": list(criterion["ideal_range_angstrom"]),
    }
    try:
        chains = _first_model_residues(pdb_path)
        if len(chains) != 1:
            return {
                **base,
                "reason": "ambiguous_monomer_chain",
                "chains": sorted(chains),
            }
        chain, residues = next(iter(chains.items()))
        if len(residues) < 2:
            return {
                **base,
                "reason": "too_few_residues",
                "chain": chain,
                "n_residues": len(residues),
            }
        first, last = residues[0], residues[-1]
        if sequence is not None and len(residues) != len(sequence):
            return {
                **base,
                "reason": "sequence_length_mismatch",
                "chain": chain,
                "pdb_length": len(residues),
                "sequence_length": len(sequence),
            }
        if canonical == "head-to-tail_amide":
            atom_1_name, atom_2_name = "C", "N"
            atom_1 = last["atoms"].get(atom_1_name)
            atom_2 = first["atoms"].get(atom_2_name)
        else:
            if first["name"] != "CYS" or last["name"] != "CYS":
                return {
                    **base,
                    "reason": "terminal_residues_not_cysteine",
                    "first_residue": first["name"],
                    "last_residue": last["name"],
                }
            atom_1_name = atom_2_name = "SG"
            atom_1 = first["atoms"].get(atom_1_name)
            atom_2 = last["atoms"].get(atom_2_name)
        missing = []
        if atom_1 is None:
            missing.append(criterion["atom_1"])
        if atom_2 is None:
            missing.append(criterion["atom_2"])
        if missing:
            return {**base, "reason": "closure_atom_missing", "missing": missing}
        distance = math.dist(atom_1, atom_2)
        screen_min, screen_max = criterion["screen_range_angstrom"]
        ideal_min, ideal_max = criterion["ideal_range_angstrom"]
        passed = screen_min <= distance <= screen_max
        return {
            **base,
            "pass": passed,
            "reason": "geometry_compatible" if passed else "distance_out_of_range",
            "chain": chain,
            "distance_angstrom": round(distance, 3),
            "ideal_geometry": ideal_min <= distance <= ideal_max,
        }
    except (OSError, UnicodeError, ValueError, KeyError) as exc:
        return {
            **base,
            "reason": "pdb_parse_failed",
            "detail": str(exc),
        }

def _parse_hotspot_residues(hotspots_str):
    """Parse a comma-separated hotspot string (e.g. ``"54,93,96"``) into a
    list of int residue numbers.  Returns ``None`` when the string is empty.
    """
    if not hotspots_str or not str(hotspots_str).strip():
        return None
    tokens = [r.strip() for r in str(hotspots_str).split(",") if r.strip()]
    for token in tokens:
        if not token.isdigit() or int(token) < 1:  # P3-2: reject 0
            raise ValueError(
                f"hotspot residue must be a positive integer (>=1), got {token!r}"
            )
    return [int(token) for token in tokens]

def _pdb_residue_range(pdb_path, chain="A", hotspot_residues=None):
    """Return (first, last) residue numbers for the chain segment that should
    be used as the receptor contig window.

    A gap > 50 residue numbers splits the chain into segments; by default the
    **longest** segment wins (this ignores crystallographic outliers such as
    ILE 500 in 3DAB).

    When *hotspot_residues* (iterable of int residue numbers) is provided, the
    function **validates that every hotspot falls inside a single contiguous
    segment** and returns that segment — even if it is shorter than another.
    If hotspots span multiple segments or lie outside all segments it raises
    ``ValueError``, preventing a contig that silently excludes approved
    binding-site residues.
    """
    # Fixed-column PDB parsing (columns 22-26 = residue number, col 22 = chain ID).
    # Assumes standard RCSB PDB format; mmCIF or non-standard files need preprocessing.
    residues = set()
    try:
        with open(pdb_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("ATOM") and len(line) >= 22 and line[21] == chain:
                    r = int(line[22:26].strip())
                    # P2-3: detect insertion codes.  MDM2/MDMX structures do
                    # not use them, but a generic pipeline should at least warn
                    # rather than silently merging residues 100 and 100A.
                    ins = line[26] if len(line) > 26 else " "
                    if ins != " ":
                        EvidenceLogger.log("design", "pdb_insertion_code_detected",
                            {"pdb": str(pdb_path), "chain": chain,
                             "residue": r, "insertion": ins,
                             "note": "insertion codes are not resolved in "
                                     "segment/hotspot logic; residues like "
                                     "100 and 100A collapse to the same number"})
                    residues.add(r)
    except (OSError, UnicodeError, ValueError) as e:
        EvidenceLogger.error("design", "pdb_parse_failed",
            f"Cannot parse approved coordinate artifact {pdb_path} chain {chain}: {e}.",
            recovery="verify target PDB path")
        raise ValueError(f"cannot parse target PDB chain {chain}: {pdb_path}") from e
    if not residues:
        EvidenceLogger.error("design", "pdb_empty_chain",
            f"No atoms found in approved coordinate artifact {pdb_path} chain {chain}.")
        raise ValueError(f"target PDB contains no atoms for chain {chain}: {pdb_path}")

    sorted_res = sorted(residues)
    # Split into contiguous segments (gap > 50 = new segment)
    segments = []
    seg_start = sorted_res[0]
    prev = seg_start
    for r in sorted_res[1:]:
        if r - prev > 50:
            segments.append((seg_start, prev))
            seg_start = r
        prev = r
    segments.append((seg_start, prev))

    if hotspot_residues:
        hotspot_set = {int(r) for r in hotspot_residues}
        present = hotspot_set & residues
        absent = sorted(hotspot_set - present)
        if absent:
            EvidenceLogger.error("design", "hotspot_absent_from_pdb",
                f"hotspots {absent} absent from chain {chain} of {pdb_path}; "
                f"present={sorted(present)}, pdb_residues={sorted(residues)}",
                recovery="verify structure_resolution approved the correct PDB")
            raise ValueError(
                f"Approved binding-site residues {absent} are absent from "
                f"the approved coordinate artifact {pdb_path} chain {chain}. "
                f"Verify that structure_resolution approved the correct PDB."
            )
        # Which segments cover at least one hotspot?
        covering = []
        for s_start, s_end in segments:
            covered = {r for r in present if s_start <= r <= s_end}
            if covered:
                covering.append((s_start, s_end, covered))
        if not covering:
            EvidenceLogger.error("design", "hotspot_no_contiguous_segment",
                f"chain {chain} segments {segments} contain none of the "
                f"hotspots {sorted(present)} in {pdb_path}",
                recovery="verify PDB chain assignment and hotspot residue numbering")
            raise ValueError(
                f"No contiguous segment of chain {chain} contains any "
                f"binding-site residue {sorted(present)}. "
                f"PDB segments: {segments}"
            )
        if len(covering) > 1:
            EvidenceLogger.error("design", "hotspot_multi_segment",
                f"hotspots span {len(covering)} segments of chain {chain}: "
                f"{[(c[0], c[1], sorted(c[2])) for c in covering]} in {pdb_path}",
                recovery="narrow hotspot range to a single contiguous segment, "
                         "or approve a PDB with co-located binding residues")
            raise ValueError(
                f"Binding-site residues span multiple segments of chain "
                f"{chain}: {[(c[0], c[1], sorted(c[2])) for c in covering]}. "
                f"Cannot build a single contig covering all hotspots."
            )
        # All hotspots in one segment — use it even if shorter than another
        best = (covering[0][0], covering[0][1])
    else:
        # No hotspot guidance → longest segment (backward compatible)
        best = max(segments, key=lambda s: s[1] - s[0])

    return best[0], best[1]

def _pdb_chain_residue_layout(pdb_path):
    """Return first-model PDB residues grouped in emitted chain order."""
    layout, seen = {}, {}
    model_seen = False
    with open(pdb_path) as handle:
        for line in handle:
            if line.startswith("MODEL "):
                if model_seen:
                    break
                model_seen = True
                continue
            if line.startswith("ENDMDL"):
                break
            if not line.startswith("ATOM"):
                continue
            altloc = line[16:17].strip()
            if altloc not in {"", "A"}:
                continue
            chain = line[21:22].strip() or "_"
            residue_number = line[22:26].strip()
            insertion_code = line[26].strip()
            if not chain or chain == "_" or not residue_number:
                raise ValueError(f"blank chain/residue identifier in {pdb_path}")
            residue_id = (residue_number, insertion_code)
            layout.setdefault(chain, [])
            seen.setdefault(chain, set())
            if residue_id not in seen[chain]:
                seen[chain].add(residue_id)
                layout[chain].append(residue_id)
    if not layout:
        raise ValueError(f"no ATOM residues found in {pdb_path}")
    return layout

def _pdb_chain_sequences(pdb_path):
    """Return first-model canonical sequences in emitted PDB chain order."""
    amino_acids = {
        "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
        "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
        "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
        "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
    }
    sequences, seen = {}, {}
    model_seen = False
    with open(pdb_path) as handle:
        for line in handle:
            if line.startswith("MODEL "):
                if model_seen:
                    break
                model_seen = True
                continue
            if line.startswith("ENDMDL"):
                break
            if not line.startswith("ATOM"):
                continue
            altloc = line[16:17].strip()
            if altloc not in {"", "A"}:
                continue
            chain = line[21:22].strip() or "_"
            residue_number = line[22:26].strip()
            insertion_code = line[26].strip()
            residue_name = line[17:20].strip().upper()
            if not chain or chain == "_" or not residue_number:
                raise ValueError(f"blank chain/residue identifier in {pdb_path}")
            residue_id = (residue_number, insertion_code)
            sequences.setdefault(chain, [])
            seen.setdefault(chain, set())
            if residue_id in seen[chain]:
                continue
            seen[chain].add(residue_id)
            if residue_name not in amino_acids:
                raise ValueError(
                    f"non-canonical residue {residue_name!r} in chain {chain} "
                    f"at {residue_number}{insertion_code}"
                )
            sequences[chain].append(amino_acids[residue_name])
    if not sequences:
        raise ValueError(f"no ATOM residues found in {pdb_path}")
    return {chain: "".join(values) for chain, values in sequences.items()}

def _verify_fixed_sequence_pdb(pdb_path, requested_sequence):
    """Require one monomer chain whose saved PDB sequence is exactly requested."""
    requested_sequence = str(requested_sequence or "").strip().upper()
    if not _validate_sequence(requested_sequence):
        raise ValueError("requested fixed sequence is invalid")
    observed = _pdb_chain_sequences(pdb_path)
    if len(observed) != 1 or list(observed.values()) != [requested_sequence]:
        raise ValueError(
            f"fixed-sequence PDB mismatch: requested={requested_sequence!r} "
            f"observed={observed!r}"
        )
    return observed

def _infer_binder_chain(pdb_path, expected_length, receptor_chain=None):
    """Return the RFdiffusion-generated binder chain.

    The RFdiffusion output must contain at least two chains (receptor + binder).
    When *receptor_chain* is provided it must be present in the PDB and is
    unconditionally excluded before length matching.
    """
    layout = _pdb_chain_residue_layout(pdb_path)
    if len(layout) < 2:
        raise ValueError(
            f"RFdiffusion complex must contain at least two chains, got "
            f"{sorted(layout)}"
        )
    if receptor_chain:
        if receptor_chain not in layout:
            raise ValueError(
                f"expected receptor chain {receptor_chain!r} not found in "
                f"RFdiffusion output; chains={sorted(layout)}"
            )
        candidate_chains = set(layout) - {receptor_chain}
    else:
        candidate_chains = set(layout)
    candidates = [
        chain for chain in candidate_chains
        if len(layout[chain]) == int(expected_length)
    ]
    if len(candidates) != 1:
        counts = {chain: len(layout[chain]) for chain in layout}
        detail = f"candidates={candidates}, lengths={counts}"
        if receptor_chain:
            detail += f", receptor={receptor_chain!r}"
        raise ValueError(
            f"expected one {expected_length}-residue binder chain; {detail}"
        )
    return candidates[0]

def _parse_binder_residues(pdb_path, binder_chain):
    """Return ``[(chain, residue_id), ...]`` for one validated binder chain."""
    layout = _pdb_chain_residue_layout(pdb_path)
    if binder_chain not in layout:
        raise ValueError(f"binder chain {binder_chain!r} is absent from {pdb_path}")
    return [
        (binder_chain, f"{number}{insertion}")
        for number, insertion in layout[binder_chain]
    ]

def _extract_ligandmpnn_binder_sequence(
        encoded, binder_chain, layout, input_sequences=None):
    """Extract the binder segment and verify that every receptor chain stayed fixed."""
    # LigandMPNN's parse_PDB() builds ``mask_c`` from a sorted chain list and
    # writes FASTA segments in that order, even when the PDB records first
    # encounter the chains in a different order.
    chain_order = sorted(layout)
    if binder_chain not in layout:
        raise ValueError(f"binder chain {binder_chain!r} is absent from PDB layout")
    segments = str(encoded).strip().upper().split(":")
    if len(segments) != len(chain_order):
        raise ValueError(
            f"FASTA has {len(segments)} chain segments, PDB has "
            f"{len(chain_order)} chains"
        )
    if input_sequences is not None:
        for chain, segment in zip(chain_order, segments):
            if chain == binder_chain:
                continue
            expected = input_sequences.get(chain)
            if expected is None or segment != expected:
                raise ValueError(
                    f"fixed chain {chain} changed during inverse folding"
                )
    sequence = segments[chain_order.index(binder_chain)]
    expected_length = len(layout[binder_chain])
    if len(sequence) != expected_length:
        raise ValueError(
            f"binder FASTA length {len(sequence)} != PDB length {expected_length}"
        )
    if not sequence or any(amino_acid not in SCAFFOLD_MUTABLE_AA for amino_acid in sequence):
        raise ValueError("binder FASTA contains non-standard amino acids")
    return sequence

def _hotspot_positions(template_seq):
    """在模板序列中检测 F/W/L hotspot 位置，返回 {0-based_position: aa}"""
    hotspots = {}
    for i, aa in enumerate(template_seq):
        if aa in "FWL":
            hotspots[i] = aa
    return hotspots

def _hotspot_fixed_residues(hotspots, binder_residues):
    """将模板 hotspot 位置映射为 LigandMPNN fixed_residues 字符串。
    固定 F/W 锚点，L 位置留给 LigandMPNN 自由设计（L26 偏置）。
    hotspots: _hotspot_positions() 返回的 {pos: aa} dict
    """
    fixed = []
    for i, aa in hotspots.items():
        if aa in "FW":
            if i < len(binder_residues):
                # W23/F19 锚点：固定不变
                ch, resi = binder_residues[i]
                fixed.append(f"{ch}{resi}")
            else:
                EvidenceLogger.error("design", "hotspot_anchor_out_of_range", {
                    "position": i,
                    "residue": aa,
                    "binder_length": len(binder_residues),
                    "remediation": "template F/W anchor position exceeds generated "
                                    "binder length; verify template-to-backbone alignment",
                })
        # L 残基不固定，让 backbone 几何自然偏置小氨基酸
    return " ".join(fixed)

def _validate_sequence(seq):
    if not isinstance(seq, str):
        return False
    valid = STANDARD_AMINO_ACIDS
    s = seq.upper().replace("-","").replace("*","")
    return (
        MIN_CYCLIC_PEPTIDE_LENGTH <= len(s) <= MAX_CYCLIC_PEPTIDE_LENGTH
        and all(c in valid for c in s)
    )

def _describe_cyclize(n_term, c_term, linker):
    parts = []
    if n_term == "C" and c_term == "C":
        parts.append("Cys-Cys_disulfide")
    elif n_term == "" and c_term == "":
        parts.append("head-to-tail_amide")
    else:
        raise ValueError(
            f"unrecognised terminal residues for cyclization: "
            f"N-term={n_term!r} C-term={c_term!r}; expected Cys-Cys or head-to-tail"
        )
    if linker:
        parts.append(f"linker={linker}")
    return ",".join(parts)
