# Agent loop v1.1：C1250/L6 服务器回归记录

日期：2026-08-03  
服务器部署分支：`deploy/route-c-l7-critic-planner-v1.1.0`  
服务器提交：`b65e193`

## 1. 范围

本次验证复用冻结的 14 条真实 Prediction records：

```text
prediction_prod_20260803_c1250_c1270_unified
```

没有启动 RFdiffusion、ColabDesign、Boltz、Rosetta 或 post-relax，也没有修改候选
指标、阈值或历史 record。重跑内容限于 Critic v1.1 和 Planner v1.1 的 CPU 逻辑。

重跑前备份：

```text
/root/damodel-tmp/novapeptide/state_backups/pre_route_c_l7_v1.1_20260803/
```

其中 `state.json`、`candidate_index.csv` 和 `evidence_log.jsonl` 均保存 SHA-256。

## 2. Critic v1.1

输出：

```text
report_id: critic_2b6033a088e1
report_sha256: cb556ad4189e46f8de73bcf7e9295014487a6e635cd01abbfdf9b50ebab037c5
verdict: iterate
```

C1250 的唯一 L7 缺失由 `design_reference_missing` 表达：

- candidate：`C1250`
- category：`design_contract`
- action：`regenerate_design_reference`
- owner：`design`
- missing evidence：`scrmsd`

报告不再为 C1250 生成 `complete_prediction_evidence`。真实生产 preflight 同时确认：

- C1250：无 reference，返回 `design_reference_missing_preflight`；
- C1255：兼容读取旧 `backbone_pdb`，role 为 `legacy_backbone_pdb`。

12 条 L6 失败候选的 AF2/Boltz pose metrics 已存在；Critic 输出
`improve_pose_robustness`，owner 为 `design`，没有将其解释为 predictor 缺失。

## 3. Planner v1.1

输出：

```text
plan_id: planner_8136a4cd2b11
plan_sha256: b712b886915e7057df58e238dc7883b83adfa89bf31dacdebd9df3239b399147
status: awaiting_approval
automatic_dispatch_allowed: false
```

任务图由旧版 8 项收敛为 4 项：

1. `T001 iterate_design`：包含 `regenerate_design_reference` 和
   `improve_pose_robustness` 等 Design directives；
2. `T002 evaluate_new_design_candidates`：只消费 T001 新增候选并复用完整历史证据；
3. `T003 review_prediction_handoff`：消费 T002 的冻结 handoff；
4. `T004 propose_threshold_calibration`：只生成阈值校准提案，不写回阈值。

旧版针对 C1250 的 Prediction 补证据任务已经消失；旧版要求“只补缺失 predictor”的
L6 GPU 任务也已经消失。新 T004 只是阈值校准提案，与旧计划中编号相同的 C1250
任务含义不同；当前可以保持未批准，继续按项目既定策略暂缓正负对照标定。

## 4. 测试

服务器无 GPU 回归：

- Critic/Planner/Prediction：63/63；
- Design：21 组全部通过；
- Research/Design reliability：9/9（使用已有 `novapeptide-core` 环境）。

本地另行通过：

- data layer：180/180；
- target bootstrap、threshold research、Orchestrator：43/43；
- 固定序列 GPU 测试本轮没有启用，其历史测试状态没有被本次 CPU 回归替代。

## 5. 当前边界

C1250 的历史 manifest 仍保持原样，不能安全 backfill。下一轮 Design 需追加一个带
v5.2 独立 reference 的新 candidate ID；完成 T001 后再对新候选运行 T002。Route C
真实小批量 GPU 回归仍是下一次 Design 执行的一部分，不属于本次无 GPU 修复验证。
