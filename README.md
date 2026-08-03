# cycpep-mdm2-mdmx

MDM2/MDMX 双靶、首尾酰胺键环肽的 in silico Agent 设计项目。当前目标是在一个月赛期内形成可追溯的 Research → Design → Prediction → Critic/Planner 闭环，并交付通过约定计算指标的候选；项目不包含 wet-lab 验证。

本仓库同时支持以 MDM2/MDMX 为回归基准的可迁移靶点流程：最小 gene、UniProt
或 PDB 输入可自动补全为项目草稿，用户检查并批准后才能启动 Research/Design。

- [中文：可迁移流程、结构闸门与阈值校准](docs/transferable_pipeline.md)
- [English: transferable workflow, structure gate, and calibration](docs/transferable_pipeline.en.md)
- [前端 API contract、请求示例与状态机](docs/frontend_api_contract.md)
- [环肽反向折叠：ProteinMPNN 的适用范围与验证要求](docs/cyclic_inverse_folding.md)
- [Design v5.2.0：Route C 独立 L7 reference 合同](docs/design_integrity_v5.2.0.md)
- [Prediction v1.4.1：Boltz-2 / PyRosetta 真实双靶标验证](docs/prediction_v1.4.1_pyrosetta_validation_20260802.md)
- [Prediction v1.5.0：环肽 post-relax 与完整七层回归](docs/prediction_v1.5.0_post_relax_validation_20260802.md)
- [Critic v1.1：Prediction 审查合同与 Planner handoff](docs/critic_agent.md)
- [Critic v1.0：C0514 真实审查验证](docs/critic_v1.0_validation_20260802.md)
- [Planner v1.1：任务图、预算请求与摘要绑定审批](docs/planner_agent.md)
- [Agent loop v1.1：C1250/L6 服务器回归记录](docs/agent_loop_v1.1_server_validation_20260803.md)
- [Planner v1.0：C0514 真实规划验证](docs/planner_v1.0_validation_20260802.md)
- [Orchestrator v1.0：审批执行、任务状态、GPU 租约与恢复](docs/orchestrator_agent.md)
- [Orchestrator v1.0：C0514 真实计划无执行验证](docs/orchestrator_v1.0_validation_20260802.md)

```bash
python -m target_bootstrap draft --identifier P12345 --type uniprot --output projects/new_target.draft.json
python -m target_bootstrap show --draft projects/new_target.draft.json
python -m target_bootstrap approve --draft projects/new_target.draft.json --output projects/new_target.json
```

The bootstrapper resolves and enriches minimal target input with the configured
LLM. Explicit, digest-bound approval is required before downstream execution;
editing approved content invalidates that approval.

## 快速开始

```bash
git clone https://github.com/chemzi/cycpep-mdm2-mdmx.git
cd cycpep-mdm2-mdmx
pip install -r requirements.txt
```

## 环境安装

当前仓库中已提交的 Python 代码，基础依赖通过下面这条即可安装：

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

这会安装当前 `Research` 脚本和回归测试实际会用到的基础包：

- `numpy`
- `biotite`

如果不安装 `biotite`，`Research` 的结构分析脚本和 `test_reliability_regressions.py` 会在导入或运行时失败。

### 可选 / 路线相关依赖

下面这些依赖与特定设计路线或后续规划有关，不包含在当前的最小 `requirements.txt` 中：

- `torch`：Route B 接入 ProteinMPNN adapter 时需要
- `colabdesign`：Route A 的 ColabDesign 原型需要
- `proteinmpnn`：Route B 当前代码期望的适配模块
- `boltz[cuda]==2.2.1`：Prediction 的独立第二模型家族；在 GPU 服务器上使用隔离环境部署

### 计划中的外部工具

根据 v5 方案，下面这些属于外部工具栈或独立部署组件，不建议直接当作普通 pip 依赖处理：

- RFpeptides / RFdiffusion
- LigandMPNN
- AfCycDesign / ColabFold
- HADDOCK
- Rosetta FastRelax；PyRosetta InterfaceAnalyzer 已按非商业研究许可独立部署
- PRODIGY
- RDKit

当前仓库状态下：

- `Research` 已有可运行实现
- `Design` 已有 RFdiffusion 宏环骨架、ProteinMPNN 反向折叠、
  AfCycDesign 固定序列回折、真实闭环原子几何门禁和候选登记逻辑
- `Prediction` 已有严格 artifact 摄取、七层指标计算、状态判定和断点续跑实现；
  Boltz-2 独立复合物 predictor 与 PyRosetta InterfaceAnalyzer 已在 4090
  服务器完成 C0514 双靶标真实回归
- `Critic` v1.1 已实现冻结 Prediction handoff/record 摄取、哈希校验、问题分类、
  候选池统计和结构化 Planner handoff，并能把 L7 reference 缺失准确归因给 Design
- `Planner` v1.1 已实现 Critic 报告摄取、确定性任务图、预算/审批闸门、启动阶段判断，
  并将完整证据下的 L6 姿态不收敛映射为 Design 迭代
  和摘要绑定 approval artifact
- `Orchestrator` v1.0 已实现 plan/approval 验证、任务依赖、Worker dispatch packet、
  单 GPU 租约、输出哈希、失败/中断恢复和成功后的 State round 推进
- 当前 Orchestrator 采用受控 Worker 模式，不直接启动模型进程；Design/Prediction
  Worker 按 dispatch packet 执行后登记结果

所以如果只是跑数据层、Research 和不调用模型的回归测试，先装
`requirements.txt` 即可；完整 Design/Prediction 需要按部署文档准备模型、
checkpoint 和外部工具。

## 当前运行策略

根据 PR #4 后的代码状态和单 GPU 实测，v5 里的“约 1000 条候选”现在按 **proposal pool** 理解，不再表示 1000 条都要跑完整 AfCycDesign refold 和七层重模型验证。

实际采用多层漏斗：

```text
Route A/B/C 生成 proposal pool
-> 序列合法性、长度、去重、来源和 manifest 检查
-> AfCycDesign quick refold 粗筛 top 50-200
-> confirm refold / 多 seed / 复合物预测验证 top 10-30
-> 七层指标电池 + Pareto front + Critic 审查
```

AfCycDesign 分两档使用：

- `quick refold`：低迭代、单 seed、限时运行，只作为粗筛 evidence。
- `confirm refold`：只对 top candidates 使用更完整参数，作为正式 Prediction evidence。

单 GPU 运行时，RFdiffusion / RFpeptides / AfCycDesign / ColabFold 等 GPU 任务必须进入队列串行执行。三条 Route 可以在策略上并行探索，但实际 GPU 进程不要同时启动多个 RFdiffusion/RFpeptides 任务，否则容易 OOM。CPU 侧的 manifest、去重、日志整理和便宜规则检查可以并行。

每轮 Critic 调整策略后，不重跑全量历史候选；只追加一小批新 proposal，并复用已有的 manifest、分数和 evidence。Week 1 的目标是证明端到端流程跑通，而不是完成 1000 条全量重验证。

共享数据入口：

```python
from data_layer import (
    State, EvidenceLogger, CandidateIndex,
    evaluate_battery, compute_pareto_front,
)
```

详细用法见 [数据层使用手册](./数据层使用手册.md)。

协作上手资料：

- [赵嘉策上手指南：Planner / Critic / Orchestrator](./docs/赵嘉策上手指南.md)

## 目录

```
cycpep-mdm2-mdmx/
├── data_layer.py              ← State、Evidence、候选索引、七层判定
├── test_data_layer.py         ← 隔离的数据层集成测试
├── test_prediction_pipeline.py ← Prediction 契约、指标与端到端回归测试
├── test_reliability_regressions.py ← Research/Design 回归测试
├── 数据层使用手册.md           ← 必读
├── v5可靠性修复说明_人类可读版.md
├── .gitignore
├── README.md
├── evidence/
│   ├── evidence_schema.json   ← v5 事件、候选和评分格式
│   └── .gitkeep
├── data/
│   └── .gitkeep               ← 运行时产出目录，不进Git
└── agents/                    ← 每人改自己的文件
    ├── planner.py             ← Critic 驱动任务图、预算与审批规划
    ├── orchestrator.py        ← 审批执行、任务 DAG、GPU 租约与运行恢复
    ├── critic.py              ← Prediction 失败审查、候选池诊断与 Planner handoff
    ├── design.py              ← 于嘉乐：三条设计路线
    ├── prediction.py          ← 七层生产编排入口（无 placeholder/demo）
    └── research.py            ← RCSB/PubMed/阈值证据调研
```

## 协作约定

- 共享 schema 变更需要张义忱、Design 和 Prediction 三方确认。
- 各人只改 `agents/` 下自己的文件。
- `data/`、`evidence/evidence_log.jsonl` 是运行时产出，不进 Git。
- 跑任务前在服务器上 `git pull`。

## 验证

```bash
python3 test_data_layer.py
python3 test_design.py
python3 -m unittest -v test_prediction_pipeline.py
python3 -m unittest -v test_critic.py
python3 -m unittest -v test_planner.py
python3 -m unittest -v test_orchestrator.py
./.venv/bin/python -m unittest -v test_reliability_regressions.py
```

部署环境中的固定序列 GPU 回归测试需要显式启用，普通 CI 会安全跳过：

```bash
CYCPEP_RUN_GPU_TESTS=1 \
/root/damodel-tmp/envs/cycpep-prediction/bin/python \
-m unittest -v test_design_gpu.py
```

Prediction 的 Design 交接契约、artifact schema、七层计算定义、状态语义与
4090 运行命令见 [Prediction 生产管线](./docs/prediction_pipeline.md)。

强制从网络重跑 Research、绕过旧缓存：

```bash
python -c "from agents.research import recompute; recompute()"
```

Research 的 `run_status` 和每个 `stage_status` 必须随结果一起检查。缺少 LLM API key 时，结构与 PubMed 部分仍可成功，LLM 提取会明确标为 degraded，并使用带来源标记的 fallback。
