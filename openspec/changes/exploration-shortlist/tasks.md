## 1. 基线确认

- [x] 1.1 阅读 `ENGINEERING_STANDARD.md`、`AGENTS.md`、`battery_evaluation.py`、`threshold_contract.py`、`experience.py` 及 `test_data_layer.py` 中 `compute_pareto_front` 的行为基线测试，确认本 change 不修改其中任何既有行为。
- [x] 1.2 与 P0-C（阈值标定）确认 `calibration_status` 分级语义与本 change 的只读消费方式；与 P0-B（Frontend V2）同步 `exploration_shortlist` 事件结构。结论：P0-C 接口锁死（字段名/operator 集合不变，标定只更新数值）；P0-B 确认 payload 并提出 envelope 约束（targets/round 走 envelope 顶层、正式 run 必须带 trace_context、建议 source_event_ids provenance），已并入 design D1/D4 与 spec。

## 2. desirability 评分（纯函数）

- [x] 2.1 实现 `exploration.py` 的指标键解析（`_LAYER_KEY_TO_METRIC` 前缀映射 + `threshold_for_target` per-target override 解析）与边距计算（D2 三种 operator 分支 + threshold=0 退化）。
- [x] 2.2 实现 `desirability`：margin 截断 [-1,1] 后取均值；无可计算指标返回 None。
- [x] 2.3 测试：各 operator 方向、缺值指标跳过、无 threshold 条目跳过、全部缺失返回 None、截断边界、严格 operator 零阈值、per-target override 优先。

## 3. Pareto + shortlist 合成（纯函数）

- [x] 3.1 实现目标推导（方向取自 METRIC_SPECS）并复用 `compute_pareto_front` 计算 front。
- [x] 3.2 实现 `exploration_shortlist(events, targets, k)` 合成规则 D4（front 优先 → desirability 补足 → partial_evidence 兜底）；同 candidate_id 去重保留最新。
- [x] 3.3 测试：全灭批次仍输出 top-k 且全部 `passed=false`；front 成员优先入选；desirability 排序正确；证据为空时返回空 shortlist 不抛异常；按 targets 过滤；重复候选去重；unmapped_metrics 可见。

## 4. Evidence 事件落库

- [x] 4.1 `contracts/event.py` 白名单新增 `exploration_shortlist`。
- [x] 4.2 实现 `record_exploration_shortlist(...)`：targets/round/trace 经 envelope 参数写入（payload 不含 targets），payload 按 D4 含 `source_event_ids` 与 calibration 汇总。
- [x] 4.3 测试：事件结构（envelope targets/round 正确、payload 无 targets）、`source_event_ids` 与来源事件一致、calibration 汇总计数、追加写不改动既有事件、thresholds=None 走 State 的真实路径。

## 5. CLI 报告

- [x] 5.1 实现 `scripts/exploration_report.py`：打印 n_evaluated / n_passed / shortlist（含 desirability、pareto 标记、reason）与 calibration 汇总；`--k`、`--round`（写入 envelope）、`--dry-run`、`--json` 选项。
- [x] 5.2 冒烟：空证据库正常降级输出；合成 6 条 `battery_evaluated` 全灭证据跑出 shortlist 并记录事件；`--dry-run` 不记录。

## 6. 验证与收口

- [x] 6.1 回归：`test_data_layer.py`、`test_experience.py`、`test_prediction_transactional.py` 原样通过（证明 battery/阈值/事务行为未动）。
- [x] 6.2 全量 unittest + `scripts/architecture_gate.py` 0 violations。
- [x] 6.3 strict OpenSpec validation + 一次独立 code review（子 agent，只读）；review 发现 1 major + 4 minor 已修复（见第 7 节），nit 登记 known debt。
- [x] 6.4 演示观察记录：合成全灭批次产出 "0/N passed BUT exploration shortlist: ..." 输出（见 CLI 冒烟记录），作为答辩素材的初步证据（不宣称学习/进化结论）。

## 7. 独立 review 修复轮

- [x] 7.1（major）per-target threshold override 解析：`threshold_for_target` 接入 desirability 与 calibration 汇总，新增 override 优先/他靶标隔离测试。
- [x] 7.2（minor）unmapped 指标列入结果 `unmapped_metrics` 字段（additive payload 字段，不破坏 P0-B 契约）。
- [x] 7.3（minor）同 candidate_id 跨轮重评估去重（保留最新），补测试。
- [x] 7.4（minor）thresholds=None 走 State.load 的默认路径补测试；catch-all 降级保持与 experience.py 同一文档化约定。
- [x] 7.5（nit）threshold=0 退化分支尊重严格 operator，补测试。
- [x] 7.6（nit）spec 文本同步：Pareto 方向来源改为 METRIC_SPECS。
- [x] 7.7（nit）`_as_float` 与 experience.py 重复：登记 known debt，不在本 change 抽取。
