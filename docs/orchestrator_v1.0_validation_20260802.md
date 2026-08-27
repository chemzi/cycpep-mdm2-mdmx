# Orchestrator v1.0：C0514 真实计划无执行验证

日期：2026-08-02  
服务器：4090 部署机  
代码分支：`deploy/orchestrator-v1.0.0`

## 1. 验证范围

本次验证使用真实 C0514 Prediction → Critic 结果，重新生成 Planner v1.0.1 计划，
随后只运行 Orchestrator `init` 与 `status`。

本次明确没有执行：

- Planner `approve`；
- Orchestrator `authorize` 或 `claim`；
- Design、Prediction、Research 或 Critic Worker；
- 任何 GPU 进程；
- 正式 State 或 CandidateIndex 写入。

## 2. Planner v1.0.1 计划

Planner v1.0.1 在 approval contract 中新增强制 GPU 分钟上限。包含 GPU task 的 approval
必须提供正数 `max_gpu_minutes`，Orchestrator 会按同一 approval 累计实际用量。

真实 C0514 新计划：

- Plan ID：`planner_80c574539183`
- Plan status：`awaiting_approval`
- Plan SHA-256：
  `8a6393c36ebc1133b2522f8ec9bc8e91cd23cb4b03b0f167a640ee2a375d7a2d`
- Task 数：4
- 需要 approval：T001、T002、T004

此前验证产生的 Planner v1.0 计划 `planner_e2f09d615588` 从未批准或执行，保留为历史
验证 artifact。后续审批和 Orchestrator 执行以 v1.0.1 计划为准。

## 3. Orchestrator run

- Run ID：`orchestrator_767c15e81411`
- Orchestrator version：`1.0.0`
- Run status：`awaiting_approval`
- Run SHA-256：
  `c9a77429f0014387e1d901fb96541fa4b2216c0e6e9a4f01db3d99c4dcd83933`
- Approval count：0
- GPU lease：`null`

任务状态：

| Task | 状态 | 原因 |
|---|---|---|
| T001 Design | `awaiting_approval` | 缺少 execution-budget approval |
| T002 Prediction | `awaiting_approval` | 缺少 execution-budget approval；执行时还需等待 T001 |
| T003 Critic | `pending_dependency` | 等待 T002 成功及其哈希输出 |
| T004 Research | `awaiting_approval` | 缺少 scientific-policy approval |

这里 T002 优先显示审批缺失；approval 加载后，状态会转为 `pending_dependency`，直到 T001
完成。

## 4. 幂等和隔离

对同一 plan 连续执行两次 `init`，再执行 `status`：

- Run ID 保持一致；
- Orchestrator State history 数量为 1；
- `orchestrator_run_initialized` Evidence 事件数量为 1；
- 没有 dispatch packet；
- 没有 global GPU lease 文件。

隔离 State 保持在 round 1。Planner 初始化将隔离 State phase 设为 `iterate`；
Orchestrator 没有推进 round，因为所有必要任务尚未完成。

正式文件前后哈希：

- `data/state.json`：
  `10c6fdf79b030e9693664cb53e1512522aaad6e1546d37664a9e1ad0825a457f`
- `data/candidate_index.csv`：
  `4e4b0a0e8be7a5e959262a3cc76db5e28f983076a7c3ce462b605eeab2e89c84`

## 5. 回归结果

服务器：

```text
Orchestrator + Planner + Critic + Prediction: 67/67 passed
```

本地数据层：

```text
Data Layer: 180/180 passed
```

测试覆盖完整 DAG 完成及 round 推进，但使用的是隔离临时文件和模拟输出，不启动 GPU。

## 6. 当前执行边界

Orchestrator v1.0 已能安全管理 Agent Worker 生命周期。它生成 dispatch packet，让人类
或 AI Worker 按 task contract 执行，然后用 claim token 登记结果。

开始真实 C0514 迭代前仍需要人工决定：

1. 是否批准 T001 的 12 条 Design proposal；
2. 是否同时批准 T002 对新候选运行 Prediction；
3. T001+T002 总 `max_gpu_minutes`；
4. 是否批准 T004 生成阈值校准提案；
5. 用于审计的 approver 身份与 justification。

只有生成并加载与新 Plan SHA 完整绑定的 approval 后，T001/T004 才会进入 `ready`。
