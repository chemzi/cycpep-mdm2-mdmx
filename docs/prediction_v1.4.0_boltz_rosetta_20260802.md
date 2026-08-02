# Prediction v1.4.0：Boltz-2 与 Rosetta 配置记录

日期：2026-08-02  
服务器：RTX 4090 部署机  
输入候选：C0514，`TGTGETLEEFQE`  
正式数据：测试全过程只读

## 1. 当前结论

- 正负对照阈值标定没有被设为运行前置条件；模型证据可以先生产和摄取。
- 独立第二 predictor 选用 Boltz-2 2.2.1，已在 MDM2、MDMX 上完成真实 GPU
  运行，并被 Prediction 的 L6 识别为独立于 AlphaFold2 的第二模型家族。
- RosettaScripts InterfaceAnalyzer 适配器已经实现：对每个复合物模型先声明环肽
  末端 C—N 共价键，再执行 ref2015 界面分析；评分与输入 PDB 的 SHA-256 一一
  绑定。服务器尚未安装 Rosetta 二进制，需先确认项目适用的 Rosetta 许可证。
- post-relax 没有包含在本次配置中，L4 继续明确返回 pending。

## 2. Boltz-2 固定环境

```text
environment: /root/damodel-tmp/envs/boltz-2.2.1
package: boltz[cuda]==2.2.1
torch: 2.13.0+cu130
GPU: NVIDIA GeForce RTX 4090
cache: /root/damodel-tmp/novapeptide/boltz_cache
```

关键模型文件：

| 文件 | SHA-256 |
|---|---|
| `boltz2_conf.ckpt` | `090e82ac8c92f5e943fa1b39e7410a44027bea7243c0bbb3caa67a77fc1428e1` |
| `boltz2_aff.ckpt` | `dcc5cd3722b1c9eaa34267e4ae32f55cbbf1963f4c19319381ccfa30fdd2ca9e` |
| `mols.tar` | `39e076d96dbec6b4e86982bbda16f3a53a2a60c9bdc17828d88f6f9a0c7d1fd7` |

当前输入使用显式空 MSA，以保证中国内地服务器不依赖远程 MSA 服务。环肽同时
设置 `cyclic: true` 和末端 C—N 显式 bond constraint。worker 会拒绝版本漂移、
checkpoint 哈希漂移、序列变化、链变化、PAE 维度错误和闭环距离大于 2.0 Å的输出。

## 3. C0514 真实双靶标回归

Boltz 原始单模型结果：

| 靶标 | Boltz ipTM | Boltz ipSAE | 末端 C—N (Å) | PRODIGY dG (kcal/mol) |
|---|---:|---:|---:|---:|
| MDM2 | 0.9298 | 0.8047 | 1.3417 | -8.552 |
| MDMX | 0.9604 | 0.8308 | 1.3411 | -7.642 |

将每个靶标的 3 个 AlphaFold2 模型和 1 个 Boltz-2 模型共同摄取后：

| 指标 | MDM2 | MDMX |
|---|---:|---:|
| 四模型中位 ipSAE | 0.2988 | 0.2867 |
| 四模型中位 ipTM | 0.6929 | 0.6512 |
| pose RMSD (Å) | 1.4208 | 0.8752 |
| hotspot coverage | 1.0 | 1.0 |

L6 在两个靶标上均通过：每靶标 4 个结构、2 个模型家族
（AlphaFold2、Boltz-2），且 2 Å 聚类收敛率为 1.0。该结果证明第二 predictor 的
artifact、metadata、PAE、集合几何和 L6 判定链路已经贯通。

这次运行仍是单候选工程回归，不能据此确认 C0514 的真实结合活性。Boltz 单模型的
分数明显高于 AlphaFold2 集合，后续应在更多候选和已知对照上判断这是稳定的模型
差异还是该候选的有效独立支持。

## 4. 当前 Prediction 状态

隔离运行状态为 `prediction_pending`：

- L1、L5、L6、L7：通过；
- L2：按当前暂定数值门槛未通过；
- L3：PRODIGY 已齐，Rosetta 的 `sc`、`dSASA_int` 尚缺；
- L4：post-relax 尚缺；
- 全部阈值来源尚未完成正负对照标定，因此禁止 `finalized`。

跳过标定只影响最终科学清关，不妨碍继续生成 Boltz/Rosetta 原始证据、调试接口和
筛选运行故障。标定完成前，任何候选最多进入工程上的 `prediction_pending`、
`needs_optimization` 或 `awaiting_threshold_calibration`。

## 5. 产物与数据保护

```text
artifact bundle:
/root/damodel-tmp/novapeptide/prediction_artifacts_v140_c0514_boltz_smoke_20260802/C0514/artifacts.json

isolated run:
/root/damodel-tmp/novapeptide/prediction_v140_c0514_boltz_smoke_20260802/runs/prediction_v140_c0514_boltz_smoke_20260802
```

正式文件在运行前后哈希一致：

- `data/state.json`：`10c6fdf79b030e9693664cb53e1512522aaad6e1546d37664a9e1ad0825a457f`
- `data/candidate_index.csv`：`4e4b0a0e8be7a5e959262a3cc76db5e28f983076a7c3ce462b605eeab2e89c84`

## 6. Rosetta 的剩余一步

代码侧已经提供 `prediction_pipeline/rosetta_worker.py` 和
`--rosetta-scripts` 接口。安装 Rosetta 之前需要由项目负责人确认此次运行属于：

1. 非商业的学术、非营利或政府研究；或
2. 公司/商业用途。

两类场景的 Rosetta 授权路径不同。确认许可证并获得合法发行包后，才能在服务器上
安装二进制并执行真实 Rosetta GPU/CPU 回归；本项目不会用来源不明的二进制代替。
