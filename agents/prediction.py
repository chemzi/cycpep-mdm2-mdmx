"""
Prediction Agent — 王修远
职责：分层评估候选，每层筛完后返回带分数的候选列表
入口：evaluate_layer(candidates, layer) → list[dict]
      _eval_monomer / _eval_mdm2_complex / _eval_mdmx_complex / _eval_full
依赖：from data_layer import EvidenceLogger, CandidateIndex
      AfCycDesign predict / ColabFold / HADDOCK / biotite
"""

# TODO: 实现四层评估漏斗
# - Layer 1: 单体折叠验证（pLDDT > 0.7, RMSD < 2.0Å）
# - Layer 2: MDM2 复合物预测（ipTM > 0.7）
# - Layer 3: MDMX 复合物预测 + 双靶评分
# - Layer 4: ColabFold + HADDOCK 交叉验证 + 界面分析
# 每层完成后调用 EvidenceLogger.evaluate_layer_complete() 和 CandidateIndex.update_score()
