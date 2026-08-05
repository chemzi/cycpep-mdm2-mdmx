# cycpep-mdm2-mdmx

MDM2/MDMX 双靶、首尾酰胺键环肽的 in silico Agent 设计项目。当前目标是在一个月赛期内形成可追溯的 Research → Design → Prediction → Critic/Planner 闭环，并交付通过约定计算指标的候选；项目不包含 wet-lab 验证。

本仓库同时支持以 MDM2/MDMX 为回归基准的可迁移靶点流程：最小 gene、UniProt
或 PDB 输入可自动补全为项目草稿，用户检查并批准后才能启动 Research/Design。

- [中文：可迁移流程、结构闸门与阈值校准](docs/transferable_pipeline.md)
- [English: transferable workflow, structure gate, and calibration](docs/transferable_pipeline.en.md)
- [前端 API contract、请求示例与状态机](docs/frontend_api_contract.md)
- [环肽反向折叠：ProteinMPNN 的适用范围与验证要求](docs/cyclic_inverse_folding.md)
- [Design v5.1.0：固定序列与闭环几何完整性门禁](docs/design_integrity_v5.1.0.md)

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

### 计划中的外部工具

根据 v5 方案，下面这些属于外部工具栈或独立部署组件，不建议直接当作普通 pip 依赖处理：

- RFpeptides / RFdiffusion
- LigandMPNN
- AfCycDesign / ColabFold
- HADDOCK
- Rosetta FastRelax / InterfaceAnalyzer
- PRODIGY
- RDKit

当前仓库状态下：

- `Research` 已有可运行实现
- `Design` 已有 RFdiffusion 宏环骨架、ProteinMPNN 反向折叠、
  AfCycDesign 固定序列回折、真实闭环原子几何门禁和候选登记逻辑
- `Prediction` 已有严格 artifact 摄取、七层指标计算、状态判定和断点续跑实现
- `Planner` / `Critic` 仍在待实现阶段

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

### 存储架构（PR3）

Agent 继续通过 `data_layer` 公共入口访问 State、CandidateIndex 和 Evidence，底层已建立统一 `storage` Store boundary。旧 JSON/CSV/JSONL 文件仍保留作为兼容 backend；设置 `CYCPEP_DB_PATH` 后可将运行时切换到 SQLite `project.db`。迁移工具 `storage.migrate_json_to_sqlite()` 幂等执行且不会删除源文件。

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
    ├── planner.py             ← 长时任务规划与迭代（待实现）
    ├── critic.py              ← 失败审查与回溯（待实现）
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
