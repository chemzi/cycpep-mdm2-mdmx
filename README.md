# cycpep-mdm2-mdmx

MDM2/MDMX 双靶、首尾酰胺键环肽的 in silico Agent 设计项目。当前目标是在一个月赛期内形成可追溯的 Research → Design → Prediction → Critic/Planner 闭环，并交付通过约定计算指标的候选；项目不包含 wet-lab 验证。

## 快速开始

```bash
git clone https://github.com/chemzi/cycpep-mdm2-mdmx.git
cd cycpep-mdm2-mdmx
```

共享数据入口：

```python
from data_layer import (
    State, EvidenceLogger, CandidateIndex,
    evaluate_battery, compute_pareto_front,
)
```

详细用法见 [数据层使用手册](./数据层使用手册.md)。

## 目录

```
cycpep-mdm2-mdmx/
├── data_layer.py              ← State、Evidence、候选索引、七层判定
├── test_data_layer.py         ← 隔离的数据层集成测试
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
    ├── prediction.py          ← 王修远：七层计算评估（待实现）
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
./.venv/bin/python -m unittest -v test_reliability_regressions.py
```

强制从网络重跑 Research、绕过旧缓存：

```bash
python -c "from agents.research import recompute; recompute()"
```

Research 的 `run_status` 和每个 `stage_status` 必须随结果一起检查。缺少 LLM API key 时，结构与 PubMed 部分仍可成功，LLM 提取会明确标为 degraded，并使用带来源标记的 fallback。
