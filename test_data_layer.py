"""
data_layer.py 完整集成测试
覆盖所有 Agent 使用场景 + 边界情况
"""
import json, sys, os, csv, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# v5 修复（吸取 PR #4）：测试用独立临时目录，不污染项目 runtime 文件
TEST_ROOT = Path(tempfile.mkdtemp(prefix="cycpep-data-layer-test-"))
os.environ["CYCPEP_DATA_DIR"] = str(TEST_ROOT / "data")
os.environ["CYCPEP_EVIDENCE_DIR"] = str(TEST_ROOT / "evidence")

os.chdir(str(ROOT))

import data_layer
DATA_DIR = data_layer.DATA_DIR
EVIDENCE_DIR = data_layer.EVIDENCE_DIR

from data_layer import (
    State, EvidenceLogger, CandidateIndex, file_hash, sanitize_id,
    evaluate_battery, INDEX_COLUMNS, _normalize_thresholds,
    _THRESHOLD_KEY_ALIASES,
)

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
    "iptm_mdm2": 0.84, "iptm_mdmx": 0.72, "dual_score": 0.72, "asymmetry": 0.12,
    "layer2_3_pass": "True"
})
r2 = CandidateIndex.find("C0042")
check("update_score 后 iptm_mdm2=0.84", r2["iptm_mdm2"] == "0.84")
# v5: layer2_3_pass 已 alias 到 l2_pass
check("update_score 后 l2_pass=True (旧名 layer2_3_pass alias)", r2["l2_pass"] == "True")
check("update_score 后 plddt=0.88 (旧名 monomer_plddt alias)", r2["plddt"] == "0.88")
check("update_score 后 scrmsd=1.1 (旧名 self_rmsd alias)", r2["scrmsd"] == "1.1")

CandidateIndex.update_score("C0088", {
    "monomer_plddt": 0.79, "layer1_pass": "True",
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
check("stats iptm_mdm2_median=0.84", stats["iptm_mdm2_median"] == 0.84)
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
# v5: INDEX_COLUMNS 已扩展为七层电池 schema（~48 列），直接用源定义做校验
check(f"CSV 所有 {len(INDEX_COLUMNS)} 列存在", all(col in reader.fieldnames for col in INDEX_COLUMNS))
# 抽查关键新列
for must_col in ["plddt","l1_pass","ipsae_mdm2","ipsae_mdmx","dg_mdm2","sc_mdm2",
                 "dsasa_mdm2","ring_closure_pre","ring_closure_post","l4_pass",
                 "site_consistency","l5_pass","pose_rmsd","seed_convergence",
                 "l6_pass","scrmsd","l7_pass","all_layers_pass","pareto_front",
                 "synth_pass","adme_net_charge","adme_tpsa","adme_clogp",
                 "adme_chameleonicity","novelty_score"]:
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
    "L5_hotspot_coverage":{"value": 0.67, "operator": ">=", "source": "1YCR hotspot analysis", "grade": "paper_explicit"},
    "L6_pose_rmsd":      {"value": 2.0,  "operator": "<",  "source": "field consensus",         "grade": "field_consensus"},
    "L7_scrmsd":         {"value": 2.0,  "operator": "<",  "source": "RFpeptides PMID:40542165", "grade": "paper_explicit"},
}

# 11a: 全清候选（七层都过关）
full_pass_candidate = {
    "candidate_id": "C_TEST_PASS", "sequence": "GFEWALAAK",
    "plddt": 0.92,                       # L1 > 0.80 ✓
    "ipsae_mdm2": 0.68,                  # L2 > 0.50 ✓
    "iptm_mdm2": 0.85,                   # 参考
    "dg_mdm2": -15.3, "sc_mdm2": 0.72, "dsasa_mdm2": 580,  # L3 ✓
    "ring_closure_pre": "True", "ring_closure_post": "True",  # L4 ✓
    "hotspot_cov_mdm2": 0.85, "site_consistency": "True",     # L5 ✓
    "pose_rmsd": 1.2, "seed_convergence": 0.80,               # L6 ✓
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
fail_l4["ring_closure_post"] = "False"  # FastRelax 破坏了环化
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
    "monomer_plddt": 0.91, "self_rmsd": 0.9,
    "layer1_pass": "True",
    "ipsae_mdm2": 0.65,
    "dg_mdm2": -12.0, "sc_mdm2": 0.68, "dsasa_mdm2": 500,
    "ring_closure_pre": "True", "ring_closure_post": "True",
    "hotspot_cov_mdm2": 0.75, "site_consistency": "True",
    "pose_rmsd": 1.5, "seed_convergence": 0.75,
    # 注意：不显式放 scrmsd，让 self_rmsd 走 alias
}
r5 = evaluate_battery(old_name_cand, test_thresholds)
check("旧名 alias 全清 all_layers_pass=True", r5["all_layers_pass"] is True)
check("旧名 alias layer_values L1_plddt=0.91", r5["layer_values"]["L1_plddt"] == 0.91)
check("旧名 alias layer_values L7_scrmsd=0.9 (self_rmsd→scrmsd)", r5["layer_values"]["L7_scrmsd"] == 0.9)

# ============================================================
section("11b. _normalize_thresholds — key 冗余归一化（regression）")
# ============================================================
# 今天发现的真实 bug：threshold_research.py 同时写出 L4_ring_closure
# 和 L4_nc_term_dist 两条同义 key，evaluate_battery 只读 L4_nc_term_dist，
# 文献值（带 PMID）被静默丢弃，电池用的是经验值那条。
# 此 regression 锁死归一化逻辑：文献优先 + evaluate_battery 认的 key 胜出。
raw_with_dup = {
    "L4_ring_closure":   {"value": 2.0, "operator": "<", "source": "PMID 35274526",
                           "confidence": "high", "pmids": ["35274526"],
                           "evidence_grade": "paper_explicit"},
    "L4_nc_term_dist":   {"value": 2.0, "operator": "<", "source": "经验值",
                           "confidence": "high", "evidence_grade": "estimate"},
    "L6_pose_convergence": {"value": 2.0, "operator": "<", "source": "PMID 35609983",
                            "confidence": "high", "pmids": ["35609983"]},
    "L6_pose_rmsd":      {"value": 2.0, "operator": "<", "source": "经验值",
                           "confidence": "medium"},
    "L1_plddt":          {"value": 0.8, "operator": ">",
                           "source": "PMID 40542165", "confidence": "high"},
}
norm = _normalize_thresholds(raw_with_dup)
check("归一化后只剩 evaluate_battery 认读的 key", set(norm.keys()) == {"L4_nc_term_dist", "L6_pose_rmsd", "L1_plddt", "_conflict_log"})
check("L4 文献值保留 (PMID)", "35274526" in norm["L4_nc_term_dist"]["source"])
check("L6 文献值保留 (PMID)", "35609983" in norm["L6_pose_rmsd"]["source"])
check("L4 旧 key 被丢弃", "L4_ring_closure" not in norm)
check("L6 旧 key 被丢弃", "L6_pose_convergence" not in norm)
check("_conflict_log 记录了冲突处理", "_conflict_log" in norm)

# 11b.b: 只有旧 key、没有新 key 时也归一化过去
only_old = {
    "L4_ring_closure": {"value": 1.8, "operator": "<", "source": "PMID 35274526",
                        "confidence": "high", "pmids": ["35274526"]},
}
norm_only = _normalize_thresholds(only_old)
check("只有旧 key 时也归一化到新 key", "L4_nc_term_dist" in norm_only)
check("只有旧 key 时新 key 内容来自旧 key", norm_only["L4_nc_term_dist"]["value"] == 1.8)

# 11b.c: 空输入安全降级
check("_normalize_thresholds({}) 返回 {}", _normalize_thresholds({}) == {})
check("_normalize_thresholds(None) 返回 {}", _normalize_thresholds(None) == {})

# 11b.d: 无 grade 时按 source 是否带 PMID 推断
inferred = _normalize_thresholds({
    "L4_ring_closure": {"value": 1.8, "source": "PMID 35274526", "operator": "<"},
    "L4_nc_term_dist": {"value": 2.0, "source": "经验值", "operator": "<"},
})
check("无 grade 时优先保留含 PMID 的 source",
      "35274526" in inferred["L4_nc_term_dist"]["source"])

# ============================================================
section("11c. State.sync_thresholds_from_cache — cache→state 合并（regression）")
# ============================================================
# 今天发现的真实 bug：因为 test 开头 rm -f state.json 后没重跑 research
# 合并 cache 回 state，导致 state.json["thresholds"] = {} 下游 Agent 裸奔。
# State.sync_thresholds_from_cache 应该从 _thresholds_cache.json 读出来、
# 归一化后合并回 state.json["thresholds"]。
import tempfile as _tf
cache_file = DATA_DIR / "_thresholds_cache.json"
cache_file.parent.mkdir(parents=True, exist_ok=True)
cache_payload = {
    "L4_ring_closure":   {"value": 2.0, "operator": "<", "source": "PMID 35274526",
                           "confidence": "high", "pmids": ["35274526"]},
    "L4_nc_term_dist":   {"value": 2.0, "operator": "<", "source": "经验值"},
    "L1_plddt":          {"value": 0.8, "operator": ">", "confidence": "high"},
}
cache_file.write_text(json.dumps(cache_payload), encoding="utf-8")

# 预置空 thresholds 的 state.json
State.save(dict(State._default, thresholds={}))
merged = State.sync_thresholds_from_cache()
s = State.load()
check("sync 后 state 含 L4_nc_term_dist key", "L4_nc_term_dist" in s["thresholds"])
check("sync 后 L4 文献值被采纳", "35274526" in s["thresholds"]["L4_nc_term_dist"]["source"])
check("sync 后 L1_plddt 写入", s["thresholds"]["L1_plddt"]["value"] == 0.8)
# evidence 应有一条 thresholds_synced_from_cache 事件
synced_events = EvidenceLogger.filter(event_type="thresholds_synced_from_cache")
check("sync 写了 thresholds_synced_from_cache 证据事件", len(synced_events) >= 1)

# 11c.b: 缺 cache 时安全降级（不抛）
cache_file.unlink()
empty_state = State.save(dict(State._default, thresholds={}))
r = State.sync_thresholds_from_cache()
check("缺 cache 时不抛、保留 state 现状", r == {})

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
