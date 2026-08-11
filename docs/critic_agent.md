# Critic Agent v1.1

Critic 是 Prediction 与 Planner 之间的审查层。它读取 Prediction 已冻结的 handoff
和逐候选 record，将失败原因转换成结构化 issue 与建议动作。Critic 不运行模型、
不修改阈值、不删除候选，也不直接启动下一轮 Design。

## 1. 权威输入

Critic 的入口是 `prediction_handoff.json`。每个 handoff 条目必须提供：

- candidate ID；
- Prediction status；
- 权威 `record_path`；
- record 的完整 SHA-256。

Critic 会重新计算 record 哈希，并核对 candidate ID、status、run ID 和 Prediction
版本。任何哈希或身份冲突都会 fail-closed。CandidateIndex 只用于补充 sequence、
source route 等候选池信息；若它与冻结 record 的序列不一致，报告为 blocker。

报告合同见 `agents/critic_report.schema.json`。

## 2. 问题分类

| 类别 | 典型 issue | 含义 |
|---|---|---|
| operational | `invalid_prediction_artifact` | 输入、哈希、序列、链或几何合同无效 |
| operational | `prediction_evidence_incomplete` | Prediction 原始证据或阈值值缺失 |
| design_contract | `design_reference_missing` | Design 未提供可计算 L7 的独立结构 reference |
| scientific_metric | `l2_interface_confidence_low` | 完整证据下 ipSAE 数值失败 |
| scientific_metric | `l3_interface_physics_low` | 完整证据下 dG、SC 或 dSASA 子门失败 |
| scientific_metric | `l4_cyclization_geometry_failed` | relax 前后闭环几何失败 |
| calibration | `threshold_calibration_pending` | 有暂定值，但依据尚不足以最终清关 |
| diversity | `duplicate_sequences` | 不同 candidate ID 使用相同序列 |
| diversity | `low_sequence_diversity` | 候选池序列相似度过高 |
| cohort | `cohort_too_small` | 样本不足以判断 route 或分布趋势 |

L2 只把 ipSAE 当主判据；ipTM 保留在指标快照中作为参考，不触发 L2 Critic issue。
L6 已由 Prediction 负责检查 AlphaFold2/Boltz 独立模型家族和姿态收敛，Critic 不再
沿用旧版 “AfCycDesign vs ColabFold” 文本比较。

v1.1 对两种容易混淆的情形作了明确分流：

- record 含 `l7_reference_missing` 时，生成 `regenerate_design_reference`，owner 为
  Design；不会再生成“补 Prediction 证据”的任务。历史候选保持不可变，通过 Design
  追加一个带独立 reference 的新候选处理。
- L6 在 AF2/Boltz 数量、模型家族和 provenance 都完整后仍失败，表示姿态没有收敛；
  `improve_pose_robustness` 的 owner 为 Design。缺 predictor 的情形仍由
  `prediction_evidence_incomplete` 负责。

## 3. Verdict

- `blocked`：存在无效 artifact、候选索引漂移等 blocker；先修复数据合同。
- `iterate`：证据完整但指标失败，或存在需要补齐的证据；Planner 应生成迭代任务。
- `review`：主要问题是阈值标定或需要人工判断的中等级问题。
- `clear`：没有 blocker/high/medium 问题。`passed=true` 只对应该状态。

Critic 的 `clear` 表示审查没有发现新的阻断问题；最终候选资格仍以 Prediction 的
`competition_clearance` / `final_status=finalized` 为准。

## 4. Planner handoff 安全边界

每份报告都带有以下固定约束：

- 不自动修改阈值；
- 不自动删除候选；
- 没有 Planner 预算和执行批准时不启动 GPU；
- 已完整的 Prediction 证据必须复用。

因此 C0514 的 L2/L3 失败会产生 Design 迭代建议，但不会建议重新运行已经完整的
Boltz、Rosetta 或 post-relax。

## 5. 运行

```bash
python agents/critic.py review \
  --handoff /path/to/prediction_handoff.json
```

默认报告写到：

```text
<prediction_run>/critic/<report_id>/critic_report.json
```

可用 `--output` 指定其他位置。相同 handoff、阈值、CandidateIndex 摘要、Critic
配置和版本会产生相同 `report_id`。重复运行不会重复写 iteration history 或
`critic_review` evidence 事件。新发布的 `critic_review` 必须携带 Critic report
不可变 `source.project_id` 提供的 project binding；direct 与 transaction-managed
发布使用同一 binding contract。Evidence 幂等身份由 project、Prediction run、
report ID 和 report digest 共同确定。既有未绑定事件保持不变且不具备项目级转换
权威；受支持的显式恢复可追加一个绑定事件，之后的重复恢复不会再次追加。

## 6. 验证

```bash
python -m unittest -v test_critic.py
```

测试覆盖 C0514 式 L2/L3 失败、pending、invalid、record 哈希漂移、CandidateIndex
序列漂移、重复序列、clear verdict 和幂等状态写回。
