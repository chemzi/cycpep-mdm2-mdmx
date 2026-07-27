"""
Prediction Agent — 编排骨架版（刘函赫代做；真实模型接入待王修远）
职责：候选数据 -> 七层字段 -> evaluate_battery() -> 每层 pass/fail -> 写回 CandidateIndex

组长的分工（本版严格遵守）：
  - 只写"判定层"编排：给每个候选装配七层字段，调 data_layer.evaluate_battery()
    拿七层 pass/fail，再把结论写回 CandidateIndex。
  - 不写真实模型计算层。真实分数暂时从 PLACEHOLDER_SCORES 取；等 AfCycDesign /
    ColabFold / PRODIGY / Rosetta 跑出来后，只替换分数来源，编排逻辑不动。

对接 data_layer v5（已对齐 origin/chemzi/dev 最新版 evaluate_battery 签名）：
  - evaluate_battery(c, thresholds, required_targets=("MDM2","MDMX")) 由组长在
    data_layer 提供，本文件直接调用。返回
    {l1_pass..l7_pass, all_layers_pass, failed_layers, required_targets,
     target_pass, layer_values}。
  - ⚠️ 双靶判定（默认 required_targets 覆盖 MDM2+MDMX）：L2/L3/L5/L6 会对每个靶标
    分别读 *_mdm2 / *_mdmx 字段。因此本文件必须为每条候选装配 per-target 字段
    （site_consistency_mdm2/mdmx、pose_rmsd_mdm2/mdmx、seed_convergence_mdm2/mdmx 等），
    只给通用字段会导致双靶候选 L5/L6 恒 False。
  - 阈值来自 state.json["thresholds"]（Research Agent 文献标定后 sync 进来）。
    L1/L2 等用 th_has() 硬门控——缺阈值时恒 False；故 demo 无阈值时用
    DEMO_THRESHOLDS 兜底（醒目标注 placeholder），保证端到端流程可演示。

数据流：
  候选(CandidateIndex) → _build_candidate_fields() → evaluate_battery(fields, thresholds)
                        → 七层 pass/fail → 写回 CandidateIndex + 证据日志

依赖：from data_layer import State, EvidenceLogger, CandidateIndex, evaluate_battery
"""
from __future__ import annotations

import os
import sys
import re
import hashlib
import argparse
from datetime import datetime, timezone

# --- 让 agents/ 下的脚本能 import 仓库根目录的 data_layer ---
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import data_layer  # noqa: E402
from data_layer import State, EvidenceLogger, CandidateIndex  # noqa: E402

CID_RE = re.compile(r"^C\d{4}$")
N_LAYERS = 7

# 七层元数据：(层号, pass 字段名, 中文名, 该层用于日志展示的指标字段)
_LAYER_META = [
    (1, "l1_pass", "环肽质量(pLDDT)",        ["plddt"]),
    (2, "l2_pass", "界面置信度(ipSAE)",      ["ipsae_mdm2", "ipsae_mdmx", "iptm_mdm2"]),
    (3, "l3_pass", "界面物理(dG/SC/dSASA)",  ["dg_mdm2", "sc_mdm2", "dsasa_mdm2"]),
    (4, "l4_pass", "环化几何QC",             ["nc_distance_pre", "nc_distance_post"]),
    (5, "l5_pass", "设计意图(热点/位点)",     ["hotspot_cov_mdm2", "site_consistency_mdm2"]),
    (6, "l6_pass", "鲁棒性(pose/seed)",      ["pose_rmsd_mdm2", "seed_convergence_mdm2"]),
    (7, "l7_pass", "可设计性(scRMSD)",       ["scrmsd"]),
]
_PASS_ORDER = [m[1] for m in _LAYER_META]

# 每层用于淘汰记录的主指标字段 + 对应 thresholds 键（L4 用 N-C 数值阈值）
_PRIMARY = {
    1: ("plddt", "L1_plddt"),
    2: ("ipsae_mdm2", "L2_ipsae"),
    3: ("dg_mdm2", "L3_dg"),
    4: ("nc_distance_post", "L4_nc_term_dist"),
    5: ("hotspot_cov_mdm2", "L5_hotspot_coverage"),
    6: ("pose_rmsd_mdm2", "L6_pose_rmsd"),
    7: ("scrmsd", "L7_scrmsd"),
}
# 每层在 evaluate_layer_start 日志里展示的 thresholds 键
_LAYER_TH_KEYS = {
    1: ["L1_plddt"], 2: ["L2_ipsae"], 3: ["L3_dg", "L3_sc", "L3_dsasa"],
    4: ["L4_nc_term_dist"], 5: ["L5_hotspot_coverage"], 6: ["L6_pose_rmsd"], 7: ["L7_scrmsd"],
}
# 每层对应的（占位）工具名——取自 evidence_schema.json 的 tool_name 枚举
_LAYER_TOOL = {
    1: "afcycdesign_monomer", 2: "afcycdesign_complex", 3: "prodigy",
    4: "biotite", 5: "biotite", 6: "colabfold", 7: "afcycdesign_monomer",
}

# 候选七层指标字段全集（写回 CandidateIndex 时用到；含双靶 per-target 字段）
FIELD_KEYS = [
    # L1
    "plddt",
    # L2 界面置信度（ipSAE 主判，ipTM 仅参考）
    "ipsae_mdm2", "ipsae_mdmx", "ipae_mdm2", "iptm_mdm2", "iptm_mdmx",
    # L3 界面物理（分靶标 + dg_method 一致性）
    "dg_mdm2", "dg_mdmx", "dg_method", "sc_mdm2", "sc_mdmx", "dsasa_mdm2", "dsasa_mdmx",
    # L4 环化几何（relax 前后各一次的 N-C 距离；布尔仅作显示）
    "nc_distance_pre", "nc_distance_post", "ring_closure_pre", "ring_closure_post",
    # L5 设计意图（分靶标热点覆盖 + 位点一致）
    "hotspot_cov_mdm2", "hotspot_cov_mdmx",
    "site_consistency_mdm2", "site_consistency_mdmx", "site_consistency",
    # L6 鲁棒性（分靶标 pose/seed 收敛 + colabfold 交叉验证）
    "pose_rmsd_mdm2", "pose_rmsd_mdmx",
    "seed_convergence_mdm2", "seed_convergence_mdmx",
    "pose_rmsd", "seed_convergence", "colab_iptm_mdm2", "colab_iptm_mdmx",
    # L7 可设计性
    "scrmsd",
]

# per-target 字段：装配时若只给了通用值，用它兜底填充到 *_mdm2 / *_mdmx
_PER_TARGET_BASE = ["site_consistency", "pose_rmsd", "seed_convergence"]


# ============================================================
# PLACEHOLDER：演示用阈值 —— 仅在 state.json 无 thresholds 时兜底
# ============================================================
# === PLACEHOLDER：真实运行时阈值来自 state.json["thresholds"]（Research Agent 文献标定后 sync）。===
# 此处仅为让 demo 可演示（否则 L1/L2 因 th_has 门控恒 False）。数值取 evaluate_battery 的
# 内置默认，operator 对齐。上线后删除本兜底、改由 state 提供。
DEMO_THRESHOLDS = {
    "L1_plddt":            {"value": 0.80, "operator": ">",  "evidence_grade": "demo_placeholder"},
    "L2_ipsae":            {"value": 0.50, "operator": ">",  "evidence_grade": "demo_placeholder"},
    "L3_dg":               {"value": -10,  "operator": "<",  "evidence_grade": "demo_placeholder"},
    "L3_sc":               {"value": 0.60, "operator": ">",  "evidence_grade": "demo_placeholder"},
    "L3_dsasa":            {"value": 400,  "operator": ">",  "evidence_grade": "demo_placeholder"},
    "L4_nc_term_dist":     {"value": 2.0,  "operator": "<",  "evidence_grade": "demo_placeholder"},
    "L5_hotspot_coverage": {"value": 0.67, "operator": ">=", "evidence_grade": "demo_placeholder"},
    "L6_pose_rmsd":        {"value": 2.0,  "operator": "<",  "min_seed_fraction": 0.67,
                            "evidence_grade": "demo_placeholder"},
    "L7_scrmsd":           {"value": 2.0,  "operator": "<",  "evidence_grade": "demo_placeholder"},
}


# ============================================================
# PLACEHOLDER：占位模型输出 —— 唯一的"未接真实工具"注入点
# ============================================================
# === PLACEHOLDER：未来替换为 AfCycDesign / ColabFold / PRODIGY / Rosetta 真实工具输出 ===
# 到时把下面的取值改成从工具输出文件里读即可，run() 及以下编排逻辑均无需改动。
# 三条候选覆盖三种结局（对 DEMO_THRESHOLDS）：全清 / L2 界面挂 / L1 折叠挂。
PLACEHOLDER_SCORES = {
    # C0001：七层全清 → finalized
    "C0001": {
        "plddt": 0.88,
        "ipsae_mdm2": 0.62, "ipsae_mdmx": 0.58, "ipae_mdm2": 8.5,
        "iptm_mdm2": 0.80, "iptm_mdmx": 0.74,
        "dg_mdm2": -42.0, "dg_mdmx": -38.0, "dg_method": "prodigy",
        "sc_mdm2": 0.65, "sc_mdmx": 0.61, "dsasa_mdm2": 650, "dsasa_mdmx": 600,
        "nc_distance_pre": 1.4, "nc_distance_post": 1.5,
        "ring_closure_pre": "true", "ring_closure_post": "true",
        "hotspot_cov_mdm2": 0.75, "hotspot_cov_mdmx": 0.70,
        "site_consistency_mdm2": "true", "site_consistency_mdmx": "true",
        "pose_rmsd_mdm2": 1.2, "pose_rmsd_mdmx": 1.4,
        "seed_convergence_mdm2": 0.80, "seed_convergence_mdmx": 0.75,
        "colab_iptm_mdm2": 0.78, "colab_iptm_mdmx": 0.72, "scrmsd": 1.5,
    },
    # C0002：L2 ipSAE 不达标 → 中层淘汰。iptm_mdm2=0.71 虚高但 ipSAE 低——导师 Trap1 的典型
    "C0002": {
        "plddt": 0.85,
        "ipsae_mdm2": 0.42, "ipsae_mdmx": 0.40, "ipae_mdm2": 12.0,
        "iptm_mdm2": 0.71, "iptm_mdmx": 0.68,
        "dg_mdm2": -38.0, "dg_mdmx": -34.0, "dg_method": "prodigy",
        "sc_mdm2": 0.62, "sc_mdmx": 0.60, "dsasa_mdm2": 600, "dsasa_mdmx": 560,
        "nc_distance_pre": 1.5, "nc_distance_post": 1.6,
        "ring_closure_pre": "true", "ring_closure_post": "true",
        "hotspot_cov_mdm2": 0.70, "hotspot_cov_mdmx": 0.66,
        "site_consistency_mdm2": "true", "site_consistency_mdmx": "true",
        "pose_rmsd_mdm2": 1.4, "pose_rmsd_mdmx": 1.5,
        "seed_convergence_mdm2": 0.75, "seed_convergence_mdmx": 0.72,
        "colab_iptm_mdm2": 0.70, "colab_iptm_mdmx": 0.66, "scrmsd": 1.6,
    },
    # C0003：L1 pLDDT 太低 → 早层淘汰
    "C0003": {
        "plddt": 0.58,
        "ipsae_mdm2": 0.60, "ipsae_mdmx": 0.55, "ipae_mdm2": 9.0,
        "iptm_mdm2": 0.75, "iptm_mdmx": 0.70,
        "dg_mdm2": -35.0, "dg_mdmx": -31.0, "dg_method": "prodigy",
        "sc_mdm2": 0.61, "sc_mdmx": 0.58, "dsasa_mdm2": 500, "dsasa_mdmx": 470,
        "nc_distance_pre": 1.6, "nc_distance_post": 1.7,
        "ring_closure_pre": "true", "ring_closure_post": "true",
        "hotspot_cov_mdm2": 0.70, "hotspot_cov_mdmx": 0.65,
        "site_consistency_mdm2": "true", "site_consistency_mdmx": "true",
        "pose_rmsd_mdm2": 1.3, "pose_rmsd_mdmx": 1.4,
        "seed_convergence_mdm2": 0.70, "seed_convergence_mdmx": 0.68,
        "colab_iptm_mdm2": 0.72, "colab_iptm_mdmx": 0.68, "scrmsd": 1.7,
    },
}

# === PLACEHOLDER：演示候选。真实运行时由 Design Agent 写入 CandidateIndex，本表仅在索引为空时播种 ===
DEMO_CANDIDATES = [
    {"candidate_id": "C0001", "sequence": "GFEWALLAQK",
     "source_route": "route_A_mdm2_first", "source_batch": "demo_batch_1"},
    {"candidate_id": "C0002", "sequence": "PFNWALGGSR",
     "source_route": "route_A_mdmx_first", "source_batch": "demo_batch_1"},
    {"candidate_id": "C0003", "sequence": "AFEWKLLSPQ",
     "source_route": "route_B_motif_graft", "source_batch": "demo_batch_1"},
]


# ============================================================
# 字段装配 & 辅助
# ============================================================
def _f(v):
    """尝试转 float，失败返回 None。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fallback_fields(cid: str) -> dict:
    """未在 PLACEHOLDER_SCORES 中预设的候选（如索引里已存在的历史行）用确定性伪值兜底，
    保证 demo 不因未知候选崩溃。数值由 candidate_id 派生，可复现。
    ⚠️ 这些不是真实分数，仅为编排骨架演示——真实工具接入后本函数应删除。"""
    h = int(hashlib.md5(cid.encode()).hexdigest(), 16)
    r = (h % 1000) / 1000.0  # 0..1，确定性
    return {
        "plddt": 0.80 + 0.15 * r,
        "ipsae_mdm2": 0.50 + 0.20 * r, "ipsae_mdmx": 0.45 + 0.20 * r, "ipae_mdm2": 8.0 + 4 * r,
        "iptm_mdm2": 0.70 + 0.15 * r, "iptm_mdmx": 0.65 + 0.15 * r,
        "dg_mdm2": -30.0 - 15 * r, "dg_mdmx": -26.0 - 15 * r, "dg_method": "prodigy",
        "sc_mdm2": 0.60 + 0.08 * r, "sc_mdmx": 0.58 + 0.08 * r,
        "dsasa_mdm2": 450 + 250 * r, "dsasa_mdmx": 420 + 250 * r,
        "nc_distance_pre": 1.3 + 0.4 * r, "nc_distance_post": 1.4 + 0.4 * r,
        "ring_closure_pre": "true", "ring_closure_post": "true",
        "hotspot_cov_mdm2": 0.67 + 0.2 * r, "hotspot_cov_mdmx": 0.60 + 0.2 * r,
        "site_consistency_mdm2": "true", "site_consistency_mdmx": "true",
        "pose_rmsd_mdm2": 1.0 + r, "pose_rmsd_mdmx": 1.1 + r,
        "seed_convergence_mdm2": 0.67 + 0.2 * r, "seed_convergence_mdmx": 0.67 + 0.2 * r,
        "colab_iptm_mdm2": 0.70 + 0.1 * r, "colab_iptm_mdmx": 0.65 + 0.1 * r,
        "scrmsd": 1.0 + r,
    }


def _build_candidate_fields(cand: dict) -> dict:
    """为一条候选装配七层字段（含 candidate_id）。含入参校验（生产环境健壮性）。"""
    cid = (cand.get("candidate_id") or "").strip()
    if not CID_RE.match(cid):
        raise ValueError(f"非法 candidate_id: {cid!r}（须匹配 C\\d{{4}}）")
    seq = (cand.get("sequence") or "").strip()
    if not seq:
        raise ValueError(f"{cid} 序列为空")
    if not (6 <= len(seq) <= 20):
        raise ValueError(f"{cid} 序列长度 {len(seq)} 越界（应为 6-20）")

    src = PLACEHOLDER_SCORES.get(cid) or _fallback_fields(cid)
    fields = {k: src.get(k) for k in FIELD_KEYS}
    # 双靶兜底：evaluate_battery 默认对 MDM2+MDMX 分别校验 L5/L6 的 per-target 字段。
    # 若来源只给了通用值，则填充到 *_mdm2 / *_mdmx（真实工具接入后应分靶标各自算）。
    for base in _PER_TARGET_BASE:
        generic = src.get(base)
        for suffix in ("mdm2", "mdmx"):
            key = f"{base}_{suffix}"
            if fields.get(key) in (None, "") and generic not in (None, ""):
                fields[key] = generic
    fields["candidate_id"] = cid
    return fields


def _first_fail(res: dict):
    """按 L1..L7 顺序找到第一处未通过的层号；全过返回 None（用于重建漏斗视图）。"""
    for i, pass_key in enumerate(_PASS_ORDER, start=1):
        if not res.get(pass_key):
            return i
    return None


def _placeholder_tool_trace(layer: int, cid: str) -> dict:
    """构造一条占位 tool_trace（字段对齐 evidence_schema.json）。"""
    return {
        "tool_name": _LAYER_TOOL.get(layer, "afcycdesign_complex"),
        "tool_version": "placeholder-0.0",
        "input_params": {"candidate_id": cid, "layer": layer},
        "exit_code": 0,
        "duration_sec": 0.0,
        "stdout_snippet": "PLACEHOLDER: 占位分数，未接真实模型计算",
    }


# ============================================================
# 写回 CandidateIndex
# ============================================================
def _writeback(cid: str, fields: dict, res: dict) -> None:
    """把七层指标 + 每层 pass/fail 写回成绩单（v5 表头已含全部列）。"""
    row = {k: v for k, v in fields.items() if k in FIELD_KEYS and v is not None}
    for pass_key in _PASS_ORDER:
        row[pass_key] = str(bool(res.get(pass_key)))
    row["all_layers_pass"] = str(bool(res.get("all_layers_pass")))
    CandidateIndex.update_score(cid, row)

    if res.get("all_layers_pass"):
        CandidateIndex.update_status(cid, "finalized", notes="七层全清 (7/7) [PLACEHOLDER 分数]")
    else:
        ff = _first_fail(res)
        name = _LAYER_META[ff - 1][2] if ff else "?"
        failed = res.get("failed_layers", [])
        CandidateIndex.update_status(
            cid, "eliminated",
            notes=f"failed@L{ff}:{name}; failed_layers={failed} [PLACEHOLDER 分数]",
        )


# ============================================================
# 主入口
# ============================================================
def run(state: dict = None) -> dict:
    """端到端：读候选 → 逐条 evaluate_battery → 漏斗日志 → 写回 → 更新 state。"""
    if not hasattr(data_layer, "evaluate_battery"):
        raise RuntimeError(
            "data_layer 未提供 evaluate_battery()——请更新到 v5 版 data_layer.py")

    s = state if state is not None else State.load()
    state_th = s.get("thresholds") or {}
    using_demo_th = not bool(state_th)
    thresholds = state_th or DEMO_THRESHOLDS

    # 1) 读候选；索引为空则播种 demo 候选（数据真实流经 data_layer）
    candidates = CandidateIndex.load()
    if not candidates:
        CandidateIndex.add_batch([dict(c) for c in DEMO_CANDIDATES])
        candidates = CandidateIndex.load()

    # 2) 逐条装配字段 + 调 evaluate_battery（默认双靶 MDM2+MDMX；单条出错不影响整批）
    results = {}  # cid -> (fields, battery_result)
    for cand in candidates:
        cid = (cand.get("candidate_id") or "").strip()
        try:
            fields = _build_candidate_fields(cand)
            results[cid] = (fields, data_layer.evaluate_battery(fields, thresholds))
        except Exception as e:  # 健壮性：记错并跳过该候选
            EvidenceLogger.error(
                "prediction", "battery_error", f"候选 {cid or '?'} 评估失败: {e}",
                recovery="跳过该候选，继续评估其余",
            )

    # 3) 按层重建漏斗日志（真 evaluate_battery 独立评估各层；此处按顺序模拟逐层筛）
    for layer in range(1, N_LAYERS + 1):
        alive = [cid for cid, (_, r) in results.items()
                 if (_first_fail(r) is None or _first_fail(r) >= layer)]
        if not alive:
            continue
        th_subset = {k: thresholds.get(k) for k in _LAYER_TH_KEYS[layer] if k in thresholds}
        EvidenceLogger.evaluate_layer_start(layer, len(alive), th_subset)

        pass_key = _PASS_ORDER[layer - 1]
        metric_field, th_key = _PRIMARY[layer]
        n_pass = n_fail = 0
        for cid in alive:
            fields, result = results[cid]
            passed = bool(result.get(pass_key))
            scores = {m: fields.get(m) for m in _LAYER_META[layer - 1][3]}
            EvidenceLogger.candidate_scored(
                cid, layer, scores, _placeholder_tool_trace(layer, cid), passed)
            if passed:
                n_pass += 1
            else:
                n_fail += 1
                val = _f(fields.get(metric_field))
                thr = thresholds.get(th_key, {}).get("value") if th_key else None
                EvidenceLogger.candidate_eliminated(
                    cid, layer,
                    reason=f"{_LAYER_META[layer - 1][2]}:{metric_field} 未达标",
                    score=float(val) if val is not None else 0.0,
                    threshold=float(thr) if thr is not None else 0.0,
                )
        EvidenceLogger.evaluate_layer_complete(layer, len(alive), n_pass, n_fail)

    # 4) 写回成绩单
    for cid, (fields, result) in results.items():
        try:
            _writeback(cid, fields, result)
        except Exception as e:
            EvidenceLogger.error("prediction", "writeback_error",
                                 f"候选 {cid} 写回失败: {e}", recovery="跳过该候选写回")

    # 5) 更新全局状态
    finalized = sorted(cid for cid, (_, r) in results.items() if r.get("all_layers_pass"))
    eliminated = sorted(cid for cid, (_, r) in results.items() if not r.get("all_layers_pass"))
    summary = {
        "evaluated": len(results),
        "finalized": finalized,
        "eliminated": eliminated,
        "using_demo_thresholds": using_demo_th,
    }
    State.update({"phase": "evaluate", "candidate_count": len(candidates)})
    State.append_history({
        "phase": "evaluate", "agent": "prediction",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "note": "PLACEHOLDER 预测（编排骨架版）；阈值来源="
                + ("DEMO_THRESHOLDS 兜底" if using_demo_th else "state.json[thresholds]"),
    })
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prediction Agent 编排骨架版 demo")
    parser.parse_args()

    print("=" * 60)
    print("Prediction Agent（编排骨架 / PLACEHOLDER）demo —— 对接 data_layer v5 七层电池")
    print("=" * 60)
    result = run()
    th_src = "DEMO_THRESHOLDS 兜底（state 无 thresholds）" \
        if result["using_demo_thresholds"] else "state.json[thresholds]"
    print(f"\n阈值来源 : {th_src}")
    print(f"共评估   : {result['evaluated']} 条候选")
    print(f"✅ 七层全清: {result['finalized']}")
    print(f"❌ 淘汰   : {result['eliminated']}")
    print("\n成绩单统计:", CandidateIndex.stats())
    print("证据日志条目数:", len(EvidenceLogger.get_all()))
    print("\ndemo 完成 —— 候选→七层字段→evaluate_battery→pass/fail→写回 已打通")
