"""Research pipeline step implementations, split from agents/research.py (PR8).

Internal module: imported by ``agents.research`` lazily on first access (PR8).
This module reads research-module globals through ``research._xxx`` at call
time, so it can be imported directly or after ``agents.research``.
"""

import os
from datetime import datetime, timezone

from data_layer import EvidenceLogger
from project_config import required_target_ids
from threshold_contract import normalize_thresholds

# Sibling-module access: the step functions were split out of agents/research
# (PR8) and legitimately share its private helpers, so we reach them through
# the module object instead of importing private names directly.
from agents import research as _research


def _build_dynamic_pockets(aggregate_result, superpose_result):
    """从 aggregate + superpose 输出构建 pocket_differences。
    
    如果 biotite 成功计算了界面残基，用动态数据；
    否则返回 None（调用方回退到常量）。
    """
    n_mdm2 = aggregate_result.get("n_mdm2_structures", 0)
    n_mdmx = aggregate_result.get("n_mdmx_structures", 0)
    if n_mdm2 == 0 or n_mdmx == 0:
        return None  # 双靶动态结论要求两侧都有结构支持

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
                # 兼容新版 "58GLY" 与旧版 "A:58GLY"。
                import re
                m = re.search(r':?(-?\d+)([A-Z]{3})$', r.upper())
                if m:
                    res_id, res_name = m.group(1), m.group(2)
                    cleaned.append(f"{res_name.capitalize()}{res_id}")
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
    _research._ensure_runtime_dirs()
    skip_heavy = os.environ.get("SKIP_BIOTITE", "").lower() in ("1", "true", "yes")
    stage_status = {}
    stage_context = {}
    fallbacks = []

    sr = _step_rcsb_search(stage_status, stage_context)
    er = _step_rcsb_enrich(stage_status, stage_context, sr)
    (pocket_differences, dynamic_pdb_list, dynamic_pdb_by_target,
     n_mdm2_structures, n_mdmx_structures) = _step_biotite(
        stage_status, stage_context, fallbacks, er, skip_heavy,
    )
    pmr = _step_pubmed(stage_status, stage_context)
    llm_binders, binder_source = _step_llm_extract(
        stage_status, stage_context, fallbacks, pmr,
    )
    thresholds, control_calibration, threshold_normalization, final_normalization = (
        _step_threshold_research(stage_status, stage_context, fallbacks)
    )
    _research._write_threshold_cache(thresholds, _research._cfg(), {
        "literature_input": threshold_normalization,
        "final": final_normalization,
        "control_calibration": control_calibration,
    })
    if not _research._module_attr("THRESHOLDS_CACHE").exists():
        _research._write_threshold_cache(thresholds, _research._cfg())

    # ===== 组装结果 =====
    stage_error_code, error_message = _research._diagnostics_for_stages(stage_status, stage_context)
    result = {
        "targets": _research.TARGETS.copy(),
        "pocket_differences": pocket_differences,
        "known_dual_binders": llm_binders,
        "known_binder_source": binder_source,
        "design_strategy_summary": _research.DESIGN_STRATEGY_SUMMARY,
        "data_quality_alert": _research.DATA_QUALITY_ALERT,
        "literature_refs": _research.LITERATURE_REFS,
        "thresholds": thresholds,
        "_pipeline_meta": {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "pocket_source": pocket_differences.get("_source", "constant"),
            "n_pdb_complexes": len(dynamic_pdb_list),
            "n_mdm2_structures": n_mdm2_structures,
            "n_mdmx_structures": n_mdmx_structures,
            "dynamic_pdb_list": (
                dynamic_pdb_by_target["MDM2"][:10]
                + dynamic_pdb_by_target["MDMX"][:10]
            ),
            "dynamic_pdb_by_target": dynamic_pdb_by_target,
            "stage_status": stage_status,
            "stage_error_code": stage_error_code,
            "error_message": error_message,
            "fallbacks_used": list(dict.fromkeys(fallbacks)),
            "control_calibration": control_calibration,
            "run_status": _research._overall_run_status(stage_status),
        },
        "_cache_meta": _research._cache_meta(_research._cfg()),
    }
    _research._atomic_write_json(_research._module_attr("CACHE_PATH"), result)
    print(f"[research] Pipeline done. pocket_source={result['_pipeline_meta']['pocket_source']}")
    return result


def _step_rcsb_search(stage_status: dict, stage_context: dict) -> dict:
    # ===== Step 1: RCSB Search =====
    print("[research] Step 1/8: RCSB Search API...")
    sr, se, sc, sd, sh = _research._run_script("search_pdb.py")
    stage_status["rcsb_search"] = (
        "complete" if sc == 0 and sr.get("run_status") == "complete"
        else "empty" if sc == 0 else "failed"
    )
    stage_context["rcsb_search"] = (sr, se)
    EvidenceLogger.log("research", "tool_call", {
        "tool_name": "rcsb_search_api", "tool_version": "v2",
        "output_hash": sh, "exit_code": sc, "duration_sec": round(sd, 1),
        "stdout_snippet": (
            f"status={stage_status['rcsb_search']} "
            f"MDM2={sr.get('n_mdm2',0)} MDMX={sr.get('n_mdmx',0)}"
        ),
    }, targets=["both"], phase="research")
    return sr


def _step_rcsb_enrich(stage_status: dict, stage_context: dict, sr: dict) -> dict:
    # ===== Step 2: GraphQL Enrich =====
    print("[research] Step 2/8: RCSB GraphQL...")
    er, ee, ec, ed, eh = _research._run_script("enrich_pdb.py", sr)
    n_peptide = er.get("n_peptide_complexes", 0)
    stage_status["rcsb_enrich"] = "complete" if ec == 0 and n_peptide > 0 else "empty" if ec == 0 else "failed"
    stage_context["rcsb_enrich"] = (er, ee)
    EvidenceLogger.log("research", "tool_call", {
        "tool_name": "rcsb_graphql_api", "output_hash": eh, "exit_code": ec,
        "duration_sec": round(ed, 1),
        "stdout_snippet": f"status={stage_status['rcsb_enrich']} peptide_complexes={n_peptide}",
    }, targets=["both"], phase="research")
    return er


def _step_biotite(
    stage_status: dict,
    stage_context: dict,
    fallbacks: list,
    er: dict,
    skip_heavy: bool,
) -> tuple[dict, list, dict, int, int]:
    # ===== Steps 3-5: biotite =====
    pocket_differences = _research.POCKET_DIFFERENCES  # 默认常量
    dynamic_pdb_list = []  # 动态 PDB 列表（来自 enrich）
    dynamic_pdb_by_target = {"MDM2": [], "MDMX": []}
    n_mdm2_structures = 0
    n_mdmx_structures = 0

    # 从 enrich 提取动态 PDB 列表
    for entry in er.get("peptide_complexes", []):
        target_name = entry.get("target")
        if (
            target_name in dynamic_pdb_by_target
            and entry["pdb_id"] not in dynamic_pdb_by_target[target_name]
        ):
            dynamic_pdb_by_target[target_name].append(entry["pdb_id"])
        if entry["pdb_id"] not in dynamic_pdb_list:
            dynamic_pdb_list.append(entry["pdb_id"])
        if entry.get("target") == "MDM2":
            n_mdm2_structures += 1
        elif entry.get("target") == "MDMX":
            n_mdmx_structures += 1

    if skip_heavy:
        print("[research] Steps 3-5: skipped (SKIP_BIOTITE=1)")
        stage_status["interface"] = "skipped"
        stage_status["aggregate"] = "skipped"
        stage_status["superposition"] = "skipped"
        stage_context.update({name: ({}, "") for name in ("interface", "aggregate", "superposition")})
        fallbacks.append("curated_mdm_pocket_definitions")
    else:
        # Step 3: biotite interface
        print("[research] Step 3/8: biotite interface...")
        iface_result = {"with_interface": []}
        try:
            ir, ie2, ic, id_, ih = _research._run_script("compute_interface.py", er)
            n_iface = ir.get("n_with_interface", 0)
            stage_status["interface"] = (
                "complete" if ic == 0 and n_iface > 0 else "empty" if ic == 0 else "failed"
            )
            stage_context["interface"] = (ir, ie2)
            EvidenceLogger.log("research", "tool_call", {
                "tool_name": "biotite", "output_hash": ih, "exit_code": ic,
                "duration_sec": round(id_, 1),
                "stdout_snippet": f"status={stage_status['interface']} with_interface={n_iface}",
            }, targets=["both"], phase="research")
            iface_result = ir
        except Exception as e:
            EvidenceLogger.error("research", "tool_failure", f"biotite: {e}", recovery="fallback")
            iface_result = {"with_interface": []}
            stage_status["interface"] = "failed"
            stage_context["interface"] = ({}, str(e))

        # Step 4: aggregate pockets
        print("[research] Step 4/8: aggregate pockets...")
        pr, pe2, pc, pd_, ph = _research._run_script("aggregate_pockets.py", iface_result)
        n_agg_mdm2 = pr.get("n_mdm2_structures", 0)
        n_agg_mdmx = pr.get("n_mdmx_structures", 0)
        stage_status["aggregate"] = (
            "complete" if pc == 0 and n_agg_mdm2 > 0 and n_agg_mdmx > 0
            else "empty" if pc == 0 else "failed"
        )
        stage_context["aggregate"] = (pr, pe2)
        EvidenceLogger.log("research", "tool_call", {
            "tool_name": "aggregate_pockets", "output_hash": ph, "exit_code": pc,
            "duration_sec": round(pd_, 1),
            "stdout_snippet": (
                f"status={stage_status['aggregate']} "
                f"MDM2={n_agg_mdm2}struct MDMX={n_agg_mdmx}struct"
            ),
        }, targets=["both"], phase="research")

        # Step 5: superpose
        print("[research] Step 5/8: superposition...")
        spr = {}
        try:
            spr, spe2, spc, spd_, sph = _research._run_script("superpose_analyze.py", pr)
            stage_status["superposition"] = (
                "complete" if spc == 0 and spr.get("ca_rmsd_A") is not None
                else "empty" if spc == 0 else "failed"
            )
            stage_context["superposition"] = (spr, spe2)
            EvidenceLogger.log("research", "tool_call", {
                "tool_name": "biotite_superimpose", "output_hash": sph, "exit_code": spc,
                "duration_sec": round(spd_, 1),
                "stdout_snippet": (
                    f"status={stage_status['superposition']} "
                    f"rmsd={spr.get('ca_rmsd_A','?')}A "
                    f"n_ca={spr.get('n_ca_atoms_superposed','?')}"
                ),
            }, targets=["both"], phase="research")
        except Exception as e:
            EvidenceLogger.error("research", "tool_failure", f"superpose: {e}", recovery="fallback")
            stage_status["superposition"] = "failed"
            stage_context["superposition"] = ({}, str(e))

        # 用动态数据构建 pocket_differences
        dynamic = _build_dynamic_pockets(pr, spr)
        if dynamic:
            pocket_differences = dynamic
            n_mdm2_structures = n_agg_mdm2
            n_mdmx_structures = n_agg_mdmx
            print(f"[research] Using dynamic pockets: MDM2={n_mdm2_structures}struct MDMX={n_mdmx_structures}struct")
        else:
            print("[research] biotite produced no interface data, using constant pockets")
            fallbacks.append("curated_mdm_pocket_definitions")
    return (pocket_differences, dynamic_pdb_list, dynamic_pdb_by_target,
            n_mdm2_structures, n_mdmx_structures)


def _step_pubmed(stage_status: dict, stage_context: dict) -> dict:
    # ===== Step 6: PubMed =====
    print("[research] Step 6/8: PubMed...")
    pmr, pme, pmc, pmd_, pmh = _research._run_script("pubmed_search.py")
    stage_status["pubmed"] = "complete" if pmc == 0 and pmr.get("n_total", 0) > 0 else "empty" if pmc == 0 else "failed"
    stage_context["pubmed"] = (pmr, pme)
    EvidenceLogger.log("research", "tool_call", {
        "tool_name": "pubmed_eutils", "output_hash": pmh, "exit_code": pmc,
        "duration_sec": round(pmd_, 1),
        "stdout_snippet": f"status={stage_status['pubmed']} n_papers={pmr.get('n_total',0)}",
    }, targets=["both"], phase="research")
    return pmr


def _step_llm_extract(
    stage_status: dict, stage_context: dict, fallbacks: list, pmr: dict
) -> tuple[list, str]:
    # ===== Step 7: LLM extract =====
    print("[research] Step 7/8: LLM extract (concurrent)...")
    llm_binders = _research.KNOWN_DUAL_BINDERS
    binder_source = "curated_fallback"
    try:
        lr, le2, lc, ld_, lh = _research._run_script("llm_extract.py", pmr, extra_args=["--concurrency", "3"])
        if lr and "error" not in lr:
            extracted = lr.get("known_binders", [])
            if extracted:
                llm_binders = extracted
                binder_source = "llm_extracted"
                print(f"[research] LLM found {len(extracted)} binders from {lr.get('n_papers_processed',0)} papers")
        raw_llm_status = lr.get("run_status", "failed")
        stage_status["llm_extract"] = (
            "degraded_no_api_key" if raw_llm_status == "degraded_no_api_key"
            else "complete" if lc == 0 and raw_llm_status == "complete"
            else "failed"
        )
        stage_context["llm_extract"] = (lr, le2)
        if binder_source == "curated_fallback":
            fallbacks.append("curated_mdm_binders")
        EvidenceLogger.log("research", "tool_call", {
            "tool_name": "llm_extract_concurrent",
            "tool_version": lr.get("llm_model", "unknown") if isinstance(lr, dict) else "unknown",
            "output_hash": lh, "exit_code": lc, "duration_sec": round(ld_, 1),
            "stdout_snippet": (
                f"status={stage_status['llm_extract']} binders={len(llm_binders)} "
                f"papers={lr.get('n_papers_processed',0) if isinstance(lr, dict) else '?'}"
            ),
        }, targets=["both"], phase="research")
    except Exception as e:
        EvidenceLogger.error("research", "tool_failure", f"LLM: {e}", recovery="fallback to constants")
        stage_status["llm_extract"] = "failed"
        stage_context["llm_extract"] = ({}, str(e))
        fallbacks.append("curated_mdm_binders")
    return llm_binders, binder_source


def _step_threshold_research(
    stage_status: dict, stage_context: dict, fallbacks: list
) -> tuple[dict, dict, dict, dict]:
    threshold_normalization = {}
    final_normalization = {}
    # ===== Step 8: 阈值文献检索 =====
    print("[research] Step 8/8: threshold literature research...")
    thresholds = _research.default_thresholds(_research._cfg())
    try:
        tr, te2, tc, td_, th = _research._run_script("threshold_research.py", extra_args=["--concurrency", "4"])
        lit, threshold_normalization = normalize_thresholds(tr.get("metric_battery", {}))
        threshold_meta = tr.get("_meta", {})
        n_found = threshold_meta.get("n_auto_usable", 0)
        raw_threshold_status = threshold_meta.get("run_status", "failed")
        stage_status["threshold_research"] = (
            "degraded_no_api_key" if raw_threshold_status == "degraded_no_llm"
            else "empty" if raw_threshold_status == "degraded_empty_results"
            else "complete" if tc == 0 and raw_threshold_status == "complete"
            else "failed"
        )
        stage_context["threshold_research"] = (tr, te2)
        # 仅自动采用已核验 PMID、摘要原句和 paper_explicit 证据的阈值。
        for layer, info in lit.items():
            if info.get("auto_usable") and info.get("value") is not None:
                thresholds[layer] = {
                    **{
                        key: value
                        for key, value in thresholds.get(layer, {}).items()
                        if key in ("method", "min_seed_fraction")
                    },
                    "value": info["value"],
                    "operator": info.get("operator", ">"),
                    "unit": info.get("unit"),
                    "source": f"PubMed PMID {info.get('source_pmid')}",
                    "confidence": info.get("confidence", "medium"),
                    "source_pmid": info.get("source_pmid"),
                    "evidence_quote": info.get("evidence_quote"),
                    "evidence_grade": info.get("evidence_grade"),
                    "quote_verified": True,
                    "calibration_status": "pending",
                    "applicable_targets": list(required_target_ids(_research._cfg())),
                }
        thresholds, final_normalization = normalize_thresholds(thresholds)
        EvidenceLogger.log("research", "tool_call", {
            "tool_name": "threshold_research", "output_hash": th, "exit_code": tc,
            "duration_sec": round(td_, 1),
            "stdout_snippet": (
                f"status={stage_status['threshold_research']} "
                f"verified_thresholds={n_found}/{len(lit)}"
            ),
        }, targets=["both"], phase="research")
        print(f"[research] Thresholds: {n_found} verified literature overrides")
    except Exception as e:
        EvidenceLogger.error("research", "tool_failure", f"threshold_research: {e}",
                             recovery="fallback to DEFAULT_THRESHOLDS")
        print(f"[research] threshold_research failed, using defaults: {e}")
        stage_status["threshold_research"] = "failed"
        stage_context["threshold_research"] = ({}, str(e))
        fallbacks.append("provisional_default_thresholds")

    if stage_status.get("threshold_research") != "complete":
        fallbacks.append("provisional_default_thresholds")
    thresholds, control_calibration = _research._apply_control_calibration(thresholds, _research._cfg())
    return thresholds, control_calibration, threshold_normalization, final_normalization



def _run_generic_pipeline():
    """Target-configured research path without MDM-specific biological fallbacks."""
    _research._ensure_runtime_dirs()
    target_ids = list(_research._module_attr("PROJECT_TARGET_IDS"))
    stage_status = {}
    stage_context = {}
    fallbacks = []

    sr = _step_generic_search(stage_status, stage_context, target_ids)
    er = _step_generic_enrich(stage_status, stage_context, target_ids, sr)
    aggregate = _step_generic_interface(stage_status, stage_context, fallbacks, er)
    pmr = _step_generic_pubmed(stage_status, stage_context, target_ids)
    known_binders, approved_binders = _step_generic_llm(
        stage_status, stage_context, fallbacks, target_ids, pmr,
    )
    thresholds, control_calibration, threshold_normalization, final_normalization = (
        _step_generic_thresholds(stage_status, stage_context, fallbacks, target_ids)
    )

    dynamic_pdb_list = [row.get("pdb_id") for row in er.get("peptide_complexes", []) if row.get("pdb_id")]
    stage_error_code, error_message = _research._diagnostics_for_stages(stage_status, stage_context)
    result = {
        "project_id": _research._cfg()["project_id"],
        "targets": {target["id"]: target for target in _research._cfg()["targets"]},
        "pocket_differences": {
            "_source": "dynamic_interface_aggregation",
            "targets": aggregate.get("results_by_target", {}),
        },
        "known_binders": known_binders,
        "known_dual_binders": known_binders,
        "known_binder_source": (
            "approved_project_config_and_llm"
            if approved_binders and any(
                binder.get("provenance") != "approved_project_config"
                for binder in known_binders
            )
            else "approved_project_config"
            if approved_binders
            else "llm_extracted"
            if known_binders
            else "none_found"
        ),
        "design_strategy_summary": "Target-configured; derive design constraints from retrieved epitope evidence.",
        "data_quality_alert": "No MDM-specific constants were used as fallback.",
        "literature_refs": pmr.get("pmids", []),
        "thresholds": thresholds,
        "_pipeline_meta": {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "dynamic_pdb_list": dynamic_pdb_list,
            "counts_by_target": (
                aggregate.get("counts_by_target")
                or er.get("n_by_target", {})
            ),
            "stage_status": stage_status,
            "stage_error_code": stage_error_code,
            "error_message": error_message,
            "fallbacks_used": list(dict.fromkeys(fallbacks)),
            "control_calibration": control_calibration,
            "run_status": _research._overall_run_status(stage_status),
        },
        "_cache_meta": _research._cache_meta(_research._cfg()),
    }
    _research._atomic_write_json(_research._module_attr("CACHE_PATH"), result)
    return result


def _step_generic_search(stage_status: dict, stage_context: dict, target_ids: list) -> dict:
    sr, se, sc, sd, sh = _research._run_script("search_pdb.py")
    stage_status["rcsb_search"] = "complete" if sc == 0 and sr.get("run_status") == "complete" else "empty" if sc == 0 else "failed"
    stage_context["rcsb_search"] = (sr, se)
    EvidenceLogger.log("research", "tool_call", {
        "tool_name": "rcsb_search_api", "output_hash": sh, "exit_code": sc,
        "duration_sec": round(sd, 1), "stdout_snippet": str(sr.get("counts_by_target", {})),
    }, targets=target_ids, phase="research")
    return sr


def _step_generic_enrich(stage_status: dict, stage_context: dict, target_ids: list, sr: dict) -> dict:
    er, ee, ec, ed, eh = _research._run_script("enrich_pdb.py", sr)
    stage_status["rcsb_enrich"] = "complete" if ec == 0 and er.get("n_peptide_complexes", 0) > 0 else "empty" if ec == 0 else "failed"
    stage_context["rcsb_enrich"] = (er, ee)
    EvidenceLogger.log("research", "tool_call", {
        "tool_name": "rcsb_graphql_api", "output_hash": eh, "exit_code": ec,
        "duration_sec": round(ed, 1), "stdout_snippet": str(er.get("counts_by_target", {})),
    }, targets=target_ids, phase="research")
    return er


def _step_generic_interface(
    stage_status: dict, stage_context: dict, fallbacks: list, er: dict
) -> dict:
    aggregate = {"results_by_target": {}, "counts_by_target": {}}
    if os.environ.get("SKIP_BIOTITE", "").lower() in ("1", "true", "yes"):
        stage_status["interface"] = "skipped"
        stage_status["aggregate"] = "skipped"
        stage_context.update({name: ({}, "") for name in ("interface", "aggregate")})
        fallbacks.append("interface_aggregation_omitted")
    else:
        try:
            ir, ie, ic, id_, ih = _research._run_script("compute_interface.py", er)
            stage_status["interface"] = "complete" if ic == 0 and ir.get("n_with_interface", 0) else "empty" if ic == 0 else "failed"
            stage_context["interface"] = (ir, ie)
            EvidenceLogger.log("research", "tool_call", {
                "tool_name": "biotite", "output_hash": ih, "exit_code": ic,
                "duration_sec": round(id_, 1),
                "stdout_snippet": f"interfaces={ir.get('n_with_interface', 0)}",
            }, targets=target_ids, phase="research")
            aggregate, ae, ac, ad, ah = _research._run_script("aggregate_pockets.py", ir)
            has_aggregate = bool(aggregate.get("results_by_target") or aggregate.get("counts_by_target"))
            stage_status["aggregate"] = "complete" if ac == 0 and has_aggregate else "empty" if ac == 0 else "failed"
            stage_context["aggregate"] = (aggregate, ae)
            EvidenceLogger.log("research", "tool_call", {
                "tool_name": "aggregate_pockets", "output_hash": ah, "exit_code": ac,
                "duration_sec": round(ad, 1),
                "stdout_snippet": str(aggregate.get("counts_by_target", {})),
            }, targets=target_ids, phase="research")
        except Exception as exc:
            stage_status["interface"] = "failed"
            stage_status["aggregate"] = "failed"
            stage_context["interface"] = ({}, str(exc))
            stage_context["aggregate"] = ({}, str(exc))
            fallbacks.append("interface_aggregation_omitted")
            EvidenceLogger.error("research", "tool_failure", str(exc), recovery="continue without interface aggregation")
    return aggregate


def _step_generic_pubmed(stage_status: dict, stage_context: dict, target_ids: list) -> dict:
    pmr, pme, pc, pd, ph = _research._run_script("pubmed_search.py")
    stage_status["pubmed"] = "complete" if pc == 0 and pmr.get("n_total", 0) > 0 else "empty" if pc == 0 else "failed"
    stage_context["pubmed"] = (pmr, pme)
    EvidenceLogger.log("research", "tool_call", {
        "tool_name": "pubmed_eutils", "output_hash": ph, "exit_code": pc,
        "duration_sec": round(pd, 1), "stdout_snippet": f"papers={pmr.get('n_total', 0)}",
    }, targets=target_ids, phase="research")
    return pmr


def _step_generic_llm(
    stage_status: dict, stage_context: dict, fallbacks: list, target_ids: list, pmr: dict
) -> tuple[list, list]:
    approved_binders = _research._approved_known_binders(_research._cfg())
    known_binders = list(approved_binders)
    try:
        lr, le, lc, ld, lh = _research._run_script("llm_extract.py", pmr, extra_args=["--concurrency", "3"])
        extracted_binders = lr.get("known_binders", [])
        known_binders = _research._merge_known_binders(
            approved_binders, extracted_binders
        )
        raw_llm_status = lr.get("run_status", "failed")
        stage_status["llm_extract"] = (
            "degraded_no_api_key" if raw_llm_status == "degraded_no_api_key"
            else "complete" if lc == 0 and raw_llm_status == "complete"
            else "failed"
        )
        stage_context["llm_extract"] = (lr, le)
        if not known_binders:
            fallbacks.append("no_binder_fallback")
        EvidenceLogger.log("research", "tool_call", {
            "tool_name": "llm_extract", "output_hash": lh, "exit_code": lc,
            "duration_sec": round(ld, 1), "stdout_snippet": f"binders={len(known_binders)}",
        }, targets=target_ids, phase="research")
    except Exception as exc:
        stage_status["llm_extract"] = "failed"
        stage_context["llm_extract"] = ({}, str(exc))
        fallbacks.append("no_binder_fallback")
        EvidenceLogger.error("research", "tool_failure", str(exc), recovery="no fabricated binder fallback")
    return known_binders, approved_binders


def _step_generic_thresholds(
    stage_status: dict, stage_context: dict, fallbacks: list, target_ids: list
) -> tuple[dict, dict, dict, dict]:
    threshold_normalization = {}
    final_normalization = {}
    thresholds = _research.default_thresholds(_research._cfg())
    try:
        tr, te, tc, td, thash = _research._run_script("threshold_research.py", extra_args=["--concurrency", "4"])
        literature_thresholds, threshold_normalization = normalize_thresholds(tr.get("metric_battery", {}))
        for key, info in literature_thresholds.items():
            if info.get("auto_usable") and info.get("value") is not None:
                thresholds[key] = {
                    **{name: value for name, value in thresholds.get(key, {}).items()
                       if name in ("method", "min_seed_fraction")},
                    "value": info["value"], "operator": info.get("operator", ">"),
                    "unit": info.get("unit"), "source": f"PubMed PMID {info.get('source_pmid')}",
                    "source_pmid": info.get("source_pmid"), "evidence_quote": info.get("evidence_quote"),
                    "evidence_grade": "paper_explicit", "quote_verified": True,
                    "calibration_status": "pending",
                    "applicable_targets": target_ids,
                }
        raw_threshold_status = tr.get("_meta", {}).get("run_status", "failed")
        stage_status["threshold_research"] = (
            "degraded_no_api_key" if raw_threshold_status == "degraded_no_llm"
            else "empty" if raw_threshold_status == "degraded_empty_results"
            else "complete" if tc == 0 and raw_threshold_status == "complete"
            else "failed"
        )
        stage_context["threshold_research"] = (tr, te)
        thresholds, final_normalization = normalize_thresholds(thresholds)
        EvidenceLogger.log("research", "tool_call", {
            "tool_name": "threshold_research", "output_hash": thash, "exit_code": tc,
            "duration_sec": round(td, 1),
            "stdout_snippet": f"usable={tr.get('_meta', {}).get('n_auto_usable', 0)}",
        }, targets=target_ids, phase="research")
    except Exception as exc:
        stage_status["threshold_research"] = "failed"
        stage_context["threshold_research"] = ({}, str(exc))
        fallbacks.append("provisional_default_thresholds")

    if stage_status.get("threshold_research") != "complete":
        fallbacks.append("provisional_default_thresholds")
    thresholds, control_calibration = _research._apply_control_calibration(thresholds, _research._cfg())
    _research._write_threshold_cache(thresholds, _research._cfg(), {
        "literature_input": threshold_normalization if "threshold_normalization" in locals() else {},
        "final": final_normalization if "final_normalization" in locals() else {},
        "control_calibration": control_calibration,
    })
    if not _research._module_attr("THRESHOLDS_CACHE").exists():
        _research._write_threshold_cache(thresholds, _research._cfg())
        EvidenceLogger.error("research", "tool_failure", str(exc), recovery="provisional thresholds remain non-clearable")
    return thresholds, control_calibration, threshold_normalization, final_normalization
