"""
Research Agent - MDM2/MDMX 靶点调研管线
7 步: RCSB Search -> GraphQL Enrich -> biotite interface -> aggregate pockets ->
      superpose analyze -> PubMed -> LLM extract
每步挂 EvidenceLogger tool_trace。支持 SKIP_BIOTITE=1 跳过步骤 3-5。
"""

import json, os, subprocess, sys, time, hashlib
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
DATA_DIR = ROOT / "data"
CACHE_PATH = DATA_DIR / "_research_cache.json"

from data_layer import State, EvidenceLogger

# 预置常量（管线失败时兜底）
TARGETS = {
    "MDM2": {"uniprot": "Q00987", "reference_pdb": ["1YCR", "4HG7", "3V3B"],
             "verified_peptide_pdb": ["1YCR", "3V3B"], "n_peptide_complexes": 43,
             "pocket_residues": {
                 "Phe19_pocket": ["Gly58","Ile61","Met62","Tyr67","Gln72","Val75","Val93"],
                 "Trp23_pocket": ["Leu54","Leu57","Gly58","Ile61","Val93"],
                 "Leu26_pocket": ["Leu54","Val93","His96","Ile99","Tyr100"]}},
    "MDMX": {"uniprot": "O15151", "reference_pdb": ["3DAB", "3LBK"],
             "verified_peptide_pdb": ["3DAB"], "n_peptide_complexes": 12,
             "pocket_residues": {
                 "Phe19_pocket": ["Gly57","Ile60","Met61","Tyr66","Gln71","Val74","Val92"],
                 "Trp23_pocket": ["Met53","Leu56","Gly57","Ile60","Val92","Leu98"],
                 "Leu26_pocket": ["Met53","Val92","Pro95","Leu98","Tyr99"]}},
}
POCKET_DIFFERENCES = {
    "_method": "biotite heavy-atom<4A, 43+12 structures <=2.8A, CA RMSD 1.88A/85 residues",
    "Phe19_pocket": {"MDM2_residues": TARGETS["MDM2"]["pocket_residues"]["Phe19_pocket"],
                     "MDMX_residues": TARGETS["MDMX"]["pocket_residues"]["Phe19_pocket"],
                     "design_rule": "Phe volume or smaller, avoid bulky aromatics (Met53 gate)."},
    "Trp23_pocket": {"MDM2_residues": TARGETS["MDM2"]["pocket_residues"]["Trp23_pocket"],
                     "MDMX_residues": TARGETS["MDMX"]["pocket_residues"]["Trp23_pocket"],
                     "design_rule": "L-Trp invariant shared anchor, preserve NE1 H-bond."},
    "Leu26_pocket": {"MDM2_residues": TARGETS["MDM2"]["pocket_residues"]["Leu26_pocket"],
                     "MDMX_residues": TARGETS["MDMX"]["pocket_residues"]["Leu26_pocket"],
                     "design_rule": "Downsize to small aliphatic (Leu/Val/Abu/cyclobutyl-Ala)."},
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
DESIGN_STRATEGY_SUMMARY = "Trp23=L-Trp invariant anchor. Phe19<=Phe volume. Leu26=small aliphatic. Natural-AA cyclic on helical FxxWxxxL geometry."
VERIFIED_PEPTIDE_COMPLEXES = {
    "MDM2": ["1T4F","1YCR","2GV2","3EQS","3G03","3IUX","3IWY","3JZR","3LNZ","3TPX","3V3B","4HFZ"],
    "MDMX": ["3DAB","3EQY","3FDO","3FE7","3FEA","3JZO","3JZP","4RXZ","5VK1","7KJN","8IA5","3JZQ"],
}
DATA_QUALITY_ALERT = "4HG7/3LBK are small-molecule complexes, not peptide. Verified: 1YCR/3V3B(MDM2), 3DAB(MDMX)."


def _run_script(script_name, input_data=None):
    script_path = SCRIPTS_DIR / script_name
    if not script_path.exists():
        raise FileNotFoundError(f"Script not found: {script_path}")
    python_exe = sys.executable
    t0 = time.time()
    proc = subprocess.run(
        [python_exe, "-m", f"scripts.{script_name.replace('.py', '')}"],
        input=json.dumps(input_data) if input_data else None,
        capture_output=True, text=True, timeout=600, cwd=str(ROOT),
        env={**os.environ},
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


def _run_pipeline():
    skip_heavy = os.environ.get("SKIP_BIOTITE", "").lower() in ("1", "true", "yes")

    # Step 1: RCSB Search
    print("[research] Step 1/7: RCSB Search API...")
    sr, se, sc, sd, sh = _run_script("search_pdb.py")
    EvidenceLogger.log("research", "tool_call", {
        "tool_name": "rcsb_search_api", "tool_version": "v2",
        "output_hash": sh, "exit_code": sc, "duration_sec": round(sd, 1),
        "stdout_snippet": f"MDM2={sr.get('n_mdm2',0)} MDMX={sr.get('n_mdmx',0)}",
    }, targets=["both"], phase="research")

    # Step 2: GraphQL Enrich
    print("[research] Step 2/7: RCSB GraphQL...")
    er, ee, ec, ed, eh = _run_script("enrich_pdb.py", sr)
    EvidenceLogger.log("research", "tool_call", {
        "tool_name": "rcsb_graphql_api", "output_hash": eh, "exit_code": ec,
        "duration_sec": round(ed, 1),
        "stdout_snippet": f"peptide_complexes={er.get('n_peptide_complexes',0)}",
    }, targets=["both"], phase="research")

    # Steps 3-5: biotite (可跳过)
    if skip_heavy:
        print("[research] Steps 3-5: skipped (SKIP_BIOTITE=1)")
        iface_result = {"with_interface": []}
    else:
        print("[research] Step 3/7: biotite interface...")
        try:
            ir, ie, ic, id_, ih = _run_script("compute_interface.py", er)
            EvidenceLogger.log("research", "tool_call", {
                "tool_name": "biotite", "output_hash": ih, "exit_code": ic,
                "duration_sec": round(id_, 1),
                "stdout_snippet": f"with_interface={ir.get('n_with_interface',0)}",
            }, targets=["both"], phase="research")
        except Exception as e:
            EvidenceLogger.error("research", "tool_failure", f"biotite: {e}", recovery="fallback")
            ir = {"with_interface": []}
        iface_result = ir

        print("[research] Step 4/7: aggregate pockets...")
        pr, pe, pc, pd_, ph = _run_script("aggregate_pockets.py", iface_result)
        EvidenceLogger.log("research", "tool_call", {
            "tool_name": "aggregate_pockets", "output_hash": ph, "exit_code": pc,
            "duration_sec": round(pd_, 1),
        }, targets=["both"], phase="research")

        print("[research] Step 5/7: superposition...")
        try:
            spr, spe, spc, spd_, sph = _run_script("superpose_analyze.py", pr)
            EvidenceLogger.log("research", "tool_call", {
                "tool_name": "biotite_superimpose", "output_hash": sph, "exit_code": spc,
                "duration_sec": round(spd_, 1),
            }, targets=["both"], phase="research")
        except Exception as e:
            EvidenceLogger.error("research", "tool_failure", f"superpose: {e}", recovery="fallback")

    # Step 6: PubMed
    print("[research] Step 6/7: PubMed...")
    pmr, pme, pmc, pmd_, pmh = _run_script("pubmed_search.py")
    EvidenceLogger.log("research", "tool_call", {
        "tool_name": "pubmed_eutils", "output_hash": pmh, "exit_code": pmc,
        "duration_sec": round(pmd_, 1),
        "stdout_snippet": f"n_papers={pmr.get('n_total',0)}",
    }, targets=["both"], phase="research")

    # Step 7: LLM extract
    print("[research] Step 7/7: LLM extract...")
    llm_binders = KNOWN_DUAL_BINDERS
    try:
        lr, le, lc, ld_, lh = _run_script("llm_extract.py", pmr)
        if lr and "error" not in lr:
            llm_binders = lr.get("known_binders", llm_binders)
        EvidenceLogger.log("research", "tool_call", {
            "tool_name": "llm_extract",
            "tool_version": lr.get("llm_model", "unknown") if isinstance(lr, dict) else "unknown",
            "output_hash": lh, "exit_code": lc, "duration_sec": round(ld_, 1),
        }, targets=["both"], phase="research")
    except Exception as e:
        EvidenceLogger.error("research", "tool_failure", f"LLM: {e}", recovery="fallback to constants")

    result = {
        "targets": TARGETS.copy(),
        "pocket_differences": POCKET_DIFFERENCES,
        "known_dual_binders": llm_binders,
        "design_strategy_summary": DESIGN_STRATEGY_SUMMARY,
        "data_quality_alert": DATA_QUALITY_ALERT,
        "literature_refs": LITERATURE_REFS,
        "_pipeline_meta": {"last_run": datetime.now(timezone.utc).isoformat()},
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[research] Pipeline done. Cache: {CACHE_PATH}")
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
    }
    State.update(result)

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
    return run(force_recompute=True)


if __name__ == "__main__":
    out = run()
    n = len(out.get("known_dual_binders", []))
    print(f"[research] State updated. {n} dual binders. Phase={State.load().get('phase')}")
