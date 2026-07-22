"""
Critic Agent — 赵嘉策
职责：每轮结束后检查全局候选池质量，产出评审报告，触发 Planner 策略调整
入口：review(candidates) → dict
      检查项：MDMX偏靶 / 双靶不对称 / 工具分歧 / 多样性 / 热点覆盖
依赖：from data_layer import State, EvidenceLogger, CandidateIndex
"""

# TODO: 实现评审逻辑
# - 检查 MDMX ipTM 中位数是否偏低（mdmx_bias）
# - 检查 mean_asymmetry 是否超标（high_asymmetry）
# - 检查 AfCycDesign vs ColabFold 分歧率（tool_divergence）
# - 检查序列多样性（low_diversity）
# - 检查双靶热点覆盖率（low_hotspot_coverage）
# 产出 report 后，Planner 根据 report["issues"] 调整策略
