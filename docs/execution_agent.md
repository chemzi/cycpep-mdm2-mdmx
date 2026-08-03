# Execution Worker v1.0

Execution 位于 Planner/Orchestrator 与科学 Agent 进程之间。Planner 只产生语义动作，
Orchestrator 负责依赖、审批、claim token 与单 GPU lease，Execution 使用固定 handler
构造参数数组并启动进程。任务合同没有 `command`、`shell`、`executable` 或任意环境变量
字段。

## 1. v1 可执行动作

| action | 固定 handler | 权威输出 |
|---|---|---|
| `iterate_design` | 依次运行物化后的 target/route/length/count/seed Design jobs；核对 CandidateIndex 只能追加 | `design_task_result.json` |
| `evaluate_new_design_candidates` | 复用完整 artifact；缺失时固定运行 AF2/PRODIGY → Boltz-2/Rosetta/post-relax → Prediction ingest | `prediction_handoff.json` |
| `review_prediction_handoff` | 只读取上游 SHA-256 绑定的 handoff，运行 Critic | `critic_report.json` |
| `propose_threshold_calibration` | 生成控制数据需求和阈值快照；不写回 State 阈值 | `threshold_calibration_proposal.json` |

Planner v1.2 会把 T001 物化为 `design_jobs`。每个 job 明确声明 `route`、
`target_id`、`lengths`、`proposal_count` 和 `seed`；Worker 不再临时猜路线。T002 固定使用
`af2_boltz2_prodigy_rosetta_postrelax_v1` 协议。Critic 提出的
`complete_prediction_evidence` 会被 Planner 映射到同一个 Prediction handler，并用显式
候选范围补齐或复用证据，不再产生无法执行的旧 action。

## 2. 运行

执行一个 ready task：

```bash
python -m execution.worker run-task \
  --run /path/to/orchestrator_run.json \
  --task T001 \
  --worker execution-worker-01
```

按 task ID 顺序执行所有 ready task，直到计划完成、等待审批或失败：

```bash
python -m execution.worker drain \
  --run /path/to/orchestrator_run.json \
  --worker execution-worker-01
```

使用已有完整 artifact 做一次隔离的真实链路自检：

```bash
python scripts/run_execution_selfcheck.py \
  --project-config projects/mdm2_mdmx.json \
  --source-data-dir /path/to/formal/data \
  --artifacts-root /path/to/full/prediction_artifacts \
  --work-root /path/to/new/selfcheck_run \
  --candidate C1270
```

脚本复制 `state.json` 和 `candidate_index.csv` 后，按正常合同运行
Planner → Orchestrator → Execution → Prediction ingest → Critic。它只写 `work-root`，适合
部署后健康检查；自检报告明确记录是否复用了完整证据、是否意外重跑重模型，以及两层输出哈希。

每个进程保存 `stdout.log`、`stderr.log` 和 `process.json`。超时或 Worker 收到中断时，
会先终止整个子进程组，再向 Orchestrator 报告失败。GPU 用量当前按任务持有 GPU lease
的墙钟时间计入审批预算。

## 3. 服务端可信配置

这些路径来自 Worker 服务环境，不来自 Planner 或前端：

| 环境变量 | 用途 |
|---|---|
| `CYCPEP_EXECUTION_ROOT` | task 日志和 execution receipt 根目录 |
| `CYCPEP_DESIGN_AGENT_PYTHON` | Design Agent Python |
| `CYCPEP_PREDICTION_PYTHON` | ColabDesign/Prediction Python |
| `CYCPEP_PREDICTION_ARTIFACTS` | 可复用完整 artifact 根目录 |
| `COLABDESIGN_DIR` / `COLABDESIGN_PARAMS` | 锁定 ColabDesign checkout/参数 |
| `CYCPEP_BOLTZ_EXECUTABLE` / `CYCPEP_BOLTZ_CACHE` / `CYCPEP_BOLTZ_CHECKPOINT` | Boltz-2 |
| `CYCPEP_PRODIGY_EXECUTABLE` | PRODIGY |
| `CYCPEP_PYROSETTA_PYTHON` | InterfaceAnalyzer 与 post-relax |
| `CYCPEP_CONTROL_DATA` | 可选、同协议正负对照数据 |

前端后续只能提交“运行已冻结计划/任务”的请求，不能传上述路径。

## 4. 完成合同

Orchestrator v1.1 不再只检查文件存在和 SHA-256，还会按 action 读取 JSON：

- Design 结果必须附带 CandidateIndex 前后快照，证明旧行未变化、新 ID 来自真实追加，并逐个验证新 manifest 哈希；
- Prediction handoff 必须与任务或上游 Design 的候选集合完全一致，并验证所有 record 路径和 SHA-256；
- Critic report 必须验证 `report_id`、`input_digest`，以及实际消费的上游 handoff SHA-256；
- calibration proposal 必须声明 `applied_to_state=false`。

内容为 `ok` 的占位文件不能完成任务。

## 5. v2 docking/MD 扩展点

以下 action 名称已经保留，但 v1 明确拒绝 claim：

- `dock_shortlisted_candidates`
- `run_md_on_docking_consensus`

拟定机器合同见 `execution/v2_extension_contracts.schema.json`。接口预留不会改变当前七层
Prediction，也不会让未标定的 docking/MD 指标成为硬门槛。v2 实现 handler、工具版本、
环化拓扑验证和正负对照后，才可提升为 executable。
