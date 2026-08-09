# cycpep-mdm2-mdmx

MDM2/MDMX 双靶、首尾酰胺键环肽的 in silico 设计与评估项目。系统把 Research、Design、Prediction、Critic、Planner、Orchestrator 和受控 Execution Worker 串成可审计的工作流；项目不包含 wet-lab 验证。

## 1. Overview

当前完整概念链为：

```text
Research → Design → Prediction → Critic → Planner
                                         ↓
                                      Orchestrator
                                         ↓
                                  Execution Worker
                                         ↓
                                   Action Handler
                                         ↓
                              Design / Prediction / Recovery
```

每轮运行的正式结果进入三个不同职责的数据面：`State`、`CandidateIndex` 和 Evidence Ledger。当前仓库建立了追踪基础设施，但不宣称已经具备完整生产级分布式 tracing、全并发安全或完全可复现环境。

## 2. Current System Architecture

```text
                    Project Config
                         │
                         ▼
                      Research
                         │
                         ▼
                       Design
                         │
                         ▼
                     Prediction
                         │
                         ▼
                       Critic
                         │
                         ▼
                      Planner
                         │  deterministic execution plan
                         ▼
                    Orchestrator
                         │  validated dispatch packet
                         ▼
                  Execution Worker
                         │
                    Action Registry
                         │
                       Handler
                   ┌─────┴─────┐
                   ▼           ▼
                Design     Prediction
                   └─────┬─────┘
                         ▼
                  Evidence Ledger
```

`contracts/` 是 Planner、Orchestrator、Execution 和 Evidence 之间共享协议的来源；`execution/` 负责受控调度、动作解析、handler 查找和结果回写。Target bootstrap 先把最小 target 输入补全为项目草稿，再以摘要绑定的 approval 作为下游入口。

## 3. Core Architectural Invariants

1. Planner 只生成 deterministic execution plan，不执行任务。
2. Orchestrator 不直接运行科学模型；它负责 plan/approval validation、依赖状态、task claim、GPU lease、dispatch，以及完成/失败/恢复状态。
3. Execution Worker 不执行任意 shell command，只执行 `execution/action_registry.py` 显式登记且存在 handler 的 action。没有 handler 的任务不得标为 `ready`。
4. Agent 间公共 Action、Task Status、Trace Contract 必须来自统一 `contracts/`，禁止复制第二套公共协议。
5. 正式 artifact 可以是 `execution_plan.json`、`approval.json`、`run.json`、`dispatch_snapshot.json` 或 `execution_receipt.json`，但必须通过 Evidence 保存 ID、path、SHA256、producer 和 lineage/trace context。
6. 禁止 shadow state / shadow log。影响正式 workflow 的状态不能只保存在私有临时 JSON 或 agent 私有日志中。
7. `workflow_id` 必须从 Planner 向下传播至 Orchestrator 和 Execution；下游不得重新生成一条独立 workflow。
8. 正式状态应通过统一 Store/数据层访问；执行失败不能留下已部分写入的正式 CandidateIndex 或 State。
9. Prediction evidence 必须携带产生它的协议 identity 对象 `{name, version, sha256}`（见 `protocols/prediction_v1.json`）；Action Contract 的 `predictor_protocol` 也必须是这个 identity 对象而非裸字符串，任务据此钉死它要执行的具体协议参数。bundle 可以合法地“未绑定”（legacy），但系统绝不允许猜测或自动补标协议；任何 reuse/resume/enrichment 都必须证明计算参数与声明的协议 SHA 完全一致。**协议 SHA 只覆盖 `parameters`（科学语义）**：`metadata`（description/author/comment）的改动不改变 SHA-256，不会使存量 evidence 失效；只有科学参数变化才要求协议版本 bump，且此时存量 bundle 在 Execution 完整性判定中视为“绑定不同协议”，不得复用（协议升级 = Prediction evidence 全量重跑）。

## 4. Quick Start

```bash
git clone https://github.com/chemzi/cycpep-mdm2-mdmx.git
cd cycpep-mdm2-mdmx
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements.txt
```

创建并批准一个 target 配置：

```bash
python -m target_bootstrap draft --identifier P12345 --type uniprot --output projects/new_target.draft.json
python -m target_bootstrap show --draft projects/new_target.draft.json
python -m target_bootstrap approve --draft projects/new_target.draft.json --output projects/new_target.json
```

批准内容经过编辑会使 approval 失效。`requirements.txt` 只覆盖基础 Python 依赖；它不能单独提供完整 Design/Prediction 所需的模型、checkpoint、GPU 驱动和外部科学工具。

## 5. Target Bootstrap / Project Configuration

`target_bootstrap.py` 接收 gene、UniProt 或 PDB 等最小输入，解析并补全项目草稿；用户检查后生成摘要绑定的 approved project configuration。项目配置、科学参数和外部工具路径应通过显式配置传入，避免继续扩大 import-time 全局状态。

## 6. Agent Workflow

- **Research**：收集靶点、结构、文献和阈值证据，并把工具调用与结果写入 Evidence。
- **Design**：按批准配置生成和筛选环肽候选，产出 Design artifacts。
- **Prediction**：摄取经过契约校验的输入，运行结构预测/评估并产出类型化 artifacts。
- **Critic**：审查 Prediction 结果、分类问题并生成结构化 Planner handoff；recommendation 不是 execution action。
- **Planner**：把 Critic 建议转换为带依赖、预算和审批闸门的确定性 execution plan。
- **Orchestrator**：验证计划和审批、管理依赖与资源、创建 dispatch packet，并协调 Worker 的完成、失败和恢复。
- **Execution Worker**：领取 ready task，按 Action Registry 找到唯一 handler，执行并返回 typed outputs。

## 7. Contracts and Execution Model

`contracts/` 定义 Agent 边界上的稳定协议，包括当前代码中的 `ActionSpec`、`ActionType`、`TaskStatus`、`TraceContext`、`ArtifactRef`、`ErrorInfo` 和 `EvidenceEvent`。业务逻辑 != Contract；`Critic recommendation` != `Execution action`。

`execution/action_registry.py` 是 Execution capability 的唯一权威来源：

```text
Planner executable task → Action Registry → Handler
```

Worker 的实际流程是：

```text
claim
→ validate dispatch packet
→ resolve semantic action
→ lookup handler
→ execute
→ collect typed outputs
→ complete / fail via Orchestrator
→ record Evidence
```

因此 `Orchestrator != Worker`：前者维护流程状态和调度边界，后者只执行已批准且已注册的动作。

## 8. Data Layer and Traceability

### 存储架构（PR3）

Agent 继续通过 `data_layer` 公共入口访问 State、CandidateIndex 和 Evidence，底层已建立统一 `storage` Store boundary。旧 JSON/CSV/JSONL 文件仍保留作为兼容 backend；设置 `CYCPEP_DB_PATH` 后可将运行时切换到 SQLite `project.db`。迁移工具 `storage.migrate_json_to_sqlite()` 幂等执行且不会删除源文件。

三类数据职责不同：

```text
State          = current project/runtime projection
CandidateIndex = materialized current view of candidates
Evidence Ledger = append-only audit/provenance history
```

Evidence Ledger 用于 audit、debug、scientific provenance 和 workflow reconstruction。事件/追踪合同支持 `workflow_id`、`plan_id`、`run_id`、`task_id`、`attempt_id`、`candidate_id` 及 artifact reference；正式 artifact 还应带 producer、path 和 SHA256。详细读写边界见[数据层使用手册](./数据层使用手册.md)。

一个目标级追溯关系可以表示为：

```text
Candidate C0042
    ↑
Prediction artifact
    ↑
Execution task T007 / attempt 2
    ↑
Orchestrator run
    ↑
Planner plan
    ↑
Critic report
```

这表示 trace foundation：目标是能从最终候选反查计算链路和执行来源，而不是宣称已经完成生产级分布式追踪。

## 9. Repository Layout

```text
cycpep-mdm2-mdmx/
├── agents/                  # research, design, prediction, critic, planner, orchestrator
├── contracts/               # shared action/task/trace/artifact/error/event contracts
├── execution/               # worker, supervisor, registry, handlers and execution contracts
├── prediction_pipeline/     # prediction adapters, workers, metrics and artifacts
├── data_layer.py            # State, CandidateIndex and Evidence access
├── evidence/                # evidence schema and append-only runtime log
├── data/                    # runtime data such as state and candidate index
├── projects/                # project configurations and target bootstrap outputs
├── docs/                    # architecture, agent and validation documentation
├── test_*.py                # fast and component/regression tests
├── requirements.txt         # base Python dependencies
└── ENGINEERING_STANDARD.md  # mandatory engineering and architecture rules
```

## 10. Environment and External Tooling

- **Base Python dependencies**：`requirements.txt`，用于数据层、Research 基础能力和不依赖模型的回归测试。
- **External scientific tooling**：RFdiffusion/RFpeptides、ProteinMPNN、AfCycDesign、Boltz、ColabFold、HADDOCK、Rosetta/PyRosetta、PRODIGY、RDKit 等按各自部署文档准备。
- **GPU-specific environments**：Design/Prediction 的 GPU 路线需要匹配的 CUDA、模型权重、checkpoint 和隔离环境；请以对应 agent/validation 文档为准。

完整流程的可运行性取决于实际工具和配置，不应将基础依赖安装等同于完整 GPU bootstrap。

## 11. Validation / Tests

### Fast / CPU tests

```bash
python -m unittest -v test_data_layer.py
python -m unittest -v test_reliability_regressions.py
python -m unittest -v test_threshold_research.py
```

### Component tests

```bash
python -m unittest -v test_planner.py test_orchestrator.py test_execution.py
python -m unittest -v test_critic.py test_prediction_pipeline.py test_target_bootstrap.py
```

### Architecture gate

```bash
python scripts/architecture_gate.py --baseline architecture_baseline.json
```

Enforced in CI (`.github/workflows/architecture.yml`): python files > 1000 lines,
functions > 150 lines, executable planner actions without a real Execution
handler, absolute cross-package imports of private (`_name`) symbols from
non-test code, and package initializers that mutate Python's import search path
all fail the gate.
Existing violations are tracked in
`architecture_baseline.json` and must only shrink; any NEW violation blocks the
PR. Maintainers regenerate the baseline with
`python scripts/architecture_gate.py --update-baseline`.

### GPU / external-tool validation

`test_design_gpu.py`、Prediction 服务器回归和外部工具验证需要专用环境与显式配置，不能在基础 CPU 环境中默认运行。具体命令、数据和许可要求见 `docs/` 下的 validation 文档。

## 12. Development Rules

- One PR = one architectural purpose。
- No big-bang rewrite；优先 behavior-preserving refactor。
- Public interface changes must be declared，并更新调用方和测试。
- Do not duplicate contract definitions；共享基础设施修改必须显式 review。
- Do not bypass Evidence，也不要新增 shadow state / shadow log。
- 不要绕过 Action Registry 或让未实现 action 进入 `ready`。
- 不要未经批准直接改变 scientific thresholds；参数应进入版本化 protocol/config。
- 按模块 ownership 和 PR scope 修改代码。`contracts/`、data layer、execution boundary 属于共享基础设施，不适用按个人目录隔离修改的旧规则。
- 所有人工开发和 Codex 修改都必须遵守 [ENGINEERING_STANDARD.md](./ENGINEERING_STANDARD.md)。当前仓库未提供额外的根目录 `AGENTS.md`。
- 项目上下文通过 [`core/context.py`](./core/context.py) 的 `ProjectContext` 显式注入（PR5）；`data_layer` 与 `agents.research` 的 import-time 项目全局已去除，`Design` 可直接接受 `ProjectContext`。

当前已知的文档化技术债包括 State/CandidateIndex 的并发与事务边界、execution transaction boundary、大型 agent 模块的拆分；这些不是本 README 的实现承诺。

## 13. Documentation Index

### Architecture

- [可迁移流程](./docs/transferable_pipeline.md)
- [数据层使用手册](./数据层使用手册.md)
- [工程标准](./ENGINEERING_STANDARD.md)
- [前端 API contract](./docs/frontend_api_contract.md)

### Agents and execution

- [Critic agent](./docs/critic_agent.md)
- [Planner agent](./docs/planner_agent.md)
- [Orchestrator agent](./docs/orchestrator_agent.md)
- [Execution agent](./docs/execution_agent.md)

### Prediction / Design

- [Prediction pipeline](./docs/prediction_pipeline.md)
- [Cyclic inverse folding](./docs/cyclic_inverse_folding.md)
- [Design integrity](./docs/design_integrity_v5.2.0.md)

### Validation records

- [Agent loop validation](./docs/agent_loop_v1.1_server_validation_20260803.md)
- [Backend end-to-end validation](./docs/backend_e2e_mdm2_mdmx_20260803.md)
- [Prediction pipeline 与验证入口](./docs/prediction_pipeline.md)
- 更多历史记录见 `docs/`；历史验证不是当前架构定义。
