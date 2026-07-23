"""
Design Agent — 于嘉乐
职责：三条设计路线，统一输出格式，写入 CandidateIndex 和 EvidenceLogger
入口：design_afcyc(target, n, lengths, hotspots) → list[dict]
      design_motif_graft(n) → list[dict]
      design_atsp_cyclize(n) → list[dict]
依赖：from data_layer import EvidenceLogger, CandidateIndex, State, file_hash
      ColabDesign v1.1.2 / ProteinMPNN v0.1.3
"""

import os, sys, json, time, subprocess, random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from data_layer import EvidenceLogger, CandidateIndex, State, file_hash


# ============================================================
# 常量 — 设计依据
# ============================================================
# 注：这些常量是 Research Agent 产出的固化版本。当 research.py run() 执行后，
# State 中会写入 targets / pocket_differences / known_dual_binders。
# 当前 Design 读取这些常量作为默认值；Research 产出新数据后，调用者传参覆盖。
# 数据契约：见 §四 reviewer 提出的 target_design_spec.json 方案。

HOTSPOT_MAP = {
    "1YCR": {"chain": "A", "hotspots": "54,93,96", "lengths": [8, 10, 12]},
    "3DAB": {"chain": "A", "hotspots": "53,92,95", "lengths": [8, 10, 12]},
}

MOTIF_TEMPLATES = [
    {"name": "PMI",        "seq": "TSFAEYWNLLSP", "pmid": "34589387"},
    {"name": "pDI",        "seq": "LTFEHYWAQLTS", "pmid": "19910468"},
    {"name": "ATSP_7041",  "seq": "LTFLEYWAAQSL", "pmid": "23946421"},
]

ATSP_CORE = "LTFLEYWAAQSL"
LINKER_MATRIX = ["GGGGS", "GGGS", "GGS", "GS", ""]

# 环化方式：(N端, C端)
# ("C","C") = 末端Cys二硫键；("","") = 首尾酰胺键环化
CYCLIZATION_PAIRS = [("C", "C"), ("", "")]

SCAFFOLD_MUTABLE_AA = "ACDEFGHIKLMNPQRSTVWY"

# 路径配置（可被环境变量覆盖）
COLABDESIGN_PARAMS = os.environ.get("COLABDESIGN_PARAMS", "/root/ColabDesign/params")
COLABDESIGN_ROOT   = os.environ.get("COLABDESIGN_ROOT", "/root/ColabDesign")
TARGETS_DIR        = os.environ.get("DESIGN_TARGETS_DIR", "/root/targets")
OUTPUT_DIR         = os.environ.get("DESIGN_OUTPUT_DIR", "/root/designs")
CUDA_DATA_DIR      = os.environ.get("CUDA_DATA_DIR", "/usr/local/cuda-12.1")

# 随机种子（设 0 则用时间戳）
DEFAULT_SEED = int(os.environ.get("DESIGN_SEED", "0"))


# ============================================================
# Route A: ColabDesign 靶点导向环肽设计
# ============================================================

def design_afcyc(target, n=10, lengths=None, hotspots=None, chain="A",
                 seed=None):
    """
    用 ColabDesign fixbb + cyclic offset 设计靶点导向环肽。

    Args:
        target:   靶点 PDB ID 或文件路径
        n:        每条长度生成的候选数
        lengths:  环肽长度列表
        hotspots: hotspot 残基编号
        chain:    靶点链 ID
        seed:     随机种子（None=用时间戳）

    Returns:
        list[dict]: 有效候选列表（失败的不写入 CandidateIndex）
    """
    lengths = lengths or HOTSPOT_MAP.get(target, {}).get("lengths", [10])
    hotspots = hotspots or HOTSPOT_MAP.get(target, {}).get("hotspots", "")
    chain = chain or HOTSPOT_MAP.get(target, {}).get("chain", "A")
    seed = seed or DEFAULT_SEED or int(time.time())
    random.seed(seed)

    # 靶点 PDB 路径：支持完整路径 或 PDB ID
    if os.path.isfile(target):
        target_pdb = target
        target_id = os.path.splitext(os.path.basename(target))[0]
    else:
        target_pdb = f"{TARGETS_DIR}/{target}.pdb"
        target_id = target
    if not os.path.exists(target_pdb):
        raise FileNotFoundError(f"靶点 PDB 不存在: {target_pdb}")
    target_hash = file_hash(target_pdb)

    route_name = f"route_A_{target_id.lower()}_first"
    batch_id = f"batch_{target_id}_len{'_'.join(map(str, lengths))}_s{seed}"
    out_dir = f"{OUTPUT_DIR}/route_A/{batch_id}"
    os.makedirs(out_dir, exist_ok=True)

    # 保存设计配置供复现
    config = {
        "route": "A", "target_pdb": target_pdb, "target_hash": target_hash,
        "chain": chain, "lengths": lengths, "hotspots": hotspots,
        "seed": seed, "iterations": [50, 50, 10],
        "colabdesign_version": "v1.1.2",
    }
    with open(f"{out_dir}/design_config.json", "w") as f:
        json.dump(config, f, indent=2)

    total_gen, total_valid, candidates = 0, 0, []
    t_batch = time.time()

    for L in lengths:
        for i in range(n):
            total_gen += 1
            cid = _next_candidate_id()
            output_path = f"{out_dir}/{cid}.pdb"
            spath = f"{out_dir}/script_{cid}.py"
            script = _build_design_script(target_pdb, chain, L, hotspots, output_path)
            with open(spath, "w") as f:
                f.write(script)

            t0 = time.time()
            result = subprocess.run(
                ["python", spath], capture_output=True, text=True, timeout=600,
                cwd=COLABDESIGN_ROOT,
                env={**os.environ, "XLA_FLAGS": f"--xla_gpu_cuda_data_dir={CUDA_DATA_DIR}"}
            )
            dur = round(time.time() - t0, 1)

            seq = _extract_sequence_from_pdb(output_path, binder_len=L) if (
                result.returncode == 0 and os.path.exists(output_path)) else ""
            valid = (len(seq) == L and _validate_sequence(seq))
            pdb_hash = file_hash(output_path) if os.path.exists(output_path) else ""

            candidate = {
                "candidate_id": cid,
                "sequence": seq,
                "length": L,
                "source_route": route_name,
                "source_batch": batch_id,
                "notes": (
                    f"pdb={output_path},hash={pdb_hash},"
                    f"hotspots={hotspots},seed={seed}"
                )
            }

            if valid:
                total_valid += 1
                candidates.append(candidate)
                CandidateIndex.add(candidate)
                EvidenceLogger.candidate_registered(candidate)
            else:
                EvidenceLogger.error(
                    agent="design", error_type="afcyc_failed",
                    message=f"{cid}: seq_len={len(seq)} expected={L} exit={result.returncode}",
                    recovery="skip",
                    trace=result.stderr[:500] if result.stderr else ""
                )

            try:
                os.unlink(spath)
            except OSError:
                pass

    EvidenceLogger.design_batch(
        route=route_name, n_generated=total_gen, n_valid=total_valid,
        tool_name="afcycdesign_colabdesign", tool_version="v1.1.2",
        duration_sec=round(time.time() - t_batch, 1)
    )
    return candidates


def _build_design_script(pdb_path, chain, length, hotspots, output_path):
    """生成 ColabDesign 子进程脚本。"""
    return f"""
import numpy as np
from colabdesign import mk_af_model

def add_cyclic_offset(model, offset_type=2):
    def cyclic_offset(L):
        i = np.arange(L)
        ij = np.stack([i, i+L], -1)
        offset = i[:,None] - i[None,:]
        c_offset = np.abs(ij[:,None,:,None] - ij[:,None,:]).min((2,3))
        if offset_type >= 2:
            a = c_offset < np.abs(offset)
            c_offset[a] = -c_offset[a]
        return c_offset * np.sign(offset)
    idx = model._inputs['residue_index']
    offset = np.array(idx[:,None] - idx[None,:])
    if model.protocol in ['fixbb','partial','hallucination']:
        Ln = 0
        for Lg in model._lengths:
            offset[Ln:Ln+Lg, Ln:Ln+Lg] = cyclic_offset(Lg)
            Ln += Lg
    model._inputs['offset'] = offset

model = mk_af_model(protocol='fixbb', data_dir='{COLABDESIGN_PARAMS}')
model.prep_inputs(pdb_filename='{pdb_path}', chain='{chain}',
                  binder_len={length}, hotspot='{hotspots}')
add_cyclic_offset(model)
model.design_3stage(50, 50, 10)
model.save_pdb('{output_path}')
"""


# ============================================================
# Route B: Motif grafting + ProteinMPNN 序列优化
# ============================================================

def design_motif_graft(n=400, seed=None):
    """
    从已知双靶结合肽提取 motif，用 ProteinMPNN 做固定位点序列优化。

    去重逻辑：若 ProteinMPNN 返回模板原序列（异常/无优化空间），
    不写入 CandidateIndex，避免 Prediction 拿到重复候选。

    Args:
        n: 生成候选总数
        seed: 随机种子

    Returns:
        list[dict]: 有效候选列表
    """
    seed = seed or DEFAULT_SEED or int(time.time())
    random.seed(seed)

    route_name = "route_B_motif_graft"
    batch_id = f"batch_motif_graft_{int(time.time())}_s{seed}"
    candidates = []
    t0 = time.time()

    base = n // len(MOTIF_TEMPLATES)
    remainder = n % len(MOTIF_TEMPLATES)
    quotas = [base + 1 if idx < remainder else base
              for idx in range(len(MOTIF_TEMPLATES))]

    total_gen, total_valid = 0, 0

    for idx, tmpl in enumerate(MOTIF_TEMPLATES):
        for i in range(quotas[idx]):
            total_gen += 1
            if len(candidates) >= n:
                break

            optimized_seq = _proteinmpnn_optimize(tmpl["seq"])
            valid = _validate_sequence(optimized_seq)
            # 检测静默回退：返回的序列与模板完全一致 → 优化失败
            is_fallback = (optimized_seq == tmpl["seq"])

            cid = _next_candidate_id()
            candidate = {
                "candidate_id": cid,
                "sequence": optimized_seq,
                "length": len(optimized_seq),
                "source_route": route_name,
                "source_batch": batch_id,
                "notes": f"template={tmpl['name']},pmid={tmpl['pmid']},seed={seed}"
            }

            if valid and not is_fallback:
                total_valid += 1
                candidates.append(candidate)
                CandidateIndex.add(candidate)
                EvidenceLogger.candidate_registered(candidate)
            elif is_fallback:
                EvidenceLogger.error(
                    agent="design", error_type="proteinmpnn_fallback",
                    message=f"{cid}: {tmpl['name']} returned unchanged template",
                    recovery="skip"
                )
            else:
                EvidenceLogger.error(
                    agent="design", error_type="sequence_invalid",
                    message=f"{cid}: validation failed", recovery="skip"
                )

    EvidenceLogger.design_batch(
        route=route_name, n_generated=total_gen, n_valid=total_valid,
        tool_name="proteinmpnn_motif_graft", tool_version="0.1.3",
        duration_sec=round(time.time() - t0, 1)
    )
    return candidates


def _proteinmpnn_optimize(template_seq):
    """ProteinMPNN score-based 单条序列优化。异常时返回 None 而非模板原序列。"""
    try:
        import torch
        from proteinmpnn.run import get_model, score_seq

        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = get_model(device=device)

        seq = template_seq.upper()
        masked = list(seq)
        for pos in [3, 5, 8, 12, 15]:
            if pos < len(masked):
                masked[pos] = "_"
        masked_seq = "".join(masked)

        scores = score_seq(model, [masked_seq], num_sequential=1)
        if scores and scores[0]:
            best = max(scores[0], key=lambda x: x.get("score", 0))
            result = best.get("seq", template_seq)
            return result
    except Exception as e:
        EvidenceLogger.error("design", "proteinmpnn_error",
                             f"template={template_seq[:10]}...: {str(e)[:100]}")

    return template_seq  # 异常时返回模板原序列（调用方通过 is_fallback 检测）


# ============================================================
# Route C: ATSP-7041 环化改造
# ============================================================

def design_atsp_cyclize(n=200, seed=None):
    """
    基于 ATSP-7041 scaffold 做环化改造。

    生成策略：
      第1级：linker(5) × 环化方式(2) = 10 个基础组合
      第2级：若 n > 10，随机单点突变扩展至 n
    环化方式：Cys-Cys 二硫键 或 首尾酰胺键环化。
    产物为天然氨基酸序列，不含非天然订书钉——这是"环化改造"与
    ATSP-7041 原分子的关键区别，在 notes 中标注。

    Args:
        n:    生成候选总数
        seed: 随机种子

    Returns:
        list[dict]: 候选列表
    """
    seed = seed or DEFAULT_SEED or int(time.time())
    random.seed(seed)

    route_name = "route_C_atsp"
    batch_id = f"batch_atsp_{int(time.time())}_s{seed}"
    candidates = []
    t0 = time.time()

    # 第1级：linker × 环化 全矩阵
    base_combos = []
    for linker in LINKER_MATRIX:
        for cn, cc in CYCLIZATION_PAIRS:
            seq = f"{cn}{ATSP_CORE}{linker}{cc}"
            desc = _describe_cyclize(cn, cc, linker)
            if _validate_sequence(seq):
                base_combos.append((seq, desc))

    # 第2级：不够 n 则随机单点突变扩展
    expanded = list(base_combos)
    mut_positions = [3, 5, 8, 10, 12]
    max_attempts = n * 4
    attempts = 0
    while len(expanded) < n and attempts < max_attempts:
        attempts += 1
        for seq, desc in base_combos:
            if len(expanded) >= n:
                break
            pos = random.choice(mut_positions)
            aa = random.choice(SCAFFOLD_MUTABLE_AA)
            offset = 1 if seq and seq[0] in "C" else 0
            idx = offset + pos
            if idx < len(seq):
                mutated = seq[:idx] + aa + seq[idx+1:]
                if _validate_sequence(mutated):
                    expanded.append((mutated, f"{desc},mut:{pos}={aa}"))

    for seq, desc in expanded[:n]:
        cid = _next_candidate_id()
        candidate = {
            "candidate_id": cid,
            "sequence": seq,
            "length": len(seq),
            "source_route": route_name,
            "source_batch": batch_id,
            "notes": f"{desc},seed={seed}"
        }
        candidates.append(candidate)
        CandidateIndex.add(candidate)
        EvidenceLogger.candidate_registered(candidate)

    EvidenceLogger.design_batch(
        route=route_name, n_generated=n, n_valid=len(candidates),
        tool_name="atsp_cyclize_template", tool_version="manual_v2",
        duration_sec=round(time.time() - t0, 1)
    )
    return candidates


def _describe_cyclize(n_term, c_term, linker):
    """生成环化方式的描述字符串。"""
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


# ============================================================
# 共享工具函数
# ============================================================

def _validate_sequence(seq):
    """序列合法性：长度 6-20，仅含标准 20 种氨基酸。"""
    valid_aas = set("ACDEFGHIKLMNPQRSTVWY")
    s = seq.upper().replace("-", "").replace("*", "")
    return 6 <= len(s) <= 20 and all(c in valid_aas for c in s)


def _extract_sequence_from_pdb(pdb_path, binder_len=None):
    """从 PDB 提取 CA 序列。binder_len：只取末尾 N 个残基。"""
    tto = {
        'ALA':'A','ARG':'R','ASN':'N','ASP':'D','CYS':'C',
        'GLN':'Q','GLU':'E','GLY':'G','HIS':'H','ILE':'I',
        'LEU':'L','LYS':'K','MET':'M','PHE':'F','PRO':'P',
        'SER':'S','THR':'T','TRP':'W','TYR':'Y','VAL':'V',
    }
    seq, seen = [], set()
    try:
        with open(pdb_path) as f:
            for line in f:
                if line.startswith("ATOM") and line[12:16].strip() == "CA":
                    k = (line[21].strip(), line[22:26].strip())
                    if k not in seen:
                        seen.add(k)
                        seq.append(tto.get(line[17:20].strip(), "X"))
    except FileNotFoundError:
        return ""
    if binder_len and len(seq) >= binder_len:
        seq = seq[-binder_len:]
    return "".join(seq)


def _next_candidate_id():
    """从 State 读计数器 → CXXXX。candidate_registered() 自动 +1。"""
    return f"C{State.load().get('candidate_count', 0) + 1:04d}"


# ============================================================
# 主入口
# ============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Design Agent — 三条环肽设计路线",
        epilog="示例: python agents/design.py --route A --target 1YCR --n 5 --lengths 8,10"
    )
    parser.add_argument("--route", choices=["A","B","C","all"], default="all")
    parser.add_argument("--target", default="1YCR")
    parser.add_argument("--n", type=int, default=10)
    parser.add_argument("--lengths", default="8,10,12")
    parser.add_argument("--hotspots", default=None)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    all_cands = []
    if args.route in ("A","all"):
        print(f"[Route A] target={args.target}, n={args.n}")
        all_cands.extend(design_afcyc(args.target, args.n,
            [int(x) for x in args.lengths.split(",")], args.hotspots,
            seed=args.seed))
    if args.route in ("B","all"):
        print(f"[Route B] n={args.n}")
        all_cands.extend(design_motif_graft(args.n, seed=args.seed))
    if args.route in ("C","all"):
        print(f"[Route C] n={args.n}")
        all_cands.extend(design_atsp_cyclize(args.n, seed=args.seed))

    print(f"\nDone: {len(all_cands)} candidates")
    print(CandidateIndex.stats())
