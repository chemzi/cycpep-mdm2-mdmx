# KEAP1 后端端到端与结构回放验证（2026-08-03）

## 1. 目的与范围

本次使用 KEAP1 Kelch domain 作为 MDM2/MDMX 以外的单靶标 benchmark，验证项目配置、
Research、Route A Design、Prediction、Critic 和 Planner 能否通过通用合同运行。测试分为：

1. 已知结构回放：冻结 7K2E、7K2F、7K2G、7K2H、7K2I、7K2M 六个晶体复合物，
   模型阶段不读取实验结合姿态，最后解盲计算 target-aligned peptide pose RMSD；
2. de novo 小批量：从 KEAP1 受体结构和审核 hotspot 出发，实际运行
   RFdiffusion → LigandMPNN → fixed-sequence cyclic refold，再将新候选送入完整下游。

服务器隔离运行根目录：

```text
/root/damodel-tmp/novapeptide/keap1_benchmark_20260803_v1
```

## 2. Research

Research v3 完成 RCSB 搜索、GraphQL enrichment、界面计算、口袋聚合、PubMed、LLM
抽取和阈值检索，未使用 fallback。共发现 36 个 KEAP1 peptide complex，首轮口袋聚合
使用 10 个结构。审核项目配置中的已知 binder `c[GDEETGE]` 以
`approved_project_config` provenance 进入 Research 交接。

KEAP1 没有经过正负对照标定的 L5 hotspot coverage 阈值，因此该阈值保持 null。运行时
不会把 null 转成 0，也不会因此崩溃或产生伪清关。

## 3. 已知结构回放

六个 7 aa head-to-tail cyclic peptide 均通过固定序列 refold 的序列一致性和闭环预筛，
随后每个候选生成：

- 1 个 ColabDesign cyclic monomer；
- 3 个使用不同 AF2 权重的 KEAP1 complex；
- 1 个 Boltz-2 complex；
- 4 份 PRODIGY 与 4 份 PyRosetta InterfaceAnalyzer 结果；
- 3 重复、受坐标约束的 PyRosetta post-relax。

解盲 pose recovery 结果：

| Candidate | PDB | Sequence | Best RMSD (Å) | Median RMSD (Å) | Best < 2 Å |
|---|---|---:|---:|---:|---:|
| C0001 | 7K2E | GDEETGE | 0.513 | 0.658 | yes |
| C0002 | 7K2F | GAEETGE | 1.241 | 4.881 | yes |
| C0003 | 7K2G | GDEEAGE | 0.495 | 1.098 | yes |
| C0004 | 7K2H | GDPETGE | 0.784 | 6.586 | yes |
| C0005 | 7K2I | GAPETGE | 5.219 | 6.374 | no |
| C0006 | 7K2M | GEPETGE | 6.217 | 6.483 | no |

至少一个模型达到 RMSD <2 Å 的恢复率为 **4/6（66.7%）**。Boltz-2 单模型恢复
4/6；三个 AF2 模型中至少一个恢复的比例为 2/6。若使用四模型 pose RMSD 中位数作为
稳定恢复标准，则为 2/6。该结果说明独立 predictor 对本数据集有实际增益，同时也说明
“偶尔命中正确姿态”和“多模型稳定收敛”必须分别报告。

六个候选在七层 Prediction 中均为 `prediction_pending`，根因是 L5 阈值为 null；
`missing_evidence=[]`，模型、Rosetta 和 post-relax 证据完整。该状态不能解释为候选失败，
也不能解释为最终通过。

权威回测文件：

```text
/root/damodel-tmp/novapeptide/keap1_benchmark_20260803_v1/benchmark_pose_recovery_all6.json
/root/damodel-tmp/novapeptide/keap1_benchmark_20260803_v1/prediction_runs/prediction_keap1_reference_all6_full/prediction_handoff.json
```

## 4. de novo Route A

Route A 使用长度 7、9、12 aa 各生成一个 RFdiffusion cyclic binder backbone：

- 3 个 RFdiffusion backbone 全部生成；
- LigandMPNN 共返回 21 条原始序列；
- 全局 cheap filter 保留 3 条；
- 2 条通过 fixed-sequence refold 和末端 C—首端 N 几何检查，登记为 C0007、C0008；
- C0009 闭环检查失败，未写入 CandidateIndex。

C0007（VTDLRNTGI）被选入完整 Prediction。关键结果：

| Layer/metric | Value | Result |
|---|---:|---|
| L1 pLDDT | 0.8198 | pass |
| L2 ipSAE | 0.00734 | fail under provisional gate |
| L3 PRODIGY dG | -8.885 kcal/mol | combined L3 fail |
| L3 Rosetta SC | 0.388 | combined L3 fail |
| L3 Rosetta dSASA | 963.9 Å² | combined L3 fail |
| L4 C—N pre/post | 1.222 / 1.329 Å | pass |
| post-relax backbone RMSD | 0.188 Å | stable |
| L5 hotspot coverage | 0.25 | threshold null; pending |
| L6 ensemble pose RMSD | 28.474 Å | fail |
| L6 seed convergence | 0.25 | fail |
| L7 scRMSD to RFdiffusion backbone | 2.737 Å | fail |

C0007 有完整证据且没有 artifact issue，但结合置信度、模型姿态收敛和 Design backbone
一致性较差，因此不能作为命中候选。Critic v1.1.1 输出 Design 迭代建议，Planner 生成：

- T001：迭代 Design，改进界面、物理打分、姿态鲁棒性和 L7 一致性；
- T002：仅评估 T001 新产生的候选，复用已有完整证据；
- T003：审查新的 Prediction handoff；
- T004：提出 KEAP1 阈值标定方案，未经人工科学审核不得应用。

正式 State 最终恢复到 C0007 的 Planner 阶段：

```text
Prediction run: prediction_keap1_de_novo_c0007_full
Critic report: critic_2873ace98ab6
Planner plan: planner_c9d6964beb8d
Planner status: awaiting_approval
```

## 5. 本次跨靶标验证发现并修复的问题

1. Research interface aggregate 曾硬编码 MDM2/MDMX，现按实际 target 动态聚合；
2. null threshold 曾被 Critic 误判成缺模型证据，现严格区分阈值缺失与 evidence 缺失；
3. `prediction_pending` 曾遮蔽其他已有完整证据的失败层，Critic v1.1.1 现在保留这些
   Design 反馈，同时不把 null 所属层误报成科学失败；
4. State 默认 Design budget 曾固定为 `route_A_mdm2/route_A_mdmx`，现按项目 targets
   动态生成；KEAP1 为 `route_A_keap1/route_B/route_C`；
5. benchmark 评分曾假设所有参考受体与部署受体长度完全一致，现对小量末端 overhang
   做严格全局序列对齐，并要求覆盖率至少 80%、一致性至少 90%，否则 fail-closed。

相关提交：

```text
65d7457 feat(benchmark): add KEAP1 canonical cyclic replay
7a08b3e fix(research): generalize interface aggregation
6285b21 fix(agent-loop): distinguish missing thresholds from evidence
0e4ff13 fix(critic): preserve complete metric failures under null gates
becc0b9 fix(state): derive design budgets from project targets
fb16c93 fix(benchmark): align target chains with terminal overhangs
```

本地和服务器均通过 43 项 Prediction 测试与 46 项 Research/Critic/Planner 测试。

## 6. 结论与下一步

后端已证明可以在 KEAP1 单靶标配置下完成 Research → Design → Prediction → Critic →
Planner，并能对实验结构集做盲 pose recovery。此次小批量 de novo 候选没有得到可清关的
结合姿态，Agent 正确返回迭代计划，没有把工程跑通解释成药物命中。

建议下一步按以下顺序推进：

1. 用六个结构正对照加构象/序列负对照建立 KEAP1 阈值标定集，优先处理 L2、L5、L6；
2. 分析 C0005/C0006 与 C0001–C0004 的 pose recovery 差异，评估 Boltz 多 seed 和
   AF2 权重选择对召回率的影响；
3. 执行 Planner T001 的 12 条小批量迭代，重点约束已审核 hotspot 与 backbone
   self-consistency；
4. 在前端同时展示 best-model recovery、ensemble median、missing threshold 和
   evidence completeness，避免把四种语义压成一个 pass/fail。

