# cycpep-mdm2-mdmx

MDM2/MDMX 双靶环肽 Agent 从头设计。

## 快速开始

```bash
git clone https://github.com/chemzi/cycpep-mdm2-mdmx.git
cd cycpep-mdm2-mdmx
```

所有 Agent 统一入口：

```python
from data_layer import State, EvidenceLogger, CandidateIndex
```

详细用法见 [数据层使用手册](./数据层使用手册.md)。

## 目录

```
├── data_layer.py              ← 共享数据模块（只有我改）
├── test_data_layer.py         ← 测试
├── 数据层使用手册.md           ← 必读
├── evidence/
│   └── evidence_schema.json   ← 11种事件格式定义
├── agents/                    ← 每人改自己的文件
│   ├── planner.py             ← 赵嘉策
│   ├── design.py              ← 于嘉乐
│   ├── prediction.py          ← 王修远
│   ├── research.py            ← 刘函赫
│   └── critic.py              ← 赵嘉策
├── .gitignore
└── README.md
```

## 协作约定

- `data_layer.py` 只有我改。需要加接口在群里说。
- 各人只改 `agents/` 下自己的文件。
- `data/`、`evidence/evidence_log.jsonl` 是运行时产出，不进 Git。
- 跑任务前在服务器上 `git pull`。
