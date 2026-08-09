## Context

行为基线由以下现有实现与测试确立（实施前必须阅读）：

- `battery_evaluation.py::evaluate_battery`：七层判定，`layer_values` 键为 `L1_plddt` / `L2_ipsae_<target>` / `L3_dg|sc|dsasa_<target>` / `L4_nc_distance_pre|post` / `L5_hotspot_cov_<target>` / `L6_pose_rmsd_<target>` / `L7_scrmsd`。本 change 只读其结果，不修改。
- `battery_evaluation.py::compute_pareto_front`：混合方向非支配排序，经 `data_layer` 导出，Design 侧（`agents/design/service.py::pareto_front`）已有复用先例；`test_data_layer.py:425/516` 是其行为基线测试。
- `data_layer.py::EvidenceLogger.battery_evaluated` + `prediction_pipeline/transaction_effects.py::record_battery_evaluated`（PR #44）：`battery_evaluated` 事件携 `layer_values` / `failed_layers` / `passed` / `length` / `targets` 持久化，事务与非事务两条路径均已覆盖。这是本 change 的输入数据源。
- `threshold_contract.py::normalize_threshold_entry`：threshold 条目含 `value` / `operator` / `calibration_status`；`calibrated|validated|complete` 视为已标定。本 change 只读。
- `experience.py`（PR #44）：根级纯函数模块 + Evidence 消费 + 保守降级的既有模式，本 change 对齐它。

## Decisions

### D1：shortlist 从 Evidence 离线计算，不进入 Execution 事务边界

`exploration.py` 读 `EvidenceLogger.get_all()`、写 `EvidenceLogger.log("critic", "exploration_shortlist", ...)`。它不在 Execution task 的 BEGIN/COMMIT 内运行，因此**不需要**改 `execution/prediction_effects.py` 的证据白名单（PR #44 的 CI 教训不适用于此，但需在 review 时确认无人在事务内调用本模块）。Evidence 是 append-only：重复生成只追加新事件、不产生矛盾状态；**下游消费语义为"先按 envelope scope（project/workflow/run/round）过滤，再按事件时间取最新"，而非全局取最新**（P0-B 接口约束），因此不引入额外幂等机制。

envelope 约定（已对照 `contracts/event.py` 核实：`EvidenceEvent` 顶层有 `targets`/`round`；`TRACE_KEYS` 含 `project_id/workflow_id/run_id/plan_id/task_id/attempt_id/parent_event_id`）：

- `targets` 只经 `log(targets=...)` 写入 envelope，payload 不重复；
- `round_num` 由生成方经 `log(round_num=...)` 写入 envelope；CLI 提供 `--round`；
- 生成方持有正式 run 上下文时必须传 `trace_context`；本 change 的生成路径为离线 CLI（无正式 run），故 trace 为可选透传参数——**未来 in-run 生成（Planner 消费 change）必须携带完整 trace，此处先固定该约束**；
- 已知限制（诚实记录，不本 change 解决）：PR #44 的 `battery_evaluated` 源事件未带 `round_num`，因此 shortlist 的输入是"该 targets 下累计全部证据"；Frontend 需要区分的"每轮 shortlist"由 shortlist 事件自身的 envelope round 满足。给 `battery_evaluated` 补 round 属于触碰 prediction pipeline 的后续 change。

### D2：desirability = 距阈值的归一化边距均值（可解释、保守）

对每个候选的 `layer_values` 中每个指标，若在 thresholds 中找到对应条目且 `value`/`operator` 齐全：

- `operator ∈ {">", ">="}`：margin = (value − threshold) / |threshold|
- `operator ∈ {"<", "<="}`：margin = (threshold − value) / |threshold|
- threshold 为 0 时退化为符号差（value 与 0 的比较方向符合 operator 则记 0，否则记 −1）

margin 截断到 [−1, 1]；desirability = 全部可计算指标 margin 的均值。无可计算指标 → `None`（不伪造分数）。`layer_values` 键经 `_LAYER_KEY_TO_METRIC` 前缀映射到指标空间；靶标维度键的 slug 后缀用于经 `project_config.threshold_for_target` 解析 per-target override（与 battery 侧同一解析器——P0-C 标定的主产物就是 target 级 override）；映射不上的指标跳过并列入结果的 `unmapped_metrics` 字段，不静默丢弃。同一 candidate_id 的跨轮重评估只保留最新一行证据。

### D3：Pareto 目标从 layer_values + METRIC_SPECS 推导，复用既有函数

Pareto 支配不需要阈值数值，只需要方向。方向取自 `threshold_calibration.METRIC_SPECS` 的 `direction`（代码内评审过的常量，比 threshold 条目的 operator 字符串更稳定，且在阈值完全缺失时 Pareto 仍可用）；`operator` 仅用于 D2 的边距计算。`layer_values` 键（如 `L2_ipsae_mdm2`、`L4_nc_distance_post`）经 `_LAYER_KEY_TO_METRIC` 前缀映射到 METRIC_SPECS 键空间（`L2_ipsae`、`L4_nc_term_dist`）。值缺失的候选不参与 Pareto（既有函数语义），但仍可凭 desirability 进入排名。

### D4：shortlist 合成规则（固定、简单、可审计）

1. Pareto front 成员（按 desirability 降序）先入列，理由 `pareto_front`；
2. 其余候选按 desirability 降序补足 top-k（默认 k=5），理由 `desirability_rank`；
3. desirability 为 None 的候选排最后，仅在前两类不足 k 时入选，理由 `partial_evidence`；
4. 每条入选记录：`candidate_id`、`passed`（原样保留，全灭时全为 false）、`desirability`、`pareto_front: bool`、`reason`、该候选贡献最大的边距指标名。

`exploration_shortlist` 事件：envelope 携带 `targets`（必有）与 `round`/`trace_context`（有上下文时必有）；payload 为 `k`、`n_evaluated`、`n_passed`、`shortlist: [...]`、`source_event_ids: [...]`（来源 `battery_evaluated` 事件的 event_id 列表，精确 provenance，回应 P0-B 的非阻塞建议）、`calibration: {"calibrated": n, "provisional": n, "unavailable": n}`（所消费 threshold 条目的分级汇总，供 P0-C/前端区分软结论与硬结论）。

### D5：依赖与回滚

依赖方向：`exploration.py` → `data_layer` / `threshold_contract`；无反向依赖；不新增 import-time 项目全局状态。回滚 = 删除新文件 + 回退 `contracts/event.py` 一行白名单，无任何已存数据需要迁移（事件未产生前系统行为与现状逐字节一致）。

## Known debt（登记，不阻塞本 change）

- `battery_evaluated` 源事件未带 envelope `round`（`data_layer.py::battery_evaluated` 与事务镜像路径均未传 `round_num`）。本 change 的 shortlist 输入因此为"该 targets 累计全部证据"（语义选择，见 spec）；补 round 需动 prediction pipeline 持久化链路 + 事务效果，属后续独立 change。
- `_as_float` 与 `experience.py` 逐字重复（独立 review nit #7）：跨模块抽取会扩大本 change 边界，留待后续基础设施统一 change 处理。

## Risks

- desirability 公式是启发式：已把"不得进入 hard clearance"写入 spec 红线，并在事件 payload 暴露 calibration 汇总，防止下游误当硬结论。
- thresholds 键与 layer_values 键的映射可能不全：跳过并计数，report 中可见，不静默归零。
