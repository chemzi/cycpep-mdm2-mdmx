## Why

DeeCamp 终期冲刺行动方案 v3 将 Tournament / Pareto Exploration Shortlist 定为 P0-E（头条创新）。当前系统的七层 hard clearance 是二元判定：一批候选全灭时，系统只输出"0/N passed"，不告诉下一轮该继续探索谁。答辩需要的核心故事是：

> 绝对标准负责科学可信度，相对多目标排名负责失败后的探索决策。

证据基板已经铺好：PR #44 合入后，每次 battery 评估都会以 `battery_evaluated` 结构化事件（含 `layer_values`、`failed_layers`、`passed`）进入 Evidence；`compute_pareto_front` 已在 `battery_evaluation.py` 中存在并被 Design 侧复用；`threshold_contract.calibration_status` 已区分 calibrated / provisional / unavailable。本 change 在这些既有基础上构建，不重新发明。

## What Changes

- 新增 `exploration.py`（根模块，模式对齐 `experience.py`）：
  - `desirability`：由 `battery_evaluated.layer_values` 与对应 threshold 的 `operator`/`value` 计算连续满意分数（距阈值的归一化边距均值），可解释、缺证据时保守返回 None；
  - `exploration_shortlist`：对一批候选计算 Pareto front（复用 `compute_pareto_front`，目标方向由 threshold `operator` 推导）+ desirability 相对排名，输出 top-k 及每个入选者的理由。
- 新增 `exploration_shortlist` 证据事件类型（`contracts/event.py` 白名单 +1），shortlist 结果以 Evidence 事件落库：
  - `targets` / `round` 只走正式 Evidence envelope（`EvidenceLogger.log` 的 `targets=` / `round_num=` 顶层字段），**不在 payload 内重复**，避免两套来源（P0-B 接口约束）；
  - 生成方持有正式 run 上下文时经 envelope `trace_context` 携带 `project_id/workflow_id/run_id`（及有真实上下文时的 `plan_id/task_id/attempt_id`）；
  - payload 含 `source_event_ids`（来源 `battery_evaluated` 事件的 event_id 列表）提供精确 provenance，并标注所消费指标的 `calibration_status` 汇总。
- 新增 `scripts/exploration_report.py` CLI：读取 Evidence，打印当前批次的 shortlist（含全灭场景）。
- 科学语义红线（写入 spec 作为硬性 requirement）：
  - 不修改 `evaluate_battery`、`l1_pass..l7_pass`、threshold 判定、threshold 数据的任何行为与格式；
  - shortlist 中的候选永远保持 `not passed`，不存在任何形式的"强制放行"（方案 v3 已明确取消原 A4）；
  - 未标定（`calibration_status` 非 calibrated/validated/complete）的指标只参与 desirability / ranking，绝不影响 hard clearance——本 change 不触碰 hard clearance 路径，天然满足。

## Non-goals

- Planner 消费 shortlist 驱动下一轮生成（后续独立 change，模式参照 PR #44 的 experience 偏好闭环）。
- Frontend 展示（P0-B 同事负责；本 change 只保证 `exploration_shortlist` 事件结构稳定、可供 read model 直接消费）。
- 阈值标定（P0-C 同事负责；本 change 只读 thresholds，不写）。
- 真实 GPU 候选批次（P0-D；本 change 的验收用合成证据 + 冒烟完成）。

## Capabilities

### New Capabilities

- `evaluation/exploration-shortlist`: 基于 battery 评估证据的 soft desirability 评分、多目标 Pareto front 与全灭场景下的 top-k 探索 shortlist 及理由记录。

### Modified Capabilities

None.

## Impact

- 新增文件：`exploration.py`、`scripts/exploration_report.py`、`test_exploration.py`。
- 修改文件：`contracts/event.py`（事件白名单 +1）。
- 无 public interface 破坏、无数据格式迁移、无 State / CandidateIndex / 事务路径改动。
- 依赖方向：`exploration.py` → `data_layer`（EvidenceLogger 读证据、`compute_pareto_front`）与 `threshold_contract`（calibration 语义），无反向依赖。
