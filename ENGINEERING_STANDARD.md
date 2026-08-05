# Engineering Standard — Architecture & Anti-Spaghetti Rules

本项目进入架构收敛阶段。所有人工开发、Codex / AI 修改、Pull Request 均必须遵守本文件。

## 1. 核心原则

优先保证：

Correctness > Architecture > Maintainability > Convenience > Development Speed

禁止为了“先跑通”长期牺牲系统边界。

任何新代码不得继续扩大现有技术债。

---

## 2. 修改范围原则

每个 PR 必须有单一明确目的。

禁止一个 PR 同时大规模修改多个独立层，例如：

Research + Planner + Orchestrator + Execution + Prediction

除非这些修改属于同一个不可拆分的接口迁移。

如果 AI 发现额外问题，应记录为 TODO / Issue，而不是顺手一起修改。

---

## 3. 函数与文件复杂度

以下情况视为 Code Smell，必须主动拆分或说明理由：

- 单函数超过约 50–80 行
- 3 层以上嵌套 if / loop
- 一个函数同时负责校验、业务决策、I/O、状态更新和日志
- 核心业务文件超过约 500–800 行且继续增长
- 多个模块复制相同 JSON、hash、path、atomic-write、validation 逻辑

禁止为了机械满足行数而无意义拆函数。

拆分目标是实现单一职责和明确边界。

---

## 4. Agent 边界

Agent 之间只能依赖公开 Contract。

禁止：

```python
from agents.xxx import _private_function
```

Planner、Critic、Orchestrator、Execution 不得互相依赖具体实现细节。

共享逻辑应移动到独立 contract / domain / infrastructure 模块。

---

## 5. Planner / Execution Closed World

Planner 生成的任何 executable action：

```text
Planner
→ Orchestrator
→ Execution
```

必须存在真实可运行的 Execution handler。

禁止存在：

```text
plan.status = ready
```

但 Execution 无 handler 的任务。

如果 action 尚未实现，只允许：

```text
blocked
blocked_unimplemented
manual_required
```

不得标记为 ready。

必须有端到端测试验证：

```text
Critic → Planner → Orchestrator → Execution → Complete
```

---

## 6. 状态与事务

正式状态不得由多个 Agent 任意直接修改。

State、CandidateIndex、Evidence、Run metadata 应通过统一 Store 层访问。

禁止：

```text
load
→ 修改
→ save
```

在并发 Worker 环境中作为主要事务机制。

任何 Execution task 必须满足：

```text
BEGIN
→ 执行
→ staging
→ validation
→ COMMIT
```

失败必须：

```text
ROLLBACK
```

禁止出现：

```text
task = failed
但正式 CandidateIndex / State 已部分写入
```

---

## 7. Project Context

禁止新增 import-time 项目全局状态：

```python
ACTIVE_PROJECT_CONFIG = ...
PROJECT_CONFIG = ...
```

核心业务应逐步迁移至显式：

```python
ProjectContext
```

并通过依赖注入传递：

```text
Research(context)
Design(context)
Prediction(context)
Critic(context)
Planner(context)
Execution(context)
```

目标是支持多个项目安全共存。

---

## 8. Scientific Protocol

科学计算参数不得散落在 handler 中成为 Magic Numbers。

例如：

```text
seed
models
num_recycles
post_relax_repeats
thresholds
timeout
```

应进入版本化 protocol/config。

计划和审批结果应绑定：

```text
protocol_version
protocol_sha256
```

保证实验可复现。

---

## 9. API Stability

任何公开函数签名发生变化必须：

1. 明确说明 breaking change；
2. 搜索整个仓库调用方；
3. 更新调用方；
4. 增加兼容层，或者明确完成版本迁移；
5. 添加测试。

不得静默修改公开接口。

---

## 10. Exception Handling

禁止无理由使用：

```python
except Exception:
```

如果确实需要 catch-all：

- 必须记录异常
- 必须明确 fallback 行为
- fallback 本身必须测试
- 不得隐藏不可恢复错误

---

## 11. AI / Codex 修改规则

每次修改前，AI 必须：

1. 阅读本文件；
2. 阅读相关模块和测试；
3. 说明此次修改边界；
4. 明确是否改变公开接口或数据格式。

修改完成后必须输出：

```text
Changed files
Behavior changed
Public interfaces changed
Tests added / updated
Tests executed
Remaining risks
```

禁止只回复：

```text
Done
Tests pass
```

---

## 12. 重构原则

禁止 Big-Bang Rewrite。

优先使用：

```text
Characterization Test
→ Introduce abstraction
→ Redirect caller
→ Verify behavior
→ Remove old implementation
```

重构 PR 原则上不得同时改变科学算法行为。

如果必须改变行为，应拆成：

```text
PR A: characterization / refactor
PR B: behavior change
```

---

## 13. Merge Gate

每个 PR 必须进行 Strict Code Review。

评分：

- ≥ 85：允许合并
- < 85：禁止合并

以下任一情况即使总分较高，也原则上禁止合并：

- ready task 无 Execution handler
- 数据可能部分提交
- 多 Worker 会造成 lost update / ID collision
- 未声明 breaking API change
- 新增明显巨型函数或巨型模块
- 没有覆盖核心行为的测试

---

# 当前仓库架构整改顺序

必须按以下优先级执行：

P0-1  
修复 Planner 可以生成 Execution 无法执行的 action。

P0-2  
解决 State / CandidateIndex 的并发安全和事务一致性。

P0-3  
解决 Execution task failed 但正式数据已经部分写入的问题。

P1-1  
建立统一 ProjectContext，减少 import-time global state。

P1-2  
拆分 Planner / Orchestrator / Critic / Research / Design 等巨型模块。

P1-3  
统一 JSON / hash / path / contract / atomic-write 基础设施。

P1-4  
将科学计算参数迁移到版本化 Protocol。

在前三个 P0 问题解决之前，原则上暂停增加新的 Agent 或大型计算阶段。

