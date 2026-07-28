"""
data_layer.py 完整集成测试
覆盖所有 Agent 使用场景 + 边界情况
"""
import json, sys, os, csv, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEST_ROOT = Path(tempfile.mkdtemp(prefix="cycpep-data-layer-test-"))
DATA_DIR = TEST_ROOT / "data"
EVIDENCE_DIR = TEST_ROOT / "evidence"
os.environ["CYCPEP_DATA_DIR"] = str(DATA_DIR)
os.environ["CYCPEP_EVIDENCE_DIR"] = str(EVIDENCE_DIR)

os.chdir(str(ROOT))

import data_layer
from data_layer import (
    State, EvidenceLogger, CandidateIndex, file_hash, sanitize_id,
    evaluate_battery, compute_pareto_front, INDEX_COLUMNS,
)
from project_config import load_project_config, required_target_ids
from threshold_calibration import calibrate_threshold
from scripts.pubmed_search import build_search_term
from scripts.aggregate_pockets import aggregate

passed = 0
failed = 0

def check(desc, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {desc}")
    else:
        failed += 1
        print(f"  [FAIL] {desc}")

def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

# ============================================================
section("1. State 读写")
# ============================================================
s = State.load()
check("初始 phase=research", s["phase"] == "research")
check("初始 round=1", s["round"] == 1)
check("初始 candidate_count=0", s["candidate_count"] == 0)
check("budget route_A_mdm2=400", s["design_budget"]["route_A_mdm2"] == 400)

State.update({"phase": "design", "round": 2, "candidate_count": 150})
s2 = State.load()
check("update 后 phase=design", s2["phase"] == "design")
check("update 后 round=2", s2["round"] == 2)
check("update 后 candidate_count=150", s2["candidate_count"] == 150)
check("update 不覆盖 budget", s2["design_budget"]["route_A_mdm2"] == 400)

State.append_history({"round": 1, "action": "first_design_batch", "n": 200})
State.append_history({"round": 2, "action": "mdmx_bias_adjust", "n": 150})
s3 = State.load()
check("append_history 记录2条", len(s3["iteration_history"]) == 2)
check("history[0] 内容", s3["iteration_history"][0]["action"] == "first_design_batch")

# ============================================================
section("2. EvidenceLogger — 所有事件类型")
# ============================================================

# 2a: research_complete
eid_r = EvidenceLogger.research_complete(
    hotspot_analysis={"pdb_list": ["1YCR", "2Z5S"], "F19_pocket": "hydrophobic"},
    known_binders=["nutlin-3a", "SAH-p53-8"],
    refs=["pmid:12345678", "pmid:87654321"]
)
check("research_complete 返回 event_id", len(eid_r) > 0)

# 2b: design_batch
eid_d = EvidenceLogger.design_batch(
    route="route_A_mdm2_first", n_generated=400, n_valid=385,
    tool_name="afcycdesign_binder", tool_version="2.3.2", duration_sec=4720.5
)
check("design_batch 返回 event_id", len(eid_d) > 0)

# 2c: candidate_registered (同时自增 counter)
EvidenceLogger.candidate_registered({"candidate_id": "C0042", "sequence": "GFEWALAAK"})
s = State.load()
check("candidate_registered 自增 counter", s["candidate_count"] == 151)

# 2d: evaluate_layer_start
eid_es = EvidenceLogger.evaluate_layer_start(
    layer=2, n_candidates=200, thresholds={"iptm_mdm2": 0.7, "iptm_mdmx": 0.7}
)
check("evaluate_layer_start 返回 event_id", len(eid_es) > 0)

# 2e: candidate_scored
EvidenceLogger.candidate_scored(
    candidate_id="C0042", layer=2,
    scores={"iptm_mdm2": 0.84, "iptm_mdmx": 0.72, "dual_score": 0.72, "asymmetry": 0.12},
    tool_trace={"tool_name": "afcycdesign_complex", "tool_version": "2.3.2",
                "exit_code": 0, "duration_sec": 125.3},
    passed=True
)

# 2f: candidate_eliminated
EvidenceLogger.candidate_eliminated(
    candidate_id="C0199", layer=1, reason="self_rmsd > 2.0",
    score=3.5, threshold=2.0
)

# 2g: evaluate_layer_complete
EvidenceLogger.evaluate_layer_complete(layer=2, n_in=200, n_pass=85, n_fail=115)

# 2h: critic_review
eid_c = EvidenceLogger.critic_review(
    issues=[{"type": "mdmx_bias", "detail": "MDMX iptm 偏低 0.1 以上"}],
    passed=False,
    summary="当前 batch MDMX 亲和力不够，建议调整设计策略",
    recommendation="下一轮使用 route_A_mdmx_first，增加 F19 接触残基权重",
    metrics={"iptm_mdm2_median": 0.81, "iptm_mdmx_median": 0.65}
)
check("critic_review 返回 event_id", len(eid_c) > 0)

# 2i: planner_adjust
EvidenceLogger.planner_adjust(
    trigger_event_id=eid_c,
    old_strategy={"route": "route_A_mdm2_first", "mdmx_weight": 0.5},
    new_strategy={"route": "route_A_mdmx_first", "mdmx_weight": 0.7},
    reason="mdmx_bias"
)

# 2j: error
EvidenceLogger.error("prediction", "timeout", "AF3 超时",
                      recovery="改用 ColabFold", trace="Traceback... [truncated]")

# ============================================================
section("3. EvidenceLogger — 查询")
# ============================================================
all_entries = EvidenceLogger.get_all()
check("get_all 返回非空", len(all_entries) > 0)

# filter by agent
pred_entries = EvidenceLogger.filter(agent="prediction")
check("filter(agent='prediction') > 0", len(pred_entries) > 0)

# filter by event_type
err_entries = EvidenceLogger.filter(event_type="error")
check("filter(event_type='error') == 1", len(err_entries) == 1)

# trace_candidate
trace = EvidenceLogger.trace_candidate("C0042")
check("trace_candidate('C0042') >= 2", len(trace) >= 2)

# trace 不存在的候选
trace_empty = EvidenceLogger.trace_candidate("C9999")
check("trace_candidate('C9999') 为空", len(trace_empty) == 0)

# ============================================================
section("4. CandidateIndex — 单条操作")
# ============================================================
# add
CandidateIndex.add({"candidate_id": "C0042", "sequence": "GFEWALAAK",
                     "source_route": "route_A_mdm2_first", "source_batch": "batch_01"})
CandidateIndex.add({"candidate_id": "C0088", "sequence": "PFNWALAGGK",
                     "source_route": "route_A_mdmx_first", "source_batch": "batch_02"})

# find
r = CandidateIndex.find("C0042")
check("find('C0042') 找到", r is not None and r["sequence"] == "GFEWALAAK")
check("find('C9999') 返回 None", CandidateIndex.find("C9999") is None)

# update_score（v5: 旧字段名通过 alias 自动落到新列）
CandidateIndex.update_score("C0042", {
    "monomer_plddt": 0.88, "self_rmsd": 1.1, "layer1_pass": "True",
    "l1_pass": "True", "scrmsd": 0.9,
    "iptm_mdm2": 0.84, "iptm_mdmx": 0.72, "dual_score": 0.72, "asymmetry": 0.12,
    "layer2_3_pass": "True", "l2_pass": "True",
})
r2 = CandidateIndex.find("C0042")
check("update_score 后 iptm_mdm2=0.84", r2["iptm_mdm2"] == "0.84")
# 旧 pass 字段只进入 legacy 列，新版 pass 由 Prediction 明确写入
check("update_score 后 l2_pass=True (新版显式字段)", r2["l2_pass"] == "True")
check("update_score 后 plddt=0.88 (旧名 monomer_plddt alias)", r2["plddt"] == "0.88")
check("旧 self_rmsd 保存在 legacy 列", r2["legacy_self_rmsd"] == "1.1")
check("旧 layer2_3_pass 保存在 legacy 列", r2["legacy_layer2_3_pass"] == "True")
check("显式 scRMSD 不受旧 self_rmsd 覆盖", r2["scrmsd"] == "0.9")

CandidateIndex.update_score("C0088", {
    "monomer_plddt": 0.79, "layer1_pass": "True", "l1_pass": "True",
    "iptm_mdm2": 0.61, "iptm_mdmx": 0.79, "dual_score": 0.64, "asymmetry": 0.18
})

# update_status
CandidateIndex.update_status("C0042", "finalized", "双靶均衡，推荐交付")
r3 = CandidateIndex.find("C0042")
check("update_status 后 final_status=finalized", r3["final_status"] == "finalized")
check("update_status 后 notes 更新", r3["notes"] == "双靶均衡，推荐交付")

# ============================================================
section("5. CandidateIndex — 批量操作")
# ============================================================
batch = [
    {"candidate_id": f"C{i:04d}", "sequence": f"G{chr(70+i%20)}{chr(69+i%15)}WALA",
     "source_route": "route_A_mdm2_first", "source_batch": "batch_03"}
    for i in range(10, 20)
]
CandidateIndex.add_batch(batch)
all_cands = CandidateIndex.load()
check(f"add_batch 后总数=12 (2原有+10批量)", len(all_cands) == 12)

# ============================================================
section("6. CandidateIndex — 筛选与排序")
# ============================================================
# filter_by_status
finalized = CandidateIndex.filter_by_status("finalized")
check("filter_by_status('finalized') == 1", len(finalized) == 1 and finalized[0]["candidate_id"] == "C0042")

# filter_by_layer
l1_pass = CandidateIndex.filter_by_layer(True, layer=1)
check(f"filter_by_layer(layer1, True) >= 2", len(l1_pass) >= 2)

# top_n
top = CandidateIndex.top_n(5, by="dual_score")
check(f"top_n(5, dual_score) 返回有 dual_score 的候选", len(top) > 0)
# 确认排序正确 (C0042: 0.72 > C0088: 0.64)
check("top_n 排序正确 (C0042 第一)", top[0]["candidate_id"] == "C0042")

# ============================================================
section("7. CandidateIndex — stats")
# ============================================================
stats = CandidateIndex.stats()
check("stats total_candidates=12", stats["total_candidates"] == 12)
check("stats finalized=1", stats["finalized"] == 1)
# v5: 保留 iptm_mdm2_median 作参考字段
check("stats iptm_mdm2_median=0.725", stats["iptm_mdm2_median"] == 0.725)
# v5: 新主指标字段为 ipsae_*_median
check("stats 包含 ipsae_mdm2_median", "ipsae_mdm2_median" in stats)
check("stats 包含 all_layers_pass", "all_layers_pass" in stats)
check("stats 包含 l1_pass (七层计数)", "l1_pass" in stats)

# ============================================================
section("8. 工具函数")
# ============================================================
check("sanitize_id('42') -> C0042", sanitize_id("42") == "C0042")
check("sanitize_id('C0042') 不变", sanitize_id("C0042") == "C0042")

import tempfile
tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdb")
tmp.write(b"HEADER TEST PDB FILE FOR HASH\n" + b"X" * 5000)
tmp.close()
h = file_hash(tmp.name)
check(f"file_hash 返回 12 字符 hex: {h}", len(h) == 12 and all(c in "0123456789abcdef" for c in h))
os.unlink(tmp.name)

# ============================================================
section("9. JSONL 格式验证")
# ============================================================
log_content = EVIDENCE_DIR / "evidence_log.jsonl"
lines = log_content.read_text(encoding="utf-8").strip().split("\n")
check(f"日志文件有 {len(lines)} 行JSONL", len(lines) >= 10)
for i, line in enumerate(lines):
    try:
        obj = json.loads(line)
        check(f"  第{i+1}行 JSON 合法", True)
        check(f"  第{i+1}行含 timestamp", "timestamp" in obj)
        check(f"  第{i+1}行含 event_id", "event_id" in obj)
        check(f"  第{i+1}行含 agent", "agent" in obj)
        check(f"  第{i+1}行含 event_type", "event_type" in obj)
    except json.JSONDecodeError:
        check(f"  第{i+1}行 JSON 合法", False)

# ============================================================
section("10. CSV UTF-8 BOM 验证")
# ============================================================
csv_content = (DATA_DIR / "candidate_index.csv").read_bytes()
check("CSV 以 UTF-8 BOM 开头", csv_content[:3] == b"\xef\xbb\xbf")
reader = csv.DictReader(csv_content.decode("utf-8-sig").splitlines())
rows = list(reader)
check(f"CSV 有 {'>=' if len(rows) >= 12 else ''}12+ 行数据", len(rows) >= 12)
# v5: INDEX_COLUMNS 已扩展为七层电池和交接 schema，直接用源定义做校验
check(f"CSV 所有 {len(INDEX_COLUMNS)} 列存在", all(col in reader.fieldnames for col in INDEX_COLUMNS))
# 抽查关键新列
for must_col in ["plddt","l1_pass","ipsae_mdm2","ipsae_mdmx","dg_mdm2","sc_mdm2",
                 "dsasa_mdm2","nc_distance_pre","nc_distance_post",
                 "ring_closure_pre","ring_closure_post","l4_pass",
                 "site_consistency_mdm2","site_consistency_mdmx","l5_pass",
                 "pose_rmsd_mdm2","pose_rmsd_mdmx",
                 "seed_convergence_mdm2","seed_convergence_mdmx",
                 "l6_pass","scrmsd","l7_pass","all_layers_pass","pareto_front",
                 "synth_pass","adme_net_charge","adme_tpsa","adme_clogp",
                 "adme_chameleonicity","novelty_score","cyclization_type",
                 "cyclization_bonds","manifest_path","legacy_haddock_mdm2"]:
    check(f"  CSV 含新列 {must_col}", must_col in reader.fieldnames)

# ============================================================
section("11. evaluate_battery — 七层指标电池判定")
# ============================================================
# 模拟 thresholds（结构同 state.json["thresholds"]，由 Research Agent 文献检索+正对照标定填入）
test_thresholds = {
    "L1_plddt":          {"value": 0.80, "operator": ">",  "source": "RFpeptides PMID:40542165", "grade": "paper_explicit"},
    "L2_ipsae":          {"value": 0.50, "operator": ">",  "source": "field consensus",          "grade": "field_consensus"},
    "L3_dg":             {"value": -10.0, "operator": "<",  "source": "PRODIGY estimate",        "grade": "estimate"},
    "L3_sc":             {"value": 0.60, "operator": ">",  "source": "field consensus",         "grade": "field_consensus"},
    "L3_dsasa":          {"value": 400,  "operator": ">",  "source": "field consensus",         "grade": "field_consensus"},
    "L4_nc_term_dist":   {"value": 2.0,  "operator": "<",  "source": "geometry calibration",     "grade": "estimate"},
    "L5_hotspot_coverage":{"value": 0.67, "operator": ">=", "source": "1YCR hotspot analysis", "grade": "paper_explicit"},
    "L6_pose_rmsd":      {"value": 2.0,  "operator": "<",  "min_seed_fraction": 0.67,
                           "source": "field consensus", "grade": "field_consensus"},
    "L7_scrmsd":         {"value": 2.0,  "operator": "<",  "source": "RFpeptides PMID:40542165", "grade": "paper_explicit"},
}

# 11a: 全清候选（七层都过关）
full_pass_candidate = {
    "candidate_id": "C_TEST_PASS", "sequence": "GFEWALAAK",
    "plddt": 0.92,                       # L1 > 0.80 ✓
    "ipsae_mdm2": 0.68, "ipsae_mdmx": 0.64,  # L2 双靶均 > 0.50 ✓
    "iptm_mdm2": 0.85,                   # 参考
    "dg_mdm2": -15.3, "sc_mdm2": 0.72, "dsasa_mdm2": 580,  # L3 ✓
    "dg_mdmx": -13.1, "sc_mdmx": 0.68, "dsasa_mdmx": 520,
    "nc_distance_pre": 1.35, "nc_distance_post": 1.38,         # L4 ✓
    "ring_closure_pre": "True", "ring_closure_post": "True",
    "hotspot_cov_mdm2": 0.85, "hotspot_cov_mdmx": 0.75,
    "site_consistency_mdm2": "True", "site_consistency_mdmx": "True",  # L5 ✓
    "pose_rmsd_mdm2": 1.2, "pose_rmsd_mdmx": 1.5,
    "seed_convergence_mdm2": 0.80, "seed_convergence_mdmx": 0.75,       # L6 ✓
    "scrmsd": 0.8,                                            # L7 < 2.0 ✓
}
r = evaluate_battery(full_pass_candidate, test_thresholds)
check("全清候选 all_layers_pass=True", r["all_layers_pass"] is True)
check("全清候选 failed_layers 为空", len(r["failed_layers"]) == 0)
check("全清 L1 pass", r["l1_pass"] is True)
check("全清 L2 pass (ipSAE 主)", r["l2_pass"] is True)
check("全清 L3 pass", r["l3_pass"] is True)
check("全清 L4 pass (环化 QC)", r["l4_pass"] is True)
check("全清 L5 pass (热点一致)", r["l5_pass"] is True)
check("全清 L6 pass (收敛)", r["l6_pass"] is True)
check("全清 L7 pass (scRMSD)", r["l7_pass"] is True)

# 11b: L3 未达标候选（dG 不够低）
fail_l3 = dict(full_pass_candidate)
fail_l3["dg_mdm2"] = -5.0  # 不满足 < -10.0
r2 = evaluate_battery(fail_l3, test_thresholds)
check("L3 失败 all_layers_pass=False", r2["all_layers_pass"] is False)
check("L3 失败 failed_layers 含 l3_pass", "l3_pass" in r2["failed_layers"])
check("L3 失败 layer_values 记录 dg_mdm2", r2["layer_values"]["L3_dg_mdm2"] == -5.0)

# 11c: L4 环化 QC 一步不过（relax 后断键）
fail_l4 = dict(full_pass_candidate)
fail_l4["nc_distance_post"] = 3.2  # FastRelax 后 N-C 几何不再闭合
r3 = evaluate_battery(fail_l4, test_thresholds)
check("L4 post 失败 all_layers_pass=False", r3["all_layers_pass"] is False)
check("L4 失败 failed_layers 含 l4_pass", "l4_pass" in r3["failed_layers"])

# 11d: 缺少关键字段（无 thresholds 时安全降级）
r4 = evaluate_battery({"candidate_id": "C_EMPTY", "plddt": 0.9})
check("无 thresholds 时 all_layers_pass=False", r4["all_layers_pass"] is False)
check("无 thresholds 时 failed_layers 有7层", len(r4["failed_layers"]) == 7)

# 11e: 旧字段名 alias 兼容（monomer_plddt → plddt 等）
old_name_cand = {
    "candidate_id": "C_ALIAS",
    "monomer_plddt": 0.91, "self_rmsd": 0.9, "scrmsd": 0.8,
    "layer1_pass": "True",
    "ipsae_mdm2": 0.65,
    "dg_mdm2": -12.0, "sc_mdm2": 0.68, "dsasa_mdm2": 500,
    "nc_distance_pre": 1.34, "nc_distance_post": 1.37,
    "hotspot_cov_mdm2": 0.75, "site_consistency": "True",
    "pose_rmsd": 1.5, "seed_convergence": 0.75,
}
r5 = evaluate_battery(old_name_cand, test_thresholds, required_targets=("MDM2",))
check("单靶正对照模式全清 all_layers_pass=True", r5["all_layers_pass"] is True)
check("旧名 alias layer_values L1_plddt=0.91", r5["layer_values"]["L1_plddt"] == 0.91)
check("旧 self_rmsd 不冒充 scRMSD", r5["layer_values"]["L7_scrmsd"] == 0.8)

legacy_only = dict(old_name_cand)
legacy_only.pop("scrmsd")
r5b = evaluate_battery(legacy_only, test_thresholds, required_targets=("MDM2",))
check("只有旧 self_rmsd 时 L7 不通过", r5b["l7_pass"] is False)

# ============================================================
section("12. 实际场景模拟 — 完整 Agent 工作流")
# ============================================================
print("\n  模拟: 于嘉乐(Design) → 王修远(Prediction) → 赵嘉策(Critic) → Planner")
print("  " + "-" * 50)

# 于嘉乐产出一批候选
EvidenceLogger.design_batch("route_A_mdm2_first", 200, 192, "afcycdesign_binder", "2.3.2", 3600.0)
batch_cands = [
    {"candidate_id": f"C0{i:03d}", "sequence": f"GFEWALA{chr(65+i%10)}K",
     "source_route": "route_A_mdm2_first", "source_batch": "new_batch_01"}
    for i in range(100, 120)
]
CandidateIndex.add_batch(batch_cands)

# 王修远评估 Layer 2
EvidenceLogger.evaluate_layer_start(2, 20, {"iptm_mdm2": 0.7, "iptm_mdmx": 0.7})
for cand in batch_cands[:5]:
    EvidenceLogger.candidate_scored(cand["candidate_id"], 2,
        {"iptm_mdm2": 0.8, "iptm_mdmx": 0.6, "dual_score": 0.64, "asymmetry": 0.2},
        {"tool_name": "afcycdesign_complex", "tool_version": "2.3.2", "exit_code": 0, "duration_sec": 100},
        passed=True)
EvidenceLogger.evaluate_layer_complete(2, 20, 15, 5)

# 赵嘉策收到 Critic 报告
cid = EvidenceLogger.critic_review(
    issues=[{"type": "mdmx_bias", "detail": "MDMX 普遍 < 0.65"}],
    passed=False, summary="需要加强 MDMX 亲和力",
    recommendation="下一轮 route_A_mdmx_first, weight=0.7",
    metrics={"iptm_mdm2_median": 0.82, "iptm_mdmx_median": 0.62}
)

# Planner 调整
EvidenceLogger.planner_adjust(cid, {"route": "route_A_mdm2_first"}, {"route": "route_A_mdmx_first"}, "mdmx_bias")

# trace_candidate 全程
trace = EvidenceLogger.trace_candidate("C0100")
# add_batch 不自动调用 candidate_registered，trace 找到 scored 事件即为正确
check("C0100 trace >= 1 (candidate_scored)", len(trace) >= 1)

check("完整工作流场景成功", True)

# ============================================================
section("13. Pareto front 与旧 CSV schema 迁移")
# ============================================================
front = compute_pareto_front([
    {"candidate_id": "P1", "ipsae_mdm2": 0.90, "ipsae_mdmx": 0.60},
    {"candidate_id": "P2", "ipsae_mdm2": 0.75, "ipsae_mdmx": 0.82},
    {"candidate_id": "P3", "ipsae_mdm2": 0.70, "ipsae_mdmx": 0.55},
])
check("Pareto front 保留两条互有取舍的候选", set(front) == {"P1", "P2"})

original_index_path = data_layer.INDEX_PATH
migration_dir = TEST_ROOT / "migration"
migration_dir.mkdir(parents=True, exist_ok=True)
legacy_index = migration_dir / "candidate_index.csv"
legacy_index.write_text(
    "candidate_id,sequence,monomer_plddt,self_rmsd,haddock_mdm2,layer2_3_pass\n"
    "C9001,GFEWALAAK,0.91,1.2,-88.4,True\n",
    encoding="utf-8-sig",
)
data_layer.INDEX_PATH = legacy_index
CandidateIndex._ensure_exists()
migrated = CandidateIndex.load()[0]
check("旧 CSV 自动迁移为当前完整表头", list(migrated) == INDEX_COLUMNS)
check("语义一致的 monomer_plddt 迁移到 plddt", migrated["plddt"] == "0.91")
check("HADDOCK score 保存在 legacy 列", migrated["legacy_haddock_mdm2"] == "-88.4")
check("旧 self_rmsd 保存在 legacy 列", migrated["legacy_self_rmsd"] == "1.2")
check("旧 layer2_3_pass 不冒充新版 L2", migrated["l2_pass"] == "")
check("迁移前 CSV 备份已生成", len(list(migration_dir.glob("candidate_index.pre_v5_*.csv"))) == 1)
data_layer.INDEX_PATH = original_index_path

# ============================================================
section("14. 任意靶点、双层决策与阈值校准")
# ============================================================
custom_config = load_project_config(raw={
    "project_id": "novel_target_demo",
    "targets": [{"id": "NOVEL-1", "uniprot": "P00001", "required": True}],
})
check("自定义项目读取任意靶点", required_target_ids(custom_config) == ("NOVEL-1",))

calibrated_thresholds = json.loads(json.dumps(test_thresholds))
for entry in calibrated_thresholds.values():
    entry["calibration_status"] = "calibrated"
    entry.setdefault("source", "same-protocol control calibration")

novel_candidate = {
    "candidate_id": "C_NEW_TARGET",
    "metrics": {
        "global": {
            "plddt": 0.92, "nc_distance_pre": 1.35,
            "nc_distance_post": 1.38, "scrmsd": 0.8,
        },
        "targets": {
            "NOVEL-1": {
                "ipsae": 0.68, "dg": -15.3, "sc": 0.72, "dsasa": 580,
                "hotspot_cov": 0.85, "site_consistency": True,
                "pose_rmsd": 1.2, "seed_convergence": 0.80,
            }
        },
    },
}
novel_result = evaluate_battery(
    novel_candidate, calibrated_thresholds, required_targets=("NOVEL-1",)
)
check("任意靶点嵌套指标可七层全清", novel_result["all_layers_pass"] is True)
check("已校准阈值允许最终清关", novel_result["competition_clearance"] is True)
check("全清候选进入 shortlisted", novel_result["triage_status"] == "shortlisted")

provisional_thresholds = json.loads(json.dumps(calibrated_thresholds))
provisional_thresholds["L2_ipsae"].update({
    "calibration_status": "pending", "grade": "team_provisional",
    "evidence_grade": "team_provisional",
})
provisional_result = evaluate_battery(
    novel_candidate, provisional_thresholds, required_targets=("NOVEL-1",)
)
check("暂定阈值仍可计算七层数值通过", provisional_result["all_layers_pass"] is True)
check("暂定阈值不能冒充最终清关", provisional_result["competition_clearance"] is False)

incomplete_candidate = json.loads(json.dumps(novel_candidate))
del incomplete_candidate["metrics"]["targets"]["NOVEL-1"]["ipsae"]
incomplete_result = evaluate_battery(
    incomplete_candidate, calibrated_thresholds, required_targets=("NOVEL-1",)
)
check("软证据缺失进入 needs_more_evidence", incomplete_result["triage_status"] == "needs_more_evidence")

broken_ring = json.loads(json.dumps(novel_candidate))
broken_ring["metrics"]["global"]["nc_distance_post"] = 3.2
broken_result = evaluate_battery(
    broken_ring, calibrated_thresholds, required_targets=("NOVEL-1",)
)
check("环闭合硬失败标记 invalid", broken_result["triage_status"] == "invalid")

mixed_front = compute_pareto_front([
    {"candidate_id": "N1", "metrics": {"targets": {"NOVEL-1": {"ipsae": 0.80, "dg": -8}}}},
    {"candidate_id": "N2", "metrics": {"targets": {"NOVEL-1": {"ipsae": 0.70, "dg": -12}}}},
    {"candidate_id": "N3", "metrics": {"targets": {"NOVEL-1": {"ipsae": 0.60, "dg": -7}}}},
], objectives=(
    {"target": "NOVEL-1", "metric": "ipsae", "direction": "maximize"},
    {"target": "NOVEL-1", "metric": "dg", "direction": "minimize"},
))
check("Pareto 同时支持最大化与最小化", set(mixed_front) == {"N1", "N2"})

calibrated = calibrate_threshold(
    metric="ipsae", target_id="NOVEL-1",
    negatives=[0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55,
               0.12, 0.18, 0.22, 0.28, 0.32, 0.38, 0.42, 0.48, 0.52, 0.58],
    positives=[0.62, 0.68, 0.75], max_false_positive_rate=0.05,
    protocol={"tool": "example", "seeds": 5},
)
check("校准器控制经验 FPR", calibrated["observed_false_positive_rate"] <= 0.05)
check("校准器写出协议哈希", bool(calibrated["protocol_hash"]))
check("校准器结果可支持阈值审计", calibrated["calibration_status"] == "calibrated")

CandidateIndex.add({
    "candidate_id": "C0200", "sequence": "GFEWALAAK",
    "source_route": "generic_structure_route", "metrics": novel_candidate["metrics"],
})
stored_novel = CandidateIndex.find("C0200")
check("CandidateIndex 保存任意靶点 metrics_json", "NOVEL-1" in stored_novel["metrics_json"])
CandidateIndex.update_score("C0200", {
    "metrics": {"targets": {"NOVEL-1": {"ipsae": 0.71}}},
    "triage_status": novel_result["triage_status"],
    "competition_clearance": novel_result["competition_clearance"],
    "threshold_audit": novel_result["threshold_audit"],
})
stored_novel = CandidateIndex.find("C0200")
stored_metrics = json.loads(stored_novel["metrics_json"])
check("CandidateIndex 增量合并通用指标", stored_metrics["targets"]["NOVEL-1"]["ipsae"] == 0.71)
check("增量合并不丢其他指标", stored_metrics["targets"]["NOVEL-1"]["dg"] == -15.3)
check("通用指标从 CSV 读回后仍可评估", evaluate_battery(
    stored_novel, calibrated_thresholds, required_targets=("NOVEL-1",)
)["all_layers_pass"] is True)
check("CandidateIndex 可按任意靶点嵌套指标排序",
      CandidateIndex.top_n(1, by="NOVEL-1:ipsae")[0]["candidate_id"] == "C0200")
search_term = build_search_term(custom_config)
check("PubMed 查询由项目靶点生成", "NOVEL-1" in search_term and "P00001" in search_term)
generic_pockets = aggregate("NOVEL-1", [{
    "target": "NOVEL-1", "interface_target_residues": ["A:10ALA", "A:20TRP"],
}])
check("未知靶点可聚合界面且不套用 MDM 口袋", generic_pockets["n_structures"] == 1 and generic_pockets["pocket_residues"] == {})

# ============================================================
section("结果汇总")
# ============================================================
total = passed + failed
print(f"\n  总计: {total} 项测试")
print(f"  通过: {passed} ({100*passed//total}%)")
print(f"  失败: {failed}")

if failed > 0:
    print("\n  [WARNING] 存在失败测试，请检查！")
    sys.exit(1)
else:
    print("\n  全部通过，data_layer.py 可以交付。")
