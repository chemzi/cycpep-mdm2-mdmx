"""Shared data-layer schema constants and legacy state helpers.

Split from data_layer.py (PR8) so the core module stays under the
architecture-gate file-size limit. data_layer re-exports every name defined
here, so ``from data_layer import INDEX_COLUMNS`` keeps working.
"""

import json
import math
import os
import tempfile
from pathlib import Path

from project_config import required_target_ids, target_slug


LEGACY_DEFAULT_DESIGN_BUDGET = {
    "route_A_mdm2": 400,
    "route_A_mdmx": 400,
    "route_B": 400,
    "route_C": 200,
}


def default_design_budget(config: dict) -> dict[str, int]:
    """Build route capacity keys from the approved target identities."""
    budget = {
        f"route_A_{target_slug(target_id)}": 400
        for target_id in required_target_ids(config)
    }
    budget.update({"route_B": 400, "route_C": 200})
    return budget


def default_state(config: dict) -> dict:
    """Default State projection for the given project config (PR5, lazy)."""
    return {
        "project": config.get("name", config["project_id"]),
        "project_id": config["project_id"],
        "project_config": config,
        "targets": {
            target["id"]: {key: value for key, value in target.items() if key != "id"}
            for target in config["targets"]
        },
        "phase": "research",
        "round": 1,
        "pocket_differences": {},
        "known_dual_binders": [],
        "design_budget": default_design_budget(config),
        "candidate_count": 0,
        "iteration_history": [],
        "thresholds": {},
    }


def _write_json_atomic(path: str | Path, payload: dict):
    """Write JSON beside its destination and atomically replace the old file."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        os.replace(temp_name, destination)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def to_float(value):
    """Coerce a value to float; return None when it is empty or non-finite.

    Shared by the battery evaluator and CandidateIndex (PR8).  Kept public so
    sibling modules can reuse it without importing a private name.  NaN/Inf
    are rejected so they can never flow into ranking or Pareto logic.
    """
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number

# v5: 七层指标电池主列。旧列名保留做 alias（见 _ALIAS_MAP），不破坏已有代码。
INDEX_COLUMNS = [
    # --- 基础标识 ---
    "candidate_id","sequence","length","source_route","source_batch",
    # --- 化学与结构交接契约 ---
    "cyclization_type","cyclization_bonds","design_pdb_path","design_pdb_hash",
    "manifest_path",
    # --- v6 通用指标载荷（任意靶点；旧 MDM2/MDMX 列继续用于表格展示）---
    "metrics_json",
    # --- L1 环肽质量 ---
    "plddt","l1_pass",
    # --- L2 界面置信度（ipSAE 主, ipTM 仅做参考, 不卡门槛）---
    "ipsae_mdm2","ipsae_mdmx","ipae_mdm2","iptm_mdm2","iptm_mdmx","l2_pass",
    # --- L3 界面物理 ---
    "dg_mdm2","dg_mdmx","dg_method","sc_mdm2","sc_mdmx","dsasa_mdm2","dsasa_mdmx","l3_pass",
    # --- L4 环化几何 QC（relax 前后各一次）---
    "nc_distance_pre","nc_distance_post","ring_closure_pre","ring_closure_post","l4_pass",
    # --- L5 设计意图 ---
    "hotspot_cov_mdm2","hotspot_cov_mdmx","site_consistency_mdm2",
    "site_consistency_mdmx","site_consistency","l5_pass",
    # --- L6 鲁棒性（多预测器/多 seed 收敛）---
    "pose_rmsd_mdm2","pose_rmsd_mdmx","seed_convergence_mdm2",
    "seed_convergence_mdmx","pose_rmsd","seed_convergence",
    "colab_iptm_mdm2","colab_iptm_mdmx","l6_pass",
    # --- L7 可设计性（scRMSD）---
    "scrmsd","l7_pass",
    # --- 综合判定 ---
    "all_layers_pass","metric_clearance","competition_clearance",
    "triage_status","threshold_audit_json","pareto_front",
    # --- 可合成性（Critic 检查）---
    "synth_pass",
    # --- ADME bonus ---
    "adme_net_charge","adme_tpsa","adme_clogp","adme_chameleonicity","novelty_score",
    # --- 双靶参考（导师禁止做加权门槛但保留做汇报）---
    "asymmetry","dual_score",
    # --- 状态/产出 ---
    "final_status","final_rank","notes","last_updated",
    # --- v4/早期原型字段，只保留历史含义，不参与 v5 判定 ---
    "legacy_self_rmsd","legacy_haddock_mdm2","legacy_haddock_mdmx",
    "legacy_layer1_pass","legacy_layer2_3_pass","legacy_layer4_pass",
    "legacy_cross_tool_ok",
]

# 旧名 → 兼容列。只有 monomer_plddt 与当前 pLDDT 含义一致；
# 其余旧指标保存在 legacy_*，不能直接参与新版七层判定。
_ALIAS_MAP = {
    "monomer_plddt": "plddt",
    "self_rmsd": "legacy_self_rmsd",
    "haddock_mdm2": "legacy_haddock_mdm2",
    "haddock_mdmx": "legacy_haddock_mdmx",
    "layer1_pass": "legacy_layer1_pass",
    "layer2_3_pass": "legacy_layer2_3_pass",
    "layer4_pass": "legacy_layer4_pass",
    "cross_tool_ok": "legacy_cross_tool_ok",
}


def alias_keys(row: dict) -> dict:
    """旧字段名 → 新字段名（向前兼容旧 Agent 代码）。"""
    for old, new in _ALIAS_MAP.items():
        if old in row and row.get(old) not in (None, "") and row.get(new) in (None, ""):
            row[new] = row.pop(old)
    return row
