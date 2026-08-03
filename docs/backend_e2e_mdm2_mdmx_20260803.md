# Research → Planner 真实后端回归（2026-08-03）

## 1. 测试范围与隔离策略

本次在 RTX 4090 部署服务器上，以获批的 MDM2/MDMX 项目配置运行一次真实后端闭环：

`Research → Design Route C → Prediction → Critic → Planner`

测试使用独立 State、Evidence、Design 和 Prediction 根目录：

```text
/root/damodel-tmp/novapeptide/e2e_backend_v1_1_20260803_run01
```

正式 State 和正式 CandidateIndex 未被本次测试修改。所有失败样本和中间 artifact 均保留，
没有用占位值、模拟分数或手工补写结果完成流程。

## 2. 运行中发现并修复的问题

第一轮 Route C 生成了 19 aa 末端 Cys 二硫键候选，但当前项目合同声明
`head_to_tail_cyclic_peptide`，下游 Prediction 也只接受首尾酰胺环肽。该候选在真实
SG–SG 几何检查中被正确淘汰，同时暴露出两个跨层问题：

1. Route C 没有按项目 modality 筛选环化类型；
2. Route C 忽略了 CLI/获批配置中的 `lengths`。

Design v5.2.1 已修复：

- 首尾环肽项目只生成 `head-to-tail_amide`；
- 二硫键项目只生成末端 Cys-Cys 候选；
- 未支持的化学类型 fail-closed；
- Route C 最终序列必须落在获批长度集合；
- `refold_failed` 证据新增环化类型、闭环原子、距离与失败原因。

本地与服务器的 21 组 Design 测试均通过。服务器部署提交为 `ec91437`。

## 3. Research

真实检索结果：

- RCSB search/enrichment、界面分析、口袋聚合、结构叠合、PubMed 和阈值检索完成；
- MDM2、MDMX 各使用 10 个肽复合物结构构建动态口袋；
- PubMed 返回 30 篇论文；
- 9 类阈值中得到 2 个可自动采用的已核验文献覆盖，所有阈值键均存在；
- LLM binder extraction 子进程处理论文期间以退出码 1 中断，Research 按合同使用 8 条
  curated binder 回退，最终状态为 `degraded_with_fallbacks`。

LLM 子进程不影响后续 Design 输入的完整性，但原诊断只保留 stderr 前 240 字符，无法看到
末尾异常原因。本次补充了首尾保留、长度受限且 API key 脱敏的诊断逻辑；下一次复现时可
区分 HTTP、网络与脚本异常。

## 4. Design v5.2.1

修复后生成并登记候选 C0002：

| 字段 | 结果 |
|---|---|
| 序列 | `TSFAEYWNLLSP` |
| 长度 | 12 aa |
| 环化类型 | `head-to-tail_amide` |
| Design refold pLDDT | 0.658 |
| refold 首尾 C–N | 1.371 Å，理想区间内 |
| 独立 L7 reference | RFdiffusion target-bound backbone |
| reference SHA-256 | `2f394c453df89fdc7a4e4d0142a01622f80027842899b28162b5dbc778d9f743` |
| refold SHA-256 | `30546c4e67a1403cc120a0164de6658ce9e4913c721488a101d447bc9b6a3ae5` |

reference 与固定序列 refold 的路径及哈希不同，Route C 的 L7 reference 合同通过。

## 5. Prediction v1.5.1

真实生成的证据包括：

- 1 个 ColabDesign/AF2 单体；
- MDM2、MDMX 各 3 个不同 AF2 参数模型；
- MDM2、MDMX 各 1 个 Boltz-2 模型；
- 8 个复合物的 PRODIGY 和 PyRosetta InterfaceAnalyzer 结果；
- 3 repeats PyRosetta post-relax；
- 独立 RFdiffusion Design reference 的 scRMSD。

完整 artifact bundle SHA-256：

```text
1dd0fca7393aaa417dcd74ed91379bfe3c75da2fe79c7706774dedfe5584cf83
```

七层结果：

| 层 | 结果 | 主要数值 |
|---|---|---|
| L1 单体质量 | 未通过 | pLDDT 0.6579 |
| L2 界面置信度 | 未通过 | ipSAE：MDM2 0.3628，MDMX 0.4839 |
| L3 界面物理 | 未通过 | PRODIGY dG：-6.029/-5.823 kcal/mol；SC 和 dSASA 子项通过 |
| L4 环化几何 | 通过 | C–N 1.3707 → 1.3289 Å |
| L5 hotspot 覆盖 | 未通过 | MDM2 1.0，MDMX 0.6667 |
| L6 跨模型姿态收敛 | 通过 | RMSD：MDM2 0.6784 Å，MDMX 1.0881 Å |
| L7 Design 一致性 | 未通过 | scRMSD 2.7286 Å |

Prediction record 满足：

```text
missing_evidence=[]
missing_thresholds=[]
issues=[]
triage_status=needs_optimization
```

因此 `needs_optimization` 是真实数值失败，不是工具、artifact 或 Null 阈值故障。当前候选
不能被解释为药物命中，也不能因使用了已知 PMI 序列而跳过实验验证。

## 6. Critic v1.1 与 Planner v1.1

Critic：

```text
report_id: critic_0a56f7bcaad9
report_sha256: 9b7a0e8e0558263ef3f5dcfaaee7cebd1399f5060bf92ac671d2f6ba52fe249f
verdict: iterate
```

Critic 正确识别 L1/L2/L3/L5/L7 数值失败、阈值标定待办和单候选 cohort 过小；没有把 L4、
L6 或完整 predictor 证据误报为缺失。

Planner：

```text
plan_id: planner_cd58260b3720
plan_sha256: 50659e71cd9abe98f63cab9b0964effe70bc6fdc7a1e4bbdd1aae4c6a43102f8
status: awaiting_approval
```

生成 4 个任务：

1. T001：回到 Design，生成 12 条改进候选；
2. T002：只评估 T001 新候选并复用既有完整证据；
3. T003：Critic 审查新的 immutable Prediction handoff；
4. T004：单独提出阈值标定方案，禁止自动应用。

修复前错误的“重复 Prediction 修复 L7 reference”和“已有完整 AF2/Boltz 证据仍补缺失
predictor”任务均未出现。

## 7. 结论与前端接口建议

后端主控制流已完成一次真实 Research→Planner 回归，State、Evidence、artifact 哈希、
CandidateIndex、Prediction handoff、Critic report 和 Planner DAG 能够连续工作。可以开始
前端开发，首批界面应优先覆盖：

- 每阶段状态、降级原因和 fallback 的明确展示；
- 候选七层数值、缺失证据与真实数值失败的区分；
- artifact/report/plan 哈希与可追溯路径；
- Planner 任务依赖、预算和审批界面；
- 阈值“暂定/已标定”状态，避免把 `needs_optimization` 或工程通过误写成实验命中。

仍需保留的科学待办是正负对照阈值标定和多候选 cohort 回归。Research LLM extractor 的
退出码 1 需要在新诊断版本下再次复现后归因；该问题已有受控回退，不阻断当前 MDM2/MDMX
后端流程。
