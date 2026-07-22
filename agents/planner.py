"""
Planner Agent — 赵嘉策
职责：读 state.json → 判断当前阶段 → 产出任务列表 → 根据 Critic 反馈调整策略
入口：plan(phase, state) → list[Task]
      adjust(report) → dict
依赖：from data_layer import State, EvidenceLogger
"""

# TODO: 实现 Planner 主循环
# - research 阶段：产出 ResearchAgent 任务
# - design 阶段：产出三条路线的 DesignAgent 任务
# - evaluate 阶段：产出四层评估任务
# - 监听 critic_review，触发策略调整
