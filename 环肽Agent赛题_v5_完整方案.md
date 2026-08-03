# MDM2/MDMX 双靶环肽 Agent 设计——完整方案 v5

> 版本：v5 完整版（2026-07-24）
> 本文档自成一体：读完它，不看任何其他文档，就能知道这个项目做什么、为什么做、用什么工具、每一步怎么跑、谁负责什么、什么算成功。

---

## 第一部分：我们要做什么

### 1.1 一句话

**输入一个 UniProt ID，AI Agent 系统自动完成从靶点调研、环肽生成、到七层指标验证的全流程，交付一批在 in silico 指标电池上全清的环肽候选分子，每个数字都有证据链。**

### 1.2 靶点与生物学

- **靶点**：MDM2（UniProt Q00987），加分项 MDMX（UniProt O15151）
- **机制**：p53 是最重要的抑癌蛋白，MDM2/MDMX 是抑制 p53 的两道锁。阻断它们→p53 复活→癌细胞凋亡
- **表位**：p53 螺旋上的 F19/W23/L26 三个疏水残基插入 MDM2/MDMX 的三个疏水口袋——这是全领域公认的结合模式
- **产物**：7-20 残基的头尾酰胺环肽，只含天然氨基酸

### 1.3 什么算成功（导师定义的"完成"）

不是"跑出一个漂亮的 ipTM"，而是：

> **一条环肽同时通过全部七层指标，且每一层都有可追溯的工具调用记录。**

```
七层指标电池（全部通过才算成功，缺一不可）：
┌─────────────────────────────────────────────────────────┐
│ L1 环肽质量      pLDDT > 0.8                             │
│ L2 界面置信度    ipSAE 达标（主指标，不用 ipTM 做门槛）    │
│ L3 界面物理      dG、SC（形状互补）、dSASA 达标            │
│ L4 环化几何      relax 前后环闭合 QC 均通过                │
│ L5 设计意图      热点覆盖 ≥2/3，结合位点 = 设计位点         │
│ L6 鲁棒性        多个预测器、多个 seed 收敛到同一结合模式    │
│ L7 可设计性      序列能 refold 回设计骨架（scRMSD < 2Å）   │
└─────────────────────────────────────────────────────────┘
```

### 1.4 创新点在哪里（不说"没人做过"，说"我们怎么做的"）

MDM2 是被研究烂了的靶点，靶点本身不加分。我们的差异化在**流程与算法**：

1. **Agent 自主完成 Deep Research**：从 UniProt ID 出发，自己检索 PDB/PubMed、自己分析口袋差异、自己发现数据陷阱（如 4HG7 是小分子复合物不能用于肽界面分析）
2. **正对照闭环自检**：Agent 必须能"重新发现"已知热点残基和已知 binder——这同时是能力基准测试和防幻觉测试
3. **七层指标电池 + 阈值过滤 + Pareto 前沿**：不做加权玄学的单一分数，用可审计的多目标决策

---

## 第二部分：整体流程（端到端全景）

```
输入: "Q00987"（一个 UniProt ID，仅此而已）
  │
  ▼
┌─ Phase 0: DEEP RESEARCH ──────────────────────────────┐
│ Research Agent                                         │
│ 检索 PDB/PubMed → 界面残基分析 → 口袋差异 → 已知binder  │
│ 产出: 靶点档案 + 热点清单 + 正对照集 + 数据质量警报      │
│ 自检: 重新发现 F19/W23/L26 = 能力验证通过               │
└──────────────────┬─────────────────────────────────────┘
                   ▼
┌─ Phase 1: POSITIVE CONTROL ───────────────────────────┐
│ Prediction Agent                                       │
│ 用已知 binder（PMI/ATSP-7041）跑通七层指标电池          │
│ 产出: 标定后的各层阈值                                  │
│ 门禁: 正对照不过 → 指标电池是坏的，先修，不许往下走      │
└──────────────────┬─────────────────────────────────────┘
                   ▼
┌─ Phase 2: DESIGN ─────────────────────────────────────┐
│ Design Agent                                           │
│ RFpeptides 生成环肽骨架 → LigandMPNN 设计序列           │
│ 三条路线策略并行，单 GPU 实际串行排队（详见第四部分）     │
│ 产出: ~1000 条 proposal，重模型只筛 top 子集             │
└──────────────────┬─────────────────────────────────────┘
                   ▼
┌─ Phase 3: SCORE（七层指标电池）───────────────────────┐
│ Prediction Agent                                       │
│ cheap screen → quick refold → confirm → L1→L7 逐级过滤  │
│ 产出: ~10-20 条全清候选                                │
└──────────────────┬─────────────────────────────────────┘
                   ▼
┌─ Phase 4: CRITIC + ITERATE ───────────────────────────┐
│ Critic Agent → Planner Agent                           │
│ 审查候选池（含可合成性）→ 发现问题 → 调整策略 → 再设计  │
│ 产出: 第 2-3 轮迭代后的收敛候选池                       │
└──────────────────┬─────────────────────────────────────┘
                   ▼
┌─ Phase 5: DUAL-TARGET EXTENSION（加分项）─────────────┐
│ 同一管线换 MDMX 结构重跑，零重工程                       │
│ 阈值过滤 + Pareto 前沿选双靶候选                        │
└──────────────────┬─────────────────────────────────────┘
                   ▼
┌─ Phase 6: REPORT ─────────────────────────────────────┐
│ Reporter Agent                                         │
│ 指标电池总表 + 每条候选证据链 + 决策轨迹                 │
└────────────────────────────────────────────────────────┘

全程: Evidence Logger 记录每一次工具调用（参数/版本/耗时/输出哈希）
      state.json 作为所有 Agent 共享的白板
```

**为什么是这个顺序**：Phase 0 的正对照自检（重新发现已知热点）和 Phase 1 的指标电池标定是整个项目的地基——如果 Agent 找不到已知热点、或已知 binder 在我们的电池上得分很差，后面生成的所有数字都是垃圾。先把尺子校准，再量东西。

---

## 第三部分：工具清单（用什么、怎么调用）

### 3.1 工具总表

| # | 工具 | 角色 | 用在哪个 Phase |
|---|------|------|---------------|
| 1 | RCSB Search API / GraphQL | 检索靶点复合物 PDB | P0 |
| 2 | biotite | 界面残基分析、结构叠合、热点覆盖计算 | P0/P3 |
| 3 | PubMed E-utilities | 文献检索 | P0 |
| 4 | LLM 抽取 | 从文献提取已知 binder 序列和热点 | P0 |
| 5 | **RFpeptides**（RFdiffusion + `inference.cyclic=True`） | **环肽骨架生成器（主力）** | P2 |
| 6 | **LigandMPNN** | **序列设计（每个骨架出 8 条序列）** | P2 |
| 7 | **AfCycDesign** | **refold 验证器（不是生成器）** | P1/P3 |
| 8 | ColabFold | 独立第二预测器（交叉验证） | P1/P3 |
| 9 | HADDOCK | 对接 + dG/界面物理 | P3 |
| 10 | Rosetta FastRelax | 结构弛豫 | P3 |
| 11 | PRODIGY | 结合自由能 dG 预测 | P3 |
| 12 | RDKit | 可合成性检查 + TPSA/cLogP | P4 |

### 3.2 关键工具调用方式

**RFpeptides——生成器（最核心）**

```bash
# 论文：Rettie et al., Nat Chem Biol 2025
# 原理：RFdiffusion 的环肽模式，在靶点存在下扩散生成大环骨架
python RFdiffusion/run_inference.py \
    inference.output_prefix=designs/mdm2_len12 \
    inference.input_pdb=targets/mdm2_p53.pdb \
    inference.cyclic=True \
    contigmap.contigs=['A25-109,0 12-12'] \
    inference.num_designs=100 \
    diffuser.T=50
# 输出：环肽骨架 PDB（无序列，只有 Cα 轨迹）
```

**LigandMPNN——序列设计**

```bash
python LigandMPNN/run.py \
    --pdb_path designs/mdm2_len12_0001.pdb \
    --num_seq_per_target 8 \
    --sampling_temp 0.1 \
    --seed 42
# 输出：每个骨架 8 条候选序列
```

**AfCycDesign——refold 验证器（注意角色转变）**

```bash
# 不用于生成！只用于验证"序列能否折叠回设计骨架"
python scripts/predict_cyclic_structure.py \
    --seq GFEWALAAKCFG \
    --soft_iters 50 --quiet
# 输出：pLDDT + 预测结构
# 判定：pLDDT > 0.8 且 scRMSD(预测结构, 设计骨架) < 2.0 Å
# （阈值来自 RFpeptides 论文的毕业标准）
```

**七层指标的计算来源**

| 层 | 指标 | 由谁计算 |
|----|------|---------|
| L1 | pLDDT | AfCycDesign refold |
| L2 | ipSAE | AfCycDesign + ColabFold 复合物预测的 PAE 矩阵（对界面残基切片计算） |
| L3 | dG / SC / dSASA | PRODIGY（dG）、Rosetta InterfaceAnalyzer（SC, dSASA） |
| L4 | 环化 QC | 自写脚本：检查 N/C 端肽键距离 < 2.0Å（relax 前后各跑一次） |
| L5 | 热点覆盖/位点一致 | biotite：界面残基 ∩ 设计热点 |
| L6 | 多预测器收敛 | AfCycDesign vs ColabFold 结合 pose 的 RMSD；≥3 个 seed |
| L7 | scRMSD | refold 结构 vs 设计骨架的骨架 RMSD |

---

## 第四部分：设计流程详解

### Phase 0: Deep Research（W1，刘函赫）

**输入**：`Q00987`（+ 加分项 `O15151`）
**五步流程**：

```
Step 1: RCSB Search API
  查所有 MDM2/MDMX-肽复合物（≤2.8Å）
  实际结果：MDM2 43 个 + MDMX 12 个肽复合物

Step 2: biotite 界面分析
  对每个复合物找重原子 <4Å 的界面残基 → 按频率聚合

Step 3: 结构叠合
  1YCR(MDM2) 与 3DAB(MDMX) 按 Cα 叠合（RMSD 1.88Å/85 Cα）
  → 三口袋逐残基对比

Step 4: PubMed 检索 "MDM2 MDMX dual peptide inhibitor" → 40 篇

Step 5: LLM 抽取 → 已知 binder 清单
  已产出 8 个：PMI, PMI-M3, ATSP-7041, ALRN-6924, pDI, pDI6W, pDIQ, M3-2K
```

**产出物**（已完成，存于 state.json / _research_cache.json）：
- 三口袋残基清单 + 差异描述 + 设计规则（"Trp23 不变锚点 / Phe19 ≤ Phe 体积 / Leu26 换小脂肪族"）
- 8 个已知 binder（含序列、Kd、PMID）→ 正对照集
- 数据质量警报：4HG7/3LBK 是小分子复合物，禁止用于肽界面分析

**自检门禁**：Agent 找到的热点必须包含 F19/W23/L26 三口袋——这就是导师说的"能力基准 + 防幻觉测试"。已通过。

### Phase 1: 正对照标定（W1，王修远 + 吴伶韵）

用 3 个已知 binder（PMI、ATSP-7041 核心段、文献报道的 MDM2 环肽）跑通完整七层电池：

1. 验证每个指标的计算代码没写错
2. **标定阈值**：正对照的指标分布 → 定出各层门槛（如正对照 ipSAE 最低 0.55，门槛定 0.5）
3. 记录正负对照分离度

**硬门禁**：任何一个正对照过不了电池 → 停下来修电池。这是 Week 1 最重要的交付物。

### Phase 2: Design（W2，于嘉乐）

**三条路线策略并行，GPU 执行串行：**

原方案中的“约 1000 条候选”按 proposal pool 理解，不代表 1000 条都要完成 AfCycDesign refold 和完整七层重验证。单 GPU 条件下，RFdiffusion / RFpeptides / AfCycDesign / ColabFold 任务进入同一个 GPU 队列，轮流小批量执行；CPU 侧的去重、manifest 检查、候选登记和日志整理可以并行。

| 路线 | 策略 | 数量 | 具体做法 |
|------|------|------|---------|
| A | RFpeptides 自由生成 | proposal ~500 | hotspot 约束到三口袋，长度 10/12/14 扫描；按 GPU 时间小批量追加 |
| B | motif 引导生成 | proposal ~300 | contigmap 中固定 FxxWxxxL motif 位置，扩散其余部分；L26 位点 LigandMPNN 采样时偏置小脂肪族（Leu/Val/Ala） |
| C | 已知 binder 环化改造 | proposal ~200 | ATSP-7041 核心序列保留 F/W/L 锚点，2-4 残基 Gly/Ser linker 替换 staple 位点；先作为便宜 proposal 进入漏斗 |

**每条候选入库**：`CandidateIndex.add_batch()`，记 `design_batch` 日志。

**GPU 队列建议**：

```text
Route A small batch
-> Route B small batch
-> Route C proposal registration
-> AfCycDesign quick_refold top N
-> confirm_refold top M
```

Critic 第二、三轮调整策略时，不重跑全量历史候选；只追加 50-100 条新 proposal，并复用已有分数和 evidence。

### Phase 3: Score——七层指标电池（W2-W3，王修远）

```
~1000 条 proposal pool
  │ cheap screen: 长度/合法氨基酸/去重/manifest/来源检查 → top 200 左右
  │ quick refold: AfCycDesign 低迭代、单 seed、限时粗筛    → top 50-100
  │ confirm refold: top 子集用更完整参数重跑              → top 10-30
  │ L1: pLDDT > 0.8                                      → 通过者进入后续层
  │ L2: 复合物预测 ipSAE > 标定阈值（主指标）              → ~100
  │     注：ipTM 只记录不卡门槛（小界面会虚高）
  │ L3: PRODIGY dG < 阈值, SC > 0.6, dSASA > 400Å² → ~50
  │ L4: FastRelax 前后环化 QC 双通过               → ~45（淘汰 relax 断环的）
  │ L5: 热点覆盖 ≥2/3 且 结合位点=设计位点          → ~30（淘汰结合错位置的）
  │ L6: AfCycDesign 与 ColabFold pose RMSD < 2Å
  │     且 ≥3 seed 收敛                             → ~20
  │ L7: scRMSD < 2.0Å                              → ~10-15
  ▼
全清候选（每条 7/7 通过，一张总表）
```

`quick_refold` 只用于粗筛，不包装成最终证据；报告正式候选时，需要明确对应 candidate 是否经过 `confirm_refold` 或完整 Prediction evidence。

### Phase 4: Critic + Iterate（W3，赵嘉策）

**Critic 检查项**（在 v4 基础上新增可合成性）：

| 检查 | 方法 | 触发动作 |
|------|------|---------|
| 可合成性 | RDKit：疏水长段/多余Cys/Met/Trp氧化/NG脱酰胺/DP断裂 | 淘汰 + 序列约束写入下轮 prompt |
| 多样性 | 序列唯一性 < 0.5 | 提高 LigandMPNN sampling_temp |
| 预测器分歧 | AfCycDesign vs ColabFold 分歧率 > 30% | 分歧样本加 seed 重跑 |
| 与已知 binder 撞车 | 序列相似度检查 | 撞车的标注重衍生，不计新颖性 |
| 阈值合理性 | 审计每个阈值的文献/正对照依据 | 无依据的阈值退回标定 |

**Planner 调整**：根据 Critic 的 issue 调整下一轮路线配比和约束（如 MDMX 弱→下轮 L26 位点强制 Ala/Val）。

### Phase 5: 双靶扩展（W3-W4，加分项）

- 同一管线，target PDB 换成 3DAB（MDMX），**零重工程**
- 双靶决策**不用加权分数**：先各自过阈值（MDMX 阈值按正对照单独标定，可略低），存活者取 **Pareto 前沿**（ipSAE_mdm2 × ipSAE_mdmx 两目标非支配集）

### Phase 6: Report（W4，我）

- 一张指标电池总表（候选 × 七层，全绿才出现）
- 每条 Top 候选：从 evidence_log.jsonl 抽完整证据链（哪次工具调用、什么参数、输出哈希）
- 决策轨迹：几轮迭代、Critic 每次发现什么、Planner 改了什么

---

## 第五部分：Agent 设计

### 5.1 架构总览

```
                 ┌──────────────┐
                 │   Planner    │  状态机：读 state.json → 派任务 → 按 Critic 反馈调策略
                 └──────┬───────┘
        ┌───────┬───────┼───────┬────────┐
        ▼       ▼       ▼       ▼        ▼
    Research  Design  Prediction Critic  Reporter
     调研      生成     评分      审查     汇报
        └───────┴───┬───┴───────┴────────┘
                    ▼
         ┌─────────────────────┐
         │ Evidence Logger      │  所有 Agent 的每次工具调用都写日志
         │ state.json / CSV     │  共享白板 + 候选索引
         └─────────────────────┘
```

### 5.2 每个 Agent：为什么存在、输入、输出、怎么做

**① Planner Agent（赵嘉策）**
- **为什么**：7 个阶段、3 条路线、2 个靶点，需要一个决策者决定"现在干什么、资源怎么分"
- **输入**：state.json（phase/round/候选统计）+ Critic 报告
- **输出**：任务列表（如 `{action: "rfpeptides", target: "MDM2", n: 200, lengths: [10,12,14]}`）
- **怎么做**：规则状态机，不是 LLM 自由发挥——每个 phase 的任务模板写死，只有路线配比和约束参数按 Critic 反馈调整（可审计）

**② Research Agent（刘函赫）**
- **为什么**：把"人查文献写调研报告"变成"Agent 自动产出结构化靶点档案"；同时承担防幻觉自检（重新发现已知热点）
- **输入**：UniProt ID
- **输出**：`research_targets` 事件（PDB 清单、口袋差异、已知 binder、文献、数据质量警报）
- **怎么做**：第五部分 Phase 0 五步流程，已实现，产出已验证

**③ Design Agent（于嘉乐）**
- **为什么**：生成环节是项目的技术核心，必须用最强方法（RFpeptides）而非最顺手的方法
- **输入**：Planner 任务 + state.json 的设计规则（口袋残基、L26 约束）
- **输出**：候选批次 → CandidateIndex
- **怎么做**：RFpeptides 出骨架 → LigandMPNN 每骨架 8 序列 → 记 `design_batch` 日志

**④ Prediction Agent（王修远）**
- **为什么**：七层指标电池是项目的"裁判"，裁判必须独立、可复现、阈值有出处
- **输入**：候选列表 + 标定阈值
- **输出**：每条候选的七层结果 → `candidate_scored` / `candidate_eliminated` 日志
- **怎么做**：每个指标一个函数，工具调用全部留 trace；绝不"一个模型既设计又裁判"（AfCycDesign 设计 → ColabFold 独立复核）

**⑤ Critic Agent（赵嘉策）**
- **为什么**：没有审查的 pipeline 只会把错误放大；可合成性、撞车、阈值合法性都必须有人挑刺
- **输入**：全候选池 + 指标电池结果
- **输出**：`critic_review` 事件（issues + 建议）
- **怎么做**：规则检查（可合成性/多样性/撞车）+ 阈值审计（每个阈值能否说出依据）

**⑥ Evidence Logger（我）——已上线**
- **为什么**：导师要的"可追溯"不是口号——每个数字必须能倒推到某次工具调用
- **形式**：evidence_log.jsonl（每次调用：工具/版本/参数/耗时/exit_code/输出哈希）+ state.json + candidate_index.csv
- **现状**：data_layer.py 已写完，96 项测试通过，Research 阶段 72 条日志已验证

**⑦ Reporter Agent（我）**
- **为什么**：答辩时导师会抽查"这个数字哪来的"，必须一键生成证据链
- **输入**：state.json + evidence_log.jsonl
- **输出**：指标电池总表 + 候选证据卡 + 决策轨迹

### 5.3 防幻觉三原则（贯穿所有 Agent）

1. **设计与裁判分离**：AfCycDesign 参与设计链，ColabFold 必须独立复核
2. **每个结论挂 trace**：没有 tool_trace 的数字不许进 state
3. **正对照锚定**：任何指标必须先证明"已知 binder 能过"，才配用来筛未知分子

---

## 第六部分：四周执行计划

| 周 | 里程碑 | 交付物 | 门禁 |
|----|--------|--------|------|
| W1 | 调研完成 + 流程跑通 | 靶点档案（✅已有）、RFpeptides 环境冒烟、Prediction 能接候选并写回、至少一批 quick screen evidence | 能展示 Research → Design proposal → Prediction 判定 → Evidence 追溯 |
| W2 | 首轮设计 + 粗筛 | ~1000 proposal 入库，cheap screen + quick refold top 子集 | AfCycDesign 不全量跑；按 GPU 时间预算截断并记录 |
| W3 | 精筛 + 迭代 + 双靶扩展 | L4-L7 筛完 ≥10 条全清；Critic ≥1 次有效反馈；MDMX 管线复用 | 全清候选 ≥5；MDMX 不重写代码 |
| W4 | 收敛 + 交付 | 指标总表、证据链、Pareto 前沿图、汇报 PPT | 现场抽查任意数字可溯源 |

**今晚就做**（修正版）：
- 于嘉乐：部署 RFdiffusion + RFpeptides，跑通 `inference.cyclic=True` 冒烟测试（不再是 AfCycDesign 安装）
- 王修远：写 ipSAE 计算函数 + 环化 QC 脚本，用 PMI 做第一个正对照
- 赵嘉策：orchestrator 状态机骨架（phase 流转 + 任务模板）
- 刘函赫：Research 产出整理成证据包（✅基本完成）
- 吴伶韵：PyMOL 双靶口袋对比图（已有残基清单，直接画）
- 我：数据层加七层指标字段 + 可合成性检查函数

---

## 第七部分：风险与兜底

| 风险 | 概率 | 兜底 |
|------|------|------|
| RFpeptides 部署失败/无 GPU | 中 | AutoDL A100；仍失败则降级 AfCycDesign hallucination 生成，但报告明示这是 weaker strategy 并加强 L6/L7 验证 |
| AfCycDesign refold 太慢 | 高 | 改为 quick/confirm 两档：全量只做便宜筛，top 50-200 做 quick refold，top 10-30 做 confirm |
| 单 GPU 并行跑 RFdiffusion OOM | 高 | Planner / Orchestrator 维护 GPU 串行队列；三条 Route 只在策略层并行 |
| ipSAE 计算没现成实现 | 低 | 按 Dunbrack 定义从 PAE 矩阵自写（~50 行），正对照验证 |
| 正对照过不了电池 | 低 | 说明电池实现错了——这正是 Phase 1 存在的意义，修电池 |
| 全清候选为 0 | 中 | 放宽策略不是降阈值，而是加迭代轮数 + 调整路线配比；实在为 0 则报告各层漏斗数据（深度 > 勉强凑数） |
| MDMX 全线偏低 | 中 | 导师已认可"单靶做深"是完整答案，MDMX 降级为加分项 |

---

*最后更新：2026-07-24*
