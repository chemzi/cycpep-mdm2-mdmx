"""
Design Agent v5.1.0 — 于嘉乐
职责：RFpeptides 生成环肽骨架 → LigandMPNN 序列设计 → AfCycDesign refold 验证
入口：design_rfpeptides(target_spec, design_config) → list[dict]
      design_motif_guided(target_spec, design_config) → list[dict]
      design_atsp_derived(target_spec, design_config) → list[dict]
      threshold_filter(candidates, thresholds) → list[dict]
      pareto_front(candidates) → list[dict]
依赖：from data_layer import EvidenceLogger, CandidateIndex, State, file_hash
工具：RFdiffusion (rfdiff_env) / LigandMPNN (rfdiff_env) / AfCycDesign (cycpep)

Agent 职责边界：
  Design 阶段只做基础验证（能折叠 + 环闭合）。
  pLDDT > 0.8 的最终过滤由 Prediction Agent (Phase 3 L1) 负责。
"""

import math, os, sys, json, time, subprocess, threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from data_layer import EvidenceLogger, CandidateIndex, State, file_hash
from project_config import (
    load_project_config,
    required_target_ids,
    target_slug,
    target_value,
    threshold_for_target,
)
from structure_resolution import assert_target_structure_ready
from target_bootstrap import assert_project_approved


# ============================================================
# 环境路径
# ============================================================

ACTIVE_PROJECT_CONFIG = load_project_config()

# 新服务器路径可全部通过环境变量覆盖；默认值对应 damodel 部署。
CYCPEP_CONDA = os.environ.get(
    "CYCPEP_CONDA", "/root/damodel-tmp/envs/cycpep-prediction"
)
CYCPEP_PYTHON = os.environ.get("CYCPEP_PYTHON", f"{CYCPEP_CONDA}/bin/python")
RFDIFF_CONDA = os.environ.get(
    "RFDIFF_CONDA", "/root/damodel-tmp/envs/rfdiffusion-design"
)
RFDIFF_PYTHON = os.environ.get("RFDIFF_PYTHON", f"{RFDIFF_CONDA}/bin/python")
RFDIFF_DIR = os.environ.get(
    "RFDIFF_DIR", "/root/workspace/NovaPeptide/tools/RFdiffusion"
)
LIGANDMPNN_DIR = os.environ.get(
    "LIGANDMPNN_DIR", "/root/workspace/NovaPeptide/tools/LigandMPNN"
)
COLABDESIGN_DIR = os.environ.get(
    "COLABDESIGN_DIR", "/root/workspace/NovaPeptide/tools/ColabDesign"
)
COLABDESIGN_PARAMS = os.environ.get(
    "COLABDESIGN_PARAMS", f"{COLABDESIGN_DIR}/params"
)
COLABDESIGN_COMMIT = "094e2cb3603dee7d99846e0977736bd943c830c2"
SE3_ROOT = os.environ.get("SE3_ROOT", f"{RFDIFF_DIR}/env/SE3Transformer")
CUDA_DATA_DIR = os.environ.get(
    "CUDA_DATA_DIR",
    f"{CYCPEP_CONDA}/lib/python3.10/site-packages/nvidia/cuda_nvcc",
)
DAMODEL_DATA_ROOT = Path("/root/damodel-tmp/novapeptide")


def _resolve_output_dir(environ=None, damodel_data_root=None):
    """Resolve a writable design root without assuming /root is accessible."""
    env = os.environ if environ is None else environ
    explicit_root = env.get("CYCPEP_DESIGN_ROOT")
    if explicit_root:
        return Path(explicit_root)

    np_data_root = env.get("NP_DATA")
    if np_data_root:
        return Path(np_data_root) / "designs"

    damodel_root = DAMODEL_DATA_ROOT if damodel_data_root is None else damodel_data_root
    try:
        if damodel_root.is_dir():
            return damodel_root / "designs"
    except OSError:
        # GitHub runners and other non-root users cannot stat paths below /root.
        pass

    runner_temp = env.get("RUNNER_TEMP")
    if runner_temp:
        return Path(runner_temp) / "novapeptide" / "designs"
    return ROOT / "data" / "designs"


DEFAULT_OUTPUT_DIR = _resolve_output_dir()
OUTPUT_DIR = str(DEFAULT_OUTPUT_DIR)
RFDIFF_TIMESTEPS = int(os.environ.get("RFDIFF_TIMESTEPS", "50"))
LIGANDMPNN_MODEL_TYPE = os.environ.get("LIGANDMPNN_MODEL_TYPE", "protein_mpnn")
LIGANDMPNN_CHECKPOINT = os.environ.get(
    "LIGANDMPNN_CHECKPOINT",
    f"{LIGANDMPNN_DIR}/model_params/proteinmpnn_v_48_020.pt",
)
DEFAULT_SEED = None
DESIGN_PIPELINE_VERSION = "5.1.0"

# Geometry gates are deliberately labelled as compatibility checks.  A model
# whose terminal atoms are close enough for a covalent bond is suitable for
# downstream relaxation/validation; coordinates alone do not prove that the
# bond has been chemically formed.
CLOSURE_GEOMETRY = {
    "head-to-tail_amide": {
        "atom_1": "last:C",
        "atom_2": "first:N",
        # The wwPDB validation range for a peptide C-N bond is 1.30-1.45 Å.
        # Design uses a wider pre-relax screen and records ideal-range status.
        "screen_range_angstrom": (1.15, 2.00),
        "ideal_range_angstrom": (1.30, 1.45),
    },
    "Cys-Cys_disulfide": {
        "atom_1": "first:SG",
        "atom_2": "last:SG",
        # Typical protein disulfides are close to 2.03 Å.  The wider screen
        # tolerates an unrelaxed predictor output without accepting CA proxies.
        "screen_range_angstrom": (1.80, 2.30),
        "ideal_range_angstrom": (1.90, 2.15),
    },
}


# ============================================================
# 设计常量（Research 产出可覆盖）
# ============================================================

# 所有设计常量从 Research State 读取（_load_target_spec）。
_LOCK = threading.Lock()
CYCLIZATION_PAIRS = [("C", "C"), ("", "")]
LINKER_MATRIX = ["GGGGS", "GGGS", "GGS", "GS", ""]
SCAFFOLD_MUTABLE_AA = "ACDEFGHIKLMNPQRSTVWY"

# 便宜预筛参数
CHEAP_FILTER_TOP_K = 4    # refold 前保留序列数
HYDROPHOBIC = set("AILMFWV")
POS_CHARGED = set("KR")
NEG_CHARGED = set("DE")


def _require_mdm_reference_route(route_name):
    target_ids = set(required_target_ids(ACTIVE_PROJECT_CONFIG))
    if target_ids != {"MDM2", "MDMX"}:
        raise RuntimeError(
            f"{route_name} contains MDM-specific motif knowledge and is disabled for "
            f"project {ACTIVE_PROJECT_CONFIG['project_id']}; provide project-specific motifs instead"
        )


def _cheap_filter_sequences(seqs, seen_seqs=None, top_k=CHEAP_FILTER_TOP_K):
    """
    便宜预筛（无 GPU）：合成可行性 + 基本理化性质。
    返回 top_k 条最优序列，格式 [(seq, score), ...]
    """
    if seen_seqs is None:
        seen_seqs = set()
    scored = []
    for seq in seqs:
        if seq in seen_seqs:
            continue
        violations = _synthesizability_violations(seq)
        if violations:
            continue  # 硬淘汰
        score = _sequence_quality_score(seq)
        scored.append((seq, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


def _synthesizability_violations(seq):
    """
    检查 Kickoff 定义的可合成性规则。返回违规列表，空列表 = 通过。
    - 聚集：连续 >4 个疏水氨基酸
    - 游离 Cys：不在 N/C 端的 Cys
    - 氧化：Met / Trp（软警告，不硬淘汰）
    - 脱酰胺：Asn-Gly
    - Asp-Pro 断裂
    """
    v = []
    # 连续疏水
    run = 0
    for aa in seq:
        if aa in HYDROPHOBIC:
            run += 1
        else:
            run = 0
        if run > 4:
            v.append("aggregation")
            break
    # 游离 Cys（不在首尾）
    for i, aa in enumerate(seq):
        if aa == "C" and i not in (0, len(seq) - 1):
            v.append("stray_cys")
            break
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
    # 首尾连接也要检查（环化后 N-term 和 C-term 相邻）
    if seq[0] == "G" and seq[-1] == "N":
        v.append("deamidation_NG_cyclic")
    if seq[0] == "P" and seq[-1] == "D":
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
    # Met/Trp 氧化风险：扣 0.15（软惩罚，不硬淘汰）
    for aa in seq:
        if aa in "MW":
            total -= 0.15
            break
    return max(total, 0.0)


# ============================================================
# Route A: RFpeptides 自由生成
# ============================================================

def design_rfpeptides(target_spec=None, design_config=None):
    """RFpeptides → LigandMPNN → AfCycDesign refold"""
    config = _merge_config(target_spec, design_config)
    route_name = f"route_A_{target_slug(config['target_id'])}"
    batch_id = f"batch_rfpep_{config['target_name']}_s{config['seed']}"
    batch_dir = f"{OUTPUT_DIR}/route_A/{batch_id}"
    os.makedirs(batch_dir, exist_ok=True)

    with open(f"{batch_dir}/design_config.json", "w") as f:
        json.dump(config, f, indent=2, default=str)

    candidates = []
    total_gen, total_valid = 0, 0
    t_batch = time.time()
    target_range = _pdb_residue_range(config["target_pdb"], config["chain"])

    for L in config["lengths"]:
        n_designs = max(1, config["n"] // len(config["lengths"]))
        backbone_dir = f"{batch_dir}/backbones_len{L}"
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

        bb_files = sorted(Path(backbone_dir).glob("bb_*.pdb"))
        print(f"[Route A] RFdiff 完成, 找到 {len(bb_files)} 个骨架PDB")
        for bb_path in bb_files[:n_designs]:
            total_gen += 1
            try:
                binder_chain = _infer_binder_chain(str(bb_path), L)
            except ValueError as exc:
                EvidenceLogger.error(
                    "design", "rfdiff_binder_chain_invalid",
                    f"{bb_path}: {exc}", recovery="skip ambiguous backbone",
                )
                continue
            mpnn_dir = f"{batch_dir}/mpnn_{bb_path.stem}"
            os.makedirs(mpnn_dir, exist_ok=True)
            seqs = _run_ligandmpnn(
                str(bb_path), mpnn_dir, n_seq=8, binder_chain=binder_chain
            )
            if not seqs:
                print(f"[Route A] LigandMPNN 返回 0 条序列: {bb_path.name}")
                continue
            # 便宜预筛 → 只 refold top 4
            filtered = _cheap_filter_sequences(seqs, top_k=4)
            print(f"[Route A] cheap filter: {len(seqs)}→{len(filtered)} sequences")

            for seq, quality_score in filtered:
                cid = _next_candidate_id()
                refold_dir = f"{batch_dir}/candidates/{cid}"
                os.makedirs(refold_dir, exist_ok=True)
                refold_pdb = f"{refold_dir}/refold.pdb"
                plddt = _run_refold(seq, refold_pdb)
                cyclization = _infer_cyclization_type(seq)
                rc = (
                    _ring_closure_check(refold_pdb, cyclization, sequence=seq)
                    if os.path.exists(refold_pdb)
                    else {"pass": False, "reason": "refold_pdb_missing"}
                )

                if plddt and rc.get("pass"):
                    total_valid += 1
                    manifest = _write_manifest(
                        cid, seq, route_name, batch_id, refold_pdb, config,
                        backbone_pdb=str(bb_path), cyclization=cyclization,
                        ring_closure=rc,
                    )
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


# ============================================================
# Route B: motif 引导生成
# ============================================================

def design_motif_guided(target_spec=None, design_config=None):
    """RFpeptides motif 引导 + LigandMPNN L26 偏置 + refold"""
    config = _merge_config(target_spec, design_config)
    _require_mdm_reference_route("route_B_motif")
    route_name = f"route_B_motif_{target_slug(config['target_id'])}"
    batch_id = f"batch_motif_s{config['seed']}"
    spec = _load_target_spec()
    binders = spec.get("known_dual_binders", [])
    if not binders:
        EvidenceLogger.error("design", "no_binders",
            "known_dual_binders empty in state.json — Research 尚未产出或格式错误",
            recovery="先跑 Research Agent 产出设计规则再跑 Route B")
        return []

    batch_dir = f"{OUTPUT_DIR}/route_B/{batch_id}"
    os.makedirs(batch_dir, exist_ok=True)
    with open(f"{batch_dir}/design_config.json", "w") as f:
        json.dump(config, f, indent=2, default=str)

    templates = [(b.get("sequence") or b.get("seq", ""), b.get("name","tmpl"))
                 for b in binders if b.get("sequence") or b.get("seq")]

    candidates = []
    total_gen, total_valid = 0, 0
    t_batch = time.time()
    n_per = max(1, config.get("n", 100) // max(1, len(templates)))
    target_range = _pdb_residue_range(config["target_pdb"], config["chain"])

    for tmpl_seq, tmpl_name in templates:
        if len(tmpl_seq) < 8:
            continue
        L = len(tmpl_seq)
        tmpl_hotspots = _hotspot_positions(tmpl_seq)
        backbone_dir = f"{batch_dir}/backbones_{tmpl_name}"
        os.makedirs(backbone_dir, exist_ok=True)
        # Route B: motif 约束由 LigandMPNN 的 fixed_residues 实现，不通过 RFdiffusion inpaint_seq
        rfdiff_ok = _run_rfdiff(target_pdb=config["target_pdb"], binder_len=L,
            n_designs=n_per, output_prefix=f"{backbone_dir}/bb",
            contig=_binder_first_contig(
                config["chain"], target_range[0], target_range[1], L
            ),
            seed=config["seed"],
            hotspots=config.get("hotspots"),
            chain=config["chain"])
        if not rfdiff_ok:
            print(f"[Route B] RFdiff 失败 {tmpl_name}，跳过")
            continue

        bb_files = sorted(Path(backbone_dir).glob("bb_*.pdb"))
        print(f"[Route B] {tmpl_name}: RFdiff 完成, 找到 {len(bb_files)} 个骨架PDB")
        for bb_path in bb_files[:n_per]:
            total_gen += 1
            try:
                binder_chain = _infer_binder_chain(str(bb_path), L)
            except ValueError as exc:
                EvidenceLogger.error(
                    "design", "rfdiff_binder_chain_invalid",
                    f"{bb_path}: {exc}", recovery="skip ambiguous backbone",
                )
                continue
            binder_res = _parse_binder_residues(str(bb_path), binder_chain)
            fixed_res = _hotspot_fixed_residues(tmpl_hotspots, binder_res) if binder_res else ""
            mpnn_dir = f"{batch_dir}/mpnn_{bb_path.stem}"
            os.makedirs(mpnn_dir, exist_ok=True)
            seqs = _run_ligandmpnn(str(bb_path), mpnn_dir, n_seq=8,
                binder_chain=binder_chain, fixed_residues=fixed_res or None)
            if not seqs:
                print(f"[Route B] LigandMPNN 返回 0 条序列: {bb_path.name}")
                continue
            # 便宜预筛 → 只 refold top 4
            filtered = _cheap_filter_sequences(seqs, top_k=4)
            print(f"[Route B] cheap filter: {len(seqs)}→{len(filtered)} sequences")

            for seq, quality_score in filtered:
                cid = _next_candidate_id()
                refold_dir = f"{batch_dir}/candidates/{cid}"
                os.makedirs(refold_dir, exist_ok=True)
                refold_pdb = f"{refold_dir}/refold.pdb"
                plddt = _run_refold(seq, refold_pdb)
                cyclization = _infer_cyclization_type(seq)
                rc = (
                    _ring_closure_check(refold_pdb, cyclization, sequence=seq)
                    if os.path.exists(refold_pdb)
                    else {"pass": False, "reason": "refold_pdb_missing"}
                )

                if plddt and rc.get("pass"):
                    total_valid += 1
                    manifest = _write_manifest(
                        cid, seq, route_name, batch_id, refold_pdb, config,
                        backbone_pdb=str(bb_path), cyclization=cyclization,
                        ring_closure=rc,
                    )
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
                    print(f"[Route B] refold失败: {cid} pLDDT={plddt} ring_closed={rc.get('pass')}")

    EvidenceLogger.design_batch(route=route_name, n_generated=total_gen,
        n_valid=total_valid, tool_name="rfpeptides_motif",
        tool_version=DESIGN_PIPELINE_VERSION,
        duration_sec=round(time.time()-t_batch, 1))
    return candidates


# ============================================================
# Route C: ATSP-7041 环化改造
# ============================================================

def design_atsp_derived(target_spec=None, design_config=None):
    """ATSP-7041 模板环化：linker × 环化矩阵 + 随机突变扩展 + refold 验证"""
    config = _merge_config(target_spec, design_config)
    _require_mdm_reference_route("route_C_atsp")
    n = config.get("n", 200)
    seed = config["seed"]  # _merge_config already resolves None → timestamp
    import random
    random.seed(seed)

    route_name = f"route_C_atsp_{target_slug(config['target_id'])}"
    batch_id = f"batch_atsp_{int(time.time())}_s{seed}"
    batch_dir = f"{OUTPUT_DIR}/route_C/{batch_id}"
    os.makedirs(batch_dir, exist_ok=True)

    with open(f"{batch_dir}/design_config.json", "w") as f:
        json.dump(config, f, indent=2)

    # ATSP-7041 核心序列从 Research 数据取
    spec = _load_target_spec()
    binders = spec.get("known_dual_binders", [])
    atsp_seq = None
    for b in binders:
        name = b.get("name", "")
        seq_candidate = b.get("sequence") or b.get("seq", "")
        if "ATSP" in name.upper() and seq_candidate:
            atsp_seq = seq_candidate
            break
    if not atsp_seq:
        EvidenceLogger.error("design", "no_atsp",
            "known_dual_binders 中未找到 ATSP-7041 — Research 尚未产出",
            recovery="先跑 Research Agent 产出 ATSP-7041 序列再跑 Route C")
        return []
    # Route C 序列设计: linker × 环化 全矩阵
    base_combos = []
    for linker in LINKER_MATRIX:
        for cn, cc in CYCLIZATION_PAIRS:
            seq = f"{cn}{atsp_seq}{linker}{cc}"
            if _validate_sequence(seq):
                base_combos.append((seq, _describe_cyclize(cn, cc, linker)))

    # 第2级：不够 n 则随机突变扩展
    expanded = list(base_combos)
    seen_seqs = set(s for s, _ in base_combos)
    attempts = 0
    while len(expanded) < n and attempts < n * 10:
        attempts += 1
        seq, desc = random.choice(base_combos)
        pos = random.choice([3, 5, 8, 10, 12])
        aa = random.choice(SCAFFOLD_MUTABLE_AA)
        off = 1 if seq and seq[0] == "C" else 0
        ix = off + min(pos, len(seq)-1)
        mutated = seq[:ix] + aa + seq[ix+1:]
        if _validate_sequence(mutated) and mutated not in seen_seqs:
            seen_seqs.add(mutated)
            expanded.append((mutated, f"{desc},mut:{pos}={aa}"))

    candidates = []
    total_gen, total_valid = 0, 0
    t_batch = time.time()

    for seq, desc in expanded[:n]:
        total_gen += 1
        cid = _next_candidate_id()
        refold_dir = f"{batch_dir}/candidates/{cid}"
        os.makedirs(refold_dir, exist_ok=True)
        refold_pdb = f"{refold_dir}/refold.pdb"
        plddt = _run_refold(seq, refold_pdb)
        rc = (
            _ring_closure_check(refold_pdb, desc, sequence=seq)
            if os.path.exists(refold_pdb)
            else {"pass": False, "reason": "refold_pdb_missing"}
        )

        if plddt and rc.get("pass"):
            total_valid += 1
            manifest = _write_manifest(
                cid, seq, route_name, batch_id, refold_pdb, config,
                cyclization=desc, ring_closure=rc,
            )
            candidate = _candidate_from_manifest(manifest, plddt, notes={"design": desc})
            CandidateIndex.add(candidate)
            EvidenceLogger.log("design", "candidate_registered",
                {"candidate": candidate},
                targets=[config["target_id"]], phase="design")
            candidates.append(candidate)
        else:
            EvidenceLogger.error("design", "refold_failed",
                f"{cid}: pLDDT={plddt}", recovery="skip")

    EvidenceLogger.design_batch(route=route_name, n_generated=total_gen,
        n_valid=total_valid, tool_name="atsp_derived",
        tool_version=DESIGN_PIPELINE_VERSION,
        duration_sec=round(time.time()-t_batch, 1))
    return candidates


# ============================================================
# 评分 — 阈值过滤 + Pareto 前沿
# ============================================================

def threshold_filter(candidates, thresholds, project_config=None):
    """Apply independent per-target ipSAE and hotspot-coverage gates."""
    project = project_config or ACTIVE_PROJECT_CONFIG
    target_ids = required_target_ids(project)
    passed = []
    for candidate in candidates:
        accepted = True
        for index, target_id in enumerate(target_ids):
            slug = target_slug(target_id)
            ipsae_rule = threshold_for_target(thresholds, "L2_ipsae", target_id)
            hotspot_rule = threshold_for_target(
                thresholds, "L5_hotspot_coverage", target_id
            )
            ipsae_threshold = thresholds.get(
                f"ipsae_{slug}", ipsae_rule.get("value", 0.6 if index == 0 else 0.5)
            )
            hotspot_threshold = thresholds.get(
                f"hotspot_cov_{slug}",
                thresholds.get("hotspot_cov", hotspot_rule.get("value", 0.67)),
            )
            ipsae = target_value(candidate, target_id, "ipsae")
            hotspot_cov = target_value(candidate, target_id, "hotspot_cov")
            if (
                ipsae is None or float(ipsae) < float(ipsae_threshold)
                or hotspot_cov is None or float(hotspot_cov) < float(hotspot_threshold)
            ):
                accepted = False
                break
        if accepted:
            passed.append(candidate)
    return passed


def pareto_front(candidates, obj_x=None, obj_y=None, project_config=None):
    """Return the non-dominated front for configured or explicit objectives."""
    project = project_config or ACTIVE_PROJECT_CONFIG
    if obj_x is None:
        target_ids = required_target_ids(project)
        objectives = [(target_id, "ipsae") for target_id in target_ids[:2]]
    else:
        objectives = [obj_x]
        if obj_y is not None:
            objectives.append(obj_y)

    def objective_value(candidate, objective):
        if isinstance(objective, tuple):
            return target_value(candidate, objective[0], objective[1]) or 0
        if ":" in objective:
            target_id, metric = objective.split(":", 1)
            return target_value(candidate, target_id, metric) or 0
        return candidate.get(objective, 0)

    front = []
    for c1 in candidates:
        dominated = False
        for c2 in candidates:
            if c2 is c1:
                continue
            c1_values = [objective_value(c1, objective) for objective in objectives]
            c2_values = [objective_value(c2, objective) for objective in objectives]
            if (
                all(right >= left for left, right in zip(c1_values, c2_values))
                and any(right > left for left, right in zip(c1_values, c2_values))
            ):
                dominated = True
                break
        if not dominated:
            front.append(c1)
    return front


# ============================================================
# candidate_manifest.json
# ============================================================

def _write_manifest(
        cid, seq, route, batch_id, refold_pdb, config, backbone_pdb=None,
        cyclization=None, ring_closure=None):
    """Write one versioned candidate manifest with audited closure geometry."""
    refold_dir = os.path.dirname(refold_pdb)
    manifest_path = f"{refold_dir}/manifest.json"
    if cyclization is None:
        cyclization = _infer_cyclization_type(seq)
    cyclization_description = str(cyclization)
    canonical_cyclization = _canonical_cyclization_type(
        cyclization_description, sequence=seq
    )
    rc = ring_closure
    if rc is None:
        rc = (
            _ring_closure_check(
                refold_pdb, canonical_cyclization, sequence=seq
            )
            if os.path.exists(refold_pdb)
            else {"pass": False, "reason": "refold_pdb_missing"}
        )
    observed_type = rc.get("cyclization_type")
    if observed_type and observed_type != canonical_cyclization:
        raise ValueError(
            "ring-closure result cyclization does not match manifest: "
            f"{observed_type!r} != {canonical_cyclization!r}"
        )
    manifest = {
        "design_pipeline_version": DESIGN_PIPELINE_VERSION,
        "candidate_id": cid, "sequence": seq, "length": len(seq),
        "source_route": route, "source_batch": batch_id,
        "cyclization_type": canonical_cyclization,
        "cyclization_description": cyclization_description,
        "refold_pdb": refold_pdb,
        "refold_pdb_hash": file_hash(refold_pdb) if os.path.exists(refold_pdb) else "",
        "backbone_pdb": backbone_pdb or "",
        "backbone_pdb_hash": file_hash(backbone_pdb) if (backbone_pdb and os.path.exists(backbone_pdb)) else "",
        "ring_closure": rc,
        "design_config_summary": {
            "project_id": config.get("project_id"),
            "target": config.get("target_id"),
            "target_pdb": config.get("target_pdb"),
            "target_pdb_sha256": config.get("target_pdb_sha256"),
            "seed": config.get("seed"),
        }
    }
    manifest["manifest_path"] = manifest_path
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    return manifest


def _manifest_summary(manifest):
    return {
        key: manifest[key]
        for key in [
            "design_pipeline_version", "candidate_id", "sequence",
            "refold_pdb_hash", "manifest_path",
        ]
        if key in manifest
    }


def _candidate_from_manifest(manifest, plddt, notes=None):
    """Convert a v5 manifest into the stable dev candidate handoff contract."""
    length = manifest["length"]
    cyclization = manifest["cyclization_type"]
    if "head-to-tail_amide" in cyclization:
        bonds = [{
            "atom_1": "residue_1:N",
            "atom_2": f"residue_{length}:C",
            "bond_type": "amide",
        }]
    elif "Cys-Cys_disulfide" in cyclization:
        bonds = [{
            "atom_1": "residue_1:SG",
            "atom_2": f"residue_{length}:SG",
            "bond_type": "disulfide",
        }]
    else:
        bonds = []
    note_payload = {**_manifest_summary(manifest), **(notes or {})}
    return {
        "candidate_id": manifest["candidate_id"],
        "sequence": manifest["sequence"],
        "length": length,
        "source_route": manifest["source_route"],
        "source_batch": manifest["source_batch"],
        "cyclization_type": cyclization,
        "cyclization_bonds": bonds,
        "design_pdb_path": manifest["refold_pdb"],
        "design_pdb_hash": manifest["refold_pdb_hash"],
        "manifest_path": manifest["manifest_path"],
        "monomer_plddt": round(float(plddt), 3),
        "notes": json.dumps(note_payload, ensure_ascii=False),
    }


# ============================================================
# 工具调用封装
# ============================================================

def _binder_first_contig(target_chain, target_start, target_end, binder_len):
    """Build the RFdiffusion macrocyclic-binder contig in official chain order.

    RFdiffusion assigns the first contig segment to internal chain ``a``.
    Because ``inference.cyc_chains=a`` is used below, the generated binder must
    be the first segment and the fixed receptor must follow it.
    """
    chain = str(target_chain or "").strip()
    if len(chain) != 1:
        raise ValueError(f"target chain must be one PDB chain ID, got {target_chain!r}")
    start, end, length = int(target_start), int(target_end), int(binder_len)
    if start > end:
        raise ValueError(f"target residue range is reversed: {start}-{end}")
    if not 8 <= length <= 20:
        raise ValueError(f"binder length must be 8-20, got {length}")
    return f"{length}-{length} {chain}{start}-{end}/0"


def _run_rfdiff(target_pdb, binder_len, n_designs, output_prefix, contig,
                seed=None, hotspots=None, chain="A"):
    """RFdiffusion 子进程。hotspots: 逗号分隔的残基号如 '54,93,96'"""
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
        # 补链名前缀: "54,93,96" → "A54,A93,A96"
        formatted = ",".join(f"{chain}{r.strip()}" for r in hotspots.split(",") if r.strip())
        if formatted:
            cmd.append(f"ppi.hotspot_res=[{formatted}]")
    # RFdiffusion Hydra config 没有 inference.seed 字段，seed 通过 contig 控制
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600,
            cwd=RFDIFF_DIR,
            env=_rfdiff_subprocess_env())
        if r.returncode != 0:
            print(f"[RFdiff 失败] exit={r.returncode}")
            print(f"  stderr: {r.stderr[-500:]}")
            EvidenceLogger.error("design", "rfdiff_failed",
                f"exit={r.returncode} stderr={r.stderr[-300:]}")
            return False
        return True
    except Exception as e:
        print(f"[RFdiff 异常] {e}")
        EvidenceLogger.error("design", "rfdiff_exception", str(e))
        return False


def _run_ligandmpnn(backbone_pdb, output_dir, n_seq=8, binder_chain=None,
                    fixed_residues=None):
    """LigandMPNN subprocess with an explicitly validated binder chain.

    The RFdiffusion output chain labels are discovered from the emitted PDB,
    rather than inferred from the input receptor's chain label.
    fixed_residues: 空格分隔的 chain+resi 列表，如 'B25 B26 B27'，这些残基在 LigandMPNN 中固定不变。"""
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
    except (OSError, ValueError) as exc:
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
    batch_size = min(max(1, int(n_seq)), 4)
    number_of_batches = max(1, (int(n_seq) + batch_size - 1) // batch_size)
    cmd = [
        RFDIFF_PYTHON, f"{LIGANDMPNN_DIR}/run.py",
        "--model_type", LIGANDMPNN_MODEL_TYPE,
        f"--checkpoint_protein_mpnn={LIGANDMPNN_CHECKPOINT}",
        f"--pdb_path={backbone_pdb}",
        f"--out_folder={output_dir}",
        f"--batch_size={batch_size}",
        f"--number_of_batches={number_of_batches}",
        "--temperature=0.1", "--seed=42",
        "--fasta_seq_separation=:",
        f"--chains_to_design={binder_chain}",
    ]
    if fixed_residues:
        cmd.append(f"--fixed_residues={fixed_residues}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
            cwd=LIGANDMPNN_DIR,
            env=_rfdiff_subprocess_env())
        if r.returncode != 0:
            print(f"[LigandMPNN 失败] exit={r.returncode} stderr={r.stderr[-300:]}")
            return []
        seqs = []
        for fa in sorted(Path(output_dir).glob("**/*.fa"))[:1]:  # 一个 PDB 只出一个 FASTA
            with open(fa) as fh:
                is_generated_record = False
                for line in fh:
                    line = line.strip()
                    if line.startswith(">"):
                        # The first FASTA record is the native/reference
                        # complex. Only records carrying a design id are model
                        # outputs and may become candidates.
                        is_generated_record = ", id=" in line
                        continue
                    if not line or not is_generated_record:
                        continue
                    try:
                        line = _extract_ligandmpnn_binder_sequence(
                            line, binder_chain, layout, input_sequences
                        )
                    except ValueError as exc:
                        EvidenceLogger.error(
                            "design", "ligandmpnn_fasta_invalid",
                            f"{fa}: {exc}", recovery="skip malformed output",
                        )
                        return []
                    is_generated_record = False
                    # 跳过全 G 或单氨基酸重复（LigandMPNN baseline）
                    if len(set(line)) <= 2:
                        continue
                    if line not in seqs:
                        seqs.append(line)
        return seqs[:n_seq]
    except Exception as e:
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


def _build_refold_script(sequence, output_pdb):
    """Build a fixed-sequence AfCycDesign prediction script.

    ``design_3stage`` optimizes sequence logits and therefore cannot be used
    for refolding an already designed LigandMPNN sequence.  Prediction uses
    ``predict(seq=...)`` and verifies both ColabDesign's hard sequence and the
    emitted PDB before the manifest is allowed downstream.
    """
    if not _validate_sequence(sequence):
        raise ValueError("refold sequence must contain 8-20 standard amino acids")
    L = len(sequence)
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
if '"offset" in batch' not in source and "'offset' in batch" not in source:
    raise RuntimeError('ColabDesign backend does not consume cyclic pairwise offset')

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
idx = np.array(model._inputs['residue_index'])
off = np.array(idx[:,None] - idx[None,:])
off[:{L}, :{L}] = c_offset
model._inputs['offset'] = off

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


def _run_refold(sequence, output_pdb):
    """
    AfCycDesign refold：hallucination 折叠固定序列为环肽。
    只做基础折叠验证。pLDDT > 0.8 的最终过滤由 Prediction Agent 的 L1 负责。
    """
    script = _build_refold_script(sequence, output_pdb)
    spath = f"/tmp/refold_{os.getpid()}_{hash(sequence) % 100000}.py"
    plddt_file = f"{output_pdb}.plddt"
    # A failed retry must never inherit a PDB or score produced by an older run.
    for stale_artifact in (output_pdb, plddt_file):
        try:
            os.unlink(stale_artifact)
        except FileNotFoundError:
            pass
    with open(spath, "w") as f:
        f.write(script)
    try:
        r = subprocess.run([CYCPEP_PYTHON, spath], capture_output=True, text=True,
            timeout=1200,
            env={**os.environ,
                 "XLA_FLAGS": f"--xla_gpu_cuda_data_dir={CUDA_DATA_DIR}"})
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
        if not math.isfinite(plddt) or not 0.0 <= plddt <= 1.0:
            raise ValueError(f"invalid refold pLDDT: {plddt!r}")
        return plddt
    except Exception as e:
        EvidenceLogger.error("design", "refold_exception", str(e))
        return None
    finally:
        try:
            os.unlink(spath)
        except OSError:
            pass


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
            except ValueError as exc:
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
    except ValueError as exc:
        return {
            "pass": False,
            "reason": "unsupported_cyclization",
            "detail": str(exc),
        }
    criterion = CLOSURE_GEOMETRY[canonical]
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
    except (OSError, ValueError) as exc:
        return {
            **base,
            "reason": "pdb_parse_failed",
            "detail": str(exc),
        }


# ============================================================
# 共享工具
# ============================================================

def _pdb_residue_range(pdb_path, chain="A"):
    """解析 PDB 指定链的残基范围，返回 (min_resi, max_resi)。"""
    min_r, max_r = None, None
    try:
        with open(pdb_path) as f:
            for line in f:
                if line.startswith("ATOM") and line[21] == chain:
                    r = int(line[22:26].strip())
                    if min_r is None or r < min_r:
                        min_r = r
                    if max_r is None or r > max_r:
                        max_r = r
    except Exception as e:
        EvidenceLogger.error("design", "pdb_parse_failed",
            f"Cannot parse approved coordinate artifact {pdb_path} chain {chain}: {e}.",
            recovery="verify target PDB path")
        raise ValueError(f"cannot parse target PDB chain {chain}: {pdb_path}") from e
    if min_r is None:
        EvidenceLogger.error("design", "pdb_empty_chain",
            f"No atoms found in approved coordinate artifact {pdb_path} chain {chain}.")
        raise ValueError(f"target PDB contains no atoms for chain {chain}: {pdb_path}")
    return min_r, max_r


def _pdb_chain_residue_layout(pdb_path):
    """Return first-model PDB residues grouped in emitted chain order."""
    layout, seen = {}, {}
    model_seen = False
    with open(pdb_path) as handle:
        for line in handle:
            if line.startswith("MODEL"):
                if model_seen:
                    break
                model_seen = True
                continue
            if line.startswith("ENDMDL"):
                break
            if not line.startswith("ATOM"):
                continue
            chain = line[21].strip()
            residue_number = line[22:26].strip()
            insertion_code = line[26].strip()
            if not chain or not residue_number:
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
            if line.startswith("MODEL"):
                if model_seen:
                    break
                model_seen = True
                continue
            if line.startswith("ENDMDL"):
                break
            if not line.startswith("ATOM"):
                continue
            chain = line[21].strip()
            residue_number = line[22:26].strip()
            insertion_code = line[26].strip()
            residue_name = line[17:20].strip().upper()
            if not chain or not residue_number:
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


def _infer_binder_chain(pdb_path, expected_length):
    """Require one unique emitted chain with the requested binder length."""
    layout = _pdb_chain_residue_layout(pdb_path)
    if len(layout) < 2:
        raise ValueError(
            f"RFdiffusion binder complex requires at least two chains, got "
            f"{sorted(layout)}"
        )
    candidates = [
        chain for chain, residues in layout.items()
        if len(residues) == int(expected_length)
    ]
    if len(candidates) != 1:
        counts = {chain: len(residues) for chain, residues in layout.items()}
        raise ValueError(
            f"expected one {expected_length}-residue binder chain, "
            f"found {candidates}; chain lengths={counts}"
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
        if aa in "FW" and i < len(binder_residues):
            # W23/F19 锚点：固定不变
            ch, resi = binder_residues[i]
            fixed.append(f"{ch}{resi}")
        # L 残基不固定，让 backbone 几何自然偏置小氨基酸
    return " ".join(fixed)


def _validate_sequence(seq):
    valid = set("ACDEFGHIKLMNPQRSTVWY")
    s = seq.upper().replace("-","").replace("*","")
    return 8 <= len(s) <= 20 and all(c in valid for c in s)


def _next_candidate_id():
    with _LOCK:
        s = State.load()
        s["candidate_count"] = s.get("candidate_count", 0) + 1
        State.save(s)
        return f"C{s['candidate_count']:04d}"


def _describe_cyclize(n_term, c_term, linker):
    parts = []
    if n_term == "C" and c_term == "C":
        parts.append("Cys-Cys_disulfide")
    elif n_term == "" and c_term == "":
        parts.append("head-to-tail_amide")
    else:
        parts.append(f"{n_term or 'X'}-{c_term or 'X'}")
    if linker:
        parts.append(f"linker={linker}")
    return ",".join(parts)


def _load_target_spec():
    """
    从 State 读取 Research 产出的设计规则。
    若 Research 未运行则返回空结构（Route B/C 会报错退出）。
    """
    s = State.load()
    # 设计规则：Trp23 不变 / Phe19 ≤ Phe体积 / Leu26 换小脂肪族
    design_rules = s.get("design_rules", {}) or s.get("pocket_differences", {})
    return {
        "targets": s.get("targets", {}),
        "pocket_differences": s.get("pocket_differences", {}),
        "known_dual_binders": s.get("known_dual_binders", []),
        "design_rules": design_rules,
    }


def _merge_config(target_spec, design_config):
    """Merge run controls with the approved target and coordinate artifact.

    Target identity, chain, hotspots, and coordinate path are security-sensitive
    project inputs.  They come from the approved project config; callers may
    select a configured target but may not replace those fields ad hoc.
    """
    ts = target_spec or {}
    dc = design_config or {}
    project = ACTIVE_PROJECT_CONFIG
    assert_project_approved(project)

    default_target = project["targets"][0]["id"]
    target_ref = (
        dc.get("target_id") or ts.get("target_id") or ts.get("id")
        or dc.get("target_name") or ts.get("target_name")
        or default_target
    )
    target = assert_target_structure_ready(project, target_ref)
    structure = target.get("structure") or {}
    coordinate_value = structure.get("coordinate_path")
    if not coordinate_value:
        raise RuntimeError(
            f"approved target {target['id']} has no structure.coordinate_path; "
            "materialize and approve the coordinate artifact before Design"
        )
    coordinate_path = Path(coordinate_value).expanduser().resolve()
    if not coordinate_path.is_file():
        raise FileNotFoundError(
            f"approved coordinate artifact does not exist: {coordinate_path}"
        )

    requested_path = dc.get("target_pdb") or ts.get("target_pdb")
    if requested_path and Path(requested_path).expanduser().resolve() != coordinate_path:
        raise ValueError("target_pdb cannot override the approved coordinate_path")

    chain = structure.get("chain")
    if not chain:
        raise RuntimeError(f"approved target {target['id']} has no structure.chain")
    requested_chain = dc.get("chain") or ts.get("chain")
    if requested_chain and requested_chain != chain:
        raise ValueError("chain cannot override the approved target chain")

    binding_site = target.get("binding_site") or {}
    hotspots = ",".join(str(residue) for residue in binding_site.get("residues", []))
    requested_hotspots = dc.get("hotspots") or ts.get("hotspots")
    if requested_hotspots and requested_hotspots != hotspots:
        raise ValueError("hotspots cannot override the approved binding site")

    lengths = dc.get("lengths") or ts.get("lengths") or (
        target.get("design") or {}
    ).get("lengths", [10, 12, 14])
    lengths = [int(length) for length in lengths]
    if not lengths or any(length < 8 or length > 20 for length in lengths):
        raise ValueError("cyclic peptide lengths must be between 8 and 20 residues")

    n = dc.get("n") if dc.get("n") is not None else ts.get("n", 100)
    n = int(n)
    if n < 1:
        raise ValueError("n must be at least 1")

    seed = dc.get("seed") if dc.get("seed") is not None else ts.get("seed")
    if seed is None:
        seed = DEFAULT_SEED if DEFAULT_SEED is not None else int(time.time())

    return {
        "project_id": project["project_id"],
        "target_id": target["id"],
        "target_name": target["id"],
        "target_pdb": str(coordinate_path),
        "target_pdb_sha256": structure.get("coordinate_sha256"),
        "pdb_id": structure.get("pdb_id"),
        "chain": chain,
        "hotspots": hotspots,
        "lengths": lengths,
        "n": n,
        "seed": seed,
    }


# ============================================================
# 兼容旧 API
# ============================================================

def design_afcyc(target=None, n=10, lengths=None, hotspots=None, chain=None, seed=None):
    import warnings
    warnings.warn("deprecated, use design_rfpeptides", DeprecationWarning)
    target_spec = {}
    if target is not None:
        target_spec["target_name"] = target
    if chain is not None:
        target_spec["chain"] = chain
    if hotspots is not None:
        target_spec["hotspots"] = hotspots
    return design_rfpeptides(
        target_spec=target_spec,
        design_config={"n": n, "lengths": lengths or [10], "seed": seed})


def design_motif_graft(n=400, seed=None):
    import warnings
    warnings.warn("deprecated, use design_motif_guided", DeprecationWarning)
    return design_motif_guided(design_config={"n": n, "seed": seed})


def design_atsp_cyclize(n=200, seed=None):
    import warnings
    warnings.warn("deprecated, use design_atsp_derived", DeprecationWarning)
    return design_atsp_derived(design_config={"n": n, "seed": seed})


# ============================================================
# 兼容旧 dual_target_score（保留但不推荐）
# ============================================================

def dual_target_score(iptm_mdm2, iptm_mdmx):
    """旧版加权组合打分（被 Pareto 前沿替代，保留兼容）"""
    import warnings
    warnings.warn("dual_target_score deprecated, use threshold_filter+pareto_front",
                  DeprecationWarning)
    combined = (iptm_mdm2 + iptm_mdmx) / 2
    asymmetry = abs(iptm_mdm2 - iptm_mdmx)
    return {
        "dual_score": round(combined - 0.5 * asymmetry, 4),
        "combined": round(combined, 4),
        "asymmetry": round(asymmetry, 4),
        "passed": iptm_mdm2 > 0.7 and iptm_mdmx > 0.55 and asymmetry < 0.25,
    }


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(
        description=f"Design Agent v{DESIGN_PIPELINE_VERSION}"
    )
    p.add_argument("--route", choices=["A","B","C","all"], default="all")
    p.add_argument("--target", default=None,
                   help="configured target ID or PDB ID; defaults to the first approved target")
    p.add_argument("--n", type=int, default=10)
    p.add_argument("--lengths", default="10,12,14")
    p.add_argument("--hotspots", default=None)
    p.add_argument("--chain", default=None,
                   help="must match the approved target chain when provided")
    p.add_argument("--seed", type=int, default=None)
    args = p.parse_args()

    lengths = [int(x) for x in args.lengths.split(",")]
    ts = {}
    if args.chain:
        ts["chain"] = args.chain
    if args.target:
        ts["target_name"] = args.target
    if args.hotspots:
        ts["hotspots"] = args.hotspots
    dc = {"n": args.n, "lengths": lengths, "seed": args.seed}

    all_cands = []
    if args.route in ("A","all"):
        print(f"[Route A v5] target={args.target}, n={args.n}, len={lengths}")
        result = design_rfpeptides(target_spec=ts, design_config=dc)
        all_cands.extend(result)
        print(f"[Route A] 完成: {len(result)} candidates")
    if args.route in ("B","all"):
        print(f"[Route B v5] n={args.n}")
        result = design_motif_guided(target_spec=ts, design_config=dc)
        all_cands.extend(result)
        print(f"[Route B] 完成: {len(result)} candidates")
    if args.route in ("C","all"):
        print(f"[Route C v5] n={args.n}")
        result = design_atsp_derived(target_spec=ts, design_config=dc)
        all_cands.extend(result)
        print(f"[Route C] 完成: {len(result)} candidates")

    print(f"\nDone: {len(all_cands)} candidates")
    print(CandidateIndex.stats())
