# 回退式搜索 Orchestrator 说明：Planner、Critic 与搜索树引擎

## 一、这次修改解决什么问题

本次工作补上了 v5 方案里一直是 TODO 骨架的调度层。改动之前，`agents/planner.py` 和 `agents/critic.py` 各只有十几行注释，没有任何可执行逻辑；项目也没有任何调度入口，Research、Design、Prediction 之间只能靠人手工按顺序调用。

更关键的是缺一个结构性能力：项目原本是「单一可变白板 + 三个只追加存储」。`State` 是一个会被反复覆写的 `state.json`，`EvidenceLogger` 和 `CandidateIndex` 只能往后追加。这套结构可以记录「发生过什么」，但无法表达「我在策略空间的哪个位置、上一步是什么、这条路走不通该退回哪里」。一轮设计失败之后，除了从头再来没有别的选择。

这次引入了一个策略轮次级的有界回溯搜索树：每个节点是一轮 design → predict → critic 的策略配置，失败时不重启，而是退回父节点换一条没试过的兄弟分支。全程只追加不删除，失败分支保留下来本身就是「这条路走不通」的证据。

所有验证都在 `/tmp` 下的隔离数据目录中执行（`CYCPEP_DATA_DIR` / `CYCPEP_EVIDENCE_DIR`），仓库内原有的 `data/state.json`、`candidate_index.csv` 和 `evidence_log.jsonl` 没有被测试或 dry-run 触碰。

## 二、搜索树引擎

新文件 `agents/search_tree.py`（430 行）实现 `SearchTree` 类，纯逻辑，不依赖其他 agent，持久化到 `data/search_tree.json`。该文件属于运行时产出，已被 `.gitignore` 的 `data/*` 规则排除，不进版本控制。

每个节点记录以下内容。设计目标是让「这个节点为什么存在」可以被完整回答：

- `node_id` / `parent_id` / `depth` / `round`
- `status`：`open`、`expanding`、`evaluated`、`passed`、`dead_end`、`pruned`
- `strategy`：`{route_mix, lengths, constraints}`
- `strategy_diff`：相对父节点改了哪些参数，`{from, to}` 成对记录
- `branch_key`：策略的规范化 JSON 指纹，键名排序，保证同一策略在任何字典顺序下得到同一个 key
- `checkpoint`：`{candidate_ids, stats_snapshot, thresholds_ref}`，用于恢复到这一步
- `critic_verdict`：`advance`、`backtrack`、`dead_end`、`done`
- `children` / `tried_branch_keys`
- `trigger_event_id`：指向导致本节点诞生的那条 `critic_review` 证据事件

树容器另外保存 `root_id`、`active_id`、`frontier`（beam 待扩展队列）、`config{beam_width, max_nodes}` 和 `node_seq`。

`tried_branch_keys` 是回退能否真正前进的关键。父节点会累积所有已经尝试过的子策略指纹，`add_child()` 在遇到重复指纹时直接抛 `ValueError`。没有这个约束，回退之后 Planner 可能反复提出同一条已经失败的策略，搜索会原地打转。

`persist()` 使用同目录临时文件加 `os.replace()` 原子替换，避免写入中断留下半个 JSON。

## 三、有界回溯的两个预算

`beam_width`（k）限制每个父节点同时保留几条活分支。超出 k 的子节点标记为 `pruned` 而不是删除，`restore()` 可以把 `pruned` 节点重新打开回到 frontier。

`max_nodes`（M）是全局硬预算，对应单 GPU 的算力上限。`budget_exhausted()` 为真时 `add_child()` 抛 `RuntimeError`，orchestrator 捕获后停止并汇报当前最优分支。两个参数都可以通过 CLI 手工指定，也可以在运行中调用 `set_config()` 动态调整。

回退语义是：当前节点标 `dead_end` → `active_id` 指回父节点 → 父节点重新进入 frontier（因为它还可能长出未试过的兄弟）→ Planner 提出一条不在 `tried_branch_keys` 里的新策略 → 加为子节点并展开。候选和证据全部保留，`CandidateIndex` 一行都不删。

根节点是特例。根没有父节点，如果按普通回退处理会直接判定整棵树死亡。实际行为是根节点失败时保持 `evaluated` 状态，作为长期可用的分支点，直接在其下展开新的子分支。

## 四、Planner 的职责边界

`agents/planner.py` 现在有三个入口。

`plan(state, tree, node, candidates)` 是阶段路由：state 里没有 research 结果就派 research；有 research 但本节点没有候选就派 design；有候选但没有评分就派 prediction；已有评分就派 critic。返回 list of dict，每项带 `agent`、`action`、`phase`、`reason`、`needs_gpu`、`node_id`、`round`。

`reason` 字段是有意保留的。出问题时它能直接说明 Planner 当时为什么做这个决定，不需要反推。`needs_gpu` 目前只是标记，供未来的 GPU 串行队列使用，本次没有实现真正的排队。

候选归属按节点划分而不是全局池。`_candidates_for_node()` 用 `source_batch` 前缀筛出属于当前节点的候选，这样同一棵树上不同分支的候选不会互相污染彼此的判定。只有根节点在尚未打过标签时才回退到读全局池。

`propose_children(parent, critic_report, exclude, max_proposals)` 从父节点策略派生兄弟分支，并根据 Critic 报告里的 issue code 调整方向。默认提供三条：加大 route_B 预算、加大 route_C 预算、向 MDMX 倾斜。命中 `low_diversity` 时追加更长更多样的长度扫描；命中 `threshold_needs_review` 时追加要求先跑正对照的分支；命中 `duplicate_sequences` 时追加提高采样温度的分支。所有提案都会被 `branch_key` 去重并排除已试过的。

`adjust(report, tree, parent)` 把 Critic 结论落地：选一条新策略、写回 `state["design_budget"]`、递增 round、调 `EvidenceLogger.planner_adjust()` 记录 old → new 策略与触发事件 id，最后在树上生成子节点。预算耗尽或无未试分支时返回明确的 `budget_exhausted` / `no_untried_branch` 状态，不静默失败。

`State.update()` 是浅合并，直接传嵌套字典会整体替换。`adjust()` 因此先 `State.load()`、改字段、再 `State.save()`。

## 五、Critic 的判据选择

`agents/critic.py` 的 `review()` 先做五项不依赖生物知识的工程检查：候选池是否为空、序列重复率与唯一性、是否缺 `manifest_path`、是否有候选尚未评分、阈值是否已标定。

然后对已评分候选逐条调用 `evaluate_battery()`，统计 `metric_clearance` 的通过与失败数。

**判据走 `metric_clearance`，不走 `competition_clearance`。** 这一点需要专门说明。`evaluate_battery()` 返回两个不同的通过标志：`metric_clearance` 表示七层数值都达标，`competition_clearance` 额外要求每个阈值都有出处（`calibration_status` 为 calibrated，或 `evidence_grade` 属于 paper_explicit 等档位）。而 `agents/research.py` 第 76 至 108 行写入 state 的九个阈值，`calibration_status` 全部是 `pending`。也就是说在正对照标定完成之前，`competition_clearance` 必然恒为 False，不管候选跑得多好。

如果 Critic 拿 `competition_clearance` 当判据，流程会永远无法收敛，而且失败原因会被误读成「候选质量不行」。现在的处理是：`metric_clearance` 决定 verdict，阈值未标定作为 `threshold_needs_review` 这条 advisory issue 单独列出，不阻断流程但始终可见。

Verdict 到 orchestrator 动作的映射：

| verdict | 触发条件 | orchestrator 动作 |
|---|---|---|
| `done` | 有候选达到 metric_clearance | 标 `passed`，停止 |
| `advance` | 部分清关 | 在当前节点下加更精细的子节点，走深 |
| `backtrack` | 无清关，或缺评分 / 缺 manifest | 退回父节点，换未试过的兄弟 |
| `dead_end` | 候选池为空 | 同上，并标记该分支为死路 |

缺评分和缺 manifest 判 `backtrack` 而不是 `done`，是为了防止不完整的上游产出被当成合格结果放行。

## 六、可溯源性如何接线

三条链路把节点、候选和证据双向连起来，且都没有改动共享 schema。

候选侧：`source_batch` 写成 `{node_id}/{route}/L{length}`，例如 `N0003/route_C/L10`。`source_batch` 是 `INDEX_COLUMNS` 里已有的自由文本列，因此 69 列 schema 一列未动，不需要三方签字。从任意一条候选可以直接读出它由哪个节点产生。

证据侧：所有事件调用都传 `round_num = node["round"]`。为此在 `data_layer.py` 里给 `EvidenceLogger.critic_review()` 和 `planner_adjust()` 各加了一个默认为 `None` 的 `round_num` 参数。这是本次对共享数据层的唯一改动，10 行，向后兼容，既有调用方不受影响。

决策侧：回退时 `planner_adjust` 的 `trigger_event_id` 指向触发它的那条 `critic_review` 的 `event_id`，同时新节点的 `trigger_event_id` 记同一个值。完整链条是 `critic_review 事件 → planner_adjust 事件 → 新节点 → 该节点的候选`。dry-run 验证中两条 `planner_adjust` 的 `trigger_event_id` 都能在 `critic_review` 的 event_id 集合里找到。

## 七、Orchestrator 入口

`scripts/run_pipeline.py`（558 行）是调度入口，支持 `--dry-run`、`--max-nodes`、`--beam-width`、`--max-steps`、`--resume`。

dry-run 使用确定性 mock adapter，用 SHA-256 派生固定序列，伪造一小批候选和分数，让整条回溯循环在没有 GPU、没有 LLM、没有任何重型工具的环境下可以完整演示。真实 adapter 后续换成 `agents.design` 和 `agents.prediction`，`_execute()` 的接口不变。非 dry-run 模式目前会主动抛错说明适配器未接入，而不是假装跑通。

一次完整 dry-run 的实际输出（`--max-nodes 8 --beam-width 2`）：

| 步骤 | 节点 | 结果 |
|---|---|---|
| 1-2 | N0001 根节点，默认策略 | 4 条候选，metric_clearance_pass=0，verdict=backtrack |
| 3 | N0002，boost_route_B 分支 | 检出 duplicate_sequences，verdict=backtrack，标 dead_end 退回 N0001 |
| 4 | N0003，boost_route_C 分支 | metric_clearance_pass=2，verdict=done |

最终 4 步收敛，3 个节点，最优分支 N0003。`--resume` 从 `search_tree.json` 恢复后直接接在 N0003 上单步跑完，确认检查点恢复可用。

## 八、测试与验证命令

新增两个测试文件，共 18 项，全部无需 GPU 和生物工具：

```bash
python3 -m unittest -v test_search_tree.py test_planner_critic.py
```

`test_search_tree.py`（9 项）覆盖根节点初始化、回退到父节点后展开新兄弟、重复策略被拒、beam 宽度裁剪、`max_nodes` 触顶抛错、`restore` 恢复检查点且不破坏子节点数据、持久化后重新加载、`branch_key` 顺序无关、`pick_next` 跳过死节点。

`test_planner_critic.py`（9 项）覆盖空 state 排 research、有 research 无候选排 design、有评分排 critic、池空被报出、重复序列被报出、缺 manifest 与未评分同时被报出且判 backtrack、阈值 advisory、`propose_children` 排除已试分支、`adjust` 写出带 `trigger_event_id` 的 `planner_adjust` 证据。

回归情况：

```bash
python3 test_data_layer.py                              # 180/180
python3 -m unittest -v test_reliability_regressions.py  # 7/7（需要 biotite）
```

数据层 180 项全通过，确认 `round_num` 参数的加入没有破坏任何既有用例。Research/Design 可靠性回归 7 项全通过。注意 `数据层使用手册.md`（第 601、640 行）和 `v5可靠性修复说明_人类可读版.md`（第 95 行）里写的「161 项」已经过时，当前数据层测试数量是 180，增长来自本次之前的其他工作。

`test_reliability_regressions.py` 需要 `biotite`，本次为跑通它在当前 Python 环境安装了 `biotite 1.6.0`。这不是仓库改动；服务器上仍应按 README 使用项目 `.venv`。

## 九、本次没有做的事

调度层跑通不等于计算管线跑通。以下内容仍然待办：

真实 adapter 没有接入。dry-run 的分数是确定性编造的，只用来验证控制流，任何情况下都不能当作候选质量证据。`agents/prediction.py` 仍是 407 行的 TODO 骨架，七层字段齐备但真实打分工具未实现。

GPU 串行队列没有实现。Planner 产出的任务带 `needs_gpu` 标记，但实际排队和串行执行留给 real adapter 阶段。单 GPU 条件下同时启动多个 RFdiffusion / AfCycDesign 任务仍会 OOM，这个约束目前靠人遵守。

正对照标定没有做，因此 `competition_clearance` 仍然恒为 False。这是当前项目形式上最硬的一道卡口：在标定完成之前，没有任何候选能算作正式清关。Critic 会持续把它报成 `threshold_needs_review`。

搜索策略本身还很朴素。`propose_children` 目前是规则驱动的固定模板加 issue 触发的追加分支，没有任何基于历史节点表现的学习或打分排序。beam 裁剪按「保留最近 k 条」而不是「保留最好 k 条」，因为节点质量的可比指标要等 Prediction 真实接入后才有意义。

## 十、文件清单

新增：

- `agents/search_tree.py`（430 行）搜索树引擎
- `agents/__init__.py`（1 行）包声明
- `scripts/run_pipeline.py`（558 行）orchestrator 入口
- `test_search_tree.py`（223 行，9 项）
- `test_planner_critic.py`（190 行，9 项）

修改：

- `agents/planner.py`（13 行 TODO 骨架 → 332 行实现）
- `agents/critic.py`（15 行 TODO 骨架 → 260 行实现）
- `data_layer.py`（10 行：两个 logger 方法各加一个可选 `round_num` 参数）

共享 69 列 `INDEX_COLUMNS` schema、`evidence_schema.json` 和其他 agent 文件均未改动。
