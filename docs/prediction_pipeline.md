# Prediction 生产管线

## 1. 边界与数据流

Prediction 只处理 Design 已登记到 `CandidateIndex` 的候选。生产路径不创建候选、不填 demo 阈值、不生成伪分数。

```text
CandidateIndex + Design manifest
        │  candidate/sequence/path/hash 门禁
        ▼
GPU/CPU tools ──> <artifact-root>/<candidate_id>/artifacts.json
        │              原始 PDB / PAE / tool output / metadata
        ▼
strict parser ──> validated metrics + provenance + recoverable issues
        │
        ▼
evaluate_battery(metrics, Research thresholds, required_targets)
        │
        ├── candidate_index.csv        供表格/UI快速读取
        ├── records/<candidate>.json   权威逐候选记录
        └── prediction_handoff.json    Critic/Planner/实验选择入口
```

权威证据是逐候选 record；CSV 是便于检索的摘要。所有输入和 artifact 使用完整 SHA-256 记录。Design 早期 manifest 中的 12 位哈希仍可验证，因为它被当作完整哈希前缀，Prediction 会重新计算并保存完整值。

## 2. Design → Prediction 输入契约

每条候选必须同时满足：

- `candidate_id` 匹配 `C\d{4,}`，且在本批次唯一；
- 序列只含 20 种标准氨基酸，长度 8–20；
- CandidateIndex 与 manifest 的 candidate ID、序列、长度一致；
- manifest 的 refold PDB 存在且哈希一致；
- refold PDB 的首个 `MODEL` 中恰好有一条链与候选序列完全一致；
- 当前 L4 支持 `head_to_tail_amide`（兼容旧拼写后转成统一值）；
- L7 优先使用 manifest v5.2 的 `design_reference_pdb`，并兼容旧
  `backbone_pdb`；reference 与 fixed-sequence refold 路径相同或文件内容相同时
  fail closed，不能用 refold 冒充独立设计骨架。

Prediction v1.5.1 将“历史证据摄取”和“新生产计算”分开处理：核心摄取管线仍会把
旧候选的缺失 reference 记录为 `l7_reference_missing` / pending，保留可审计历史；
`run_prediction_predictors.py` 与 `enrich_prediction_evidence.py` 会在启动任何
ColabDesign、Boltz、Rosetta 或 post-relax 计算前做整批 preflight。缺少 reference
时返回 `design_reference_missing_preflight`，要求回到 Design 生成新候选，避免其余
六层模型全部完成后才发现 scRMSD 无法计算。

Design 的 refold 已改为 `model.predict(seq=...)`。它会检查 ColabDesign hard
sequence 和输出 PDB 序列；`design_3stage` 不再参与固定序列 refold。运行时还会
核对 ColabDesign 完整提交、tracked source 清洁状态，以及 AlphaFold relative
position 模块是否实际消费 pairwise `offset`。循环矩阵的相邻位点和首尾位点都由
回归测试验证为序列距离 1。

## 3. Artifact bundle

Schema 位于 [`prediction_pipeline/artifacts.schema.json`](../prediction_pipeline/artifacts.schema.json)。每条候选固定使用：

```text
<artifact-root>/C0001/artifacts.json
```

最小示例：

```json
{
  "schema_version": 1,
  "candidate_id": "C0001",
  "sequence": "ACDEFGHI",
  "global": {
    "monomer_predictions": [
      {
        "predictor": "ColabDesign",
        "seed": 0,
        "primary": true,
        "pdb": "colabdesign_monomer/seed_0/prediction.pdb",
        "pae": "colabdesign_monomer/seed_0/pae.npz",
        "metadata": "colabdesign_monomer/seed_0/metadata.json"
      }
    ],
    "post_relax_pdb": "relax/post_relax.pdb",
    "post_relax_metadata": "relax/metadata.json",
    "design_reference_pdb": "/absolute/path/to/design_backbone.pdb"
  },
  "targets": {
    "MDM2": {
      "target_chain": "A",
      "complex_predictions": [
        {
          "predictor": "ColabDesign",
          "seed": 0,
          "primary": true,
          "pdb": "mdm2/colabdesign_seed0.pdb",
          "pae": "mdm2/colabdesign_seed0_pae.json",
          "metadata": "mdm2/colabdesign_seed0_metadata.json",
          "binder_chain": "B"
        },
        {
          "predictor": "Boltz",
          "seed": 101,
          "pdb": "mdm2/boltz_seed101/prediction.pdb",
          "pae": "mdm2/boltz_seed101/pae.npz",
          "metadata": "mdm2/boltz_seed101/metadata.json",
          "binder_chain": "B"
        }
      ],
      "prodigy_outputs": [],
      "rosetta_outputs": []
    },
    "MDMX": {
      "target_chain": "A",
      "complex_predictions": [],
      "prodigy_output": "mdmx/prodigy.txt",
      "rosetta_output": "mdmx/interface_analyzer.sc"
    }
  }
}
```

可为每个文件附加 `<field>_sha256`。如果声明了哈希，Prediction 要求完整相等；未声明时也会计算并写入 provenance。

## 4. 七层指标的实际定义

| 层 | 生产输入 | 计算与聚合 | 缺失行为 |
|---|---|---|---|
| L1 | primary monomer PDB | 候选链 CA 的 B-factor；自动识别 0–1 或 0–100 并归一到 0–1 | pending |
| L2 | 每靶标 complex PDB + PAE | DunbrackLab IPSAE v4 的 residue-specific `d0res`，双方向取 max；多个声明预测取中位数 | pending；PAE/PDB 维度不符为 invalid |
| L3 | 每个声明复合物模型的 PRODIGY 输出 + Rosetta InterfaceAnalyzer score | `dg` 取多模型 PRODIGY 中位数；`sc`、`dSASA_int` 取逐模型 Rosetta 结果中位数 | pending；覆盖不完整、歧义或格式损坏为 invalid；旧版单文件仍可读取 |
| L4 | primary monomer + post-relax PDB/metadata | metadata 必须核对工具版本、protocol、输入/输出哈希、序列、环化类型并声明已应用共价闭环拓扑；随后计算真正的末位残基 `C` 到首位残基 `N` 距离 | 缺 post-relax 或 provenance 为 pending；哈希、序列、拓扑声明冲突为 invalid；有值但硬门失败为 invalid |
| L5 | 所有声明 complex PDB + project binding site | 每模型计算 4.5 Å 重原子接触；热点覆盖率取中位数，位点一致性要求严格多数模型命中已审阅位点 | pending |
| L6 | 每靶标至少 3 个模型、至少 2 个独立模型家族 | 逐项核对 metadata 中的 tool、版本、model_family、model_id 和 seed；拒绝重复 tool/model/seed 与重复 PDB；靶蛋白共同 CA 做 Kabsch 后计算 binder backbone RMSD，跨模型家族 RMSD 取中位数，ensemble convergence 为 2 Å 簇最大占比 | provenance、独立模型家族或 ensemble 成员不足为 pending；声明与 metadata 冲突为 invalid |
| L7 | primary monomer + 独立 Design reference | 等残基顺序的 N/CA/C Kabsch backbone RMSD | 历史摄取为 pending；新生产计算在 GPU 前置门禁失败 |

`ipsae_pae_cutoff=10 Å`、接触距离和 seed 聚类距离属于方法参数，会写入 record；它们与 Research 提供的候选筛选阈值分开管理。

靶标热点始终使用已审批 target PDB 的原始残基编号。ColabDesign 等 predictor
可能把输出靶标链改成从 1 开始、负数或其他内部编号；Prediction v1.2.1 会先验证
输出靶标链与已审批坐标的序列和长度完全一致，再按序列顺序恢复原始 PDB 编号。
映射使用的 target PDB 路径和完整 SHA-256 会写入 provenance。跨 predictor 的 L6
靶标对齐也优先使用已验证的相同序列顺序，避免不同编号体系造成假性不收敛。

L6 将底层模型家族视为独立性的边界。ColabDesign 和 ColabFold 都基于
AlphaFold2 时只计为一个模型家族，不能通过修改 `predictor` 字符串充当两种独立
证据。每个参与 L6 的 metadata 必须包含 `tool`、`tool_commit` 或
`tool_version`、`model_family`、`model_id` 和 `seed`，并与 artifact 声明逐项一致。
ColabDesign 在 `dropout=False` 且固定 AF2 参数模型时是确定性的，只改变 seed 会
产生完全相同的 PDB。批量 runner 因此默认把 seed 0/1/2 分别配给 AF2 model
0/1/2；也可用 `--model-numbers` 显式指定一一对应的模型列表。

Prediction v1.3.0 不再让固定的 `primary/model 0` 单独决定复合物层指标。
L5 对 artifacts 中声明的全部复合物模型逐一计算热点覆盖并聚合；启用
`--prodigy` 时，runner 也会为每个模型生成一份带 PDB 哈希关联的输出，L3 的
`dg` 取集合中位数。`prodigy_outputs` 必须一一覆盖全部 complex prediction；
缺失、重复或指向错误 PDB 会 fail-closed。旧版 `prodigy_output` 单文件仍可摄取，
其 provenance 会明确标记为 `legacy_single_prediction`。

Prediction v1.4.1 增加独立的 Boltz-2 证据和逐模型 Rosetta 证据。Boltz 固定为
2.2.1，并同时写入 `cyclic: true` 与末端 C—N 显式共价键；输出 PDB 必须保持候选
序列、链 ID 和不大于 2.0 Å 的闭环距离，PAE 维度必须与靶标加环肽的残基数完全
一致。当前部署使用显式空 MSA，绕开大陆服务器不可用的远程 MSA 服务；这是可复现
的单序列模式，但官方也提示其精度低于有 MSA 的运行，后续可为 MDM2/MDMX 预计算
并冻结 MSA 后再升级证据。

`rosetta_outputs` 与 `prodigy_outputs` 一样，必须通过 PDB SHA-256 一一覆盖所有
`complex_predictions`。RosettaScripts 在 InterfaceAnalyzerMover 前先用
DeclareBond 删除线性末端类型并声明环肽首尾酰胺键，然后用 ref2015、
`pack_input=true`、`pack_separated=true` 和 `interface_sc=true` 计算结果。
`pack_input` 用于先处理 AlphaFold2/Boltz 这类非 Rosetta 输入的侧链构象。直接把
环肽当线性肽打分的 scorefile 不进入 v1.4.1 正式证据。

Prediction 管线版本参与 config/cache digest。编号算法或其他指标实现升级后，旧 run
不能以 resume 方式冒充新版本结果，需要建立新 run ID 重新摄取 artifact。

## 5. 状态语义

- `finalized`：七层数值全部通过，且每个阈值都有可用于最终清关的来源/校准证据。
- `awaiting_threshold_calibration`：七层数值通过，但至少一个阈值只有暂定值或缺少来源。
- `prediction_pending`：缺原始 artifact、指标或阈值值；保留恢复原因码。
- `needs_optimization`：证据齐全，至少一个数值门未通过。
- `invalid`：输入 hash/序列/链/PAE 维度错误，或 L4 硬几何门失败。

`all_layers_pass` 与 `metric_clearance` 表示数值层通过；实验候选最终选择必须使用 `competition_clearance` 或 `final_status=finalized`。

## 6. 4090 服务器运行

锁定的 ColabDesign 提交：

```text
094e2cb3603dee7d99846e0977736bd943c830c2
```

先确认项目配置中的每个 target 已有经过审核的 `coordinate_path` 和完整 SHA-256。然后生成 ColabDesign 原始结果：

```bash
cd /root/workspace/NovaPeptide/cycpep-mdm2-mdmx

/root/damodel-tmp/envs/cycpep-prediction/bin/python \
  scripts/run_prediction_predictors.py \
  --artifacts-root /root/damodel-tmp/novapeptide/prediction_artifacts \
  --seeds 0,1,2 \
  --model-numbers 0,1,2 \
  --prodigy prodigy \
  --resume
```

批量 runner 会检查 ColabDesign git HEAD、tracked source、cyclic-offset backend、
固定序列张量、输出 PDB 序列、PAE 和 metadata。它只登记真实输出。Rosetta、
post-relax 或独立 predictor 尚未登记时，后续运行会精确返回对应 pending 原因；
独立 predictor 可用下面的 enrichment 命令补充为 Boltz-2。

在不修改原 v1.3 artifact 的情况下增加 Boltz-2（以及可选 Rosetta）证据：

```bash
/root/damodel-tmp/envs/novapeptide-core/bin/python \
  scripts/enrich_prediction_evidence.py \
  --source-bundle /root/damodel-tmp/novapeptide/prediction_artifacts_v130_c0514_smoke/C0514/artifacts.json \
  --output-root /root/damodel-tmp/novapeptide/prediction_artifacts_v140_c0514_boltz \
  --boltz /root/damodel-tmp/envs/boltz-2.2.1/bin/boltz \
  --boltz-cache /root/damodel-tmp/novapeptide/boltz_cache \
  --boltz-checkpoint /root/damodel-tmp/novapeptide/boltz_cache/boltz2_conf.ckpt \
  --prodigy /root/damodel-tmp/envs/cycpep-prediction/bin/prodigy \
  --seed 101
```

服务器的非商业研究部署使用固定季度版 PyRosetta：

```text
/root/damodel-tmp/envs/pyrosetta-2026.29-minsizerel
PyRosetta 2026.29+releasequarterly.80a0635615
```

仅补 Rosetta 证据、复用已有 Boltz/AlphaFold2 结构时，可省略全部 `--boltz*`
参数并增加：

```text
--pyrosetta-python /root/damodel-tmp/envs/pyrosetta-2026.29-minsizerel/bin/python
```

如团队另有合法的 RosettaScripts 二进制，也可改用
`--rosetta-scripts /path/to/rosetta_scripts.default.linuxgccrelease`；两个引擎参数互斥。

Prediction v1.5.0 使用同一锁定 PyRosetta 环境补充 L4 post-relax。该步骤只读取
artifact 中的 primary 单体预测，要求它仅含一条与候选序列完全一致的链，并执行：

1. `PeptideCyclizeMover` 声明首尾酰胺键，同时添加 C—N 距离、键角和二面角约束；
2. ref2015 FastRelax，启用起始坐标约束，约束权重不递减，序列设计关闭；
3. 再次声明化学键以更新聚合物依赖原子，并删除坐标约束使用的临时虚拟锚点；
4. 独立验证输入/输出序列、链、C—N 距离、主链 RMSD、PDB 哈希和锁定工具版本；
5. 将无约束 ref2015 前后能量、约束协议参数和随机 seed 写入 metadata。

只补 post-relax、复用已有预测器和界面证据时：

```bash
/root/damodel-tmp/envs/cycpep-prediction/bin/python \
  scripts/enrich_prediction_evidence.py \
  --source-bundle /path/to/source/C0001/artifacts.json \
  --output-root /path/to/new_artifact_root \
  --post-relax-python \
    /root/damodel-tmp/envs/pyrosetta-2026.29-minsizerel/bin/python \
  --post-relax-seed 20260802 \
  --post-relax-repeats 3 \
  --post-relax-coordinate-stdev 0.5
```

相同输入和 seed 生成的 PDB 会移除 Rosetta footer 中与输出目录有关的绝对路径，
因此跨目录保持稳定哈希。任何序列/链漂移、闭环断裂、主链 RMSD 大于 2.0 Å、
metadata 与实际 PDB 不一致，均 fail-closed。

该 enrichment 命令只读 State/CandidateIndex，并把新证据写到新的 artifact root；
不会修改正式 State/CSV。跳过正负对照阈值标定时，可以完成模型和物理证据生产，
完整证据可以让候选进入 `needs_optimization`；暂定阈值仍禁止 `finalized`，不得据此
宣称候选已完成最终清关。

摄取并判定：

```bash
/root/damodel-tmp/envs/cycpep-prediction/bin/python \
  agents/prediction.py run \
  --artifacts-root /root/damodel-tmp/novapeptide/prediction_artifacts \
  --run-root /root/damodel-tmp/novapeptide/prediction_runs
```

断点续跑必须使用同一个 run ID：

```bash
/root/damodel-tmp/envs/cycpep-prediction/bin/python \
  agents/prediction.py run \
  --artifacts-root /root/damodel-tmp/novapeptide/prediction_artifacts \
  --run-root /root/damodel-tmp/novapeptide/prediction_runs \
  --run-id prediction_20260729T000000Z_deadbeef \
  --resume
```

相同候选输入、artifact digest、方法配置和 thresholds digest 会直接命中逐候选缓存。任一 artifact 改变后只重新计算受影响候选。

## 7. 本地验证

```bash
python -m py_compile agents/prediction.py prediction_pipeline/*.py
python -m unittest -v test_prediction_pipeline.py
```

单元测试覆盖：多 `MODEL`、pLDDT 两种量纲、真 C–N 距离、官方 ipSAE
定义、Boltz 双重闭环声明、Rosetta 环化后打分与逐模型聚合、受约束 post-relax、工具输出解析、缺值 pending、序列/哈希漂移 invalid、非法 ipTM
fail-closed、双靶完整清关、暂定阈值禁止 finalization、resume 缓存，以及
artifact 撤回或失效时对历史 Prediction 指标的原子清除。
