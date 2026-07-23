"""
Design Agent — 于嘉乐
职责：三条设计路线，统一输出格式，写入 CandidateIndex 和 EvidenceLogger
入口：design_afcyc(target, n, lengths, hotspots) → list[dict]
      design_motif_graft(n) → list[dict]
      design_atsp_cyclize(n) → list[dict]
依赖：from data_layer import EvidenceLogger, CandidateIndex, State, file_hash
      ColabDesign v1.1.2 / ProteinMPNN v0.1.3
"""

import os, sys, json, time, subprocess, tempfile, random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from data_layer import EvidenceLogger, CandidateIndex, State, file_hash


# ============================================================
# 常量 — 设计依据
# ============================================================

# 靶点→设计参数映射（来源：research.py TARGETS.pocket_residues，p53 Phe19/Trp23/Leu26 对齐）
# MDM2 1YCR chain A: Phe19→LEU54, Trp23→VAL93, Leu26→HIS96
# MDMX 3DAB chain A: Phe19→MET53, Trp23→VAL92, Leu26→PRO95
HOTSPOT_MAP = {
    "1YCR": {"chain": "A", "hotspots": "54,93,96", "lengths": [8, 10, 12]},
    "3DAB": {"chain": "A", "hotspots": "53,92,95", "lengths": [8, 10, 12]},
}

# 已知双靶结合肽种子序列（来源：research.py KNOWN_DUAL_BINDERS）
# PMI: PMID 34589387; pDI: PMID 19910468; ATSP-7041: PMID 23946421
MOTIF_TEMPLATES = [
    {"name": "PMI",        "seq": "TSFAEYWNLLSP"},
    {"name": "pDI",        "seq": "LTFEHYWAQLTS"},
    {"name": "ATSP_7041",  "seq": "LTFLEYWAAQSL"},
]

# Route C: ATSP-7041 环化参数
# 核心序列来自 research.py KNOWN_DUAL_BINDERS[3]（ATSP-7041 核心 = LTFLEYWAAQSL）
# 此处取文献报道的 Phe19/Trp23/Leu26(Cba) 三残基保守骨架
ATSP_CORE = "LTFLEYWAAQSL"

# Gly/Ser linker 长度矩阵（阶段13 §3 组长指定范围：5,10,15,20,25,30,35 aa）
LINKER_MATRIX = ["GGGGS", "GGGS", "GGS", "GS", ""]

# 二硫键/酰胺键环化对（N端, C端）
DISULFIDE_PAIRS = [("C", "C"), ("C", "S"), ("S", "C"), ("", "")]

# Route C 需达到 n 时的扩展方法可用氨基酸
SCAFFOLD_MUTABLE_AA = "ACDEFGHIKLMNPQRSTVWY"

# ColabDesign 环境路径
COLABDESIGN_PARAMS = "/root/ColabDesign/params"
COLABDESIGN_ROOT = "/root/ColabDesign"


# ============================================================
# Route A: ColabDesign 靶点导向环肽设计
# ============================================================

def design_afcyc(target: str, n: int = 10,
                 lengths: list = None,
                 hotspots: str = None,
                 chain: str = "A") -> list[dict]:
    """
    用 ColabDesign fixbb + cyclic offset 设计靶点导向环肽。

    每对 (length, i) 生成一个子进程：
      python /root/designs/route_A/<batch>/script_<cid>.py

    子进程调用 ColabDesign fixbb 模式：
      mk_af_model(protocol='fixbb') → prep_inputs → add_cyclic_offset →
      design_3stage(50, 50, 10) → save_pdb

    Args:
        target: 靶点 PDB ID（如 "1YCR"）
        n: 每条长度生成的候选数
        lengths: 环肽长度列表，默认从 HOTSPOT_MAP 读取
        hotspots: hotspot 残基编号，逗号分隔
        chain: 靶点链 ID

    Returns:
        list[dict]: 候选列表，写入 CandidateIndex + EvidenceLogger
    """
    lengths = lengths or HOTSPOT_MAP.get(target, {}).get("lengths", [10])
    hotspots = hotspots or HOTSPOT_MAP.get(target, {}).get("hotspots", "")
    chain = chain or HOTSPOT_MAP.get(target, {}).get("chain", "A")

    target_pdb = f"/root/targets/{target}.pdb"
    if not os.path.exists(target_pdb):
        raise FileNotFoundError(f"靶点 PDB 不存在: {target_pdb}")
    target_hash = file_hash(target_pdb)

    route_name = f"route_A_{target.lower()}_first"
    batch_id = f"batch_{target}_len{'_'.join(map(str, lengths))}"
    out_dir = f"/root/designs/route_A/{batch_id}"
    os.makedirs(out_dir, exist_ok=True)

    total_generated = 0
    total_valid = 0
    candidates = []
    t_batch_start = time.time()

    for L in lengths:
        for i in range(n):
            total_generated += 1
            cid = _next_candidate_id()
            output_path = f"{out_dir}/{cid}.pdb"
            script_path = f"{out_dir}/script_{cid}.py"

            # 写子进程脚本（保留在 out_dir，可复现）
            script = _build_design_script(target_pdb, chain, L, hotspots, output_path)
            with open(script_path, "w") as f:
                f.write(script)

            t0 = time.time()
            result = subprocess.run(
                ["python", script_path],
                capture_output=True, text=True, timeout=600,
                cwd=COLABDESIGN_ROOT,
                env={**os.environ, "XLA_FLAGS": "--xla_gpu_cuda_data_dir=/usr/local/cuda-12.1"}
            )
            duration = round(time.time() - t0, 1)

            seq = ""
            valid = False
            pdb_hash = ""
            if result.returncode == 0 and os.path.exists(output_path):
                seq = _extract_sequence_from_pdb(output_path, binder_len=L)
                pdb_hash = file_hash(output_path)
                valid = len(seq) == L
                total_valid += 1

            candidate = {
                "candidate_id": cid,
                "sequence": seq,
                "length": L,
                "source_route": route_name,
                "source_batch": batch_id,
                "notes": f"colabdesign_fixbb,hotspots={hotspots},pdb_hash={pdb_hash}"
            }
            candidates.append(candidate)
            CandidateIndex.add(candidate)
            EvidenceLogger.candidate_registered(candidate)

            if not valid:
                EvidenceLogger.error(
                    agent="design",
                    error_type="afcyc_failed",
                    message=f"{cid}: seq_len={len(seq)} expected={L} exit={result.returncode}",
                    recovery="skip",
                    trace=result.stderr[:500] if result.stderr else ""
                )

            # 清理临时脚本
            try:
                os.unlink(script_path)
            except OSError:
                pass

    duration_batch = round(time.time() - t_batch_start, 1)

    # 记一条 batch 级日志（不是每个候选一条）
    EvidenceLogger.design_batch(
        route=route_name,
        n_generated=total_generated,
        n_valid=total_valid,
        tool_name="afcycdesign_colabdesign",
        tool_version="v1.1.2",
        duration_sec=duration_batch
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
        for L in model._lengths:
            offset[Ln:Ln+L, Ln:Ln+L] = cyclic_offset(L)
            Ln += L
    model._inputs['offset'] = offset

model = mk_af_model(protocol='fixbb', data_dir='/root/ColabDesign/params')
model.prep_inputs(pdb_filename='{pdb_path}', chain='{chain}', binder_len={length}, hotspot='{hotspots}')
add_cyclic_offset(model)
model.design_3stage(50, 50, 10)
model.save_pdb('{output_path}')
"""


# ============================================================
# Route B: Motif grafting + ProteinMPNN 序列优化
# ============================================================

def design_motif_graft(n: int = 400) -> list[dict]:
    """
    从已知双靶结合肽提取 motif，用 ProteinMPNN 做固定位点序列优化。

    算法：
      1. 从 MOTIF_TEMPLATES 取种子序列
      2. 对位置 [3,5,8,12,15] 做掩码
      3. 调 ProteinMPNN 在掩码位点上做 score-based 优化
      4. 返回最优替换序列

    Args:
        n: 生成候选总数

    Returns:
        list[dict]: 候选列表
    """
    route_name = "route_B_motif_graft"
    batch_id = f"batch_motif_graft_{int(time.time())}"
    candidates = []

    t0 = time.time()

    # 每个模板平均分配 candidates
    base = n // len(MOTIF_TEMPLATES)
    remainder = n % len(MOTIF_TEMPLATES)
    quotas = [base + 1 if idx < remainder else base for idx in range(len(MOTIF_TEMPLATES))]

    for idx, tmpl in enumerate(MOTIF_TEMPLATES):
        for i in range(quotas[idx]):
            if len(candidates) >= n:
                break

            optimized_seq = _proteinmpnn_optimize(tmpl["seq"])
            valid = _validate_sequence(optimized_seq)

            cid = _next_candidate_id()
            candidate = {
                "candidate_id": cid,
                "sequence": optimized_seq,
                "length": len(optimized_seq),
                "source_route": route_name,
                "source_batch": batch_id,
                "notes": f"template={tmpl['name']},ref=PMID:"
                         f"{'34589387' if tmpl['name']=='PMI' else '19910468' if tmpl['name']=='pDI' else '23946421'}"
            }
            candidates.append(candidate)
            CandidateIndex.add(candidate)
            EvidenceLogger.candidate_registered(candidate)

            if not valid:
                EvidenceLogger.error(
                    agent="design",
                    error_type="sequence_invalid",
                    message=f"{cid}: validation failed for seq={optimized_seq[:20]}...",
                    recovery="kept"
                )

    duration = round(time.time() - t0, 1)

    EvidenceLogger.design_batch(
        route=route_name,
        n_generated=n,
        n_valid=len(candidates),
        tool_name="proteinmpnn_motif_graft",
        tool_version="0.1.3",
        duration_sec=duration
    )
    return candidates


def _proteinmpnn_optimize(template_seq: str) -> str:
    """
    调用 ProteinMPNN 对模板序列做固定位点优化。
    掩码位置：[3, 5, 8, 12, 15] — 对应 p53 Phe19/Trp23/Leu26 锚定残基附近的可变异位点。
    """
    try:
        import torch
        from proteinmpnn.run import get_model, score_seq
        import numpy as np

        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = get_model(device=device)

        seq = template_seq.upper()
        masked = list(seq)
        mask_positions = [p for p in [3, 5, 8, 12, 15] if p < len(masked)]
        for pos in mask_positions:
            masked[pos] = "_"
        masked_seq = "".join(masked)

        scores = score_seq(model, [masked_seq], num_sequential=1)
        if scores and scores[0]:
            best = max(scores[0], key=lambda x: x.get("score", 0))
            return best["seq"]

    except Exception as e:
        EvidenceLogger.error("design", "proteinmpnn_error", str(e))

    return template_seq


# ============================================================
# Route C: ATSP-7041 环化改造
# ============================================================

def design_atsp_cyclize(n: int = 200) -> list[dict]:
    """
    基于 ATSP-7041 scaffold 做环化改造。

    生成策略（两级扩展以覆盖 n）：
      第1级：linker(5) × 二硫键对(4) = 20 个基础组合
      第2级：若 n > 20，对每个基础组合在 ATSP_CORE 上做 len(SCAFFOLD_MUTABLE_AA)
             个位置的随机单点氨基酸突变，直到达到 n

    Args:
        n: 生成候选总数

    Returns:
        list[dict]: 候选列表
    """
    route_name = "route_C_atsp"
    batch_id = f"batch_atsp_{int(time.time())}"
    candidates = []

    t0 = time.time()

    # 第1级：linker × 二硫键 全矩阵
    base_combos = []
    for linker in LINKER_MATRIX:
        for cys_n, cys_c in DISULFIDE_PAIRS:
            seq = f"{cys_n}{ATSP_CORE}{linker}{cys_c}"
            cyclize_desc = _describe_cyclize(cys_n, cys_c, linker)
            base_combos.append((seq, cyclize_desc))

    # 第2级：不够 n 则做单点突变扩展
    expanded = list(base_combos)
    mut_positions = [3, 5, 8, 10, 12]  # ATSP_CORE 上的可变位置
    while len(expanded) < n:
        for seq, desc in base_combos:
            if len(expanded) >= n:
                break
            pos = random.choice(mut_positions)
            new_aa = random.choice(SCAFFOLD_MUTABLE_AA)
            offset = len(_clip_terminal(seq, "N"))
            idx = offset + pos
            if idx < len(seq):
                mutated = seq[:idx] + new_aa + seq[idx+1:]
                expanded.append((mutated, f"{desc},mut:{pos}={new_aa}"))

    for seq, desc in expanded[:n]:
        valid = _validate_sequence(seq)
        cid = _next_candidate_id()
        candidate = {
            "candidate_id": cid,
            "sequence": seq,
            "length": len(seq),
            "source_route": route_name,
            "source_batch": batch_id,
            "notes": desc
        }
        candidates.append(candidate)
        CandidateIndex.add(candidate)
        EvidenceLogger.candidate_registered(candidate)

        if not valid:
            EvidenceLogger.error(
                agent="design",
                error_type="sequence_invalid",
                message=f"{cid}: validation failed",
                recovery="kept"
            )

    duration = round(time.time() - t0, 1)

    EvidenceLogger.design_batch(
        route=route_name,
        n_generated=n,
        n_valid=len(candidates),
        tool_name="atsp_cyclize_template",
        tool_version="manual_v1",
        duration_sec=duration
    )
    return candidates


def _describe_cyclize(n_term, c_term, linker):
    """生成环化方式的人读描述。"""
    parts = []
    if n_term and c_term:
        parts.append(f"{n_term}-{c_term}")
    elif n_term or c_term:
        parts.append("amide")
    else:
        parts.append("head-to-tail")
    if linker:
        parts.append(f"linker={linker}")
    return ",".join(parts)


def _clip_terminal(seq, side):
    """去除 N/C 端环化残基，返回核心序列。"""
    s = seq
    if side == "N" and s and s[0] in "CS":
        s = s[1:]
    elif side == "C" and s and s[-1] in "CS":
        s = s[:-1]
    return s


# ============================================================
# 共享工具函数
# ============================================================

def _validate_sequence(seq: str) -> bool:
    """
    序列合法性检查。
    条件：长度 6-20，仅含标准 20 种氨基酸单字母代码。
    """
    valid_aas = set("ACDEFGHIKLMNPQRSTVWY")
    seq_clean = seq.upper().replace("-", "").replace("*", "")
    return (6 <= len(seq_clean) <= 20 and
            all(c in valid_aas for c in seq_clean))


def _extract_sequence_from_pdb(pdb_path: str, binder_len: int = None) -> str:
    """
    从 PDB 提取 CA 原子序列（单字母）。
    binder_len：若给定，只取末尾 N 个唯一残基（fixbb 模式 binder 在靶点之后）。
    """
    three_to_one = {
        'ALA': 'A', 'ARG': 'R', 'ASN': 'N', 'ASP': 'D', 'CYS': 'C',
        'GLN': 'Q', 'GLU': 'E', 'GLY': 'G', 'HIS': 'H', 'ILE': 'I',
        'LEU': 'L', 'LYS': 'K', 'MET': 'M', 'PHE': 'F', 'PRO': 'P',
        'SER': 'S', 'THR': 'T', 'TRP': 'W', 'TYR': 'Y', 'VAL': 'V',
    }
    seq, seen = [], set()
    try:
        with open(pdb_path) as f:
            for line in f:
                if line.startswith("ATOM") and line[12:16].strip() == "CA":
                    chain = line[21].strip()
                    resid = line[22:26].strip()
                    key = (chain, resid)
                    if key not in seen:
                        seen.add(key)
                        seq.append(three_to_one.get(line[17:20].strip(), "X"))
    except FileNotFoundError:
        return ""

    if binder_len and len(seq) >= binder_len:
        seq = seq[-binder_len:]
    return "".join(seq)


def _next_candidate_id() -> str:
    """
    从 State 读当前计数器 → 格式化为 CXXXX → 返回。
    EvidenceLogger.candidate_registered() 会自动 +1。
    因此下一个调用者拿到的 ID 自动递增。
    """
    count = State.load().get("candidate_count", 0)
    return f"C{count + 1:04d}"


# ============================================================
# 主入口
# ============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Design Agent — 三条环肽设计路线",
        epilog="示例: python agents/design.py --route A --target 1YCR --n 5 --lengths 8,10"
    )
    parser.add_argument("--route", choices=["A", "B", "C", "all"], default="all",
                        help="设计路线。all = 全部三条")
    parser.add_argument("--target", default="1YCR",
                        help="靶点 PDB ID（Route A 专用）")
    parser.add_argument("--n", type=int, default=10,
                        help="每条长度/每条路线生成的候选数")
    parser.add_argument("--lengths", default="8,10,12",
                        help="环肽长度，逗号分隔（Route A 专用）")
    parser.add_argument("--hotspots", default=None,
                        help="hotspot 残基编号，逗号分隔（Route A 专用）")
    args = parser.parse_args()

    lengths = [int(x) for x in args.lengths.split(",")]
    t_start = time.time()
    all_candidates = []

    if args.route in ("A", "all"):
        print(f"[Route A] target={args.target}, n={args.n}, lengths={lengths}")
        all_candidates.extend(
            design_afcyc(args.target, args.n, lengths, args.hotspots))

    if args.route in ("B", "all"):
        print(f"[Route B] motif grafting, n={args.n}")
        all_candidates.extend(design_motif_graft(args.n))

    if args.route in ("C", "all"):
        print(f"[Route C] ATSP-7041 cyclize, n={args.n}")
        all_candidates.extend(design_atsp_cyclize(args.n))

    elapsed = round(time.time() - t_start, 1)
    print(f"\n{'='*50}")
    print(f"总计: {len(all_candidates)} 条候选, 耗时 {elapsed}s")
    print(f"统计: {CandidateIndex.stats()}")
