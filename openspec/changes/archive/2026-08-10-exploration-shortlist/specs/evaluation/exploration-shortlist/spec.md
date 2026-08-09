## Purpose

在七层 hard clearance 之外提供连续 soft desirability 评分与多目标 Pareto 探索 shortlist：当一批候选全部未通过 hard clearance 时，系统仍能基于证据给出下一轮最值得探索的候选及理由，而不伪造科学通过结论。

## ADDED Requirements

### Requirement: 连续 desirability 评分
系统 SHALL 从 `battery_evaluated` 证据的 `layer_values` 与对应 threshold 条目的 `value`/`operator` 计算每个候选的连续 desirability 分数（距阈值归一化边距的均值，截断 [-1,1]）。

#### Scenario: 指标齐全时产出可解释分数
- **WHEN** 一个候选的层指标均有对应 threshold 条目（value 与 operator 齐全）
- **THEN** desirability 为各指标归一化边距的均值，且每个指标的方向由 operator 决定（">"/">=" 越大越好，"<"/"<=" 越小越好）

#### Scenario: per-target override 优先于 base 条目
- **WHEN** 某靶标维度的指标存在 target 级 threshold override（如 P0-C 标定产物）
- **THEN** margin 与 calibration 汇总消费该 override（经 `threshold_for_target` 解析，与 battery 侧同一解析器），其他靶标仍使用 base 条目

#### Scenario: 不可映射的指标可见
- **WHEN** 批次证据中存在无法映射到指标空间的 layer_values 键
- **THEN** 这些键被跳过且列入结果的 `unmapped_metrics` 字段，不得静默丢弃

#### Scenario: 证据缺失时保守处理
- **WHEN** 某指标值缺失、或找不到对应 threshold 条目、或条目缺 value/operator
- **THEN** 该指标被跳过；全部指标均不可计算时 desirability 为 None，不得合成虚构分数

### Requirement: Pareto 探索 shortlist
系统 SHALL 对一批 `battery_evaluated` 候选计算多目标非支配 Pareto front（复用既有 `compute_pareto_front` 语义，方向取自 `threshold_calibration.METRIC_SPECS` 的 direction，阈值缺失时 Pareto 仍可用），并合成 top-k shortlist：front 成员优先，其余按 desirability 降序补足，每条入选记录附理由。

#### Scenario: 全灭批次仍输出探索方向
- **WHEN** 一批候选的 `passed` 全部为 false
- **THEN** 系统仍输出不超过 k 条的 shortlist，每条含 candidate_id、desirability、是否 front 成员、入选理由，且所有入选者的 passed 保持 false

#### Scenario: 无证据时不产生 shortlist
- **WHEN** 指定 targets 下不存在任何 `battery_evaluated` 证据
- **THEN** shortlist 为空，不抛异常、不产出任何入选者

#### Scenario: 输入为累计证据（显式语义选择）
- **WHEN** 计算某 targets 的 shortlist
- **THEN** 输入为该 targets 下累计全部 `battery_evaluated` 证据（不按轮次过滤源事件）；shortlist 事件自身的轮次由事件级 envelope `round` 承担。`battery_evaluated` 源事件缺 `round` 是已知限制，登记为 known debt（后续独立 change 处理），不影响本语义

#### Scenario: 同一候选跨轮重评估
- **WHEN** 同一 candidate_id 存在多条 `battery_evaluated` 证据（重评估/重试）
- **THEN** 只保留最新一行参与评分、Pareto 与 shortlist，shortlist 中不出现重复 candidate_id

### Requirement: hard clearance 科学语义不变
本能力 SHALL NOT 修改 `evaluate_battery`、`l1_pass..l7_pass`、threshold 判定逻辑或 threshold 数据的任何行为与格式；shortlist 不得将任何候选标记为通过。

#### Scenario: 既有评估行为回归
- **WHEN** 本能力合入后运行既有 battery、阈值与事务测试
- **THEN** `test_data_layer.py`、`test_experience.py`、`test_prediction_transactional.py` 在未修改的情况下全部通过

#### Scenario: shortlist 不构成通过结论
- **WHEN** 下游读取 `exploration_shortlist` 事件
- **THEN** 每个入选者的 passed 字段与其 `battery_evaluated` 原始判定一致，事件 payload 不含任何提升候选通过状态的字段

### Requirement: 标定状态透明
`exploration_shortlist` 事件 SHALL 在 payload 中汇总所消费 threshold 条目的 `calibration_status` 分级计数，使下游能区分软结论与已标定的硬结论。

#### Scenario: 未标定指标参与排序时被标注
- **WHEN** shortlist 计算消费了 `calibration_status` 非 calibrated/validated/complete 的 threshold 条目
- **THEN** 事件 payload 的 calibration 汇总反映该计数，且这些指标不影响任何 hard clearance 判定

### Requirement: 状态边界与持久化
本能力 SHALL 只通过 Evidence 层读写（append-only），不得修改 State、CandidateIndex，不得在 Execution 事务边界内运行，不得绕过 data/store 边界。

#### Scenario: 重复生成不产生矛盾状态
- **WHEN** 对同一批证据重复生成 shortlist
- **THEN** 系统仅追加新的 `exploration_shortlist` 事件，既有事件与正式状态保持不变；下游在限定 envelope scope（project/workflow/run/round）后按事件时间取最新，而非全局取最新

### Requirement: 正式 envelope 契约
`exploration_shortlist` 事件 SHALL 使用正式 Evidence envelope 承载作用域与追踪信息：`targets` 写入 envelope 顶层（payload 不重复）；有轮次上下文时写 envelope `round`；属于正式 run 时经 envelope `trace_context` 携带 `project_id`/`workflow_id`/`run_id`（及有真实上下文时的 `plan_id`/`task_id`/`attempt_id`）。

#### Scenario: 不同轮次的 shortlist 可区分
- **WHEN** 同一 targets 下存在多个轮次生成的 shortlist 事件
- **THEN** 各事件经 envelope `round`（及 trace 字段）区分，下游按 scope 过滤后不会混淆不同轮次的 shortlist

#### Scenario: 无正式 run 上下文的离线生成
- **WHEN** shortlist 由离线 CLI 生成、无正式 run 上下文
- **THEN** 事件仍可合法写入（trace 字段缺省），且 envelope `targets` 始终存在

### Requirement: 来源可溯
`exploration_shortlist` 事件 payload SHALL 含 `source_event_ids`：本次计算消费的 `battery_evaluated` 事件的 event_id 列表。

#### Scenario: 精确 provenance
- **WHEN** 下游需要审计某条 shortlist 的依据
- **THEN** 可经 `source_event_ids` 精确定位全部来源 `battery_evaluated` 事件，不依赖 candidate_id 跨轮次弱关联
