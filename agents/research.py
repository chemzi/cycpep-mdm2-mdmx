"""
Research Agent — 刘函赫
职责：编排「PDB 检索 → biotite 界面 → Cα 叠合差异 → PubMed 检索 → 文献人工提取」五步调研，
      每一步挂 tool_trace（tool_name/tool_version/exit_code/output_sha256），
      从产物派生 targets / pocket_differences / known_dual_binders 写入共享白板。
入口：run(state=None, recompute=False) -> dict
依赖：from data_layer import State, EvidenceLogger

可追溯 / 可复现（回应 review）
------------------------------------------------
本模块不再硬编码结论，而是**从仓库内的溯源产物派生**：

    scripts/                     6 步流水线脚本（可从零复算）
      search_pdb.py              RCSB Search API v2   -> pdb_search_results.json
      enrich_pdb.py              RCSB Data GraphQL    -> pdb_enriched.json
      compute_interface.py       biotite <4A + <=2.8A -> interface_per_structure.json
      aggregate_pockets.py       共识口袋             -> pocket_report.json
      superpose_analyze.py       biotite Cα 叠合      -> pocket_differences.json
      pubmed_search.py           NCBI E-utilities     -> pubmed_catalog.json
    scripts/run_pipeline.py      编排器（按序跑 6 步、产出 tool_trace）
    scripts/provenance/          6 个产物 + 2 份摘要 + MANIFEST.json（各产物 sha256）
    scripts/provenance/curation.json  人工从 PMID 摘要提取的双靶 binder 表 + 设计解读

两种运行模式：
  run(recompute=False)  默认。校验 provenance 产物 sha256 与 MANIFEST 一致，
                        从产物派生结论，逐步挂 verify 型 tool_trace。离线、秒级。
  run(recompute=True)   调 scripts/run_pipeline 真跑 6 步（需联网 + biotite），
                        用新产物覆盖 provenance，挂 live 型 tool_trace（真实 exit_code）。

所有 pocket 残基、口袋差异数值、复合物计数、PDB 列表、PMID 均来自上述产物，
非人工填写；binder 目录为人工阅读摘要提取（无 LLM 调用），每条标注 PMID 且校验其在 catalog 内。
"""
import hashlib
import json
import os
import sys

from data_layer import State, EvidenceLogger

# --- 让 agents/ 能 import scripts/run_pipeline ---
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
import run_pipeline as rp  # noqa: E402  (STAGES / sha256 / tool_versions / run_pipeline)

PROV_DIR = rp.DEFAULT_OUTDIR                       # scripts/provenance
MANIFEST_PATH = os.path.join(PROV_DIR, "MANIFEST.json")
CURATION_PATH = os.path.join(PROV_DIR, "curation.json")

MAX_RESOLUTION = 2.8   # 与 compute_interface.py 的过滤条件一致；run() 会强制断言
TARGET_UNIPROT = {"MDM2": "Q00987", "MDMX": "O15151"}
# 输入固定的参考 PDB（原样保留；小分子结构见 curation.data_quality_alert）
REFERENCE_PDB = {"MDM2": ["1YCR", "4HG7", "3V3B"], "MDMX": ["3DAB", "3LBK"]}
VERIFIED_PEPTIDE_REF = {"MDM2": ["1YCR", "3V3B"], "MDMX": ["3DAB"]}


# ============================================================
# 小工具
# ============================================================
def _sha256(path):
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _load(name):
    with open(os.path.join(PROV_DIR, name), encoding="utf-8") as f:
        return json.load(f)


def _fmt_res(entry):
    """[58,'GLY'] -> 'Gly58'（滤掉水）。"""
    resid, resname = entry[0], entry[1]
    return None if resname == "HOH" else f"{resname.capitalize()}{resid}"


def _consensus_residues(pocket_report, target, pocket):
    out = []
    for e in pocket_report[target]["pockets"][pocket]["consensus"]:
        r = _fmt_res(e)
        if r:
            out.append(r)
    return out


# ============================================================
# 每步产物 -> tool_trace（校验模式：核对已提交产物的 sha256）
# ============================================================
def _verify_traces(manifest):
    """核对 provenance 各产物 sha256 与 MANIFEST 是否一致，返回 tool_trace 列表。"""
    versions = rp.tool_versions()
    man_by_out = {s["output_file"]: s for s in manifest["stages"]}
    traces = []
    for s in rp.STAGES:
        out_file = s["output_file"]
        out_path = os.path.join(PROV_DIR, out_file)
        digest = _sha256(out_path)
        expected = man_by_out.get(out_file, {}).get("output_sha256")
        match = (digest is not None and digest == expected)
        traces.append({
            "stage": s["stage"], "name": s["name"], "mode": "verify_committed_artifact",
            "tool_name": s["tool_name"],
            "tool_version": rp.resolve_versions(s, versions),
            "endpoint": s["endpoint"],
            "cmd": f"python scripts/{s['script']}",
            "exit_code": 0 if match else 1,
            "input_file": s["input_file"], "output_file": out_file,
            "output_sha256": digest, "manifest_sha256": expected,
            "sha256_match": match,
        })
    return traces


def _live_traces(recompute_outdir=None, structdir=None):
    """真跑 6 步脚本（联网+biotite），返回带真实 exit_code/duration 的 tool_trace。"""
    outdir = recompute_outdir or PROV_DIR
    structdir = structdir or os.path.join(PROV_DIR, "_structures")
    traces = rp.run_pipeline(outdir=outdir, structdir=structdir)
    for t in traces:
        t["mode"] = "live_run"
    return traces


# ============================================================
# 从产物派生结论
# ============================================================
def _derive(manifest, interface, pocket_report, pdiff, catalog, curation):
    # --- 强制分辨率约束（回应 review #5：约束必须可执行）---
    bad = []
    for tgt, entries in interface.items():
        for e in entries:
            res = e.get("resolution")
            if res is None or res > MAX_RESOLUTION:
                bad.append((tgt, e.get("pdb"), res))
    if bad:
        raise AssertionError(
            f"分辨率约束 <= {MAX_RESOLUTION} A 被违反，共 {len(bad)} 条: {bad[:5]}"
        )

    # --- 复合物计数 / PDB 列表：全部来自产物 ---
    verified_pdb = {t: [e["pdb"] for e in interface[t]] for t in interface}
    n_complexes = {t: pocket_report[t]["n_complexes"] for t in pocket_report}

    # --- targets：口袋残基来自 pocket_report 的共识（非硬编码）---
    targets = {}
    for t in ("MDM2", "MDMX"):
        targets[t] = {
            "uniprot": TARGET_UNIPROT[t],
            "reference_pdb": REFERENCE_PDB[t],
            "verified_peptide_pdb": VERIFIED_PEPTIDE_REF[t],
            "n_peptide_complexes": n_complexes[t],
            "pocket_residues": {
                p: _consensus_residues(pocket_report, t, p)
                for p in ("Phe19_pocket", "Trp23_pocket", "Leu26_pocket")
            },
        }

    # --- pocket_differences：数值来自 pocket_differences.json，解读来自 curation ---
    rules = curation["pocket_design_rules"]
    sasa = pdiff["pocket_sasa"]
    pocket_diff = {
        "_method": (
            f"biotite heavy-atom<4A interface over {n_complexes['MDM2']} MDM2 + "
            f"{n_complexes['MDMX']} MDMX peptide complexes (<= {MAX_RESOLUTION} A); "
            f"CA superposition RMSD {pdiff['superposition_rmsd_A']} A over "
            f"{pdiff['n_matched_CA']} CA. Source: scripts/provenance/pocket_differences.json"
        ),
        "residue_equivalence": pdiff["residue_equivalence_pockets"],
        "gatekeeper": pdiff["gatekeeper"],
        "leu26_floor": pdiff["leu26_floor"],
        "anchor_depth": pdiff["anchor_depth"],
    }
    for p in ("Phe19_pocket", "Trp23_pocket", "Leu26_pocket"):
        pocket_diff[p] = {
            "MDM2_residues": targets["MDM2"]["pocket_residues"][p],
            "MDMX_residues": targets["MDMX"]["pocket_residues"][p],
            "apo_sidechain_sasa_A2": sasa[p],
            "difference_description": rules[p]["difference_description"],
            "design_rule": rules[p]["design_rule"],
        }

    # --- known_dual_binders：人工提取表 + 校验每条 PMID 在 catalog 内（回应 review #1）---
    catalog_pmids = {c["pmid"] for c in catalog["catalog"]}
    binders = curation["known_dual_binders"]
    missing = [b["name"] for b in binders if b.get("pmid") not in catalog_pmids]
    if missing:
        raise AssertionError(
            f"binder 引用的 PMID 未出现在 pubmed_catalog.json 中: {missing}"
        )

    return {
        "targets": targets,
        "pocket_differences": pocket_diff,
        "known_dual_binders": binders,
        "design_strategy_summary": curation["design_strategy_summary"],
        "data_quality_alert": curation["data_quality_alert"],
        "literature_refs": curation["literature_refs"],
        "verified_pdb": verified_pdb,
        "n_complexes": n_complexes,
        "n_unique_pmids": len(catalog_pmids),
    }


# ============================================================
# 入口
# ============================================================
def run(state: dict = None, recompute: bool = False) -> dict:
    """靶点调研主入口（编排器）。

    recompute=False（默认）：校验 scripts/provenance/ 各产物 sha256 与 MANIFEST 一致，
        从产物派生 targets / pocket_differences / known_dual_binders，逐步挂
        research_tool_call（verify 型 tool_trace），最后 research_complete 汇总全链。
        离线、秒级、无需 biotite。
    recompute=True：调 scripts/run_pipeline 真跑 6 步（联网 + biotite），用新产物
        覆盖 provenance 后再派生，挂 live 型 tool_trace（真实 exit_code/duration）。

    返回：写入 state.json 的结果字典。
    """
    if state is None:
        state = State.load()

    manifest = json.loads(open(MANIFEST_PATH, encoding="utf-8").read())

    # 1) 跑/校验流水线 → 每步 tool_trace
    if recompute:
        traces = _live_traces()
        failed = [t for t in traces if t["exit_code"] != 0]
        if failed or len(traces) != len(rp.STAGES):
            EvidenceLogger.error("research", "pipeline_failed",
                                 f"recompute 未跑完 6 步: 完成 {len(traces)}，失败 {len(failed)}")
            raise RuntimeError("recompute 流水线未全部成功，详见 tool_trace / stderr。")
    else:
        traces = _verify_traces(manifest)
        mism = [t["name"] for t in traces if not t["sha256_match"]]
        if mism:
            EvidenceLogger.error("research", "artifact_hash_mismatch",
                                 f"provenance 产物 sha256 与 MANIFEST 不一致: {mism}")
            raise RuntimeError(f"产物校验失败（sha256 不匹配）: {mism}")

    # 每步单独记一条证据（tool_name/tool_version/exit_code/output_sha256）
    for t in traces:
        EvidenceLogger.log("research", "research_tool_call", {"tool_trace": t},
                           targets=["both"], phase="research", round_num=1)

    # 2) 从产物派生结论
    interface = _load("interface_per_structure.json")
    pocket_report = _load("pocket_report.json")
    pdiff = _load("pocket_differences.json")
    catalog = _load("pubmed_catalog.json")
    curation = _load("curation.json")
    derived = _derive(manifest, interface, pocket_report, pdiff, catalog, curation)

    # 3) 写共享白板
    targets = state.get("targets", {})
    for name, info in derived["targets"].items():
        targets.setdefault(name, {})
        targets[name].update(info)
    result = {
        "targets": targets,
        "pocket_differences": derived["pocket_differences"],
        "known_dual_binders": derived["known_dual_binders"],
        "design_strategy_summary": derived["design_strategy_summary"],
        "data_quality_alert": derived["data_quality_alert"],
    }
    State.update(result)

    # 4) research_complete 汇总全链（含每步 tool_trace + 产物 sha256）
    hotspot_analysis = {
        "pdb_list": derived["verified_pdb"]["MDM2"] + derived["verified_pdb"]["MDMX"],
        "n_mdm2_peptide_complexes": derived["n_complexes"]["MDM2"],
        "n_mdmx_peptide_complexes": derived["n_complexes"]["MDMX"],
        "n_unique_pmids": derived["n_unique_pmids"],
        "resolution_max_enforced_A": MAX_RESOLUTION,
        "method": derived["pocket_differences"]["_method"],
        "superposition_rmsd_A": pdiff["superposition_rmsd_A"],
        "pockets": {p: derived["pocket_differences"][p]
                    for p in ("Phe19_pocket", "Trp23_pocket", "Leu26_pocket")},
        "provenance_dir": "scripts/provenance",
        "manifest_path": "scripts/provenance/MANIFEST.json",
        "mode": "recompute" if recompute else "verify_committed_artifact",
        "tool_trace": traces,
        "data_quality_alert": derived["data_quality_alert"],
    }
    EvidenceLogger.research_complete(
        hotspot_analysis=hotspot_analysis,
        known_binders=derived["known_dual_binders"],
        refs=derived["literature_refs"],
    )
    return result


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Research Agent：编排/校验 6 步调研并写入 state.json")
    ap.add_argument("--recompute", action="store_true",
                    help="真跑 6 步流水线（联网+biotite）而非校验已提交产物")
    args = ap.parse_args()
    out = run(recompute=args.recompute)
    n_m2 = out["targets"]["MDM2"]["n_peptide_complexes"]
    n_mx = out["targets"]["MDMX"]["n_peptide_complexes"]
    print(f"[research] state.json 已更新：targets / pocket_differences / known_dual_binders")
    print(f"[research] 肽复合物 MDM2={n_m2} MDMX={n_mx}；已知双靶分子 {len(out['known_dual_binders'])} 个")
    print(f"[research] 模式={'recompute' if args.recompute else 'verify'}；当前 phase={State.load().get('phase')}")
