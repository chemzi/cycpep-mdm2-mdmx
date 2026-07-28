"""
Design Agent — 于嘉乐
职责：三条设计路线，统一输出格式，写入 CandidateIndex 和 EvidenceLogger
入口：design_afcyc(target, n, lengths, hotspots) → list[dict]
      design_motif_graft(n) → list[dict]
      design_atsp_cyclize(n) → list[dict]
依赖：from data_layer import EvidenceLogger, CandidateIndex, State, file_hash
      ColabDesign v1.1.2 / ProteinMPNN v0.1.3
"""

import os, sys, json, time, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from data_layer import EvidenceLogger, CandidateIndex, State, file_hash
from project_config import load_project_config, target_slug
from structure_resolution import assert_target_structure_ready
from target_bootstrap import assert_project_approved


# ============================================================
# 常量 — 设计依据
# ============================================================

# 靶点→设计参数映射（来源：research.py TARGETS.pocket_residues，p53 Phe19/Trp23/Leu26 对齐）
# MDM2 1YCR chain A: Phe19→LEU54, Trp23→VAL93, Leu26→HIS96
# MDMX 3DAB chain A: Phe19→MET53, Trp23→VAL92, Leu26→PRO95
ACTIVE_PROJECT_CONFIG = load_project_config()


def _build_hotspot_map() -> dict:
    mapping = {}
    for target in ACTIVE_PROJECT_CONFIG["targets"]:
        structure = target.get("structure") or {}
        pdb_id = structure.get("pdb_id")
        spec = {
            "target_id": target["id"],
            "pdb_id": pdb_id or target["id"],
            "coordinate_path": structure.get("coordinate_path"),
            "chain": structure.get("chain", "A"),
            "hotspots": ",".join(
                str(residue) for residue in (target.get("binding_site") or {}).get("residues", [])
            ),
            "lengths": (target.get("design") or {}).get("lengths", [10]),
        }
        mapping[str(target["id"]).upper()] = spec
        if pdb_id:
            mapping[str(pdb_id).upper()] = spec
    return mapping


HOTSPOT_MAP = _build_hotspot_map()


def _require_mdm_reference_route(route_name: str):
    target_ids = {target["id"] for target in ACTIVE_PROJECT_CONFIG["targets"]}
    if target_ids != {"MDM2", "MDMX"}:
        raise RuntimeError(
            f"{route_name} contains MDM-specific motif knowledge and is disabled for "
            f"project {ACTIVE_PROJECT_CONFIG['project_id']}; provide project-specific motifs instead"
        )

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
LINKER_MATRIX = ["", "GS", "GGS", "GGGS"]

# Route C 需达到 n 时的扩展方法可用氨基酸
SCAFFOLD_MUTABLE_AA = "ACDEFGHIKLMNPQRSTVWY"

# 运行路径均可用环境变量覆盖，默认放在仓库内，避免绑定 /root。
TARGET_ROOT = Path(os.environ.get("CYCPEP_TARGET_ROOT", ROOT / "targets"))
DESIGN_ROOT = Path(os.environ.get("CYCPEP_DESIGN_ROOT", ROOT / "data" / "designs"))
COLABDESIGN_ROOT = Path(os.environ.get("COLABDESIGN_ROOT", "/root/ColabDesign"))
COLABDESIGN_PARAMS = Path(
    os.environ.get("COLABDESIGN_PARAMS", COLABDESIGN_ROOT / "params")
)

_LAST_ISSUED_CANDIDATE_NUMBER = 0


# ============================================================
# Route A: ColabDesign 靶点导向环肽设计
# ============================================================

def design_afcyc(target: str, n: int = 10,
                 lengths: list = None,
                 hotspots: str = None,
                 chain: str = None) -> list[dict]:
    """
    用 ColabDesign fixbb + cyclic offset 设计靶点导向环肽。

    每对 (length, i) 生成一个子进程：
      python <CYCPEP_DESIGN_ROOT>/route_A/<batch>/script_<cid>.py

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
    assert_project_approved(ACTIVE_PROJECT_CONFIG)
    target_key = target.upper()
    if target_key not in HOTSPOT_MAP:
        raise ValueError(f"unsupported Route A target: {target}; choose from {sorted(HOTSPOT_MAP)}")
    target_spec = HOTSPOT_MAP[target_key]
    target_id = target_spec["target_id"]
    assert_target_structure_ready(ACTIVE_PROJECT_CONFIG, target_id)
    pdb_id = target_spec["pdb_id"]
    lengths = lengths or target_spec["lengths"]
    if any(length < 8 or length > 20 for length in lengths):
        raise ValueError("v5 product definition requires cyclic peptides of 8-20 aa")
    hotspots = hotspots or target_spec.get("hotspots", "")
    chain = chain or target_spec.get("chain", "A")

    coordinate_path = target_spec.get("coordinate_path")
    if coordinate_path:
        target_pdb = Path(coordinate_path)
    else:
        safe_id = "".join(ch for ch in str(pdb_id) if ch.isalnum() or ch in {"-", "_"})
        if not safe_id:
            raise ValueError(f"invalid pdb_id for TARGET_ROOT lookup: {pdb_id}")
        target_root = TARGET_ROOT.resolve()
        target_pdb = (target_root / f"{safe_id}.pdb").resolve()
        if target_pdb.parent != target_root:
            raise ValueError(f"invalid pdb_id escaped target root: {pdb_id}")
    if not target_pdb.exists():
        raise FileNotFoundError(f"靶点 PDB 不存在: {target_pdb}")
    target_hash = file_hash(str(target_pdb))

    route_name = f"route_structure_{target_slug(target_id)}"
    batch_id = f"batch_{target_slug(target_id)}_len{'_'.join(map(str, lengths))}"
    out_dir = DESIGN_ROOT / "route_A" / batch_id
    out_dir.mkdir(parents=True, exist_ok=True)

    total_generated = 0
    total_valid = 0
    candidates = []
    t_batch_start = time.time()

    for L in lengths:
        for i in range(n):
            total_generated += 1
            cid = _next_candidate_id()
            output_path = out_dir / f"{cid}.pdb"
            script_path = out_dir / f"script_{cid}.py"

            # 写子进程脚本（保留在 out_dir，可复现）
            script = _build_design_script(
                str(target_pdb), chain, L, hotspots, str(output_path)
            )
            script_path.write_text(script, encoding="utf-8")

            t0 = time.time()
            result = subprocess.run(
                [sys.executable, str(script_path)],
                capture_output=True, text=True, timeout=600,
                cwd=str(COLABDESIGN_ROOT),
                env={**os.environ, "XLA_FLAGS": "--xla_gpu_cuda_data_dir=/usr/local/cuda-12.1"}
            )
            duration = round(time.time() - t0, 1)

            seq = ""
            valid = False
            pdb_hash = ""
            if result.returncode == 0 and output_path.exists():
                seq = _extract_sequence_from_pdb(str(output_path), binder_len=L)
                pdb_hash = file_hash(str(output_path))
                valid = len(seq) == L

            if valid:
                total_valid += 1
                candidate = {
                    "candidate_id": cid,
                    "sequence": seq,
                    "length": L,
                    "source_route": route_name,
                    "source_batch": batch_id,
                    "cyclization_type": "head_to_tail_amide",
                    "cyclization_bonds": _head_to_tail_bond(L),
                    "design_pdb_path": str(output_path),
                    "design_pdb_hash": pdb_hash,
                    "notes": (
                        f"colabdesign_fixbb; hotspots={hotspots}; "
                        "cyclic positional encoding only; covalent closure must be "
                        "checked and relaxed in Prediction"
                    ),
                }
                candidate = _register_candidate(candidate, {
                    "design_status": "backbone_generated",
                    "tool_name": "colabdesign_fixbb_cyclic_offset",
                    "target_pdb": str(target_pdb),
                    "target_pdb_hash": target_hash,
                    "target_chain": chain,
                    "hotspots": hotspots,
                    "closure_representation": "cyclic_residue_offset_not_covalent_bond",
                    "script_path": str(script_path),
                    "runtime_sec": duration,
                })
                candidates.append(candidate)
            else:
                EvidenceLogger.error(
                    agent="design",
                    error_type="afcyc_failed",
                    message=f"{cid}: seq_len={len(seq)} expected={L} exit={result.returncode}",
                    recovery="skip",
                    trace=result.stderr[:500] if result.stderr else ""
                )

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

model = mk_af_model(protocol='fixbb', data_dir={str(COLABDESIGN_PARAMS)!r})
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
    assert_project_approved(ACTIVE_PROJECT_CONFIG)
    _require_mdm_reference_route("route_B_motif_graft")
    route_name = "route_B_motif_graft"
    batch_id = f"batch_motif_graft_{int(time.time())}"
    candidates = []

    t0 = time.time()
    if not _proteinmpnn_adapter_available():
        EvidenceLogger.error(
            agent="design",
            error_type="proteinmpnn_adapter_unavailable",
            message=(
                "Expected adapter proteinmpnn.run.get_model/score_seq is unavailable; "
                "Route B was not executed"
            ),
            recovery="implement and test an adapter for the installed ProteinMPNN/LigandMPNN version",
        )
        EvidenceLogger.design_batch(
            route=route_name,
            n_generated=0,
            n_valid=0,
            tool_name="proteinmpnn_motif_graft",
            tool_version="adapter_unavailable",
            duration_sec=round(time.time() - t0, 1),
        )
        return []

    templates = _motif_templates_from_state()
    base = n // len(templates)
    remainder = n % len(templates)
    quotas = [base + 1 if idx < remainder else base for idx in range(len(templates))]

    for idx, tmpl in enumerate(templates):
        for i in range(quotas[idx]):
            if len(candidates) >= n:
                break

            optimized_seq = _proteinmpnn_optimize(tmpl["seq"])
            valid = optimized_seq is not None and _validate_sequence(optimized_seq)
            if not valid:
                EvidenceLogger.error(
                    agent="design",
                    error_type="proteinmpnn_no_valid_output",
                    message=(
                        f"template={tmpl['name']}: ProteinMPNN adapter did not return "
                        "a valid sequence; no candidate was registered"
                    ),
                    recovery="install/configure a verified ProteinMPNN adapter or skip Route B",
                )
                continue

            cid = _next_candidate_id()
            candidate = {
                "candidate_id": cid,
                "sequence": optimized_seq,
                "length": len(optimized_seq),
                "source_route": route_name,
                "source_batch": batch_id,
                "cyclization_type": "head_to_tail_amide",
                "cyclization_bonds": _head_to_tail_bond(len(optimized_seq)),
                "notes": f"template={tmpl['name']}; source={tmpl.get('source', 'unknown')}",
            }
            candidate = _register_candidate(candidate, {
                "design_status": "sequence_proposal",
                "tool_name": "proteinmpnn_motif_graft",
                "template": tmpl,
                "structure_required_before_prediction": True,
            })
            candidates.append(candidate)

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

    return None


def _proteinmpnn_adapter_available() -> bool:
    try:
        from proteinmpnn.run import get_model, score_seq
        return callable(get_model) and callable(score_seq)
    except (ImportError, AttributeError):
        return False


# ============================================================
# Route C: ATSP-7041 环化改造
# ============================================================

def design_atsp_cyclize(n: int = 200) -> list[dict]:
    """
    基于 ATSP-7041 scaffold 做环化改造。

    生成策略：
      1. ATSP 核心与 0/2/3/4 aa Gly/Ser linker 组合；
      2. 保留 F/W/L 锚点，对其余位点做确定性的单点枚举；
      3. 所有候选统一声明为首尾酰胺键闭环意图，交由 Prediction 构建和
         检查真实闭环几何。

    Args:
        n: 生成候选总数

    Returns:
        list[dict]: 候选列表
    """
    assert_project_approved(ACTIVE_PROJECT_CONFIG)
    _require_mdm_reference_route("route_C_atsp")
    route_name = "route_C_atsp"
    batch_id = f"batch_atsp_{int(time.time())}"
    candidates = []

    t0 = time.time()

    if n < 1:
        return []

    base_combos = []
    for linker in LINKER_MATRIX:
        seq = f"{ATSP_CORE}{linker}"
        base_combos.append((seq, f"linker={linker or 'none'}"))

    expanded = list(base_combos)
    seen = {sequence for sequence, _ in expanded}
    anchor_positions = {2, 6, 11}  # ATSP_CORE 中的 F/W/L 三锚点，0-based
    mutable_positions = [
        pos for pos in range(len(ATSP_CORE)) if pos not in anchor_positions
    ]
    for base_seq, desc in base_combos:
        for pos in mutable_positions:
            for new_aa in SCAFFOLD_MUTABLE_AA:
                if new_aa == base_seq[pos]:
                    continue
                mutated = base_seq[:pos] + new_aa + base_seq[pos + 1:]
                if mutated not in seen:
                    expanded.append((mutated, f"{desc}; mut:{pos + 1}={new_aa}"))
                    seen.add(mutated)

    if n > len(expanded):
        EvidenceLogger.error(
            "design",
            "route_c_requested_too_many",
            f"requested={n}, deterministic unique library={len(expanded)}",
            recovery=f"registered the available {len(expanded)} unique candidates",
        )

    for seq, desc in expanded[:n]:
        valid = _validate_sequence(seq)
        if not valid:
            EvidenceLogger.error(
                agent="design",
                error_type="sequence_invalid",
                message=f"Route C sequence validation failed: {seq}",
                recovery="skip",
            )
            continue
        cid = _next_candidate_id()
        candidate = {
            "candidate_id": cid,
            "sequence": seq,
            "length": len(seq),
            "source_route": route_name,
            "source_batch": batch_id,
            "cyclization_type": "head_to_tail_amide",
            "cyclization_bonds": _head_to_tail_bond(len(seq)),
            "notes": f"{desc}; ATSP-derived sequence proposal",
        }
        candidate = _register_candidate(candidate, {
            "design_status": "sequence_proposal",
            "tool_name": "deterministic_template_enumeration",
            "parent_scaffold": "ATSP-7041-inspired core",
            "parent_reference": "PMID:23946421",
            "structure_required_before_prediction": True,
        })
        candidates.append(candidate)

    duration = round(time.time() - t0, 1)

    EvidenceLogger.design_batch(
        route=route_name,
        n_generated=min(n, len(expanded)),
        n_valid=len(candidates),
        tool_name="deterministic_template_enumeration",
        tool_version="v2_head_to_tail",
        duration_sec=duration
    )
    return candidates


# ============================================================
# 共享工具函数
# ============================================================

def _validate_sequence(seq: str) -> bool:
    """
    序列合法性检查。
    条件：长度 8-20，仅含标准 20 种氨基酸单字母代码。
    """
    valid_aas = set("ACDEFGHIKLMNPQRSTVWY")
    seq_clean = seq.upper().replace("-", "").replace("*", "")
    return (8 <= len(seq_clean) <= 20 and
            all(c in valid_aas for c in seq_clean))


def _motif_templates_from_state() -> list[dict]:
    """优先读取 Research 已写入 State 的真实序列，常量只作有标记的 fallback。"""
    templates = []
    seen = set()
    for binder in State.load().get("known_dual_binders", []):
        sequence = str(binder.get("sequence") or "").upper()
        if _validate_sequence(sequence) and sequence not in seen:
            templates.append({
                "name": binder.get("name") or "research_binder",
                "seq": sequence,
                "source": f"research_state PMID:{binder.get('pmid', 'unknown')}",
            })
            seen.add(sequence)
    if templates:
        return templates
    return [
        {**template, "source": "curated_fallback; run Research before production design"}
        for template in MOTIF_TEMPLATES
    ]


def _head_to_tail_bond(length: int) -> list[dict]:
    return [{
        "atom_1": "residue_1:N",
        "atom_2": f"residue_{length}:C",
        "bond_type": "amide",
    }]


def _register_candidate(candidate: dict, manifest_details: dict) -> dict:
    """先写结构化 manifest，再把同一路径交给共享索引和证据日志。"""
    if not _validate_sequence(candidate.get("sequence", "")):
        raise ValueError(f"invalid candidate sequence: {candidate.get('candidate_id')}")

    manifest_dir = DESIGN_ROOT / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / f"{candidate['candidate_id']}.json"
    manifest = {
        "schema_version": "1.0",
        "created_at": datetime_now_utc(),
        "candidate": candidate,
        "design_details": manifest_details,
    }
    temp_path = manifest_path.with_name(f".{manifest_path.name}.{os.getpid()}.tmp")
    temp_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp_path, manifest_path)

    registered = dict(candidate)
    registered["manifest_path"] = str(manifest_path)
    CandidateIndex.add(registered)
    EvidenceLogger.candidate_registered(registered)
    return registered


def datetime_now_utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


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
    global _LAST_ISSUED_CANDIDATE_NUMBER
    count = int(State.load().get("candidate_count", 0))
    existing_numbers = []
    for row in CandidateIndex.load():
        candidate_id = row.get("candidate_id", "")
        if candidate_id.startswith("C") and candidate_id[1:].isdigit():
            existing_numbers.append(int(candidate_id[1:]))
    next_number = max(
        [count, _LAST_ISSUED_CANDIDATE_NUMBER, *existing_numbers],
        default=0,
    ) + 1
    _LAST_ISSUED_CANDIDATE_NUMBER = next_number
    return f"C{next_number:04d}"


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
