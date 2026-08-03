# Planner Agent v1.1

Planner 位于 Critic 与未来 Orchestrator 之间。它读取一份冻结的
`critic_report.json`，生成确定性的 `execution_plan.json`，把“需要改进界面”这类建议
转换成带任务依赖、候选范围、资源请求、预算上限和审批要求的执行图。

Planner 只做规划和审计，不会直接运行 Research、Design、Prediction，也不会自动：

- 修改 Research 阈值；
- 删除或覆盖候选；
- 预占或消耗 Design/GPU 预算；
- 增加 State 的迭代轮次；
- 将候选提名为最终实验候选。

## 1. 输入合同

权威输入是 Critic v1 的完整报告文件。Planner 会重新计算文件 SHA-256，并检查：

- `report_id` 是否由 Critic `input_digest` 派生；
- `verdict` 与 `passed` 是否一致；
- State、Critic 和 Prediction 是否属于同一项目；
- State 若已登记 Critic report ID/SHA，是否与输入文件一致；
- 每个 issue 是否有且只有一个 recommendation 映射；
- recommendation action 是否在 Planner 的封闭动作表中；
- Critic → Planner handoff 是否保留四项强制安全约束。

遇到未知 action 时 Planner fail-closed。这样 Critic 新增一种动作后，必须同步补充
Planner 和 Orchestrator 的明确处理方式，不能把陌生字符串直接当命令执行。

## 2. 任务图

每个任务包含：

- `task_id`、Agent、action、phase 和优先级；
- 对应的 Critic reason codes；
- 受影响的候选 ID，或上游任务输出范围；
- CPU、network/CPU 或 GPU 资源类型；
- proposal 数、Prediction candidate limit 和 GPU job slot 请求；
- 审批类型、依赖任务、预期输出和不可违反的约束；
- `proposed` / `blocked` 执行闸门。

同一轮出现多个 Design 指标问题时，Planner 会将它们合并为一个小批量 Design 迭代，
随后生成 Prediction 与 Critic 复核任务：

```text
Design 小批量迭代
  → 只评估新候选或证据不完整候选
    → Critic 审查新的 Prediction handoff
```

完整历史 Prediction evidence 必须复用。默认 Design 请求 12 条 proposal；这个数字是
可配置的容量请求，并受 State `design_budget` 和 Planner 硬上限共同约束。Planner
不会把旧 `design_budget` 解释成已经批准的 GPU 时间，也不会虚构 GPU 分钟估算。

v1.1 将 `regenerate_design_reference` 和 `improve_pose_robustness` 纳入同一个
Design iteration 图。前者要求 Design 追加带独立 reference 的候选；后者表示完整
AF2/Boltz 证据下的姿态不收敛，需要改变序列、骨架或界面设计策略。两者都会进入
`Design → 仅评估新候选 → Critic`，不再产生针对旧候选的
`complete_prediction_evidence` 或“只补缺失 predictor”GPU 任务。

## 3. Critic verdict 的规划语义

| Critic verdict | Planner 行为 |
|---|---|
| `blocked` | 进入 `recovery_only`；冻结科学迭代和可选任务，只允许 P0 数据/产物修复及其复核 |
| `iterate` | 生成必要的 Design/Prediction 迭代图，GPU 任务等待执行预算审批 |
| `review` | 生成人工/Research 审查任务，例如阈值校准 proposal |
| `clear` | 生成候选审阅材料；候选提名仍需人工决定 |

`cohort_too_small` 这类 info issue 产生 optional 任务。它单独出现时不会阻止当前结果整理，
也不会使 Planner 自动请求执行 GPU。

## 4. 阈值、重复序列和数据修复

- `calibrate_thresholds` 转换为 `propose_threshold_calibration`。输出是校准提案，不能直接
  写回 State 阈值；任务要求 `scientific_policy` 审批。
- `deduplicate_candidates` 转换为只读审计与处理提案，禁止自动删除候选。
- `repair_candidate_index` 必须对照冻结的 Prediction record，并要求 `data_integrity`
  审批；修复完成后重新运行 Critic。
- Critic 存在 blocker 时，其他 Design/Prediction 分支会被加上
  `critic_blocker_requires_recovery` 闸门。

## 5. GPU 审批

每个 GPU 任务必须声明：

- `approval.required=true`；
- approval type 包含 `execution_budget`；
- GPU job slot、proposal 数和/或 candidate limit；
- `estimated_gpu_minutes=null`、`estimate_status=benchmark_required`，直到有可审计基准。

计划文件本身不会表示“已经批准”。人工确认后可单独生成与计划完整 SHA-256 绑定的
approval artifact：

```bash
python agents/planner.py approve \
  --plan /path/to/execution_plan.json \
  --task T001 \
  --task T002 \
  --approver "PI name" \
  --justification "Approved one small C0514 iteration" \
  --max-gpu-job-slots 2 \
  --max-gpu-minutes 240 \
  --max-design-proposals 12 \
  --max-prediction-candidates 12
```

审批前会重新检查 Critic 文件哈希、计划安全约束、GPU approval 类型、任务依赖和
执行闸门。包含 GPU 任务时必须设置正数 `max_gpu_minutes`；计划 JSON 有任何改动都会
改变 SHA-256，使旧 approval 失效。

未来 Orchestrator 必须同时验证 plan 与 approval；Planner 当前不会调度任务。

## 6. 运行 Planner

```bash
python agents/planner.py build \
  --critic-report /path/to/critic_report.json
```

默认输出位置：

```text
<critic-report-directory>/planner/<plan_id>/execution_plan.json
```

相同 Critic 文件、State 的项目/轮次/Design budget 快照、Planner 配置和版本会产生
相同 `plan_id` 与文件 SHA-256。重复运行不会重复写 State history 或
`planner_plan` evidence 事件。

Schema：

- `agents/planner_plan.schema.json`
- `agents/planner_approval.schema.json`

测试：

```bash
python -m unittest -v test_planner.py
```

## 7. 启动阶段兼容入口

在尚无 Critic 报告的新项目中，`plan(state=..., candidate_rows=...)` 提供轻量状态判断：

1. 项目批准摘要漂移：回到项目审核；
2. 缺少 Research 结果：运行 Research；
3. Research 完成但候选为空：请求 Design 预算审批；
4. 已有候选但没有 Prediction handoff：请求 Prediction 预算审批；
5. 已有 Prediction 但没有 Critic：运行 Critic；
6. Critic 报告就绪：进入正式 Critic 驱动规划。

该入口用于启动检查；正式迭代以冻结 Critic 报告生成的 `execution_plan.json` 为准。
