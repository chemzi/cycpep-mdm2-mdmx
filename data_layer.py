"""
环肽Agent证据日志 & 候选索引表 - 共享数据层
所有Agent通过此模块读写 state.json、evidence_log.jsonl、candidate_index.csv

使用方式:
    from data_layer import State, EvidenceLogger, CandidateIndex, evaluate_battery

    # 读全局状态
    state = State.load()

    # 记一条日志
    EvidenceLogger.log(agent="design", event_type="design_batch",
                       payload={"route": "route_A", "n_generated": 200})

    # 加一条候选到索引表（旧字段名 monomer_plddt/layer1_pass 等会自动 alias 到新列）
    CandidateIndex.add({"candidate_id": "C0001", "sequence": "GFEWALA...", ...})

    # 更新候选分数
    CandidateIndex.update_score("C0001", {"ipsae_mdm2": 0.72, "iptm_mdm2": 0.84})  # ipTM 仅做参考

    # 七层指标电池全清判定（v5 主判定函数）
    result = evaluate_battery(candidate_dict, thresholds=State.load().get("thresholds"))
    if result["all_layers_pass"]:
        # 准入下一阶段
        ...

    # 把 _thresholds_cache.json 归一化后合并回 state.json（修复丢失 thresholds）
    State.sync_thresholds_from_cache()
"""
import json, csv, hashlib, os, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CYCPEP_DATA_DIR", ROOT / "data"))
EVIDENCE_DIR = Path(os.environ.get("CYCPEP_EVIDENCE_DIR", ROOT / "evidence"))
STATE_PATH = DATA_DIR / "state.json"
LOG_PATH   = EVIDENCE_DIR / "evidence_log.jsonl"
INDEX_PATH = DATA_DIR / "candidate_index.csv"
THRESHOLDS_CACHE = DATA_DIR / "_thresholds_cache.json"

# v5: 七层指标电池主列。旧列名保留做 alias（见 _ALIAS_MAP），不破坏已有代码。
INDEX_COLUMNS = [
    # --- 基础标识 ---
    "candidate_id","sequence","length","source_route","source_batch",
    # --- L1 环肽质量 ---
    "plddt","l1_pass",
    # --- L2 界面置信度（ipSAE 主, ipTM 仅做参考, 不卡门槛）---
    "ipsae_mdm2","ipsae_mdmx","ipae_mdm2","iptm_mdm2","iptm_mdmx","l2_pass",
    # --- L3 界面物理 ---
    "dg_mdm2","dg_mdmx","sc_mdm2","sc_mdmx","dsasa_mdm2","dsasa_mdmx","l3_pass",
    # --- L4 环化几何 QC（relax 前后各一次）---
    "ring_closure_pre","ring_closure_post","l4_pass",
    # --- L5 设计意图 ---
    "hotspot_cov_mdm2","hotspot_cov_mdmx","site_consistency","l5_pass",
    # --- L6 鲁棒性（多预测器/多 seed 收敛）---
    "pose_rmsd","seed_convergence","colab_iptm_mdm2","colab_iptm_mdmx","l6_pass",
    # --- L7 可设计性（scRMSD）---
    "scrmsd","l7_pass",
    # --- 综合判定 ---
    "all_layers_pass","pareto_front",
    # --- 可合成性（Critic 检查）---
    "synth_pass",
    # --- ADME bonus ---
    "adme_net_charge","adme_tpsa","adme_clogp","adme_chameleonicity","novelty_score",
    # --- 双靶参考（导师禁止做加权门槛但保留做汇报）---
    "asymmetry","dual_score",
    # --- 状态/产出 ---
    "final_status","final_rank","notes","last_updated",
]

# 旧名 → 新名别名：旧 Agent 还用旧字段时自动落到新列
_ALIAS_MAP = {
    "monomer_plddt": "plddt",
    "self_rmsd": "scrmsd",
    "haddock_mdm2": "dg_mdm2",
    "haddock_mdmx": "dg_mdmx",
    "layer1_pass": "l1_pass",
    "layer2_3_pass": "l2_pass",
    "layer4_pass": "l4_pass",
    "cross_tool_ok": "l6_pass",
}


def _alias_keys(row: dict) -> dict:
    """旧字段名 → 新字段名（向前兼容旧 Agent 代码）。"""
    for old, new in _ALIAS_MAP.items():
        if old in row and new not in row:
            row[new] = row.pop(old)
    return row


# v5: thresholds key 归一化映射。
# threshold_research.py 早期版本会同时写两个 key（如 L4_ring_closure 与 L4_nc_term_dist），
# evaluate_battery 只认后者。归一化策略：「文献值优先」——同义 key 同时存在时，
# 保留证据等极更高的那条（paper_explicit > field_consensus > estimate）。
_THRESHOLD_KEY_ALIASES = {
    "L4_ring_closure":   "L4_nc_term_dist",
    "L6_pose_convergence": "L6_pose_rmsd",
}


def _normalize_thresholds(raw: dict) -> dict:
    """
    把 threshold_research.py 输出的 thresholds 做一次 key 归一化。
    同义 key 同时存在时，优先保留证据等极更高（paper_explicit > field_consensus > estimate）
    且 source 中带 PMID 的那条；另一条不丢弃，留到 _conflict 备查。
    缺失的 grade 字段按字段特征兜底。
    """
    if not isinstance(raw, dict) or not raw:
        return {}

    def conf_rank(entry: dict) -> int:
        g = str(entry.get("evidence_grade") or entry.get("grade") or "").lower()
        if "paper" in g:   return 3
        if "field" in g or "consensus" in g: return 2
        if "design" in g:  return 2  # design_rule 与 field_consensus 同等
        if "team"  in g or "provisional" in g: return 1
        if "estimate" in g or "经验" in g:    return 0
        # 无 grade 字段时按 source 是否带 PMID / "paper" 推断
        src = str(entry.get("source", ""))
        if "PMID" in src or "pmid" in src:
            return 3
        if src and "经验" not in src:
            return 2
        return 0

    normalized = {k: v for k, v in raw.items()}
    conflicts = {}
    for old_key, new_key in _THRESHOLD_KEY_ALIASES.items():
        if old_key in normalized:
            old_entry, new_entry = normalized.pop(old_key), normalized.get(new_key)
            merged = new_entry if new_entry is not None else old_entry
            if new_entry is not None:
                # 同义 key 都有 → 保留等级更高的
                if conf_rank(old_entry) > conf_rank(new_entry):
                    merged = old_entry
                conflicts[old_key] = {
                    "discarded_for": new_key,
                    "kept_rank": conf_rank(merged),
                    "discarded_rank": conf_rank(old_entry if merged is new_entry else new_entry),
                }
            merged.setdefault("evidence_grade",
                              merged.get("evidence_grade") or merged.get("grade") or "")
            normalized[new_key] = merged
    if conflicts:
        normalized["_conflict_log"] = conflicts
    return normalized

# ============================================================
# 全局状态
# ============================================================
class State:
    """读/写 state.json —— 所有Agent共享的'白板'"""
    
    _default = {
        "project": "MDM2/MDMX双靶环肽Agent设计",
        "targets": {"MDM2": {"uniprot": "Q00987"}, "MDMX": {"uniprot": "O15151"}},
        "phase": "research",
        "round": 1,
        "pocket_differences": {},
        "known_dual_binders": [],
        "design_budget": {"route_A_mdm2": 400, "route_A_mdmx": 400,
                          "route_B": 400, "route_C": 200},
        "candidate_count": 0,
        "iteration_history": [],
        # v5: 七层指标电池阈值（来自 data_layer.DEFAULT_THRESHOLDS，最终由正对照标定）
        "thresholds": {},
    }
    
    @classmethod
    def load(cls) -> dict:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return cls._default.copy()
    
    @classmethod
    def save(cls, data: dict):
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    
    @classmethod
    def update(cls, patches: dict):
        """合并更新，不覆盖已有字段"""
        s = cls.load()
        s.update(patches)
        cls.save(s)
        return s
    
    @classmethod
    def append_history(cls, entry: dict):
        s = cls.load()
        s.setdefault("iteration_history", []).append(entry)
        cls.save(s)

    @classmethod
    def sync_thresholds_from_cache(cls) -> dict:
        """
        读取 _thresholds_cache.json，做 key 归一化，合并回 state.json["thresholds"]。
        已有 thresholds 优先保留（除非新缓存条目的证据等级更高）。
        列出合并来源变化记一条 evidence_log。
        返回 merge 后的 thresholds dict。
        """
        if not THRESHOLDS_CACHE.exists():
            return cls.load().get("thresholds", {})
        try:
            raw_cache = json.loads(THRESHOLDS_CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return cls.load().get("thresholds", {})

        normalized = _normalize_thresholds(raw_cache)
        s = cls.load()
        existing = s.get("thresholds", {})
        # 已有 threshold 优先保留；同 key 下新条目等级更高时覆盖
        def _rank(entry):
            g = str(entry.get("evidence_grade") or entry.get("grade") or "").lower()
            if "paper" in g:   return 3
            if "field" in g or "consensus" in g or "design" in g: return 2
            if "team" in g or "provisional" in g:  return 1
            return 0
        merged = dict(existing)
        for k, v in normalized.items():
            if k.startswith("_"):
                merged[k] = v; continue
            if k not in existing or _rank(v) > _rank(existing.get(k, {})):
                merged[k] = v
        s["thresholds"] = merged
        cls.save(s)
        EvidenceLogger.log("system", "thresholds_synced_from_cache", {
            "cache_keys": list(normalized.keys()),
            "conflicts": normalized.get("_conflict_log", {}),
            "n_thresholds": len(merged),
        }, phase="research")
        return merged


# ============================================================
# 证据日志
# ============================================================
class EvidenceLogger:
    """所有操作的 JSONL 记录——每个Agent调工具前后记一笔"""
    
    @classmethod
    def _write(cls, entry: dict):
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    
    @classmethod
    def log(cls, agent: str, event_type: str, payload: dict,
            targets: list = None, phase: str = None,
            round_num: int = None, blocks: list = None):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_id": str(uuid.uuid4())[:12],
            "agent": agent,
            "event_type": event_type
        }
        if phase:    entry["phase"] = phase
        if round_num: entry["round"] = round_num
        if targets:  entry["targets"] = targets
        if blocks:   entry["blocks"] = blocks
        entry.update(payload)
        cls._write(entry)
        return entry["event_id"]

    @classmethod
    def research_complete(cls, hotspot_analysis: dict, known_binders: list, refs: list):
        return cls.log("research", "research_targets", {
            "pdb_complexes": hotspot_analysis.get("pdb_list", []),
            "hotspot_analysis": hotspot_analysis,
            "known_binders": known_binders,
            "literature_refs": refs
        }, targets=["both"], phase="research", round_num=1)

    @classmethod
    def design_batch(cls, route: str, n_generated: int, n_valid: int,
                     tool_name: str, tool_version: str, duration_sec: float):
        return cls.log("design", "design_batch", {
            "route": route, "n_generated": n_generated, "n_valid": n_valid,
            "tool_trace": {"tool_name": tool_name, "tool_version": tool_version,
                           "exit_code": 0, "duration_sec": duration_sec}
        }, targets=["both"], phase="design")

    @classmethod
    def candidate_registered(cls, candidate: dict):
        cls.log("design", "candidate_registered", {"candidate": candidate},
                targets=["both"], phase="design")
        cls._increment_counter()

    @classmethod
    def evaluate_layer_start(cls, layer: int, n_candidates: int, thresholds: dict):
        return cls.log("prediction", "evaluate_layer_start", {
            "layer": layer, "n_candidates_in": n_candidates, "thresholds": thresholds
        }, targets=["both"], phase="evaluate")

    @classmethod
    def candidate_scored(cls, candidate_id: str, layer: int, scores: dict,
                         tool_trace: dict, passed: bool):
        cls.log("prediction", "candidate_scored", {
            "candidate_id": candidate_id, "layer": layer,
            "scores": scores, "tool_trace": tool_trace, "passed": passed
        }, targets=["both" if scores.get("iptm_mdmx") else "MDM2"], phase="evaluate")

    @classmethod
    def candidate_eliminated(cls, candidate_id: str, layer: int,
                             reason: str, score: float, threshold: float):
        cls.log("prediction", "candidate_eliminated", {
            "candidate_id": candidate_id, "layer": layer,
            "reason": reason, "actual_score": score, "threshold": threshold
        }, phase="evaluate")

    @classmethod
    def evaluate_layer_complete(cls, layer: int, n_in: int, n_pass: int, n_fail: int):
        cls.log("prediction", "evaluate_layer_complete", {
            "layer": layer, "n_in": n_in, "n_pass": n_pass, "n_fail": n_fail,
            "pass_rate": round(n_pass / max(n_in, 1), 3)
        }, targets=["both"], phase="evaluate")

    @classmethod
    def critic_review(cls, issues: list, passed: bool, summary: str,
                      recommendation: str, metrics: dict):
        return cls.log("critic", "critic_review", {
            "issues": issues, "pass": passed,
            "summary": summary, "recommendation": recommendation,
            "metrics_snapshot": metrics
        }, targets=["both"], phase="critic")

    @classmethod
    def planner_adjust(cls, trigger_event_id: str, old_strategy: dict,
                       new_strategy: dict, reason: str):
        cls.log("planner", "planner_adjust", {
            "trigger_event_id": trigger_event_id,
            "old_strategy": old_strategy, "new_strategy": new_strategy,
            "reason": reason
        }, targets=["both"], phase="iterate")

    @classmethod
    def error(cls, agent: str, error_type: str, message: str,
              recovery: str = "", trace: str = ""):
        cls.log(agent, "error", {
            "error_type": error_type, "error_message": message,
            "recovery_action": recovery, "stack_trace": trace[:500]
        })

    @classmethod
    def _increment_counter(cls):
        s = State.load()
        s["candidate_count"] = s.get("candidate_count", 0) + 1
        State.save(s)

    @classmethod
    def get_all(cls) -> list:
        if not LOG_PATH.exists():
            return []
        return [json.loads(line) for line in LOG_PATH.read_text(encoding="utf-8").strip().split("\n") if line]

    @classmethod
    def filter(cls, agent: str = None, event_type: str = None, candidate_id: str = None) -> list:
        results = []
        for entry in cls.get_all():
            if agent and entry.get("agent") != agent: continue
            if event_type and entry.get("event_type") != event_type: continue
            if candidate_id and entry.get("candidate_id") != candidate_id: continue
            results.append(entry)
        return results

    @classmethod
    def trace_candidate(cls, candidate_id: str) -> list:
        """追溯某条候选的完整生命周期"""
        return [e for e in cls.get_all()
                if e.get("candidate_id") == candidate_id
                or (isinstance(e.get("candidate"), dict) and e["candidate"].get("candidate_id") == candidate_id)]


# ============================================================
# 候选索引表
# ============================================================
class CandidateIndex:
    """所有环肽候选的主索引——CSV格式，可在Excel/GoogleSheets/WPS中打开"""

    @classmethod
    def _ensure_exists(cls):
        if not INDEX_PATH.exists():
            INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(INDEX_PATH, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(INDEX_COLUMNS)

    @classmethod
    def add(cls, row: dict):
        """添加一条新候选。必须包含 candidate_id 和 sequence。"""
        cls._ensure_exists()
        row = _alias_keys(row)  # 旧名 → 新名
        row.setdefault("source_route", "")
        row.setdefault("source_batch", "")
        row.setdefault("length", len(row.get("sequence", "")))
        row.setdefault("final_status", "pending")
        row.setdefault("last_updated", datetime.now().strftime("%Y-%m-%d %H:%M"))
        # 七层 pass 默认空（待 Prediction Agent 打分后填充）
        for pass_col in ["l1_pass","l2_pass","l3_pass","l4_pass",
                         "l5_pass","l6_pass","l7_pass","all_layers_pass",
                         "synth_pass","pareto_front"]:
            row.setdefault(pass_col, "")
        ordered = {col: row.get(col, "") for col in INDEX_COLUMNS}
        with open(INDEX_PATH, "a", newline="", encoding="utf-8-sig") as f:
            csv.DictWriter(f, fieldnames=INDEX_COLUMNS, extrasaction="ignore").writerow(ordered)

    @classmethod
    def add_batch(cls, rows: list[dict]):
        cls._ensure_exists()
        for r in rows:
            _alias_keys(r)
            r.setdefault("final_status", "pending")
            r.setdefault("last_updated", datetime.now().strftime("%Y-%m-%d %H:%M"))
        with open(INDEX_PATH, "a", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=INDEX_COLUMNS, extrasaction="ignore")
            for r in rows:
                ordered = {col: r.get(col, "") for col in INDEX_COLUMNS}
                writer.writerow(ordered)

    @classmethod
    def load(cls) -> list[dict]:
        cls._ensure_exists()
        with open(INDEX_PATH, "r", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))

    @classmethod
    def find(cls, candidate_id: str) -> Optional[dict]:
        for r in cls.load():
            if r["candidate_id"] == candidate_id:
                return r
        return None

    @classmethod
    def update_score(cls, candidate_id: str, scores: dict):
        """更新某条候选的评分字段（原地修改CSV行）。
        scores 中的旧字段名（如 monomer_plddt / layer1_pass）会自动 alias 到新名。
        """
        scores = _alias_keys(dict(scores))
        rows = cls.load()
        for r in rows:
            if r["candidate_id"] == candidate_id:
                for k, v in scores.items():
                    if k in INDEX_COLUMNS:
                        r[k] = str(v) if not isinstance(v, str) else v
                r["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                break
        with open(INDEX_PATH, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=INDEX_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

    @classmethod
    def update_status(cls, candidate_id: str, status: str, notes: str = ""):
        rows = cls.load()
        for r in rows:
            if r["candidate_id"] == candidate_id:
                r["final_status"] = status
                if notes:
                    r["notes"] = notes
                r["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                break
        with open(INDEX_PATH, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=INDEX_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

    @classmethod
    def filter_by_status(cls, status: str) -> list[dict]:
        return [r for r in cls.load() if r.get("final_status") == status]

    @classmethod
    def filter_by_layer(cls, layer_pass: bool, layer: int = 1) -> list[dict]:
        col = {1: "l1_pass", 2: "l2_pass", 3: "l3_pass",
               4: "l4_pass", 5: "l5_pass", 6: "l6_pass", 7: "l7_pass"}[layer]
        return [r for r in cls.load() if r.get(col) == str(layer_pass)]

    @classmethod
    def top_n(self, n: int = 10, by: str = "dual_score") -> list[dict]:
        rows = [r for r in self.load() if r.get(by)]
        rows.sort(key=lambda r: float(r.get(by, 0)), reverse=True)
        return rows[:n]

    @classmethod
    def stats(cls) -> dict:
        """快速统计：总数、七层各层通过数、ipSAE/dG 中位数（v5 主指标）"""
        rows = cls.load()
        ipsae_m2 = [float(r["ipsae_mdm2"]) for r in rows if r.get("ipsae_mdm2")]
        ipsae_mx = [float(r["ipsae_mdmx"]) for r in rows if r.get("ipsae_mdmx")]
        dg_m2   = [float(r["dg_mdm2"]) for r in rows if r.get("dg_mdm2")]
        scrmsds = [float(r["scrmsd"]) for r in rows if r.get("scrmsd")]
        # ipTM 保留做参考（导师 Trap 1：不做门槛）
        iptm_m2 = [float(r["iptm_mdm2"]) for r in rows if r.get("iptm_mdm2")]

        def med(lst):
            lst = sorted(lst)
            return lst[len(lst)//2] if lst else 0

        return {
            "total_candidates": len(rows),
            # 七层通过计数（v5 主判定）
            "l1_pass": sum(1 for r in rows if r.get("l1_pass") == "True"),
            "l2_pass": sum(1 for r in rows if r.get("l2_pass") == "True"),
            "l3_pass": sum(1 for r in rows if r.get("l3_pass") == "True"),
            "l4_pass": sum(1 for r in rows if r.get("l4_pass") == "True"),
            "l5_pass": sum(1 for r in rows if r.get("l5_pass") == "True"),
            "l6_pass": sum(1 for r in rows if r.get("l6_pass") == "True"),
            "l7_pass": sum(1 for r in rows if r.get("l7_pass") == "True"),
            "all_layers_pass": sum(1 for r in rows if r.get("all_layers_pass") == "True"),
            "synth_pass": sum(1 for r in rows if r.get("synth_pass") == "True"),
            "pareto_front": sum(1 for r in rows if r.get("pareto_front") == "True"),
            "finalized": sum(1 for r in rows if r.get("final_status") == "finalized"),
            # 主指标中位数（v5: ipSAE 替代 ipTM）
            "ipsae_mdm2_median": round(med(ipsae_m2), 3),
            "ipsae_mdmx_median": round(med(ipsae_mx), 3),
            "dg_mdm2_median": round(med(dg_m2), 3),
            "scrmsd_median": round(med(scrmsds), 3),
            "iptm_mdm2_median": round(med(iptm_m2), 3),  # 参考
        }


# ============================================================
# 工具函数
# ============================================================
def file_hash(path: str, n_bytes: int = 4096) -> str:
    """计算文件MD5（前n_bytes采样，适合大PDB文件）"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read(n_bytes))
    return h.hexdigest()[:12]


def sanitize_id(s: str) -> str:
    """C0001格式的候选ID"""
    return s if (len(s) == 5 and s.startswith("C")) else f"C{int(s):04d}"


# ============================================================
# v5: 七层指标电池判定器（所有 Agent 共用）
# ============================================================
def _cmp(value: float, op: str, threshold: float) -> bool:
    """安全比较（value 对 op threshold）。值缺失返回 False。"""
    if value is None or value == "" or threshold is None:
        return False
    ops = {">": lambda a, b: a > b,
           "<": lambda a, b: a < b,
           ">=": lambda a, b: a >= b,
           "<=": lambda a, b: a <= b}
    return ops.get(op, lambda a, b: False)(float(value), float(threshold))


def _f(v):
    """尝试转 float，失败返回 None。"""
    if v is None or v == "": return None
    try: return float(v)
    except (TypeError, ValueError): return None


def evaluate_battery(c: dict, thresholds: dict | None = None) -> dict:
    """
    七层指标电池判定（v5 主判定）。

    导师要求（DeeCamp）：七层全清才算成功，缺一不可。每层阈值须有出处
    （来自 thresholds 由 Research Agent 文献检索+正对照标定填入）。

    参数:
      c: 候选 dict（含七层指标字段，旧名自动 alias）
      thresholds: 来自 state.json["thresholds"]，结构同 _DEFAULT_THRESHOLDS

    返回:
      {l1_pass..l7_pass: bool, all_layers_pass: bool,
       failed_layers: list[str],
       layer_values: dict,  # 每层主值 }
    """
    c = _alias_keys(dict(c))  # 旧名兜底
    th = thresholds or {}

    def th_has(key): return bool(th.get(key) and th[key].get("value") is not None)

    # L1 环肽质量：pLDDT
    l1 = _cmp(_f(c.get("plddt")), th.get("L1_plddt", {}).get("operator", ">"),
              th.get("L1_plddt", {}).get("value", 0.8)) if th_has("L1_plddt") else False

    # L2 界面置信度：ipSAE 主（小环肽 ipTM 会虚高, 导师 Trap 1）
    t2 = th.get("L2_ipsae", {})
    l2 = (_cmp(_f(c.get("ipsae_mdm2")), t2.get("operator", ">"), t2.get("value", 0.5))
          if th_has("L2_ipsae") else False)

    # L3 界面物理：dG + 形状互补 + dSASA（任一缺即视为该层未通过严谨审查）
    l3 = all([
        _cmp(_f(c.get("dg_mdm2")), th.get("L3_dg", {}).get("operator", "<"), th.get("L3_dg", {}).get("value", -10)),
        _cmp(_f(c.get("sc_mdm2")), th.get("L3_sc", {}).get("operator", ">"), th.get("L3_sc", {}).get("value", 0.6)),
        _cmp(_f(c.get("dsasa_mdm2")), th.get("L3_dsasa", {}).get("operator", ">"), th.get("L3_dsasa", {}).get("value", 400)),
    ]) if (th_has("L3_dg") or c.get("dg_mdm2")) else False

    # L4 环化几何 QC：relax 前后环闭合均通过（导师 Trap 2）
    pre = c.get("ring_closure_pre", "")
    post = c.get("ring_closure_post", "")
    l4 = (str(pre).lower() in ("true", "1", "yes")) and (str(post).lower() in ("true", "1", "yes"))

    # L5 设计意图：热点覆盖 + 位点一致性（导师 Trap 3）
    t5 = th.get("L5_hotspot_coverage", {})
    hc = _f(c.get("hotspot_cov_mdm2"))
    l5 = (_cmp(hc, t5.get("operator", ">="), t5.get("value", 0.67)) and
          str(c.get("site_consistency", "")).lower() in ("true", "1", "yes")) \
         if (th_has("L5_hotspot_coverage") or hc is not None) else False

    # L6 鲁棒性：多预测器 pose RMSD 收敛 + 多 seed 收敛
    t6 = th.get("L6_pose_rmsd", {})
    pr = _f(c.get("pose_rmsd"))
    sc_ = _f(c.get("seed_convergence"))
    l6 = (_cmp(pr, t6.get("operator", "<"), t6.get("value", 2.0)) and
          (sc_ is None or sc_ >= 0.67)) \
         if (th_has("L6_pose_rmsd") or pr is not None) else False

    # L7 可设计性：scRMSD
    t7 = th.get("L7_scrmsd", {})
    l7 = _cmp(_f(c.get("scrmsd")), t7.get("operator", "<"), t7.get("value", 2.0)) \
         if (th_has("L7_scrmsd") or c.get("scrmsd")) else False

    layer_pass = {
        "l1_pass": bool(l1), "l2_pass": bool(l2),
        "l3_pass": bool(l3), "l4_pass": bool(l4),
        "l5_pass": bool(l5), "l6_pass": bool(l6),
        "l7_pass": bool(l7),
    }
    failed = [k for k, v in layer_pass.items() if not v]

    return {
        **layer_pass,
        "all_layers_pass": len(failed) == 0,
        "failed_layers": failed,
        "layer_values": {
            "L1_plddt": _f(c.get("plddt")),
            "L2_ipsae_mdm2": _f(c.get("ipsae_mdm2")),
            "L2_iptm_mdm2": _f(c.get("iptm_mdm2")),   # 参考
            "L3_dg_mdm2": _f(c.get("dg_mdm2")),
            "L3_sc_mdm2": _f(c.get("sc_mdm2")),
            "L3_dsasa_mdm2": _f(c.get("dsasa_mdm2")),
            "L4_pre": str(pre), "L4_post": str(post),
            "L5_hotspot_cov_mdm2": hc,
            "L6_pose_rmsd": pr, "L6_seed_conv": sc_,
            "L7_scrmsd": _f(c.get("scrmsd")),
        },
    }


if __name__ == "__main__":
    print("=== data_layer.py 自检 ===")
    
    # 测试State
    s = State.load()
    print(f"State loaded: phase={s['phase']}, round={s['round']}")
    
    # 测试日志
    eid = EvidenceLogger.log("system", "test", {"msg": "冒烟测试"},
                              targets=["both"], phase="research")
    print(f"Log written: event_id={eid}")
    
    # 测试候选索引
    CandidateIndex.add({
        "candidate_id": "C0001",
        "sequence": "GFEWALAAK",
        "source_route": "route_A_mdm2_first",
        "source_batch": "batch_mdm2_len10"
    })
    CandidateIndex.add({
        "candidate_id": "C0002",
        "sequence": "PFNWALGGS",
        "source_route": "route_A_mdmx_first",
        "source_batch": "batch_mdmx_len12"
    })
    
    # 模拟评分
    CandidateIndex.update_score("C0001", {
        "monomer_plddt": 0.85,
        "self_rmsd": 1.2,
        "layer1_pass": "True",
        "iptm_mdm2": 0.84,
        "iptm_mdmx": 0.72,
        "dual_score": 0.72,
        "asymmetry": 0.12
    })
    CandidateIndex.update_score("C0002", {
        "monomer_plddt": 0.78,
        "self_rmsd": 1.8,
        "layer1_pass": "True",
        "iptm_mdm2": 0.61,
        "iptm_mdmx": 0.79,
        "dual_score": 0.64,
        "asymmetry": 0.18
    })
    
    print(f"\n索引表统计: {CandidateIndex.stats()}")
    print(f"\nTop 候选: {CandidateIndex.top_n(2)}")
    print("\n日志条目数:", len(EvidenceLogger.get_all()))
    print("自检通过")
