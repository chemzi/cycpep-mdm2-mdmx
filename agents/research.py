"""
Research Agent — 刘函赫
职责：自动检索 PDB/PubMed → 结构化热点分析 → 双靶差异对比
入口：run(state) → dict (写入 state.json 的 targets/pocket_differences/known_dual_binders)
工具链：RCSB Search API → biotite 界面分析 → PubMed E-utilities → LLM 结构化提取
依赖：from data_layer import State, EvidenceLogger
"""

# TODO: 实现靶点调研流程
# - Step 1: RCSB Search API 搜 MDM2/MDMX-肽段复合物 PDB
# - Step 2: biotite 解析界面残基（重原子距离 < 4Å）
# - Step 3: 叠合两靶点结构，算逐残基差异
# - Step 4: PubMed 搜 "MDM2 MDMX dual peptide inhibitor"
# - Step 5: LLM 提取文献热点残基 + 已知 binder
# 完成后调用 EvidenceLogger.research_complete()
