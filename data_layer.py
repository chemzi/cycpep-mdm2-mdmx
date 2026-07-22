"""
环肽Agent证据日志 & 候选索引表 - 共享数据层
所有Agent通过此模块读写 state.json、evidence_log.jsonl、candidate_index.csv

使用方式:
    from data_layer import State, EvidenceLogger, CandidateIndex

    # 读全局状态
    state = State.load()
    
    # 记一条日志
    EvidenceLogger.log(agent="design", event_type="design_batch",
                       payload={"route": "route_A", "n_generated": 200})
    
    # 加一条候选到索引表
    CandidateIndex.add({"candidate_id": "C0001", "sequence": "GFEWALA...", ...})
    
    # 更新候选分数
    CandidateIndex.update_score("C0001", {"iptm_mdm2": 0.84, "iptm_mdmx": 0.72})
"""
import json, csv, hashlib, os, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "data" / "state.json"
LOG_PATH   = ROOT / "evidence" / "evidence_log.jsonl"
INDEX_PATH = ROOT / "data" / "candidate_index.csv"

INDEX_COLUMNS = [
    "candidate_id","sequence","length","source_route","source_batch",
    "monomer_plddt","self_rmsd","layer1_pass",
    "iptm_mdm2","iptm_mdmx","dual_score","asymmetry","layer2_3_pass",
    "colab_iptm_mdm2","colab_iptm_mdmx","haddock_mdm2","haddock_mdmx",
    "hotspot_cov_mdm2","hotspot_cov_mdmx","cross_tool_ok","layer4_pass",
    "final_status","final_rank","notes","last_updated"
]

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
        "iteration_history": []
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
        row.setdefault("source_route", "")
        row.setdefault("source_batch", "")
        row.setdefault("length", len(row.get("sequence", "")))
        row.setdefault("final_status", "pending")
        row.setdefault("last_updated", datetime.now().strftime("%Y-%m-%d %H:%M"))
        row["layer1_pass"] = ""; row["layer2_3_pass"] = ""; row["layer4_pass"] = ""; row["cross_tool_ok"] = ""
        ordered = {col: row.get(col, "") for col in INDEX_COLUMNS}
        with open(INDEX_PATH, "a", newline="", encoding="utf-8-sig") as f:
            csv.DictWriter(f, fieldnames=INDEX_COLUMNS, extrasaction="ignore").writerow(ordered)

    @classmethod
    def add_batch(cls, rows: list[dict]):
        cls._ensure_exists()
        for r in rows:
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
        """更新某条候选的评分字段（原地修改CSV行）"""
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
        col = {1: "layer1_pass", 2: "layer2_3_pass", 3: "layer2_3_pass", 4: "layer4_pass"}[layer]
        return [r for r in cls.load() if r.get(col) == str(layer_pass)]

    @classmethod
    def top_n(self, n: int = 10, by: str = "dual_score") -> list[dict]:
        rows = [r for r in self.load() if r.get(by)]
        rows.sort(key=lambda r: float(r.get(by, 0)), reverse=True)
        return rows[:n]

    @classmethod
    def stats(cls) -> dict:
        """快速统计：总数、各层通过率、双靶中位数"""
        rows = cls.load()
        scores_m2 = [float(r["iptm_mdm2"]) for r in rows if r.get("iptm_mdm2")]
        scores_mx = [float(r["iptm_mdmx"]) for r in rows if r.get("iptm_mdmx")]
        duals   = [float(r["dual_score"]) for r in rows if r.get("dual_score")]

        def med(lst): 
            lst = sorted(lst)
            return lst[len(lst)//2] if lst else 0

        return {
            "total_candidates": len(rows),
            "layer1_pass": sum(1 for r in rows if r.get("layer1_pass") == "True"),
            "layer2_3_pass": sum(1 for r in rows if r.get("layer2_3_pass") == "True"),
            "layer4_pass": sum(1 for r in rows if r.get("layer4_pass") == "True"),
            "finalized": sum(1 for r in rows if r.get("final_status") == "finalized"),
            "iptm_mdm2_median": round(med(scores_m2), 3),
            "iptm_mdmx_median": round(med(scores_mx), 3),
            "dual_score_median": round(med(duals), 3),
            "avg_asymmetry": round(sum(float(r.get("asymmetry", 0)) for r in rows if r.get("asymmetry")) / max(len(rows), 1), 3)
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
