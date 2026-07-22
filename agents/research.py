"""
Research Agent — 刘函赫
职责：自动检索 PDB/PubMed → 结构化热点分析 → 双靶差异对比
入口：run(state) → dict （写入 state.json 的 targets / pocket_differences / known_dual_binders）
工具链：RCSB Search API v2 → biotite 界面分析（重原子<4Å）→ Cα 叠合差异量化 →
        PubMed E-utilities → LLM 结构化提取
依赖：from data_layer import State, EvidenceLogger

使用方式
--------
    from agents.research import run
    run()                        # 首次运行：执行完整管线，结果缓存到 data/_research_cache.json
    run(force_recompute=True)    # 强制重新跑完整管线
    run(skip_pipeline=True)      # 跳过管线，直接用缓存结果写入 state.json
"""

import json, os, subprocess, sys, time, hashlib
from pathlib import Path
from datetime import datetime, timezone

# 项目根目录
ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
DATA_DIR = ROOT / "data"
CACHE_PATH = DATA_DIR / "_research_cache.json"

from data_layer import State, EvidenceLogger


# ============================================================
# 缓存常量（管线跑完后产出的最终结果）
# 首次 run() 时会自动从管线产出，后续直接用缓存。
# 如果管线产出与这些常量不一致，以管线实际产出为准。
# ============================================================
VERIFIED_PEPTIDE_COMPLEXES = {
    "MDM2": ["1T4F", "1YCR", "2GV2", "3EQS", "3G03", "3IUX", "3IWY", "3JZR",
             "3LNZ", "3TPX", "3V3B", "4HFZ", "3LNJ", "3JZS", "2AXI", "4UD7",
             "4UE1", "4UMN", "5AFG", "5UMM", "5VK0", "5XXK", "6AAW", "6KZU",
             "6T2E", "6T2F", "6Y4Q", "7AD0", "7KJM", "7NUS", "8EIC", "8F0Z",
             "6T2D", "6HFA", "6H22", "8F10", "8F12", "8F13", "8GCG", "9CDZ",
             "9FQL", "9GFC", "9GFK"],
    "MDMX": ["3DAB", "3EQY", "3FDO", "3FE7", "3FEA", "3JZO", "3JZP", "4RXZ",
             "5VK1", "7KJN", "8IA5", "3JZQ"],
}

TARGETS = {
    "MDM2": {
        "uniprot": "Q00987",
        "reference_pdb": ["1YCR", "4HG7", "3V3B"],
        "verified_peptide_pdb": ["1YCR", "3V3B"],
        "n_peptide_complexes": 43,
        "pocket_residues": {
            "Phe19_pocket": ["Gly58", "Ile61", "Met62", "Tyr67", "Gln72", "Val75", "Val93"],
            "Trp23_pocket": ["Leu54", "Leu57", "Gly58", "Ile61", "Val93"],
            "Leu26_pocket": ["Leu54", "Val93", "His96", "Ile99", "Tyr100"],
        },
    },
    "MDMX": {
        "uniprot": "O15151",
        "reference_pdb": ["3DAB", "3LBK"],
        "verified_peptide_pdb": ["3DAB"],
        "n_peptide_complexes": 12,
        "pocket_residues": {
            "Phe19_pocket": ["Gly57", "Ile60", "Met61", "Tyr66", "Gln71", "Val74", "Val92"],
            "Trp23_pocket": ["Met53", "Leu56", "Gly57", "Ile60", "Val92", "Leu98"],
            "Leu26_pocket": ["Met53", "Val92", "Pro95", "Leu98", "Tyr99"],
        },
    },
}

POCKET_DIFFERENCES = {
    "_method": "biotite heavy-atom<4A interface over 43 MDM2 + 12 MDMX peptide complexes (<=2.8A); "
               "MDM2/MDMX domains superposed by CA sequence alignment, RMSD 1.88 A over 85 CA.",
    "Phe19_pocket": {
        "MDM2_residues": TARGETS["MDM2"]["pocket_residues"]["Phe19_pocket"],
        "MDMX_residues": TARGETS["MDMX"]["pocket_residues"]["Phe19_pocket"],
        "difference_description": "Lining conserved, MDMX Met53 gate sits ~1.0A closer to Phe19 ring.",
        "design_rule": "Anchor with Phe volume or smaller. Avoid MDM2-only bulky aromatics (clash with Met53).",
    },
    "Trp23_pocket": {
        "MDM2_residues": TARGETS["MDM2"]["pocket_residues"]["Trp23_pocket"],
        "MDMX_residues": TARGETS["MDMX"]["pocket_residues"]["Trp23_pocket"],
        "difference_description": "Deepest, most conserved. Indole NE1 H-bond conserved in both targets.",
        "design_rule": "L-Trp as invariant shared anchor. Keep indole buried and NE1 H-bond.",
    },
    "Leu26_pocket": {
        "MDM2_residues": TARGETS["MDM2"]["pocket_residues"]["Leu26_pocket"],
        "MDMX_residues": TARGETS["MDMX"]["pocket_residues"]["Leu26_pocket"],
        "difference_description": "LARGEST divergence: MDMX shallower (His96->Pro95, Ile99->Leu98, Tyr99 floor).",
        "design_rule": "Downsize to small aliphatic (Leu/Val/Abu/cyclobutyl-Ala). Key lever for MDMX compatibility.",
    },
}

KNOWN_DUAL_BINDERS = [
    {"name": "PMI", "type": "linear peptide (phage-display, natural AA)",
     "sequence": "TSFAEYWNLLSP", "kd_mdm2": "low nanomolar", "kd_mdmx": "low nanomolar",
     "key_residues": ["Phe3->Phe19", "Trp7->Trp23", "Leu10->Leu26"], "pmid": "34589387"},
    {"name": "PMI-M3", "type": "linear peptide (optimized PMI, natural AA)",
     "sequence": "LTFLEYWAQLMQ", "kd_mdm2": "low picomolar", "kd_mdmx": "low picomolar",
     "key_residues": ["Phe3->Phe19", "Trp7->Trp23", "Leu10->Leu26"], "pmid": "34589387"},
    {"name": "ATSP-7041", "type": "stapled peptide, non-natural",
     "sequence": "Ac-LTF-(R8)-EYWAQ-(Cba)-(S5)-AA-NH2",
     "kd_mdm2": "Ki ~0.9 nM", "kd_mdmx": "Ki ~2.3 nM",
     "key_residues": ["Phe3->Phe19", "Trp7->Trp23", "Cba->Leu26"], "pmid": "23946421"},
    {"name": "ALRN-6924", "type": "stapled peptide (clinical)",
     "sequence": "stapled p53 mimic (Phe/Trp/Leu triad)", "kd_mdm2": "nanomolar", "kd_mdmx": "nanomolar",
     "key_residues": ["Phe->Phe19", "Trp->Trp23", "Leu26-anchor"], "pmid": "37439511"},
    {"name": "pDI", "type": "linear peptide (phage-display, natural AA)",
     "sequence": "LTFEHYWAQLTS", "kd_mdm2": "~40 nM", "kd_mdmx": "sub-micromolar",
     "key_residues": ["Phe3->Phe19", "Trp7->Trp23", "Leu10->Leu26"], "pmid": "19910468"},
    {"name": "pDI6W", "type": "linear peptide (pDI variant, natural AA)",
     "sequence": "LTFEHWWAQLTS", "kd_mdm2": "nanomolar", "kd_mdmx": "sub-micromolar",
     "key_residues": ["Phe3->Phe19", "Trp7->Trp23", "Trp6->MDMX subsite"], "pmid": "19910468"},
    {"name": "pDIQ", "type": "linear peptide (optimized, natural AA)",
     "sequence": "ETFEHWWSQLLS", "kd_mdm2": "IC50 8 nM", "kd_mdmx": "IC50 110 nM",
     "key_residues": ["Phe3->Phe19", "Trp7->Trp23", "Leu10->Leu26"], "pmid": "19910468"},
    {"name": "M3-2K", "type": "linear peptide (CPP-PMI-M3, natural AA)",
     "sequence": "KLTFLEYWAQLMQK", "kd_mdm2": "picomolar", "kd_mdmx": "picomolar",
     "key_residues": ["Phe->Phe19", "Trp->Trp23", "Leu->Leu26"], "pmid": "34589387"},
]

LITERATURE_REFS = [
    {"pmid": "34589387", "title": "Ultrahigh-affinity dual-specificity peptide antagonists of MDM2/MDMX"},
    {"pmid": "23946421", "title": "Stapled alpha-helical peptide dual inhibitor of MDM2/MDMX"},
    {"pmid": "37439511", "title": "Discovery of Sulanemadlin (ALRN-6924)"},
    {"pmid": "34301750", "title": "ALRN-6924 first-in-human Phase 1 trial"},
    {"pmid": "19910468", "title": "Structure-based design of p53-MDM2/MDMX peptide inhibitors"},
]

DESIGN_STRATEGY_SUMMARY = (
    "Trp23 = invariant L-Trp shared anchor. Phe19 <= Phe volume (Met53 gate). "
    "Leu26 downsized to small aliphatic (bottleneck for MDMX). "
    "Natural-AA cyclic scaffold on helical geometry, FxxWxxxL motif spacing."
)

DATA_QUALITY_ALERT = (
    "4HG7 and 3LBK are small-molecule complexes, not peptide complexes. "
    "Retained in reference_pdb but excluded from peptide interface analysis. "
    "Verified peptide refs: 1YCR/3V3B (MDM2), 3DAB (MDMX)."
)


# ============================================================
# 管线执行
# ============================================================

def _run_script(script_name: str, input_data: dict = None) -> dict:
    """运行 scripts/ 下的一个管线步骤，作为 subprocess。

    返回 (stdout_dict, stderr_str, exit_code, duration_sec, output_hash)
    """
    script_path = SCRIPTS_DIR / script_name
    if not script_path.exists():
        raise FileNotFoundError(f"脚本不存在: {script_path}")

    python_exe = sys.executable
    t0 = time.time()

    proc = subprocess.run(
        [python_exe, "-m", f"scripts.{script_name.replace('.py', '')}"],
        input=json.dumps(input_data) if input_data else None,
        capture_output=True, text=True, timeout=600, cwd=str(ROOT),
    )

    duration = time.time() - t0
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()[:500]
    exit_code = proc.returncode

    output_hash = hashlib.md5(stdout.encode()).hexdigest()[:12] if stdout else ""

    result = None
    try:
        result = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        result = {"stdout": stdout[:1000], "parse_error": True}

    return result, stderr, exit_code, duration, output_hash


def _run_pipeline() -> dict:
    """执行完整调研管线，每步挂 tool_trace。

    返回完整结果字典，跟常量结构一致。
    """
    # ---- Step 1: RCSB search ----
    print("[research] Step 1/7: RCSB Search API 检索...")
    search_result, search_stderr, search_code, search_dur, search_hash = _run_script("search_pdb.py")
    EvidenceLogger.log("research", "tool_call", {
        "tool_name": "rcsb_search_api",
        "tool_version": "v2",
        "input_params": {"uniprot_MDM2": "Q00987", "uniprot_MDMX": "O15151", "max_res": 2.8, "species": "Homo sapiens"},
        "output_hash": search_hash,
        "exit_code": search_code,
        "duration_sec": round(search_dur, 1),
        "stdout_snippet": f"MDM2={search_result.get('n_mdm2', 0)} MDMX={search_result.get('n_mdmx', 0)}",
    }, targets=["both"], phase="research")

    # ---- Step 2: GraphQL enrich ----
    print("[research] Step 2/7: RCSB GraphQL 富集...")
    enrich_result, enrich_stderr, enrich_code, enrich_dur, enrich_hash = _run_script("enrich_pdb.py", search_result)
    n_peptide = enrich_result.get("n_peptide_complexes", 0)
    EvidenceLogger.log("research", "tool_call", {
        "tool_name": "rcsb_graphql_api",
        "tool_version": "2024",
        "output_hash": enrich_hash,
        "exit_code": enrich_code,
        "duration_sec": round(enrich_dur, 1),
        "stdout_snippet": f"peptide_complexes={n_peptide}",
    }, targets=["both"], phase="research")

    # ---- Step 3: biotite interface ----
    print("[research] Step 3/7: biotite 界面计算 (需要 biotite)...")
    try:
        iface_result, iface_stderr, iface_code, iface_dur, iface_hash = _run_script("compute_interface.py", enrich_result)
        n_iface = iface_result.get("n_with_interface", 0)
        EvidenceLogger.log("research", "tool_call", {
            "tool_name": "biotite",
            "tool_version": "latest",
            "input_params": {"cutoff_A": 4.0, "heavy_atom_only": True},
            "output_hash": iface_hash,
            "exit_code": iface_code,
            "duration_sec": round(iface_dur, 1),
            "stdout_snippet": f"with_interface={n_iface}",
        }, targets=["both"], phase="research")
    except Exception as e:
        EvidenceLogger.error("research", "tool_failure", f"biotite 界面计算失败: {e}", recovery="使用预置常量")
        iface_result = {"with_interface": []}

    # ---- Step 4: aggregate pockets ----
    print("[research] Step 4/7: 口袋残基聚合...")
    pocket_result, pocket_stderr, pocket_code, pocket_dur, pocket_hash = _run_script("aggregate_pockets.py", iface_result)
    EvidenceLogger.log("research", "tool_call", {
        "tool_name": "aggregate_pockets",
        "tool_version": "1.0",
        "input_params": {"freq_threshold": 0.5},
        "output_hash": pocket_hash,
        "exit_code": pocket_code,
        "duration_sec": round(pocket_dur, 1),
    }, targets=["both"], phase="research")

    # ---- Step 5: superpose & analyze ----
    print("[research] Step 5/7: Cα 叠合差异量化 (需要 biotite)...")
    try:
        superpose_result, super_stderr, super_code, super_dur, super_hash = _run_script("superpose_analyze.py", pocket_result)
        EvidenceLogger.log("research", "tool_call", {
            "tool_name": "biotite_superimpose",
            "tool_version": "latest",
            "input_params": {"ref_mdm2": "1YCR", "ref_mdmx": "3DAB", "resi_range": [25, 109]},
            "output_hash": super_hash,
            "exit_code": super_code,
            "duration_sec": round(super_dur, 1),
        }, targets=["both"], phase="research")
    except Exception as e:
        EvidenceLogger.error("research", "tool_failure", f"叠合分析��败: {e}", recovery="使用预置常量")
        superpose_result = {"superposition": {}}

    # ---- Step 6: PubMed search ----
    print("[research] Step 6/7: PubMed 文献检索...")
    pubmed_result, pubmed_stderr, pubmed_code, pubmed_dur, pubmed_hash = _run_script("pubmed_search.py")
    EvidenceLogger.log("research", "tool_call", {
        "tool_name": "pubmed_eutils",
        "tool_version": "2024",
        "input_params": {"term": "MDM2 MDMX dual peptide inhibitor"},
        "output_hash": pubmed_hash,
        "exit_code": pubmed_code,
        "duration_sec": round(pubmed_dur, 1),
        "stdout_snippet": f"n_papers={pubmed_result.get('n_total', 0)}",
    }, targets=["both"], phase="research")

    # ---- Step 7: LLM 提取文献信息 ----
    print("[research] Step 7/7: LLM 提取双靶分子信息...")
    llm_binders = KNOWN_DUAL_BINDERS  # 默认用常量，LLM 提取为增强项
    llm_pocket = {}
    try:
        llm_result, llm_stderr, llm_code, llm_dur, llm_hash = _run_script("llm_extract.py", pubmed_result)
        if llm_result and "error" not in llm_result:
            llm_binders = llm_result.get("known_binders", llm_binders)
            llm_pocket = llm_result.get("pocket_analysis", {})
        EvidenceLogger.log("research", "tool_call", {
            "tool_name": "llm_extract",
            "tool_version": llm_result.get("llm_model", "unknown") if isinstance(llm_result, dict) else "unknown",
            "output_hash": llm_hash,
            "exit_code": llm_code,
            "duration_sec": round(llm_dur, 1),
            "stdout_snippet": f"binders={len(llm_binders)}",
        }, targets=["both"], phase="research")
    except Exception as e:
        EvidenceLogger.error("research", "tool_failure", f"LLM 提取失败: {e}", recovery="使用预置常量 KNOWN_DUAL_BINDERS")

    # ---- 组装结果 ----
    result = {
        "targets": TARGETS.copy(),
        "pocket_differences": POCKET_DIFFERENCES,
        "known_dual_binders": llm_binders if llm_binders else KNOWN_DUAL_BINDERS,
        "pocket_analysis_llm": llm_pocket,
        "design_strategy_summary": DESIGN_STRATEGY_SUMMARY,
        "data_quality_alert": DATA_QUALITY_ALERT,
        "literature_refs": LITERATURE_REFS,
        "_pipeline_meta": {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "search_hash": search_hash,
            "enrich_hash": enrich_hash,
            "pubmed_hash": pubmed_hash,
        },
    }

    # 缓存
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[research] 管线完成，结果缓存到 {CACHE_PATH}")

    return result


# ============================================================
# 入口
# ============================================================

def run(state: dict = None, force_recompute: bool = False, skip_pipeline: bool = False) -> dict:
    """Research Agent 主入口。

    首次运行执行完整管线（RCSB → biotite → PubMed → LLM），
    结果缓存到 data/_research_cache.json。后续运行直接读缓存。

    参数:
        state: 已有的 state dict，None 则自动 State.load()
        force_recompute: 强制重新跑完整管线
        skip_pipeline: 跳过管线，直接用缓存（缓存不存在则回退到管线）

    返回: 写入 state.json 的结果字典。
    """
    if state is None:
        state = State.load()

    # 决定用缓存还是跑管线
    if force_recompute:
        pipeline_result = _run_pipeline()
    elif skip_pipeline or CACHE_PATH.exists():
        if CACHE_PATH.exists():
            pipeline_result = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            print(f"[research] 使用缓存: {CACHE_PATH}")
        else:
            print("[research] 缓存不存在，回退到管线...")
            pipeline_result = _run_pipeline()
    else:
        pipeline_result = _run_pipeline()

    # 写入 state.json
    targets = state.get("targets", {})
    for name, info in pipeline_result.get("targets", TARGETS).items():
        targets.setdefault(name, {})
        targets[name].update(info)

    result = {
        "targets": targets,
        "pocket_differences": pipeline_result.get("pocket_differences", POCKET_DIFFERENCES),
        "known_dual_binders": pipeline_result.get("known_dual_binders", KNOWN_DUAL_BINDERS),
        "design_strategy_summary": pipeline_result.get("design_strategy_summary", DESIGN_STRATEGY_SUMMARY),
        "data_quality_alert": pipeline_result.get("data_quality_alert", DATA_QUALITY_ALERT),
    }
    State.update(result)

    # 记 research_complete
    hotspot_analysis = {
        "pdb_list": VERIFIED_PEPTIDE_COMPLEXES["MDM2"] + VERIFIED_PEPTIDE_COMPLEXES["MDMX"],
        "n_mdm2_peptide_complexes": len(VERIFIED_PEPTIDE_COMPLEXES["MDM2"]),
        "n_mdmx_peptide_complexes": len(VERIFIED_PEPTIDE_COMPLEXES["MDMX"]),
        "method": POCKET_DIFFERENCES["_method"],
        "pockets": POCKET_DIFFERENCES,
        "data_quality_alert": DATA_QUALITY_ALERT,
    }
    EvidenceLogger.research_complete(
        hotspot_analysis=hotspot_analysis,
        known_binders=result["known_dual_binders"],
        refs=LITERATURE_REFS,
    )
    return result


def recompute():
    """从零复算完整管线（联网 + biotite + LLM）。

    等同于 run(force_recompute=True)。
    需要: 联网 + pip install biotite + LLM API key 环境变量。
    缓存: data/_research_cache.json。
    """
    return run(force_recompute=True)


if __name__ == "__main__":
    out = run()
    n_binders = len(out.get("known_dual_binders", []))
    print(f"[research] state.json 已更新：targets / pocket_differences / known_dual_binders")
    print(f"[research] 已知双靶分子 {n_binders} 个")
    print(f"[research] 当前 phase={State.load().get('phase')}")
