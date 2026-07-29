# 环肽反向折叠：ProteinMPNN 的适用范围与验证要求

## 结论

ProteinMPNN 可以作为本项目的第一版反向折叠器：输入 RFdiffusion/RFpeptides
产生的 MDM2 或 MDMX–环肽复合物骨架，固定受体链，仅为环肽链采样标准
L-α-氨基酸序列。RFdiffusion 官方宏环 binder 流程和 RFpeptides 论文均采用了
这类“骨架生成 → ProteinMPNN 序列设计 → 结构回折/能量验证”的组合。

当前代码通过 LigandMPNN 仓库的统一 runner 调用
`--model_type=protein_mpnn`。这里使用的是 ProteinMPNN 权重；LigandMPNN
仓库只是执行入口。MDM2/MDMX 是纯蛋白受体，当前任务不需要
`ligand_mpnn` 的小分子、金属或核酸原子上下文。

## 生产数据流

```text
审核过的 MDM2/MDMX PDB
        +
RFdiffusion cyclic binder backbone
        │  binder 必须是 contig 第一段，与 cyc_chains=a 一致
        ▼
从实际输出 PDB 识别唯一的 8–20 aa binder chain
        ▼
ProteinMPNN：只设计 binder chain，所有 receptor chains 固定
        ▼
按 LigandMPNN 的排序规则严格解析 FASTA，并复核固定 receptor 序列未改变
        ▼
便宜序列过滤与去重
        ▼
AfCycDesign 固定序列、cyclic offset 回折
        ▼
Prediction 七层 evidence battery
```

链身份不能从输入靶蛋白的链名推断。RFdiffusion 会重新标记输出链；生产代码按
期望环肽长度从实际 PDB 中识别唯一 binder chain。无法唯一识别、FASTA 链段数
不匹配、binder 长度漂移、存在非标准氨基酸，或 ProteinMPNN 输出改变受体序列
时，该骨架整批 fail closed，不进入候选索引。

## 模型能力边界

原始 ProteinMPNN 从三维主链邻域生成序列，能利用复合物界面几何，也支持固定
链和固定残基。它没有显式表示首尾酰胺共价键，训练数据中短环肽覆盖也有限。
因此 MPNN score 只表示模型对“序列适配所给骨架”的相对偏好，不能证明：

- 头尾已经形成可行的酰胺键；
- 游离环肽会稳定折叠到设计构象；
- MDM2/MDMX 结合姿态会在不同 seed 或 predictor 中收敛；
- 候选具有足够亲和力、可合成性、膜通透性或体内稳定性。

这些问题由固定序列环化感知回折、C–N 几何门、scRMSD、ipSAE/界面接触、
多 seed/多 predictor 收敛以及后续物理打分分别处理。

当前支持范围是由 20 种标准 L-α-氨基酸组成的 8–20 aa 头尾环肽。二硫键、
订书钉、D-氨基酸、N-甲基化或其他非天然残基需要独立的表示、参数和验证
路线，不能直接复用这条 ProteinMPNN 生产路径。

## CyclicMPNN 的位置

CyclicMPNN 是 2026 年发布的 ProteinMPNN 环肽专项微调版本。其预印本在
6、8、10 和 14 aa 环肽骨架上报告了比原始 ProteinMPNN 更好的结构重建指标，
也支持 motif 保留设计。它值得作为第二阶段对照，但目前不直接替换生产基线：

- 论文仍为预印本；
- 需要单独锁定代码、权重、许可证和运行环境；
- 需要验证它在“含完整 MDM2/MDMX 受体的多链条件设计”中的链固定语义；
- 应在同一批 backbone、相同采样数和相同下游 Prediction 门下做盲比较。

建议基准实验为每个靶标抽取至少 100 个 RFdiffusion backbone，每个 backbone
分别用 ProteinMPNN 与 CyclicMPNN 生成相同数量序列；比较有效输出率、固定链
完整性、环化回折 scRMSD、pLDDT、ipSAE、热点覆盖和多 seed 收敛。没有完成
这组配对基准前，CyclicMPNN 只作为研究候选。

## 实现与审计要求

- 生产模型固定为 `LIGANDMPNN_MODEL_TYPE=protein_mpnn`；
- checkpoint 路径由 `LIGANDMPNN_CHECKPOINT` 指定并在部署审计中记录哈希；
- 每个 backbone 默认采样 8 条序列，温度 0.1；
- motif route 只固定经过审核的 binder residue，不允许把靶标链误传为设计链；
- 原生/reference FASTA record 不进入候选；
- 所有下游候选必须保留 backbone、refold PDB、manifest 和文件哈希。

## 主要依据

- Dauparas et al., *Science* 2022, ProteinMPNN。
- RFdiffusion 官方 macrocyclic binder 示例与 sequence-design 说明。
- Rettie et al., RFpeptides 宏环结合肽设计工作。
- Powers et al., CyclicMPNN, bioRxiv 2026（预印本）。
