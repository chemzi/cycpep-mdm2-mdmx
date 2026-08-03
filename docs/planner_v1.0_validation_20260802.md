# Planner v1.0：C0514 真实 Critic 报告验证

> 历史说明：本页记录 Planner v1.0 的首次验证。该计划未批准、未执行。Planner
> v1.0.1 已增加强制 GPU 分钟上限；当前计划与 Orchestrator 验证见
> `docs/orchestrator_v1.0_validation_20260802.md`。

日期：2026-08-02  
服务器：4090 部署机  
代码分支：`deploy/planner-v1.0.0`

## 1. 输入

本次使用 Critic v1.0 对真实 C0514 Prediction v1.5.0 产物生成的报告：

- Critic report ID：`critic_af6225e262d5`
- Critic verdict：`iterate`
- Critic report SHA-256：
  `ed9b475a04e7d84456ac2d8637c82deded87d04dbb6bf87fb0ff335bcf8bd55e`

Critic 给出的动作包括：

- `iterate_interface_design`
- `iterate_interface_physics`
- `calibrate_thresholds`
- `generate_review_cohort`

Planner 使用 Prediction v1.5.0 验证目录中的隔离 State 与 Evidence 目录。正式
`data/state.json` 和 `data/candidate_index.csv` 未作为写入目标。

## 2. 生成的计划

- Plan ID：`planner_e2f09d615588`
- Plan status：`awaiting_approval`
- Plan SHA-256：
  `f1fbe2246a22dcbec2af817e3e59f5a57a5daf0614d6ef63062474461e5138ed`
- 任务数：4

任务图：

| Task | Priority | Agent | Action | 依赖 | 审批 |
|---|---|---|---|---|---|
| T001 | P1 | Design | `iterate_design` | 无 | `execution_budget` |
| T002 | P1 | Prediction | `evaluate_new_design_candidates` | T001 | `execution_budget` |
| T003 | P1 | Critic | `review_prediction_handoff` | T002 | 无 |
| T004 | P2 | Research | `propose_threshold_calibration` | 无 | `scientific_policy` |

两个界面指标问题被合并到同一个 Design 小批量迭代。`generate_review_cohort` 作为同一
批次的附加 strategy directive，没有额外启动一条重复 Design 作业。

预算请求：

- State Design budget 快照：Route A MDM2 400、Route A MDMX 400、Route B 400、
  Route C 200；Planner 没有扣减这些值；
- 请求 Design proposal：12；
- 请求 GPU job slot：2；
- GPU minutes：`null`；
- GPU minutes status：`benchmark_required`；
- reservation status：`not_reserved`。

本次只生成计划。没有创建 approval artifact、没有启动 GPU、没有执行 Design、
Prediction 或阈值变更。

## 3. 幂等与状态隔离

对同一 Critic 报告连续运行两次：

- Plan ID 相同；
- Plan SHA-256 相同；
- 隔离 State 中 Planner history 数量为 1；
- 隔离 Evidence 中 `planner_plan` 事件数量为 1。

正式文件运行前后哈希保持一致：

- `data/state.json`：
  `10c6fdf79b030e9693664cb53e1512522aaad6e1546d37664a9e1ad0825a457f`
- `data/candidate_index.csv`：
  `4e4b0a0e8be7a5e959262a3cc76db5e28f983076a7c3ce462b605eeab2e89c84`

## 4. 回归结果

服务器运行：

```text
Planner + Critic + Prediction: 58/58 passed
```

本地数据层回归：

```text
Data Layer: 180/180 passed
```

## 5. 结论与下一边界

Planner v1.0 已能把真实 Critic 反馈稳定转换成有预算和审批闸门的任务 DAG。
当前状态可以用于人工检查和未来 Orchestrator 摄取，但还不能把
`awaiting_approval` 理解为可执行状态。

下一模块 Orchestrator 至少需要实现：

1. 同时验证 plan 与 digest-bound approval；
2. 只调度 approval 明确覆盖且未 blocked 的 task；
3. 检查依赖完成后再启动下游任务；
4. 单 GPU 串行锁、运行中断与断点恢复；
5. 成功完成一轮后再推进 State round；
6. 每个任务输出的文件哈希、日志和资源消耗审计。
