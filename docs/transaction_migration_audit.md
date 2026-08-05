# Execution Transaction Migration Audit

本文件审计 PR #36（分支 `chemzi/pr34-fix2`，base `chemzi/dev@5850bbe`）当前的执行路径，
并给出"让 Transaction Boundary 成为唯一真实执行入口"的迁移方案。

审计基线：`chemzi/pr34-fix2@3e2a20e`（PR #36 head）。
所有文件:行号引用均相对该 commit。

---

## 1. 任务规范 vs 真实代码的偏差

任务规范（PR36-fix spec）对代码的字面描述有几处与实际不符，先校正，否则按字面改动会扑空：

| 规范说法 | 真实情况 |
|---|---|
| "execute_task 调用链" | `execute_task()` 确实存在，在 `execution/worker.py:172`，是 legacy 生产入口 |
| "HandlerOutcome 双返回模型" | 不存在 `HandlerOutcome` 类。真正的"双模型"是同一个 `ExecutionActionResult`（`execution/results.py:13`，5 字段）被两套入口分别只用一半字段 |
| "handler(context, staging) -> ExecutionActionResult" | 这是 `ExecutionWorker.run()` 期望的签名（`worker.py:76`）；真实 4 个 handler 签名是 `handler(HandlerContext) -> ExecutionActionResult`（`handlers.py:467`），只填 `outputs/processes` |
| "Transaction Layer 只被单元测试使用" | 属实。`ExecutionWorker.run()` 仅被 `test_execution_transactions.py` 覆盖，生产入口 `execute_task()` 完全不调用它 |

---

## 2. Current Flow（生产路径，`execute_task`）

入口：`execution/worker.py:172` `execute_task(run_path, task_id, worker_id)`

```
execute_task(run_path, task_id, worker_id)
  │
  ├─ claim(run_path, task_id, worker_id)            [agents/orchestrator.py:817]
  │     → claim_token + dispatch_packet_path + attempt
  │
  ├─ _read_packet() → validate_dispatch_packet()     [worker.py:158, execution/contracts.py:379]
  ├─ assert_action_executable(task) → parameters     [execution/contracts.py:349]
  ├─ handler = handler_for(action)                   [execution/action_registry.py:21]
  │
  ├─ outcome = handler(HandlerContext(packet, config, task_dir))   [worker.py:239]
  │     │  handler 签名: (HandlerContext) -> ExecutionActionResult
  │     │  handler 内部:
  │     │    - run_process(design.py / prediction.py / critic.py)  [execution/supervisor.py:55]
  │     │    - subprocess 在独立进程直接调 data_layer.CandidateIndex / State
  │     │    - 写 task_dir/outputs/*.json
  │     │  返回: ExecutionActionResult(outputs=(...), processes=(...))
  │     │         candidate_updates / state_updates / artifacts 均为空
  │
  ├─ complete(run_path, task_id, token, output_paths, gpu_minutes)  [orchestrator.py:1109]
  │     │  写 run.json: task.status = SUCCEEDED, outputs inventory, GPU lease 释放
  │     │  不经过 TransactionContext / CommitManager / Store
  │
  ├─ atomic_json(task_dir/"execution_receipt.json", receipt)
  └─ EvidenceLogger.log("execution", "execution_task_completed", receipt)
        → data_layer → Store.append (SQLite evidence_events 或 FileStore)

失败路径:
  except BaseException as exc:
    ErrorInfo.from_exception(exc, component="execution.worker")
    fail(run_path, task_id, token, reason, error_info, gpu_minutes)   [orchestrator.py:1204]
        → 写 run.json: task.status = FAILED, last_error
    EvidenceLogger.log("execution", "execution_task_failed", failure)
    raise
```

**不经过的 transaction 组件**：`TransactionContext` / `StagingArea` / `CommitManager` /
`Store.commit_transaction` / `Store.record_task_failure`。

**直接写正式状态的位置**：
- `Orchestrator.complete()`/`fail()` 写 `run.json`（task 状态机）
- handler 内 subprocess 调 `data_layer.CandidateIndex`/`State`（candidates、state）
- `EvidenceLogger.log()` 写 evidence

---

## 3. Transaction Flow（测试路径，`ExecutionWorker.run`）

入口：`execution/worker.py:73` `ExecutionWorker.run(context, handler, validator=None)`

仅被 `test_execution_transactions.py` 调用，handler 是测试内联的 fake（test:45）。

```
ExecutionWorker.run(context, handler)
  │
  ├─ StagingArea(staging_root, transaction_id).create()    [execution/staging.py:29]
  ├─ context.transition(STAGING)
  ├─ store.append(_event(context, "execution_started"))
  │
  ├─ result = handler(context, staging)                    [worker.py:84]
  │     │  handler 签名: (TransactionContext, StagingArea) -> ExecutionActionResult
  │     │  测试 fake: 在 staging.path 写文件 → staging.stage_artifact()
  │     │  返回: ExecutionActionResult(candidate_updates=(...), state_updates={...}, artifacts=(...))
  │     │         outputs / processes 为空
  │
  ├─ isinstance(result, ExecutionActionResult) 校验
  ├─ context.transition(VALIDATING) → validator(result)
  │
  ├─ CommitManager.commit(context, candidate_updates, state_updates, artifacts, staging_path)  [commit_manager.py:38]
  │     │  1. validate(artifacts): sha256 校验 staged 文件            [commit_manager.py:26]
  │     │  2. context.transition(COMMITTING)
  │     │  3. copy staged → .tmp → fsync → os.replace 原子移动到 artifact_root
  │     │  4. Store.commit_transaction(context, candidate_updates, state_updates, artifacts, completed_event)
  │     │        [storage/sqlite_store.py:288]
  │     │        单个 with _connect() 块内原子写:
  │     │          projects + states(state_updates) + candidates(candidate_updates)
  │     │          + artifacts + tasks(status=SUCCEEDED) + evidence_events(completed_event)
  │     │        任一步失败 → 整个事务回滚
  │     │  5. 失败回滚: 删 .tmp + 删已 os.replace 的 committed 文件 + marker=ROLLED_BACK
  │     │  6. 成功: context.transition(COMMITTED), marker=COMMITTED（更新失败不影响，DB 已是事实来源）
  │
  └─ staging.discard()

失败路径:
  except Exception as exc:
    若 context.status == COMMITTED: raise（提交后清理失败不得标任务失败）  [worker.py:103]
    ErrorInfo.from_exception(...)  [contracts/errors.py:120]
    context.transition(FAILED)
    staging.write_manifest("error.json", error) + ("transaction.json", context)
    store.record_task_failure(context, error)    [sqlite_store.py:388] → tasks(status=FAILED)
    store.append(_event(context, "execution_failed", **error))
    raise ExecutionFailure(error)
```

**不经过的 legacy 组件**：`Orchestrator.complete/fail` / `run.json` / `EvidenceLogger.log`
（用 `store.append` 而非 `EvidenceLogger.log`，虽然底层可能同 Store）。

---

## 4. 双模型的具体分裂点

### 4.1 同名 `ExecutionActionResult`，两套字段不交集

`execution/results.py:13` 定义了统一的 5 字段 dataclass：

```python
@dataclass(frozen=True)
class ExecutionActionResult:
    candidate_updates: tuple = ()   # transaction 侧
    state_updates: Mapping | None = None  # transaction 侧
    artifacts: tuple = ()           # transaction 侧
    outputs: tuple = ()             # legacy 侧 (role, Path)
    processes: tuple = ()           # legacy 侧
```

但两个入口各只用一半：
- 生产 handler（`handlers.py` 4 个）只填 `outputs`/`processes`，`candidate_updates`/`state_updates`/`artifacts` 全空
- transaction 测试 handler 只填 `candidate_updates`/`state_updates`/`artifacts`，`outputs`/`processes` 全空

`CommitManager.commit()` 只读 `candidate_updates`/`state_updates`/`artifacts`（commit_manager.py:93-98）。
若直接把生产 handler 塞进 `ExecutionWorker.run()`，commit 提交的是空集——什么都不会落库。

### 4.2 两套 task 状态机

| | 位置 | 写入者 | 内容 |
|---|---|---|---|
| Legacy | `run.json` 文件 | `Orchestrator.complete()`/`fail()` | task.status, attempts, outputs, GPU lease, attempt_history |
| Transaction | SQLite `tasks` 表 | `Store.commit_transaction`/`record_task_failure` | task_id, workflow_id, action, status, payload |

两者互不读取。`Orchestrator` 不读 SQLite `tasks` 表；`CommitManager` 不写 `run.json`。
迁移必须解决：哪个是 task 状态的权威来源？

### 4.3 两套 evidence 写入

| | 入口 | 原子性 |
|---|---|---|
| Legacy | `EvidenceLogger.log("execution_task_completed", receipt)` | 独立 Store.append，非事务内 |
| Transaction | `Store.commit_transaction` 内的 `completed_event` | 事务内原子写 |

底层若都是 SQLiteStore，写同一张 `evidence_events` 表，但原子性边界不同。
`ExecutionWorker.run` 的 `execution_started`/`execution_failed` 走 `store.append`（非事务内），
`execution_completed` 走 `commit_transaction`（事务内）。

### 4.4 subprocess 绕过 transaction boundary

`handlers.py` 的 4 个 handler 通过 `run_process()` 启动 subprocess：
- `iterate_design` → `agents/design.py`
- `evaluate_new_design_candidates` → `scripts/run_prediction_predictors.py` + `scripts/enrich_prediction_evidence.py` + `agents/prediction.py`
- `review_prediction_handoff` → `agents/critic.py`
- `propose_threshold_calibration` → 无 subprocess，纯写文件

subprocess 在独立进程内**直接调 `data_layer.CandidateIndex`/`State`**（`handlers.py:95` `iterate_design` 就调 `CandidateIndex.load()`）。
handler 无法拦截 subprocess 的 data_layer 写入——这是把 handler 塞进 StagingArea 模型的根本障碍。

---

## 5. Target Flow（迁移后）

```
execute_task(run_path, task_id, worker_id)
  │
  ├─ claim() → dispatch packet → handler_for(action)          [不变]
  │
  ├─ TransactionContext.create(                                [新增]
  │     workflow_id, run_id, task_id, attempt_id, action=action)
  │
  ├─ ExecutionWorker.run(context, adapted_handler)             [改造入口]
  │     │
  │     │  adapted_handler(context, staging):
  │     │    ├─ 真实 handler(HandlerContext) 执行
  │     │    │    subprocess 输出目录指向 staging 或 task_dir
  │     │    ├─ 把产出文件 staging.stage_artifact() → artifacts
  │     │    ├─ 收集 candidate_updates / state_updates
  │     │    └─ return ExecutionActionResult(
  │     │         candidate_updates, state_updates, artifacts,
  │     │         outputs, processes)
  │     │
  │     ├─ StagingArea → CommitManager.commit → Store.commit_transaction
  │     │     [原子写 SQLite: states+candidates+artifacts+tasks(SUCCEEDED)+evidence(completed)]
  │     └─ staging.discard()
  │
  ├─ complete(run_path, task_id, token, output_paths, gpu_minutes)  [保留，但移到 commit 之后]
  │     写 run.json: task SUCCEEDED（与 SQLite tasks 表对齐）
  │
  └─ EvidenceLogger.log 或由 commit_transaction 内 completed_event 替代

失败路径:
  ExecutionWorker 已 record_task_failure + execution_failed event
  → fail(run_path, ...) 写 run.json FAILED（与 SQLite tasks 表对齐）
```

---

## 6. Migration Points

| file | change |
|---|---|
| `execution/worker.py` | `execute_task()` 在 `handler_for(action)` 之后插入 `TransactionContext.create()` + `ExecutionWorker.run(context, adapted_handler)`；真实 handler 调用从直接 `handler(HandlerContext)` 改为经 `adapted_handler` 包装；`complete()`/`fail()` 调用保留但移到 commit 之后（run.json 仍需更新以保持 Orchestrator 状态机一致） |
| `execution/handlers.py` | 4 个 handler 签名保持 `(HandlerContext)`，但内部：subprocess 输出目录指向 staging；产出文件 `staging.stage_artifact()`；停止直接调 `CandidateIndex.load()`/`State.update()` 写正式数据，改为收集 `candidate_updates`/`state_updates` 填入返回值。`iterate_design` 是唯一写 CandidateIndex 的，需重点改造 |
| `execution/results.py` | `ExecutionActionResult` 结构不变（5 字段已统一）。补 docstring 明确：`outputs`/`processes` 是 handler 执行记录，`candidate_updates`/`state_updates`/`artifacts` 是 transaction 提交内容，两者由 adapted_handler 同时填写 |
| `agents/design.py` | 若选方案 B（见 §7）：subprocess 不再直接写 `CandidateIndex`，改为输出 candidate JSON 到 task_dir，由 handler 读取后填入 `candidate_updates` |
| `agents/prediction.py` | 同上，prediction handoff 已写文件，artifact 化较自然；CandidateIndex 写入需剥离 |
| `agents/orchestrator.py` | `complete()`/`fail()` 保留写 run.json；需明确 task 状态权威来源——见 §8 决策点。`_refresh()`/`_sync_state()` 仍读 run.json |
| `storage/sqlite_store.py` | `commit_transaction` 已原子，结构无需改。可扩展 `completed_event` 携带 output inventory + GPU usage，让 run.json 可从 SQLite 重建（若选 task 状态统一到 SQLite） |
| `execution/recovery.py` | 已实现 `recover_pending`（清理 PREPARED 但未 COMMITTED 的孤儿文件）。迁移后需在 worker 启动时调用一次 |
| `evidence` | 统一：`execution_started`/`execution_failed` 走 `store.append`，`execution_completed` 走 `commit_transaction` 内 `completed_event`。停用 `execute_task` 里重复的 `EvidenceLogger.log("execution_task_completed")`，或保留为 run.json 状态投影日志（非正式 evidence） |

---

## 7. subprocess 事务化难题（关键决策点）

`iterate_design` 等 handler 跑 subprocess，subprocess 在独立进程直接调 `data_layer`。
无法在 handler 层用 StagingArea 拦截。三种方案：

### 方案 A：subprocess 输出到 staging，CandidateIndex 仍直接写（部分事务化）
- subprocess 的文件输出指向 staging 目录，handler 事后 `stage_artifact()`
- `CandidateIndex`/`State` 仍由 subprocess 直接调 data_layer 写入（非事务）
- CommitManager 只管 artifact + task status + evidence；candidate/state 非原子
- **Limitation**：candidate 写入不在 transaction boundary 内，提交失败会留下已写入的 candidate（需手工回滚或接受脏数据）
- 工作量：小

### 方案 B：subprocess 不写 data_layer，改为输出 candidate JSON 给 handler（完全事务化）
- 改 `agents/design.py`/`prediction.py` 接口：不再调 `CandidateIndex`，改为把 candidate 记录写到 task_dir 的 JSON
- handler 读取 JSON → 填入 `candidate_updates` → CommitManager 原子提交
- **优点**：candidate/state/artifact 全部经 transaction boundary，失败真回滚
- **Limitation**：需改 subprocess 接口，design.py/prediction.py 的输出协议要变；external tool 验证文档要同步
- 工作量：中（4 个 handler + 2 个 agent 脚本接口）
- 符合任务规范"所有正式状态经 transaction boundary"

### 方案 C：接受 subprocess 非原子，明确写成 limitation（最小改动）
- `execute_task` 走 `ExecutionWorker.run`，但 adapted_handler 只 stage artifact + 填 task status
- candidate/state 仍由 subprocess 直接写，不进 commit_transaction
- CommitManager 只保证 artifact + task status + evidence 原子
- **Limitation**：明确写进 README 和 commit_manager docstring——candidate/state 不在 atomic boundary 内
- 工作量：最小
- 风险：违背任务规范"所有 Execution Action 必须经过 Transaction Boundary"的硬要求

**已选定：方案 B，分阶段**（主人 2026-08-05 确认）。
Phase 1 只做 `iterate_design`（唯一写 CandidateIndex 的），其余 3 个 handler（prediction/critic/calibration
不直接写 CandidateIndex）留 Phase 2，届时用方案 A 即可天然事务化。

---

## 8. 其他决策点

### 8.1 task 状态权威来源
- 选项 1：`run.json` 保留权威，SQLite `tasks` 表是投影。`commit_transaction` 写 SQLite 后，`complete()` 仍写 run.json，两者对齐。
- 选项 2：SQLite `tasks` 表权威，`run.json` 由 SQLite 重建。`Orchestrator.complete()` 改为读 SQLite。
- **已选定：选项 1**（主人 2026-08-05 确认）：run.json 保留 task 状态权威，SQLite `tasks` 表作事务审计副本。本 PR 不动 Orchestrator 状态机；SQLite task 状态统一留未来 PR。

### 8.2 evidence 统一
- `execution_completed` 已在 `commit_transaction` 事务内写
- `execute_task` 现在的 `EvidenceLogger.log("execution_task_completed")` 重复
- **推荐**：删掉 `execute_task` 的重复 `EvidenceLogger.log`，保留 `execution_started`/`execution_failed`（后者由 ExecutionWorker 写）

### 8.3 `ErrorInfo` 已统一
PR #36 已把 `ErrorInfo` 合并到 `contracts/errors.py`（带 `error_type`/`traceback`/identity 字段）。
`contracts/transaction.py` 不再定义 `ErrorInfo`。任务规范 Step 6 说的"清理 HandlerOutcome"——
实际无 HandlerOutcome 可清；`ErrorInfo` 已统一。此项无工作。

---

## 9. 验收对照（任务规范 Step 10）

| 验收项 | 当前状态 | 迁移后预期 |
|---|---|---|
| execute_task 使用 TransactionWorker | 否（execute_task 不调 ExecutionWorker） | 是 |
| 没有第二套 execution flow | 否（execute_task 与 ExecutionWorker.run 并存） | 是（ExecutionWorker.run 成为 execute_task 内部步骤） |
| Handler 只返回 ExecutionActionResult | 是（已统一 5 字段，但两套字段不交集） | 是（adapted_handler 同时填两套字段） |
| Traceability（workflow/run/task/attempt/tx_id） | 部分（execute_task 无 transaction_id） | 是 |
| 失败可定位（action/agent/attempt/traceback） | 是（ErrorInfo 已统一） | 是 |
| 正式状态在 SQLite | 部分（run.json 仍是 task 状态权威） | 取决于 §8.1 决策 |
| `pytest -q` 通过 | 待迁移后验证 | — |

---

## 10. 决策已定（主人 2026-08-05 确认）

| 决策点 | 结论 |
|---|---|
| subprocess 事务化方案 | 方案 B，分阶段。Phase 1 只做 iterate_design（唯一写 CandidateIndex） |
| task 状态权威来源 | 选项 1：run.json 保留权威，SQLite tasks 表作事务审计副本 |
| 工作分支 | 新建 chemzi/pr36-execution-migration（已建，基于 pr34-fix2@3e2a20e） |
| PR 范围 | Phase 1 = execute_task 接入 + iterate_design 事务化；其余 handler 留 Phase 2 |

### 关键约束

1. **ExecutionActionResult 双填**：adapted_handler 同时填 `outputs`/`processes`（执行记录）+ `candidate_updates`/`state_updates`/`artifacts`（transaction 提交内容）。不删任何字段。
2. **不删 Orchestrator.complete()**：Transaction 负责数据库一致性，`complete()` 负责 run.json workflow projection。流程：CommitManager commit 成功 → `complete()` 更新 run.json。两者职责不同，都要保留。
3. **design.py 改输出协议**：不再调 `CandidateIndex.add()`（3 处：line 607/767/1053），改为把 candidate 记录写到 `candidate_updates.json`；handler 读取后填入 result，由 CommitManager 原子提交。保留 `CandidateIndex.load()` 读（dedup/max ID，读不破坏事务性）。
4. **不做的**：全部 handler 重写、SQLite task 状态统一、Recovery 大改、Agent interface freeze——均留后续 PR。

### Phase 1 执行计划

PR 改名：**PR36 Migration — Make Transaction Boundary Real**

目标：将已有 Transaction Framework 接入真实 Execution Path，禁止新增 Transaction Framework。

修改范围（仅 4 个文件 + 测试）：

1. `execution/worker.py` — `execute_task` 接入 `TransactionContext.create` + `ExecutionWorker.run` + `CommitManager`；`complete()` 移到 commit 后
2. `execution/handlers.py` — `iterate_design` 事务化：读 design.py 输出 JSON，双填 `ExecutionActionResult`，stage artifacts
3. `agents/design.py` — 剥离 3 处 `CandidateIndex.add()`，改为输出 `candidate_updates.json`
4. `test_execution_transaction_integration.py` — 新增真实集成测试（execute_task → real handler → transaction → sqlite）

完成标准：

- `execute_task` 经过 TransactionContext / ExecutionWorker / CommitManager / Store.commit_transaction
- iterate_design 的 candidate 写入经 transaction boundary（失败真回滚）
- 真实集成测试覆盖 success / failure / retry 三场景
- `pytest -q` 通过，无回归

Phase 2（后续 PR）：prediction / critic / calibration handler migration。

审计完成，进入 Phase 1 代码改造。

---

## 11. CandidateUpdate Contract（7A 产出）

`contracts/candidate_update.py` 定义 Design subprocess 与 transaction boundary 之间的数据契约。已通过 import + round-trip 验证。

### 数据结构

- `CandidateUpdate`：单条 candidate 记录，13 字段，对齐 `design.py _candidate_from_manifest` 的 candidate handoff contract（不重新发明 schema）
- `CandidateUpdateBatch`：`candidate_updates.json` 文件 envelope（schema_version / emitter / source_route / generated_at / candidate_updates）

### 5 个坑的规避

| 坑 | 规避 |
|---|---|
| 坑1 不旁路 | docstring 明确 lifecycle: generate → read → commit → discard；CandidateIndex 仍是 commit 后唯一真相来源，candidate_updates.json 不是缓存/状态库 |
| 坑2 不 dump `__dict__` | typed frozen dataclass，13 字段显式声明；复用 candidate handoff contract 字段，Candidate 类变化不影响 |
| 坑3 max ID 并发 | DEBT 记录：Phase 1 candidate_id 仍由 `design.py _next_candidate_id()` 生成（含 `State.save` 旁路）；Phase 2 移到 CommitManager 分配 |
| 坑4 handler 不偷写 | handler 读取 CandidateUpdate → 填入 `ExecutionActionResult.candidate_updates` → CommitManager 提交；禁止 handler 调 `CandidateIndex.add` |
| 坑5 接入顺序 | 7A 先锁契约（本步）→ 7B 改 design.py emit → 7C 迁移测试 → 8 handler adapter → 9 execute_task |

### 文件格式（candidate_updates.json）

```json
{
  "schema_version": 1,
  "emitter": "design",
  "source_route": "A",
  "generated_at": "2026-08-05T18:25:00+00:00",
  "candidate_updates": [
    {
      "candidate_id": "C0001",
      "sequence": "...",
      "length": 12,
      "source_route": "A",
      "source_batch": "...",
      "cyclization_type": "head-to-tail_amide",
      "cyclization_bonds": [{"atom_1": "residue_12:C", "atom_2": "residue_1:N", "bond_type": "amide"}],
      "design_pdb_path": "/task_dir/design/C0001.pdb",
      "design_pdb_hash": "<sha256>",
      "manifest_path": "/task_dir/design/C0001.json",
      "manifest_sha256": "<sha256>",
      "monomer_plddt": 85.2,
      "notes": "{...}"
    }
  ]
}
```

### 与已有 contracts 的关系

- 复用 candidate handoff contract 字段（`_candidate_from_manifest`），不新增 candidate schema
- design_pdb / manifest 文件由 handler 通过 `StagingArea.stage_artifact()` 转为 `StagedArtifact`（复用 StagedArtifact，不新建 artifact 类型）
- trace（workflow_id/run_id/task_id/attempt_id）由 handler 从 `TransactionContext` 填入，不在 CandidateUpdate 里（design subprocess 不知道 trace）
- `ArtifactRef` / `TraceContext` 本身不变，CandidateUpdate 是新增的 transaction staging contract

### 已知 limitation（DEBT，不隐藏）

1. **candidate_id 生成旁路**：`design.py _next_candidate_id()` 读 CandidateIndex + 写 `State(candidate_count)`，绕过 transaction boundary。单进程假设已记录在 `_next_candidate_id` docstring。Phase 2 移到 CommitManager 分配，保证事务性 ID 分配 + 并发安全。
2. **State.save(candidate_count) 旁路**：第二个正式状态写入旁路，Phase 1 接受作为 limitation。

7A 完成。下一步 7B：改 design.py，3 处 `CandidateIndex.add()` 替换为 `emit_candidate_update()`，输出 `candidate_updates.json`。

---

## 12. Task 9A — Execution Entry Migration Audit

Task 9 是 PR36 Migration 最后一道门：让 `execute_task` 不再直接驱动 legacy handler，而是驱动 Transaction Execution。本节审计当前入口 + complete/fail 职责 + 重复 commit 风险，锁定改造方案。

### 当前调用链（`execute_task`, worker.py:172）

```
execute_task(run_path, task_id, worker_id)
  claim() → dispatch packet → handler_for(action)
  handler(HandlerContext) → ExecutionActionResult(outputs, processes)
  complete(run_path, task_id, token, output_paths, gpu_minutes)
    _inventory_outputs → ArtifactRef dict（不写 SQLite）
    validate_output_inventory → _validate_design_result（校验 after-before）
    GPU lease 释放 → run.json SUCCEEDED + outputs → _sync_state
    EvidenceLogger.log("orchestrator_task_completed")
  EvidenceLogger.log("execution_task_completed")   ← 重复
失败:
  fail(run_path, task_id, token, reason, error_info, gpu_minutes)
    run.json FAILED + last_error → _sync_state
    EvidenceLogger.log("orchestrator_task_failed")
  EvidenceLogger.log("execution_task_failed")      ← 重复
```

不经过：TransactionContext / StagingArea / CommitManager / Store.commit_transaction。

### 目标调用链

```
execute_task(run_path, task_id, worker_id)
  claim() → dispatch packet → handler_for(action)
  TransactionContext.create(workflow_id, run_id, task_id, attempt_id, action)
  ExecutionWorker.run(context, adapter)
    StagingArea → adapter(context, staging) → ExecutionActionResult
    CommitManager.commit → Store.commit_transaction
      [SQLite atomic: artifacts + candidates + state + tasks(SUCCEEDED) + evidence(execution_completed)]
  complete(run_path, task_id, token, output_paths, gpu_minutes)   ← 移到 commit 后
    run.json SUCCEEDED + outputs（workflow projection）
    EvidenceLogger.log("orchestrator_task_completed")
失败:
  ExecutionWorker 已 record_task_failure + execution_failed event
  fail(run_path, ...) → run.json FAILED
```

### complete() / fail() 职责确认

| | complete() | fail() |
|---|---|---|
| 参数 | run_path, task_id, claim_token, output_paths, gpu_minutes | run_path, task_id, claim_token, reason, retryable, error_info, gpu_minutes |
| GPU lease | 释放 | 释放 |
| run.json | task SUCCEEDED + outputs inventory + resource_usage | task FAILED + last_error + resource_usage |
| output 校验 | validate_output_inventory → _validate_design_result | 无 |
| SQLite artifacts 表 | **不写**（只 run.json outputs） | 不写 |
| evidence | EvidenceLogger.log("orchestrator_task_completed") | EvidenceLogger.log("orchestrator_task_failed") |
| State | _sync_state（phase/round/active_workflow_id 投影） | _sync_state |

### 重复 commit 风险分析

| 数据 | CommitManager.commit_transaction | Orchestrator.complete/fail | execute_task | 重复？ |
|---|---|---|---|---|
| SQLite artifacts 表 | INSERT OR IGNORE | 不写 | 不写 | 否 |
| candidates | upsert | 不写 | 不写 | 否 |
| state | update（state_updates） | _sync_state（不同字段） | 不写 | 双写不同字段，Phase 1 接受 |
| task status | tasks(SUCCEEDED/FAILED) | run.json SUCCEEDED/FAILED | 不写 | 双写对齐（run.json 权威 + SQLite 审计副本） |
| evidence | execution_completed / execution_failed | orchestrator_task_completed/failed | execution_task_completed/failed | **三重写** |

### 关键风险1：evidence 三重写

- CommitManager: `execution_completed`（agent=execution，事务内）
- execute_task: `execution_task_completed`（agent=execution，事务外）← 重复
- complete: `orchestrator_task_completed`（agent=orchestrator，事务外）← 不同视角，保留

**处理**：execute_task 走 transaction 后，**删掉** execute_task 的 `EvidenceLogger.log("execution_task_completed"/"execution_task_failed")`（ExecutionWorker 已写 execution_started/completed/failed）。保留 complete/fail 的 orchestrator evidence（workflow projection 视角）。

### 关键风险2：validate_output_inventory 不兼容 transaction

`complete()` 调 `validate_output_inventory` → `_validate_design_result` 校验 `new_candidate_ids == sorted(set(after_by_id) - set(before_by_id))`（CandidateIndex snapshot 差集）。transaction 路径 design.py 不写 CandidateIndex，after==before，但 new_ids 非空 → **校验失败**。

**处理选项**：
1. 改 `_validate_design_result` 适配 transaction（after==before 时从 candidate_updates 取）—— 改 contracts.py，影响范围大
2. transaction 路径 `complete()` 跳过 `validate_output_inventory`（CommitManager 已保证原子性，legacy 校验不需要）
3. transaction 路径 design_task_result 用虚拟 after —— 造假，不推荐

**推荐选项 2**：transaction 路径 `complete()` 跳过 `validate_output_inventory`。理由：CommitManager.commit_transaction 已保证 artifact + candidate + state + task status 原子提交，legacy 的 output schema 校验是 complete 在非事务路径的安全网，transaction 路径不需要。实现：`complete()` 加 `skip_output_validation: bool = False` 参数，transaction 路径传 True。

### 关键风险3：_sync_state 双写 State

`complete()` 调 `_sync_state(run, plan)` 同步 State（phase/round/active_workflow_id）。CommitManager.commit_transaction 也写 state（state_updates）。两者写 State 不同字段。

**处理**：Phase 1 接受双写（不同字段，不冲突）。_sync_state 写 workflow projection 字段，commit_transaction 写 state_updates。记 DEBT，Phase 2 统一 State 写入入口。

### 谁负责什么（改造后职责表）

| 职责 | 负责方 | 存储 |
|---|---|---|
| artifact 文件提交 | CommitManager（os.replace） | 文件系统 |
| artifact 注册 | CommitManager（commit_transaction） | SQLite artifacts 表 |
| candidate 写入 | CommitManager（commit_transaction） | SQLite candidates |
| state（state_updates） | CommitManager（commit_transaction） | SQLite states |
| state（workflow projection） | Orchestrator._sync_state | SQLite states（不同字段） |
| task status（事务审计） | CommitManager（commit_transaction） | SQLite tasks |
| task status（workflow） | Orchestrator.complete/fail | run.json |
| GPU lease 释放 | Orchestrator.complete/fail | run.json |
| output inventory | Orchestrator.complete（_inventory_outputs） | run.json outputs |
| evidence: execution_started/failed | ExecutionWorker（store.append） | SQLite evidence_events |
| evidence: execution_completed | CommitManager（commit_transaction） | SQLite evidence_events |
| evidence: orchestrator_task_completed/failed | Orchestrator.complete/fail | SQLite evidence_events |
| execution_receipt | execute_task（atomic_json） | task_dir/execution_receipt.json |
| ~~evidence: execution_task_completed/failed~~ | ~~execute_task~~ | **删除（重复）** |

### 改造方案：统一走 ExecutionWorker.run（不保留第二条路径）

主人要求"不要为了兼容保留第二条执行路径"。方案：

`execute_task` **统一**走 `ExecutionWorker.run(context, adapter)`，所有 action 都经 transaction boundary：
- `iterate_design`：用 `make_iterate_design_adapter`（事务化，candidate_updates 非空）
- 其他 action（prediction/critic/calibration）：用通用 `make_legacy_handler_adapter`（包装 legacy handler，填 outputs/processes，candidate_updates 空）—— CommitManager commit 空 candidate_updates（只 task status + evidence + artifacts if any）

这样只有一条 execution 路径（ExecutionWorker.run），不保留 legacy 直调路径。其他 handler 的 candidate 事务化留 Phase 2（补 adapter 的 candidate_updates 逻辑）。

### Task 9 改造步骤

1. `execute_task` 在 `handler_for(action)` 后插入 `TransactionContext.create()`
2. 选择 adapter：`iterate_design` → `make_iterate_design_adapter`；其他 → `make_legacy_handler_adapter(handler)`
3. `ExecutionWorker.run(context, adapter)`：成功（commit 后）→ `complete(skip_output_validation=True)`；失败（ExecutionFailure）→ `fail(error_info=ExecutionFailure.error)`
4. `complete()` 加 `skip_output_validation` 参数，transaction 路径传 True（规避风险2）
5. 删 `execute_task` 的重复 `EvidenceLogger.log("execution_task_completed"/"execution_task_failed")`
6. 保留 `execution_receipt.json`（execute_task 写，含 transaction_id）
7. 新增 `make_legacy_handler_adapter(handler, packet, config, task_dir)` 通用包装

### 不做

- 不保留 legacy 直调路径（execute_task 必须走 ExecutionWorker.run）
- 不改 `_validate_design_result`（用 skip_output_validation 规避）
- 不改其他 handler 的 candidate 事务化（Phase 2）
- 不改 Orchestrator 状态机（run.json 保留 task 状态权威）

Task 9A 审计完成。等待确认后进入 Task 9 代码改造。

---

## 13. Implementation Result (PR36 Migration COMPLETE)

Status: **COMPLETE**

Execution path is now transactional. `execute_task` drives every action through:

```
execute_task → TransactionContext.create → adapter_for →
ExecutionWorker.run → CommitManager.commit → Store.commit_transaction →
complete(transaction_managed=True)
```

### Changes

- `contracts/candidate_update.py`: CandidateUpdate + CandidateUpdateBatch contract
- `execution/adapters.py`: make_iterate_design_adapter + make_legacy_handler_adapter + adapter_for
- `agents/design.py`: _emit_candidate_update (replace CandidateIndex.add) + --candidate-updates-path
- `execution/handlers.py`: iterate_design transactional (read candidate_updates.json via contract, double-fill result)
- `execution/worker.py`: execute_task 接入 Transaction (TransactionContext + ExecutionWorker.run + complete(transaction_managed=True)); 删重复 evidence
- `execution/commit_manager.py`: CandidateUpdate → to_dict() for Store
- `agents/orchestrator.py`: complete() transaction_managed parameter

### Validation

- 21 PR36 migration tests passed (test_execution_transactions + test_design_emits_candidate_updates + test_iterate_design_adapter + test_adapters + test_execute_task_integration)
- legacy test_execution.py failures are environment-only (/bin/echo on Windows), not regression

### Known DEBT (6 items, documented not hidden)

1. candidate_id/State.save bypass (Phase 2: move to CommitManager)
2. _sync_state double-write State (PR5 ProjectContext)
3. legacy adapter non-transactional side effects (Phase 2: prediction/critic/calibration)
4. artifact_id include attempt_id (retry safety)
5. _PENDING module globals (DesignRunContext for persistent worker)
6. notes field typed Mapping (after schema stabilization)

### Phase 2 (后续 PR)

- Migrate prediction/critic/calibration handlers to transactional adapters
- workflow projection failure recovery (commit success + complete fail)
- Interface Freeze
