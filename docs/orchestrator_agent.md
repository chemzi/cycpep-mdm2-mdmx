# Orchestrator Agent v1.0

Orchestrator 位于 Planner 与实际执行 Worker 之间。它管理一份 Planner task DAG 的
运行状态、审批覆盖、依赖解锁、GPU 租约、Worker 认领和输出哈希。

v1 采用“受控调度包”模式：Orchestrator 不直接启动 RFdiffusion、Boltz、Rosetta 或
Research 网络任务。人类或 AI Worker 认领一个 ready task，读取 dispatch packet，执行
对应 Agent，再通过 Orchestrator 登记成功或失败。这样可在未来接入 Codex/队列服务时
保持同一套状态合同，也能避免一个尚未审核的通用 shell executor 获得服务器权限。

## 1. 安全边界

Orchestrator 每次初始化和任务状态变更都会检查：

- Planner plan ID、完整 SHA-256 和 Critic source SHA-256；
- State、Plan 和 approval 的 project ID；
- approval ID 是否由其内容摘要派生；
- approval 是否绑定相同 plan path、plan ID 和完整 plan SHA-256；
- GPU task 是否被 approval 明确覆盖；
- proposal、Prediction candidate、GPU job 数和 GPU minutes 上限；
- task 执行闸门、依赖 DAG 和当前 State round；
- 上游任务输出文件是否仍与完成时 SHA-256 一致。

一个项目同一时间只允许一份 active Orchestrator run。GPU task 还需要获得全局 GPU
租约，因此不同 run 也不能同时占用同一个配置的 GPU lease path。

默认 GPU lease 位于：

```text
<CYCPEP_DATA_DIR>/orchestrator/gpu_lease.json
```

如果一台服务器同时运行多个项目，并且它们使用不同 `CYCPEP_DATA_DIR`，应统一设置：

```bash
export CYCPEP_GPU_LEASE_PATH=/shared/novapeptide/orchestrator/gpu_lease.json
```

这个路径必须由所有项目共享，才能形成服务器级单 GPU 锁。

## 2. 运行状态

Run 状态：

- `awaiting_approval`：必要任务缺少 plan-bound approval；
- `ready`：至少一个任务可被认领；
- `running`：存在 active claim；
- `blocked`：执行闸门或上游失败阻断；
- `failed`：必要任务失败；
- `completed_required`：所有必要任务完成，仍有 optional task；
- `completed`：任务图结束。

Task 状态：

```text
blocked / awaiting_approval / pending_dependency
  → ready
    → claimed
      → succeeded | failed

optional task 还可以进入 skipped
```

失败任务不会自动重试。`retryable=true` 只作为诊断信息保留；新一轮重试需要新的
Critic/Planner 决策或显式恢复方案，防止 GPU 失败形成无限循环。

## 3. 初始化与审批

没有 approval 时也可以初始化，状态会停在 `awaiting_approval`：

```bash
python agents/orchestrator.py init \
  --plan /path/to/execution_plan.json
```

人工批准后，将 approval 加入已有 run：

```bash
python agents/orchestrator.py authorize \
  --run /path/to/orchestrator_run.json \
  --approval /path/to/approval_xxx.json
```

重复加载同一 approval 是幂等操作。两份 approval 不得重复覆盖同一 task，避免 GPU
分钟预算归属不明确。

Approval artifact 负责内容完整性与审批范围绑定。CLI 中的 `approver` 是审计字段，
本地 CLI 自身不验证人员真实身份；生产 API/网页层需要通过登录、角色和电子签名机制
提供可信身份，再调用 Planner `record_approval()`。

## 4. Worker 认领

```bash
python agents/orchestrator.py claim \
  --run /path/to/orchestrator_run.json \
  --task T001 \
  --worker design-agent-01
```

认领成功后会生成 dispatch packet，包含：

- 完整 task contract；
- plan ID/SHA；
- worker 和不可猜测的 claim token；
- 对应 approval 及预算上限；
- 所有上游输出的路径和 SHA；
- 完成时必须满足的资源与输出合同。

GPU task 认领时同时写入 run-local 和 global GPU lease。租约不会按时间自动过期，
防止一个仍在后台运行的模型进程因心跳中断而与新进程重叠。

## 5. 完成与失败

成功完成：

```bash
python agents/orchestrator.py complete \
  --run /path/to/orchestrator_run.json \
  --task T001 \
  --claim-token <token> \
  --output design_result=/path/to/task_result.json \
  --gpu-minutes 37.5
```

每个 output 必须是已有普通文件。Orchestrator 会保存完整 SHA-256 和字节数。GPU task
必须报告实际 GPU 分钟；同一 approval 下的累计用量不能超过 `max_gpu_minutes`。

失败：

```bash
python agents/orchestrator.py fail \
  --run /path/to/orchestrator_run.json \
  --task T001 \
  --claim-token <token> \
  --reason "RFdiffusion process exited with code 1" \
  --gpu-minutes 12.4 \
  --retryable
```

失败同样记录 GPU 用量并释放租约，下游进入 `blocked_dependency`。

## 6. 中断恢复

外部 Worker 或 SSH 中断后，claim 仍保持有效。先通过系统进程检查确认模型进程已经
停止，再执行：

```bash
python agents/orchestrator.py recover \
  --run /path/to/orchestrator_run.json \
  --task T001 \
  --claim-token <token> \
  --operator ops-name \
  --reason "worker host rebooted; process confirmed absent" \
  --gpu-minutes 18.0 \
  --confirmed-process-stopped
```

缺少 `--confirmed-process-stopped` 时恢复会被拒绝。恢复将任务标记为 failed 并释放
GPU lease，不会自动重试。

## 7. Optional task 与状态查询

Optional task 可以显式跳过：

```bash
python agents/orchestrator.py skip \
  --run /path/to/orchestrator_run.json \
  --task T005 \
  --reason "current cohort is sufficient for this report"
```

必要任务禁止 skip。

只读状态：

```bash
python agents/orchestrator.py status \
  --run /path/to/orchestrator_run.json
```

所有必要迭代任务成功后，Orchestrator 才将 State round 从 Planner 的
`source_round` 推进到 `target_round`。包含 Critic 复核任务的计划完成后，State phase
回到 `critic`，等待下一份 Planner plan。

## 8. Schema 与测试

- Run schema：`agents/orchestrator_run.schema.json`
- 测试：`test_orchestrator.py`

```bash
python -m unittest -v test_orchestrator.py
```

测试覆盖审批缺失、审批幂等、DAG 解锁、全流程完成、State round 推进、上游输出漂移、
GPU minutes 上限、全局 GPU lease、active run 冲突、失败无自动重试、人工恢复和
optional skip。
