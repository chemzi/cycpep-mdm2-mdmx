# Prediction v1.5.0：环肽 post-relax 与完整七层回归

日期：2026-08-02  
服务器：RTX 4090 部署机（post-relax 使用 CPU）  
候选：C0514，`TGTGETLEEFQE`  
许可场景：非商业学术、非营利或政府研究

## 1. 结论

Prediction v1.5.0 已补齐 L4 post-relax，并在 C0514 上跑通完整七层流程：

- `missing_evidence=[]`；
- `issues=[]`；
- L1、L4、L5、L6、L7 通过；
- L2、L3 按当前 team-provisional 门槛未通过；
- 最终状态为 `needs_optimization`，没有进入 `finalized`。

这次运行证明 Research/Design 产物能够进入完整 Prediction 证据链，也证明 L4
实现可工作。C0514 仍只是一条工程回归候选；这些计算结果不能替代实验结合与活性
验证。

## 2. 正式 post-relax 协议

工具固定为 PyRosetta `2026.29+releasequarterly.80a0635615`。每个候选单体依次：

1. 校验 primary 单体 PDB 仅含一条链，且序列与 CandidateIndex/Design manifest 一致；
2. 校验输入首尾 C—N 距离不大于 2.0 Å；
3. 使用 `PeptideCyclizeMover` 声明首尾酰胺键，同时添加键长、键角和二面角约束；
4. 用 ref2015 执行 3 repeats FastRelax；
5. 起始坐标约束标准差为 0.5 Å，约束不递减，侧链不做坐标约束，序列设计关闭；
6. 再次声明化学键以更新聚合物依赖原子；
7. 删除 FastRelax 临时虚拟锚点；
8. 独立检查输出序列、链、闭环距离、主链 RMSD 和哈希；
9. 保存无约束 ref2015 能量与完整协议 provenance。

仅使用 `DeclareBond` 的调试协议会让 C—N 距离从 1.280 Å 拉长到 1.494 Å。Rosetta
官方说明该 mover 只登记化学连接，不能单独维持良好键几何。正式版因而采用
`PeptideCyclizeMover` 的距离、角度和二面角约束；调试输出没有进入最终 bundle。

## 3. C0514 post-relax 结果

| 指标 | Relax 前 | Relax 后 |
|---|---:|---:|
| 末端 C—N 距离 (Å) | 1.2796 | 1.3292 |
| ref2015 总能量 (REU) | 35.8876 | -2.8481 |

补充结果：

- 主链 RMSD：0.1266 Å；
- ref2015 能量变化：-38.7357 REU；
- 临时虚拟残基删除数：1；
- 随机 seed：20260802；
- FastRelax repeats：3；
- 序列和链：`TGTGETLEEFQE` / chain A，前后完全一致。

两次固定 seed 运行的坐标和能量一致。Rosetta PDB footer 原先包含绝对输出路径，导致
相同坐标在不同目录下产生不同文件哈希；v1.5.0 已归一化该字段。归一化后的调试运行
和最终运行 PDB SHA-256 均为：

```text
721862979535db9fce027096c6eac4604396fda9b8f8ddb673fbf61f7698992f
```

## 4. 完整七层结果

| 层 | 结果 | 主要数值 |
|---|---|---|
| L1 单体质量 | 通过 | pLDDT 0.8823 |
| L2 界面置信度 | 未通过 | ipSAE: MDM2 0.2988，MDMX 0.2867；暂定门槛 >0.55 |
| L3 界面物理 | 未通过 | PRODIGY dG: -8.242/-7.881；SC: 0.5856/0.5277；暂定门槛分别 <-10、>0.6 |
| L4 环化几何 | 通过 | C—N: 1.2796→1.3292 Å；门槛 <2.0 Å |
| L5 设计意图 | 通过 | 两靶 hotspot coverage 均为 1.0 |
| L6 集合鲁棒性 | 通过 | pose RMSD: 1.4208/0.8752 Å |
| L7 可设计性 | 通过 | scRMSD 0.7993 Å |

L3 的 dSASA 为 1144.59/1012.83 Å²，高于暂定的 400 Å² 门槛；L3 仍因 dG 与 SC
子项未全部满足而失败。L2、L3、L4、L6 的阈值依据仍标记为 team-provisional，
正负对照标定继续待办。

## 5. 最终产物

```text
artifact bundle:
/root/damodel-tmp/novapeptide/prediction_artifacts_v150_c0514_final_20260802/C0514/artifacts.json

isolated run:
/root/damodel-tmp/novapeptide/prediction_v150_c0514_final_20260802/runs/prediction_v150_c0514_final_20260802
```

SHA-256：

- artifact bundle：`41d7844ca49a3901985c6a8bf851abd513dc5314f05e923ffa5b94abe920270a`
- post-relax PDB：`721862979535db9fce027096c6eac4604396fda9b8f8ddb673fbf61f7698992f`
- post-relax metadata：`874f0231f32276bd085a7990858b89b6fc8da5ef2c816b9311f7fc012a62ad11`
- C0514 record：`382a795b798ecf2b28efee90c60de188fa1ab17f37aeea5344f5a21c46429103`

正式数据在 enrichment、FastRelax 和完整七层运行前后保持不变：

- `data/state.json`：`10c6fdf79b030e9693664cb53e1512522aaad6e1546d37664a9e1ad0825a457f`
- `data/candidate_index.csv`：`4e4b0a0e8be7a5e959262a3cc76db5e28f983076a7c3ce462b605eeab2e89c84`

## 6. 下一步

1. 将 v1.5.0 用于更多 Design 候选，观察 L4 几何和能量分布；
2. 优先把 C0514 的 L2/L3 失败用于 Design 反馈，而不应放宽阈值追求通过；
3. 团队准备好正负对照后，标定 ipSAE、PRODIGY dG、SC、dSASA、L4 和 L6 门槛；
4. 阈值标定完成前，任何候选都不得因当前暂定门槛结果被宣称为最终命中。
