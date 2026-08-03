# Prediction v1.3.0 服务器部署与真实候选回归报告

日期：2026-08-01  
服务器：RTX 4090 部署机  
上游基线：`chemzi/dev` @ `0301df3`  
本地功能提交：`ee93552`  
服务器对应提交：`53f0133`  
服务器分支：`deploy/prediction-v1.3.0-ee93552`

## 1. 结论

Prediction v1.3.0 已在服务器部署并通过代码、数据层、Research、Design、结构可靠性和真实 GPU 回归。正式 `state.json` 和
`candidate_index.csv` 在全部测试前后哈希不变，候选评估全部在隔离数据副本中进行。

新版修复了真实候选回归暴露的集合评价偏差：

- L2 原已对 AF2 模型集合取中位 ipSAE，L5 和 PRODIGY 却只评估固定 `model 0`。
- `model 0` 在 C0513/MDMX 上是明显离群姿态，ipSAE 为 0.012，其他两个模型均约为 0.283。固定用它会让 L2、L3、L5 评价不同的代表结构。
- v1.3.0 使 L5 对所有声明 complex model 计算热点覆盖，取覆盖率中位数，位点一致性要求严格多数模型成立。
- runner 现为每个 complex model 运行 PRODIGY，每份输出与对应 PDB 的完整 SHA-256 绑定，L3 `dg` 取集合中位数。
- `prodigy_outputs` 必须恰好覆盖所有 complex prediction；缺失、重复、seed/predictor/model_id 不匹配或指向错误 PDB 都会 fail-closed。
- 旧版单文件 `prodigy_output` 仍可摄取，provenance 会显式标记为 `legacy_single_prediction`。

## 2. 服务器回归矩阵

| 测试 | 结果 |
|---|---:|
| Prediction 单元/集成回归 | 29/29 |
| Target bootstrap | 14/14 |
| Research threshold | 20/20 |
| Research/Design reliability | 9/9 |
| Design 脚本测试 | 21 组全部通过 |
| data_layer | 180/180 |
| 真实 GPU 固定序列 refold | 1/1 |

新增回归包括：

1. primary 模型为离群姿态时，L5 必须取全模型集合结果。
2. 多份 PRODIGY dG 必须取中位数。
3. PRODIGY 未完整覆盖所有 complex prediction 时，必须返回 `prodigy_coverage_mismatch`。
4. AF2 dropout 关闭时，seed 0/1/2 必须分别搭配 AF2 model 0/1/2，避免字节级重复证据。

## 3. 真实候选回归规模

本轮及其前置 v1.2.1 调试共使用 18 个不重复服务器候选，覆盖 Route A/B/C、MDM2/MDMX 设计来源及 10–17 aa 长度；累计完成 133 次真实 ColabDesign/AF2 前向预测。

- 初始接口/多路线集：5 个候选，35 次前向。
- 分层采样集：10 个候选，70 次前向。
- C0513 近邻家族：3 个新候选，21 次前向。
- v1.3.0 C0514 真实 smoke：7 次前向 + 6 份 PRODIGY。

10 个分层候选中，L1 通过 6/10，L5 在旧 primary 语义下通过 4/10，L2 在当前暂定 `ipSAE > 0.55` 门槛下通过 0/10。这些结果说明管线可以处理真实多路线产物，不支持将这批序列直接宣布为双靶命中。

## 4. 当前最有信息量的序列家族

| ID | 序列 | pLDDT | scRMSD (Å) | MDM2 中位 ipSAE | MDMX 中位 ipSAE |
|---|---|---:|---:|---:|---:|
| C0512 | TGTGETLEEFQA | 0.936 | 0.610 | 0.188 | 0.237 |
| C0513 | TGTGETLEEFQK | 0.955 | 0.531 | 0.215 | 0.283 |
| C0514 | TGTGETLEEFQE | 0.882 | 0.799 | 0.215 | 0.286 |
| C0515 | TGTGETLEEFRE | 0.950 | 0.586 | 0.173 | 0.302 |

该家族的中等双靶信号能在 1–2 个残基变化的近邻中复现，适合返回 Design 作为下一轮局部序列家族优化起点。它们仍低于当前暂定 L2 门槛，不应标记为 finalized。

v1.3.0 对 C0514 的真实复跑结果：

| 指标 | MDM2 | MDMX |
|---|---:|---:|
| 中位 ipSAE | 0.2145 | 0.2861 |
| 中位 ipTM | 0.6488 | 0.6479 |
| PRODIGY dG 中位数 (kcal/mol) | -8.242 | -7.896 |
| 三模型 hotspot coverage | 1.0 / 1.0 / 1.0 | 1.0 / 1.0 / 1.0 |
| site consistency fraction | 1.0 | 1.0 |

C0514 的 L1、L5、L7 通过；L2 低于当前暂定门槛；L3、L4、L6 因证据链不完整保持 pending。

## 5. 已确认的上游交接缺口

正式 CandidateIndex 共 1118 行，全部可通过 Prediction 输入契约：

- manifest：1118/1118；
- refold PDB：1118/1118；
- Design reference backbone：1015/1118。

Route A/B 共 1015 个候选都保存了原始设计骨架。Route C 的 103 个候选全部缺少 `backbone_pdb`，当前只有 refold PDB，因此 L7 会返回 `l7_reference_missing`。现有目录中没有可以安全补填的原始 Route C 骨架，Prediction 不会用 refold 冒充 reference。

## 6. 尚未完成的技术路线

- L3：服务器尚无 Rosetta InterfaceAnalyzer，缺 `sc` 和 `dSASA`。
- L4：尚未确定并登记保持共价环拓扑的 post-relax 方案。
- L6：当前三成员属于三个 AF2 参数模型，仍只有 AlphaFold2 一个模型家族；缺独立第二 predictor。
- 阈值标定：L2、L3、L4、L6 仍为 team provisional；L1/L7 有论文明确来源，L5 为已审阅设计规则。

本次没有根据 18 个设计候选反向修改阈值。阈值校准需要独立的已知阳性/阴性对照集，否则会把当前模型输出分布误当成生物活性边界。

## 7. 版本和数据保护

正式数据测试前后哈希：

- `data/state.json`: `10c6fdf79b030e9693664cb53e1512522aaad6e1546d37664a9e1ad0825a457f`
- `data/candidate_index.csv`: `4e4b0a0e8be7a5e959262a3cc76db5e28f983076a7c3ce462b605eeab2e89c84`

回滚点：

- branch: `backup/server-pre-v1.3.0-20260801T2324`
- server bundle: `/root/damodel-tmp/novapeptide/backups/server-pre-v1.3.0-20260801T2324.bundle`
- bundle SHA-256: `a86a1ad35a1f582e235aa3625ebba4bfe045468cc90ac04b3419b52c1ea97085`

真实 v1.3.0 smoke 产物：

- artifacts: `/root/damodel-tmp/novapeptide/prediction_artifacts_v130_c0514_smoke/C0514`
- isolated run: `/root/damodel-tmp/novapeptide/prediction_v130_c0514_smoke/runs/prediction_v130_c0514_smoke_20260801`
- artifact bundle SHA-256: `809f4f29a5b71db06f29a091afd3de75cf0c4e4b712d4c485555051cd8c3bff3`

## 8. 给同事群的简版更新

Prediction v1.3.0 已部署到 4090 服务器。本次使用服务器现有 Design 候选完成了多路线真实回归，累计 18 个不重复候选、133 次 ColabDesign/AF2 前向。测试发现旧版 L2 使用三模型中位数，L5 和 PRODIGY 却固定只看 model 0，当 model 0 恰好为离群姿态时会产生层间偏差。v1.3.0 已改为 L5 对所有模型聚合，PRODIGY 对每个模型单独运行并取 dG 中位数，同时强制每份评分与对应 PDB 哈希绑定。服务器回归已通过 Prediction 29/29、Target 14/14、Research 20/20、reliability 9/9、Design 21 组、data_layer 180/180 和真实 GPU refold 1/1。C0512–C0515 家族出现可复现的中等双靶信号，可作为下一轮 Design 局部优化起点；其 ipSAE 仍低于当前暂定门槛，暂不能作为命中结论。正式 State/CSV 未被测试修改。
