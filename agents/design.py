"""
Design Agent — 于嘉乐
职责：调用三条设计路线的 CLI，统一输出格式
入口：design_afcyc(target, n, lengths) → list[dict]
      design_motif_graft(n) → list[dict]
      design_atsp_cyclize(n) → list[dict]
依赖：from data_layer import EvidenceLogger, CandidateIndex
      AfCycDesign CLI / ProteinMPNN / HuggingFace datasets
"""

# TODO: 实现三条设计路线
# - route_A: AfCycDesign binder design（MDM2-first + MDMX-first）
# - route_B: motif 嫁接 + ProteinMPNN 序列优化
# - route_C: ATSP-7041 环化改造
# 产出后调用 CandidateIndex.add_batch() 写入索引表
