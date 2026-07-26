"""
Research Agent - MDM2/MDMX 靶点调研管线
7 步: RCSB Search -> GraphQL Enrich -> biotite interface -> aggregate pockets ->
      superpose analyze -> PubMed -> LLM extract
每步挂 EvidenceLogger tool_trace。biotite 失败时自动回退到预置常量。
"""

import json, os, subprocess, sys, time, hashlib
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
DATA_DIR = ROOT / "data"
CACHE_PATH = DATA_DIR / "_research_cache.json"

from data_layer import State, EvidenceLogger

# ===== 预置常量（biotite 失败时兜底）=====
TARGETS = {
    "MDM2": {"uniprot": "Q00987", "reference_pdb": ["1YCR", "4HG7", "3V3B"],
             "pocket_residues": {
                 "Phe19_pocket": ["Gly58","Ile61","Met62","Tyr67","Gln72","Val75","Val93"],
                 "Trp23_pocket": ["Leu54","Leu57","Gly58","Ile61","Val93"],
                 "Leu26_pocket": ["Leu54","Val93","His96","Ile99","Tyr100"]}},
    "MDMX": {"uniprot": "O15151", "reference_pdb": ["3DAB", "3LBK"],
             "pocket_residues": {
                 "Phe19_pocket": ["Gly57","Ile60","Met61","Tyr66","Gln71","Val74","Val92"],
                 "Trp23_pocket": ["Met53","Leu56","Gly57","Ile60","Val92","Leu98"],
                 "Leu26_pocket": ["Met53","Val92","Pro95","Leu98","Tyr99"]}},
}
POCKET_DIFFERENCES = {
    "_method": "biotite heavy-atom<4A, 43+12 structures <=2.8A, CA RMSD 1.88A/85 residues",
    "Phe19_pocket": {"MDM2_residues": TARGETS["MDM2"]["pocket_residues"]["Phe19_pocket"],
                     "MDMX_residues": TARGETS["MDMX"]["pocket_residues"]["Phe19_pocket"],
                     "design_rule": "Phe volume or smaller. Pocket conserved across MDM2/MDMX, no major steric difference."},
    "Trp23_pocket": {"MDM2_residues": TARGETS["MDM2"]["pocket_residues"]["Trp23_pocket"],
                     "MDMX_residues": TARGETS["MDMX"]["pocket_residues"]["Trp23_pocket"],
                     "design_rule": "L-Trp invariant shared anchor. MDMX Met53 (vs MDM2 Leu54) is bulkier, pocket tighter."},
    "Leu26_pocket": {"MDM2_residues": TARGETS["MDM2"]["pocket_residues"]["Leu26_pocket"],
                     "MDMX_residues": TARGETS["MDMX"]["pocket_residues"]["Leu26_pocket"],
                     "design_rule": "Downsize to small aliphatic (Leu/Val/Abu). MDMX Met53+Pro95 dual compression vs MDM2 Leu54+His96."},
}
KNOWN_DUAL_BINDERS = [
    {"name":"PMI","type":"linear peptide","sequence":"TSFAEYWNLLSP","kd_mdm2":"low nanomolar","kd_mdmx":"low nanomolar","pmid":"34589387"},
    {"name":"PMI-M3","type":"linear peptide","sequence":"LTFLEYWAQLMQ","kd_mdm2":"low picomolar","kd_mdmx":"low picomolar","pmid":"34589387"},
    {"name":"ATSP-7041","type":"stapled peptide","kd_mdm2":"Ki ~0.9 nM","kd_mdmx":"Ki ~2.3 nM","pmid":"23946421"},
    {"name":"ALRN-6924","type":"stapled peptide (clinical)","kd_mdm2":"nanomolar","kd_mdmx":"nanomolar","pmid":"37439511"},
    {"name":"pDI","type":"linear peptide","sequence":"LTFEHYWAQLTS","kd_mdm2":"~40 nM","kd_mdmx":"sub-micromolar","pmid":"19910468"},
    {"name":"pDI6W","type":"linear peptide","sequence":"LTFEHWWAQLTS","pmid":"19910468"},
    {"name":"pDIQ","type":"linear peptide","sequence":"ETFEHWWSQLLS","kd_mdm2":"IC50 8 nM","kd_mdmx":"IC50 110 nM","pmid":"19910468"},
    {"name":"M3-2K","type":"linear peptide","sequence":"KLTFLEYWAQLMQK","pmid":"34589387"},
]
LITERATURE_REFS = [
    {"pmid":"34589387"},{"pmid":"23946421"},{"pmid":"37439511"},{"pmid":"34301750"},{"pmid":"19910468"},
]
DESIGN_STRATEGY_SUMMARY = "Trp23=L-Trp invariant anchor (MDMX Met53 tighter than MDM2 Leu54). Phe19<=Phe volume (pocket conserved). Leu26=small aliphatic (MDMX Met53+Pro95 dual compression). Natural-AA cyclic on helical FxxWxxxL geometry."
VERIFIED_PEPTIDE_COMPLEXES = {
    "MDM2": ["1T4F","1YCR","2GV2","3EQS","3G03","3IUX","3IWY","3JZR","3LNZ","3TPX","3V3B","4HFZ"],
    "MDMX": ["3DAB","3EQY","3FDO","3FE7","3FEA","3JZO","3JZP","4RXZ","5VK1","7KJN","8IA5","3JZQ"],
}
DATA_QUALITY_ALERT = "4HG7/3LBK are small-molecule complexes, not peptide. Verified: 1YCR/3V3B(MDM2), 3DAB(MDMX)."

# ===== 七层指标电池阈值（文献兜底值，最终以正对照标定为准）=====
# 来源：RFpeptides (Rettie et al., Nat Chem Biol 2025)、DeeCamp kickoff 指导
DEFAULT_THRESHOLDS = {
    "L1_plddt":            {"value": 0.8,  "operator": ">",  "unit": None,
                            "source": "RFpeptides paper (Nat Chem Biol 2025)", "confidence": "high"},
    "L2_ipsae":            {"value": 0.55, "operator": ">",  "unit": None,
                            "source": "ipSAE literature estimate (Dunbrack); 待正对照标定", "confidence": "low"},
    "L3_dg":               {"value": -10,  "operator": "<",  "unit": "kcal/mol",
                            "source": "PRODIGY/interface energy 经验值", "confidence": "medium"},
    "L3_sc":               {"value": 0.6,  "operator": ">",  "unit": None,
                            "source": "Rosetta shape complementarity 经验值", "confidence": "medium"},
    "L3_dsasa":            {"value": 400,  "operator": ">",  "unit": "A^2",
                            "source": "界面埋藏面积经验值", "confidence": "medium"},
    "L4_nc_term_dist":     {"value": 2.0,  "operator": "<",  "unit": "A",
                            "source": "肽键~1.33A；N/C端距离<2.0A视为闭合", "confidence": "high"},
    "L5_hotspot_coverage": {"value": 0.67, "operator": ">=", "unit": None,
                            "source": ">=2/3 设计热点口袋被覆盖", "confidence": "medium"},
    "L6_pose_rmsd":        {"value": 2.0,  "operator": "<",  "unit": "A",
                            "source": "多预测器/多seed结合pose收敛", "confidence": "medium"},
    "L7_scrmsd":           {"value": 2.0,  "operator": "<",  "unit": "A",
                            "source": "RFpeptides paper bb-RMSD<2.0A", "confidence": "high"},
}
THRESHOLDS_CACHE = DATA_DIR / "_thresholds_cache.json"


def _run_script(script_name, input_data=None, extra_args=None):
    script_path = SCRIPTS_DIR / script_name
    if not script_path.exists():
        raise FileNotFoundError(f"Script not found: {script_path}")
    python_exe = sys.executable
    t0 = time.time()
    cmd = [python_exe, "-m", f"scripts.{script_name.replace('.py', '')}"]
    if extra_args:
        cmd.extend(extra_args)
    proc = subprocess.run(
        cmd,
        input=json.dumps(input_data) if input_data else None,
        capture_output=True, text=True, timeout=600, cwd=str(ROOT),
        env={**os.environ},
    )
    duration = time.time() - t0
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()[:500]
    exit_code = proc.returncode
    output_hash = hashlib.md5(stdout.encode()).hexdigest()[:12] if stdout else ""
    try:
        result = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        result = {"stdout": stdout[:1000], "parse_error": True}
    return result, stderr, exit_code, duration, output_hash


def _build_dynamic_pockets(aggregate_result, superpose_result):
    """从 aggregate + superpose 输出构建 pocket_differences。
    
    如果 biotite 成功计算了界面残基，用动态数据；
    否则返回 None（调用方回退到常量）。
    """
    n_mdm2 = aggregate_result.get("n_mdm2_structures", 0)
    n_mdmx = aggregate_result.get("n_mdmx_structures", 0)
    if n_mdm2 == 0 and n_mdmx == 0:
        return None  # biotite 没产出，回退常量

    # 从 aggregate 提取每靶点的口袋残基
    mdm2_agg = aggregate_result.get("MDM2", {})
    mdmx_agg = aggregate_result.get("MDMX", {})
    
    # 用 consensus 残基（如果有的话），否则用 pocket_residues 参考
    def _extract_residues(agg, pocket_name):
        consensus = agg.get("pocket_consensus", {}).get(pocket_name, [])
        if consensus:
            # 格式 "A:58GLY" -> "Gly58"
            cleaned = []
            for r in consensus:
                # "A:58GLY" -> res_name="GLY", res_id=58 -> "Gly58"
                parts = r.split(":")
                if len(parts) == 2:
                    res_info = parts[1]  # "58GLY"
                    # 分离数字和字母
                    import re
                    m = re.match(r'(\d+)([A-Z]+)', res_info)
                    if m:
                        res_id, res_name = m.group(1), m.group(2)
                        # 三字母转首字母大写
                        clean_name = res_name.capitalize()
                        cleaned.append(f"{clean_name}{res_id}")
            return cleaned if cleaned else agg.get("pocket_residues", {}).get(pocket_name, [])
        return agg.get("pocket_residues", {}).get(pocket_name, [])

    # 从 superpose 提取方法信息
    rmsd = superpose_result.get("ca_rmsd_A", "?") if superpose_result else "?"
    n_ca = superpose_result.get("n_ca_atoms_superposed", "?") if superpose_result else "?"
    method = f"biotite heavy-atom<4A, {n_mdm2}+{n_mdmx} structures, CA RMSD {rmsd}A/{n_ca} residues"

    return {
        "_method": method,
        "_source": "dynamic_biotite",
        "Phe19_pocket": {
            "MDM2_residues": _extract_residues(mdm2_agg, "Phe19_pocket"),
            "MDMX_residues": _extract_residues(mdmx_agg, "Phe19_pocket"),
            "design_rule": "Phe volume or smaller. Pocket conserved across MDM2/MDMX, no major steric difference.",
        },
        "Trp23_pocket": {
            "MDM2_residues": _extract_residues(mdm2_agg, "Trp23_pocket"),
            "MDMX_residues": _extract_residues(mdmx_agg, "Trp23_pocket"),
            "design_rule": "L-Trp invariant shared anchor. MDMX Met53 (vs MDM2 Leu54) is bulkier, pocket tighter.",
        },
        "Leu26_pocket": {
            "MDM2_residues": _extract_residues(mdm2_agg, "Leu26_pocket"),
            "MDMX_residues": _extract_residues(mdmx_agg, "Leu26_pocket"),
            "design_rule": "Downsize to small aliphatic (Leu/Val/Abu). MDMX Met53+Pro95 dual compression vs MDM2 Leu54+His96.",
        },
        "_superpose": {
            "rmsd_A": rmsd,
            "n_ca_atoms": n_ca,
            "sasa": superpose_result.get("sasa", {}) if superpose_result else {},
        } if superpose_result else {},
    }


def _run_pipeline():
    skip_heavy = os.environ.get("SKIP_BIOTITE", "").lower() in ("1", "true", "yes")

    # ===== Step 1: RCSB Search =====
    print("[research] Step 1/7: RCSB Search API...")
    sr, se, sc, sd, sh = _run_script("search_pdb.py")
    EvidenceLogger.log("research", "tool_call", {
        "tool_name": "rcsb_search_api", "tool_version": "v2",
        "output_hash": sh, "exit_code": sc, "duration_sec": round(sd, 1),
        "stdout_snippet": f"MDM2={sr.get('n_mdm2',0)} MDMX={sr.get('n_mdmx',0)}",
    }, targets=["both"], phase="research")

    # ===== Step 2: GraphQL Enrich =====
    print("[research] Step 2/7: RCSB GraphQL...")
    er, ee, ec, ed, eh = _run_script("enrich_pdb.py", sr)
    n_peptide = er.get("n_peptide_complexes", 0)
    EvidenceLogger.log("research", "tool_call", {
        "tool_name": "rcsb_graphql_api", "output_hash": eh, "exit_code": ec,
        "duration_sec": round(ed, 1),
        "stdout_snippet": f"peptide_complexes={n_peptide}",
    }, targets=["both"], phase="research")

    # ===== Steps 3-5: biotite =====
    pocket_differences = POCKET_DIFFERENCES  # 默认常量
    dynamic_pdb_list = []  # 动态 PDB 列表（来自 enrich）
    n_mdm2_structures = 0
    n_mdmx_structures = 0

    # 从 enrich 提取动态 PDB 列表
    for entry in er.get("peptide_complexes", []):
        dynamic_pdb_list.append(entry["pdb_id"])
        if entry.get("target") == "MDM2":
            n_mdm2_structures += 1
        elif entry.get("target") == "MDMX":
            n_mdmx_structures += 1

    if skip_heavy:
        print("[research] Steps 3-5: skipped (SKIP_BIOTITE=1)")
    else:
        # Step 3: biotite interface
        print("[research] Step 3/7: biotite interface...")
        iface_result = {"with_interface": []}
        try:
            ir, ie2, ic, id_, ih = _run_script("compute_interface.py", er)
            n_iface = ir.get("n_with_interface", 0)
            EvidenceLogger.log("research", "tool_call", {
                "tool_name": "biotite", "output_hash": ih, "exit_code": ic,
                "duration_sec": round(id_, 1),
                "stdout_snippet": f"with_interface={n_iface}",
            }, targets=["both"], phase="research")
            iface_result = ir
        except Exception as e:
            EvidenceLogger.error("research", "tool_failure", f"biotite: {e}", recovery="fallback")
            iface_result = {"with_interface": []}

        # Step 4: aggregate pockets
        print("[research] Step 4/7: aggregate pockets...")
        pr, pe2, pc, pd_, ph = _run_script("aggregate_pockets.py", iface_result)
        n_agg_mdm2 = pr.get("n_mdm2_structures", 0)
        n_agg_mdmx = pr.get("n_mdmx_structures", 0)
        EvidenceLogger.log("research", "tool_call", {
            "tool_name": "aggregate_pockets", "output_hash": ph, "exit_code": pc,
            "duration_sec": round(pd_, 1),
            "stdout_snippet": f"MDM2={n_agg_mdm2}struct MDMX={n_agg_mdmx}struct",
        }, targets=["both"], phase="research")

        # Step 5: superpose
        print("[research] Step 5/7: superposition...")
        spr = {}
        try:
            spr, spe2, spc, spd_, sph = _run_script("superpose_analyze.py", pr)
            EvidenceLogger.log("research", "tool_call", {
                "tool_name": "biotite_superimpose", "output_hash": sph, "exit_code": spc,
                "duration_sec": round(spd_, 1),
                "stdout_snippet": f"rmsd={spr.get('ca_rmsd_A','?')}A n_ca={spr.get('n_ca_atoms_superposed','?')}",
            }, targets=["both"], phase="research")
        except Exception as e:
            EvidenceLogger.error("research", "tool_failure", f"superpose: {e}", recovery="fallback")

        # 用动态数据构建 pocket_differences
        dynamic = _build_dynamic_pockets(pr, spr)
        if dynamic:
            pocket_differences = dynamic
            n_mdm2_structures = n_agg_mdm2
            n_mdmx_structures = n_agg_mdmx
            print(f"[research] Using dynamic pockets: MDM2={n_mdm2_structures}struct MDMX={n_mdmx_structures}struct")
        else:
            print("[research] biotite produced no interface data, using constant pockets")

    # ===== Step 6: PubMed =====
    print("[research] Step 6/7: PubMed...")
    pmr, pme, pmc, pmd_, pmh = _run_script("pubmed_search.py")
    EvidenceLogger.log("research", "tool_call", {
        "tool_name": "pubmed_eutils", "output_hash": pmh, "exit_code": pmc,
        "duration_sec": round(pmd_, 1),
        "stdout_snippet": f"n_papers={pmr.get('n_total',0)}",
    }, targets=["both"], phase="research")

    # ===== Step 7: LLM extract =====
    print("[research] Step 7/7: LLM extract (concurrent)...")
    llm_binders = KNOWN_DUAL_BINDERS
    try:
        lr, le2, lc, ld_, lh = _run_script("llm_extract.py", pmr, extra_args=["--concurrency", "3"])
        if lr and "error" not in lr:
            extracted = lr.get("known_binders", [])
            if extracted:
                llm_binders = extracted
                print(f"[research] LLM found {len(extracted)} binders from {lr.get('n_papers_processed',0)} papers")
        EvidenceLogger.log("research", "tool_call", {
            "tool_name": "llm_extract_concurrent",
            "tool_version": lr.get("llm_model", "unknown") if isinstance(lr, dict) else "unknown",
            "output_hash": lh, "exit_code": lc, "duration_sec": round(ld_, 1),
            "stdout_snippet": f"binders={len(llm_binders)} papers={lr.get('n_papers_processed',0) if isinstance(lr, dict) else '?'}",
        }, targets=["both"], phase="research")
    except Exception as e:
        EvidenceLogger.error("research", "tool_failure", f"LLM: {e}", recovery="fallback to constants")

    # ===== Step 8: 阈值文献检索 =====
    print("[research] Step 8/8: threshold literature research...")
    thresholds = DEFAULT_THRESHOLDS.copy()
    try:
        tr, te2, tc, td_, th = _run_script("threshold_research.py", extra_args=["--concurrency", "4"])
        lit = tr.get("metric_battery", {})
        n_found = tr.get("_meta", {}).get("n_found", 0)
        # 用文献值覆盖默认值（只覆盖 found 且有具体数值的）
        for layer, info in lit.items():
            if info.get("found") and info.get("value") is not None:
                thresholds[layer] = {
                    "value": info["value"],
                    "operator": info.get("operator", ">"),
                    "unit": info.get("unit"),
                    "source": f"literature: {info.get('source_papers', ['?'])[0] if info.get('source_papers') else info.get('pmids', ['?'])}",
                    "confidence": info.get("confidence", "medium"),
                    "pmids": info.get("pmids", []),
                }
        THRESHOLDS_CACHE.write_text(json.dumps(thresholds, ensure_ascii=False, indent=2), encoding="utf-8")
        EvidenceLogger.log("research", "tool_call", {
            "tool_name": "threshold_research", "output_hash": th, "exit_code": tc,
            "duration_sec": round(td_, 1),
            "stdout_snippet": f"layers_found={n_found}/{len(lit)}",
        }, targets=["both"], phase="research")
        print(f"[research] Thresholds: {n_found} layers from literature")
    except Exception as e:
        EvidenceLogger.error("research", "tool_failure", f"threshold_research: {e}",
                             recovery="fallback to DEFAULT_THRESHOLDS")
        print(f"[research] threshold_research failed, using defaults: {e}")

    # ===== 组装结果 =====
    result = {
        "targets": TARGETS.copy(),
        "pocket_differences": pocket_differences,
        "known_dual_binders": llm_binders,
        "design_strategy_summary": DESIGN_STRATEGY_SUMMARY,
        "data_quality_alert": DATA_QUALITY_ALERT,
        "literature_refs": LITERATURE_REFS,
        "thresholds": thresholds,
        "_pipeline_meta": {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "pocket_source": pocket_differences.get("_source", "constant"),
            "n_pdb_complexes": len(dynamic_pdb_list),
            "n_mdm2_structures": n_mdm2_structures,
            "n_mdmx_structures": n_mdmx_structures,
            "dynamic_pdb_list": dynamic_pdb_list[:20],
        },
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[research] Pipeline done. pocket_source={result['_pipeline_meta']['pocket_source']}")
    return result


def run(state=None, force_recompute=False, skip_pipeline=False):
    if state is None:
        state = State.load()

    if force_recompute:
        pipeline_result = _run_pipeline()
    elif skip_pipeline or CACHE_PATH.exists():
        if CACHE_PATH.exists():
            pipeline_result = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            print(f"[research] Using cache: {CACHE_PATH}")
        else:
            pipeline_result = _run_pipeline()
    else:
        pipeline_result = _run_pipeline()

    targets = state.get("targets", {})
    for name, info in pipeline_result.get("targets", TARGETS).items():
        targets.setdefault(name, {}).update(info)

    result = {
        "targets": targets,
        "pocket_differences": pipeline_result.get("pocket_differences", POCKET_DIFFERENCES),
        "known_dual_binders": pipeline_result.get("known_dual_binders", KNOWN_DUAL_BINDERS),
        "design_strategy_summary": pipeline_result.get("design_strategy_summary", DESIGN_STRATEGY_SUMMARY),
        "data_quality_alert": pipeline_result.get("data_quality_alert", DATA_QUALITY_ALERT),
        "thresholds": pipeline_result.get("thresholds", DEFAULT_THRESHOLDS),
    }
    State.update(result)

    # 用动态数据构建 hotspot_analysis
    meta = pipeline_result.get("_pipeline_meta", {})
    pocket_diff = result["pocket_differences"]
    pdb_list = meta.get("dynamic_pdb_list", [])
    if not pdb_list:
        pdb_list = VERIFIED_PEPTIDE_COMPLEXES["MDM2"] + VERIFIED_PEPTIDE_COMPLEXES["MDMX"]

    hotspot_analysis = {
        "pdb_list": pdb_list,
        "n_mdm2_peptide_complexes": meta.get("n_mdm2_structures", len(VERIFIED_PEPTIDE_COMPLEXES["MDM2"])),
        "n_mdmx_peptide_complexes": meta.get("n_mdmx_structures", len(VERIFIED_PEPTIDE_COMPLEXES["MDMX"])),
        "method": pocket_diff.get("_method", ""),
        "pocket_source": meta.get("pocket_source", "constant"),
        "pockets": pocket_diff,
        "data_quality_alert": DATA_QUALITY_ALERT,
    }
    EvidenceLogger.research_complete(
        hotspot_analysis=hotspot_analysis,
        known_binders=result["known_dual_binders"],
        refs=LITERATURE_REFS,
    )
    return result


def recompute():
    return run(force_recompute=True)


if __name__ == "__main__":
    out = run()
    n = len(out.get("known_dual_binders", []))
    print(f"[research] State updated. {n} dual binders. Phase={State.load().get('phase')}")
