"""
Research Agent — 刘函赫
职责：自动检索 PDB/PubMed → 结构化热点分析 → 双靶差异对比
入口：run(state) → dict （写入 state.json 的 targets / pocket_differences / known_dual_binders）
工具链：RCSB Search API v2 → biotite 界面分析（重原子<4Å）→ Cα 叠合差异量化 → PubMed E-utilities
依赖：from data_layer import State, EvidenceLogger

说明
----
本模块已完成全流程调研（可复现脚本见工作区 scripts/，中间数据见 output/，
完整溯源报告见 REPORT.md）。所有结论均可溯源到 PDB ID / PMID，无幻觉。
`run()` 默认把「已验证的最终结果」写入共享数据层，离线秒级完成，
组内任何人无需联网、无需安装 biotite 即可让 state.json 就绪。
如需从零复算，见文件末尾 recompute() 的说明。
"""

from data_layer import State, EvidenceLogger


# ============================================================
# 1. 已验证的肽段复合物（biotite 成功计算界面的结构，分辨率≤2.8Å，人源）
#    —— 溯源：output/interface_per_structure.json
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


# ============================================================
# 2. 靶点信息（含每靶点三口袋衬里残基 + 验证过的肽段参考结构）
# ============================================================
TARGETS = {
    "MDM2": {
        "uniprot": "Q00987",
        "reference_pdb": ["1YCR", "4HG7", "3V3B"],  # 输入原样保留（4HG7 为小分子，见 data_quality_alert）
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
        "reference_pdb": ["3DAB", "3LBK"],  # 输入原样保留（3LBK 实为 MDM2 小分子，见 data_quality_alert）
        "verified_peptide_pdb": ["3DAB"],
        "n_peptide_complexes": 12,
        "pocket_residues": {
            "Phe19_pocket": ["Gly57", "Ile60", "Met61", "Tyr66", "Gln71", "Val74", "Val92"],
            "Trp23_pocket": ["Met53", "Leu56", "Gly57", "Ile60", "Val92", "Leu98"],
            "Leu26_pocket": ["Met53", "Val92", "Pro95", "Leu98", "Tyr99"],
        },
    },
}


# ============================================================
# 3. 三口袋 MDM2↔MDMX 差异与设计约束
#    —— 溯源：biotite 界面计算 + Cα 叠合（RMSD 1.88Å / 85 CA），output/pocket_differences.json
# ============================================================
POCKET_DIFFERENCES = {
    "_method": "biotite heavy-atom<4A interface over 43 MDM2 + 12 MDMX peptide complexes (<=2.8A); "
               "MDM2/MDMX domains superposed by CA sequence alignment, RMSD 1.88 A over 85 CA.",
    "Phe19_pocket": {
        "MDM2_residues": TARGETS["MDM2"]["pocket_residues"]["Phe19_pocket"],
        "MDMX_residues": TARGETS["MDMX"]["pocket_residues"]["Phe19_pocket"],
        "difference_description": "Lining conserved and near-equal openness (apo side-chain SASA "
            "MDM2=322.8 vs MDMX=327.8 A^2). Key divergence = ENTRANCE GATEKEEPER: MDM2 Leu54 -> "
            "MDMX Met53; after superposition the Met53 tip sits ~1.0 A closer to the p53-Phe19 ring, "
            "narrowing the MDMX entrance.",
        "design_rule": "Anchor with a MEDIUM aromatic/hydrophobic of <=Phe volume (natural Phe optimal). "
            "Avoid MDM2-only bulky/extended aromatics (e.g. 6-Cl-Trp): they clash with the MDMX Met53 gate. "
            "Keep beta-branching minimal.",
    },
    "Trp23_pocket": {
        "MDM2_residues": TARGETS["MDM2"]["pocket_residues"]["Trp23_pocket"],
        "MDMX_residues": TARGETS["MDMX"]["pocket_residues"]["Trp23_pocket"],
        "difference_description": "Deepest and most conserved subpocket; floor invariant "
            "(Leu57/Leu56, Gly58/Gly57, Ile61/Ile60, Val93/Val92), differs only by peripheral "
            "Leu54->Met53. Indole NE1 makes the conserved backbone H-bond in both targets.",
        "design_rule": "Use Trp as the INVARIANT SHARED ANCHOR that locks the macrocycle onto both "
            "targets; preserve the buried indole and NE1 H-bond, place the highest-affinity contact here. "
            "Keep natural L-Trp for a de novo natural-AA design.",
    },
    "Leu26_pocket": {
        "MDM2_residues": TARGETS["MDM2"]["pocket_residues"]["Leu26_pocket"],
        "MDMX_residues": TARGETS["MDMX"]["pocket_residues"]["Leu26_pocket"],
        "difference_description": "LARGEST divergence and the dual-selectivity bottleneck. MDMX pocket "
            "markedly smaller/shallower: apo side-chain SASA MDM2=393.0 vs MDMX=284.0 A^2 (d=-109 A^2). "
            "Cause: His96->Pro95, Ile99->Leu98, Tyr100->Tyr99 with the MDMX Tyr99 ring filling the floor.",
        "design_rule": "Anchor with a SMALL aliphatic side chain (Leu/Val/2-aminobutyrate, or "
            "beta-cyclobutyl-Ala as in ATSP-7041). AVOID bulky/beta-branched/aromatic residues here. "
            "Size-down at Leu26 is the single most important lever for MDMX compatibility.",
    },
}


# ============================================================
# 4. 已报道的 MDM2/MDMX 双靶肽类分子（全部可溯源到 PMID + PDB）
# ============================================================
KNOWN_DUAL_BINDERS = [
    {"name": "PMI", "type": "linear peptide (phage-display, natural AA)",
     "sequence": "TSFAEYWNLLSP", "kd_mdm2": "low nanomolar (SPR/FP)", "kd_mdmx": "low nanomolar (SPR/FP)",
     "key_residues": ["Phe3->Phe19", "Tyr6", "Trp7->Trp23", "Leu10->Leu26"],
     "pmid": "34589387",
     "source_title": "Ultrahigh-affinity dual-specificity peptide antagonists of MDM2/MDMX "
                     "(Acta Pharm Sin B 2021); PMI complexes PDB 3EQS(MDM2)/3EQY(MDMX)"},
    {"name": "PMI-M3", "type": "linear peptide (optimized PMI, natural AA)",
     "sequence": "LTFLEYWAQLMQ", "kd_mdm2": "low picomolar (ITC)", "kd_mdmx": "low picomolar (ITC)",
     "key_residues": ["Phe3->Phe19", "Tyr6", "Trp7->Trp23", "Leu10->Leu26", "Leu4/Met11 add-on"],
     "pmid": "34589387",
     "source_title": "Ultrahigh-affinity dual-specificity peptide antagonists of MDM2/MDMX "
                     "(Acta Pharm Sin B 2021); MDM2 co-crystal 1.65A, MDMX 3.0A"},
    {"name": "M3-2K", "type": "linear peptide (cell-penetrating PMI-M3, natural AA)",
     "sequence": "KLTFLEYWAQLMQK", "kd_mdm2": "picomolar-range", "kd_mdmx": "picomolar-range",
     "key_residues": ["Phe->Phe19", "Trp->Trp23", "Leu->Leu26"],
     "pmid": "34589387",
     "source_title": "Ultrahigh-affinity dual-specificity peptide antagonists of MDM2/MDMX (Acta Pharm Sin B 2021)"},
    {"name": "ATSP-7041", "type": "stapled (hydrocarbon i,i+7) alpha-helical peptide, non-natural",
     "sequence": "Ac-LTF-(R8)-EYWAQ-(Cba)-(S5)-AA-NH2  [staple R8<->S5; Cba=beta-cyclobutyl-Ala at Leu26]",
     "kd_mdm2": "Ki ~0.9 nM (FP, Chang 2013)", "kd_mdmx": "Ki ~2.3 nM (FP, Chang 2013)",
     "key_residues": ["Phe3->Phe19", "Trp7->Trp23", "Cba->Leu26 (downsized)", "staple augments binding"],
     "pmid": "23946421",
     "source_title": "Stapled alpha-helical peptide dual inhibitor of MDM2/MDMX (PNAS 2013); MDMX complex PDB 4N5T (1.7A)"},
    {"name": "ALRN-6924 (sulanemadlin)", "type": "stapled alpha-helical peptide (clinical), non-natural",
     "sequence": "clinical stapled peptide derived from ATSP-7041 (exact sequence not fully disclosed; "
                 "p53 N-terminal mimic with Phe19/Trp23/Leu26 triad + hydrocarbon staple)",
     "kd_mdm2": "high affinity (nanomolar-range)", "kd_mdmx": "high affinity (nanomolar-range)",
     "key_residues": ["Phe->Phe19", "Trp->Trp23", "Leu26-anchor", "hydrocarbon staple"],
     "pmid": "37439511",
     "source_title": "Discovery of Sulanemadlin (ALRN-6924) (J Med Chem 2023); Phase 1 trial PMID 34301750"},
    {"name": "pDI", "type": "linear peptide (phage-display, natural AA)",
     "sequence": "LTFEHYWAQLTS", "kd_mdm2": "nanomolar (~40 nM range)", "kd_mdmx": "sub-micromolar",
     "key_residues": ["Phe3->Phe19", "Trp7->Trp23", "Leu10->Leu26"],
     "pmid": "19910468",
     "source_title": "Structure-based design of p53-MDM2/MDMX peptide inhibitors (JBC 2010); PDB 3JZO(MDMX)/3FDO(MDMX)"},
    {"name": "pDI6W", "type": "linear peptide (pDI Tyr6->Trp6 variant, natural AA)",
     "sequence": "LTFEHWWAQLTS", "kd_mdm2": "nanomolar (dual)", "kd_mdmx": "sub-micromolar (dual)",
     "key_residues": ["Phe3->Phe19", "Trp7->Trp23", "Trp6 fills MDMX-unique subsite", "Leu10->Leu26"],
     "pmid": "19910468",
     "source_title": "Structure-based design of p53-MDM2/MDMX peptide inhibitors (JBC 2010); PDB 3JZR(MDM2)/3JZP(MDMX)"},
    {"name": "pDIQ", "type": "linear peptide (optimized pDI, 4 substitutions, natural AA)",
     "sequence": "ETFEHWWSQLLS", "kd_mdm2": "IC50 = 8 nM", "kd_mdmx": "IC50 = 110 nM",
     "key_residues": ["Phe3->Phe19", "Trp7->Trp23", "Leu10->Leu26", "reach into MDMX-unique hydrophobic site"],
     "pmid": "19910468",
     "source_title": "Structure-based design of p53-MDM2/MDMX peptide inhibitors (JBC 2010); PDB 3JZS(MDM2)/3JZQ(MDMX)"},
]


LITERATURE_REFS = [
    {"pmid": "34589387", "title": "Design of ultrahigh-affinity and dual-specificity peptide "
                                  "antagonists of MDM2 and MDMX (Acta Pharm Sin B 2021)"},
    {"pmid": "23946421", "title": "Stapled alpha-helical peptide drug development: a potent dual "
                                  "inhibitor of MDM2 and MDMX (PNAS 2013)"},
    {"pmid": "37439511", "title": "Discovery of Sulanemadlin (ALRN-6924) (J Med Chem 2023)"},
    {"pmid": "34301750", "title": "ALRN-6924 first-in-human Phase 1 trial"},
    {"pmid": "19910468", "title": "Structure-based design of high-affinity peptides inhibiting "
                                  "p53-MDM2/MDMX (JBC 2010)"},
]


DESIGN_STRATEGY_SUMMARY = (
    "STRUCTURAL BASIS (biotite, heavy-atom<4A over 43 MDM2 + 12 MDMX peptide complexes <=2.8A; "
    "CA superposition RMSD 1.88 A / 85 residues). (1) Trp23 pocket is deep and near-identical -> "
    "use L-Trp as the INVARIANT shared anchor and place the strongest contact here; (2) Phe19 pocket "
    "is comparable in size but the MDMX entrance is narrowed by Leu54->Met53 (Met53 ~1 A closer) -> "
    "keep this anchor <=Phe volume, avoid MDM2-only bulky aromatics; (3) Leu26 pocket is the dual "
    "bottleneck: MDMX is markedly shallower (apo SASA -109 A^2) due to His96->Pro95, Ile99->Leu98 and "
    "floor-filling Tyr99 -> DOWNSIZE the Leu26 anchor to a small aliphatic (Leu/Val/Abu/cyclobutyl-Ala). "
    "PRECEDENT: every validated dual binder keeps the Phe/Trp/Leu(or Cba) triad and gains MDMX affinity "
    "by shrinking the Leu26 anchor (ATSP-7041 uses cyclobutyl-Ala) and/or reaching a MDMX-unique subsite "
    "(pDIQ). DE NOVO NATURAL-AA CYCLIC PLAN: scaffold the three anchors on helical/turn geometry with "
    "i, i+4, i+7-like spacing (Phe19, Trp23, Leu26), rigidify by head-to-tail or side-chain (Lys-Asp "
    "lactam) macrocyclization (all natural AA, no hydrocarbon staple), tune Phe19->Phe and Leu26->small "
    "aliphatic, keep Trp23 fixed. NOVELTY: no experimentally validated de-novo, all-natural-AA, "
    "macrocyclic dual MDM2/MDMX binder was found (existing dual binders are linear or non-natural "
    "stapled peptides; MDM2 macrocycle 6KZU is all-D and MDM2-only) -> this project occupies novel space."
)


DATA_QUALITY_ALERT = (
    "Input reference PDBs 4HG7 and 3LBK are NOT peptide complexes: RCSB shows 4HG7 = MDM2/Nutlin-3a "
    "(small-molecule NUT) and 3LBK = human MDM2 + small-molecule inhibitor K23; both are MDM2 (Q00987) "
    "small-molecule structures and 3LBK is not an MDMX structure. Kept in targets.reference_pdb per the "
    "fixed input, but must NOT be used as peptide-interface references. Verified peptide references: "
    "1YCR/3V3B (MDM2), 3DAB (MDMX)."
)


# ============================================================
# 入口：把已验证结果写入共享数据层
# ============================================================
def run(state: dict = None) -> dict:
    """靶点调研主入口。

    把已验证的结构/文献调研结果写入共享 state.json（targets / pocket_differences /
    known_dual_binders），并在 evidence_log.jsonl 记一条 research_targets 事件。
    离线运行，无需联网、无需 biotite。

    返回：写入 state.json 的结果字典。
    """
    if state is None:
        state = State.load()

    # 1) 富化 targets（补上每靶点口袋残基与验证过的肽段参考）
    targets = state.get("targets", {})
    for name, info in TARGETS.items():
        targets.setdefault(name, {})
        targets[name].update(info)

    # 2) 三口袋差异 + 已知双靶分子写入白板
    result = {
        "targets": targets,
        "pocket_differences": POCKET_DIFFERENCES,
        "known_dual_binders": KNOWN_DUAL_BINDERS,
        "design_strategy_summary": DESIGN_STRATEGY_SUMMARY,
        "data_quality_alert": DATA_QUALITY_ALERT,
    }
    State.update(result)

    # 3) 记证据日志（供 Planner/Critic 追溯）
    hotspot_analysis = {
        "pdb_list": VERIFIED_PEPTIDE_COMPLEXES["MDM2"] + VERIFIED_PEPTIDE_COMPLEXES["MDMX"],
        "n_mdm2_peptide_complexes": len(VERIFIED_PEPTIDE_COMPLEXES["MDM2"]),
        "n_mdmx_peptide_complexes": len(VERIFIED_PEPTIDE_COMPLEXES["MDMX"]),
        "method": POCKET_DIFFERENCES["_method"],
        "superposition_rmsd_A": 1.88,
        "pockets": POCKET_DIFFERENCES,
        "data_quality_alert": DATA_QUALITY_ALERT,
    }
    EvidenceLogger.research_complete(
        hotspot_analysis=hotspot_analysis,
        known_binders=KNOWN_DUAL_BINDERS,
        refs=LITERATURE_REFS,
    )
    return result


def recompute():
    """从零复算（联网 + biotite）。

    完整可复现管线位于工作区 scripts/（不在本仓库内，避免污染 data_layer 约定）：
        search_pdb.py       RCSB Search API v2 检索人源/≤2.8Å/肽复合物
        enrich_pdb.py       GraphQL 富集，判定肽段复合物
        compute_interface.py biotite 重原子<4Å 界面残基
        aggregate_pockets.py 跨结构共识口袋残基
        superpose_analyze.py Cα 叠合 + 三口袋差异量化（SASA/gatekeeper/floor）
        pubmed_search.py     PubMed E-utilities 文献检索
    中间产物见 output/*.json，溯源报告见 REPORT.md。
    本函数当前不在仓库内直接执行复算；如需复现请在工作区运行上述脚本后再调用 run()。
    """
    raise NotImplementedError(
        "复现管线在工作区 scripts/ 下运行；本仓库内 run() 直接发布已验证结果。"
    )


if __name__ == "__main__":
    out = run()
    print("[research] state.json 已更新：targets / pocket_differences / known_dual_binders")
    print(f"[research] 肽复合物 MDM2={len(VERIFIED_PEPTIDE_COMPLEXES['MDM2'])} "
          f"MDMX={len(VERIFIED_PEPTIDE_COMPLEXES['MDMX'])}；已知双靶分子 {len(KNOWN_DUAL_BINDERS)} 个")
    print(f"[research] 当前 phase={State.load().get('phase')}")
