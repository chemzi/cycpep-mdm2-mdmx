# 七层筛选的最终决策规则（Screening Decision Policy）

本文定义七层指标电池（battery）评估完成后的**最终决策口径**：科学放行与探索排序分离的
双轨制。这是答辩附录级文档，所有规则均有已合并的代码与证据事件支撑。

适用基线：`integration/data-integrity-transaction` 主干（含 PR #44 / #48 / #50）。

---

## 1. 决策问题的拆分

七层筛选实际回答两个不同的问题，必须用两套规则分别回答：

| 问题 | 性质 | 决策规则 |
|---|---|---|
| 这个候选算不算"通过"？ | 科学判定，需要**绝对标准** | 硬门槛（hard clearance，§3 R1） |
| 没通过的一堆里，下一轮该探索谁？ | 探索决策，只需要**相对标准** | 软评分 + Pareto shortlist（§3 R2–R4） |

用一套规则同时回答两个问题，会落入两个已知陷阱：

- **一刀切全灭**：绝对标准兼做排序依据时，整批 0/N passed，系统不给任何下一步方向；
- **flag-but-pass**：为避免全灭而"每层至少放行 K 个、标记警告仍算通过"——这会让
  not-passed 的候选进入通过集，科学语义造假。该做法已在终期方案 v3 中明确否决，
  本系统不采用。

## 2. 方案对比与选择

| 方案 | 做法 | 结论 |
|---|---|---|
| **A. 双轨制**（本系统采用） | 已标定指标走 hard clearance；未标定指标只进 soft desirability；批次级用 Pareto front + desirability 排名输出探索 shortlist | ✅ 采用。两个问题各得其所；结论的软/硬由 `calibration_status` 透明标注 |
| B. 纯 MPO 加权打分 | 全部层映射 0–1 满意度加权合成，按分放行 top-N | ❌ 否决。权重来源无法答辩；废除 pass/fail 等于放弃科学通过语义 |
| C. 每层保底放行 K 个 | 不足 K 个时降级为 flag-but-pass | ❌ 否决（方案 v3 明文取消）。伪造科学通过，证据链留痕后更被动 |
| D. 多目标贝叶斯优化 | qEHVI 采集函数驱动下轮候选 | ❌ 现阶段不采用。终期时间内难以做到稳定、可审计，且会丢失"确定性规则非黑盒"的既有评审优势；列为后续方向 |

## 3. 决策规则（正式定义）

- **R1 硬门槛**：`battery_evaluation.evaluate_battery` 的 l1–l7 判定语义不变。
  每个阈值的出处状态由 `threshold_audit` 记录：`calibrated`（正负对照标定，
  PR #48 链路）为硬结论；`pending`/`team_provisional`（文献/暂定值）为软结论，
  展示时必须区分。
- **R2 软评分**：`exploration.desirability` 对每个候选给出 [-1, 1] 的连续分
  （各指标距阈值的归一化边距均值，per-target override 经 `threshold_for_target`
  解析）。证据不足时返回 None，不伪造分数。
- **R3 Pareto front**：`battery_evaluation.compute_pareto_front` 计算多目标非支配
  集合（方向取自 `threshold_calibration.METRIC_SPECS`，阈值缺失时仍可用）。
  互不支配的候选全部保留。
- **R4 探索 shortlist**：`exploration.exploration_shortlist` 合成 top-k
  （front 成员优先 → desirability 降序 → partial_evidence 兜底），每条附入选理由。
  **入选者永远保持其 battery 原始判定（全灭场景全部为 not passed）**；
  shortlist 是"下一轮最值得探索谁"，不是任何形式的通过。
- **R5 标定边界**：`calibration_status` 非 calibrated/validated/complete 的指标
  只参与 R2–R4 的软排序，绝不影响 R1 硬门槛。每次 shortlist 的
  `calibration` 汇总字段记录所消费阈值的软硬分布。

## 4. 服务器操作口径（筛选 runbook）

1. **阈值**：KEAP1 基准走标定接通的 calibrated 阈值；MDM2/MDMX 使用文献
   pending 阈值。软硬状态由系统自动标注，无需人工区分。
2. **跑批**：预测管线照常执行，每个候选自动产生 `battery_evaluated` 结构化
   证据事件（含七层值、失败层、通过状态）。
3. **收决策**：
   - 存在 hard-pass → 通过者即正式候选，证据链（battery 事件 + artifact +
     protocol binding）可直接展示；
   - 全灭或需补充 → 执行
     `python scripts/exploration_report.py --targets <靶标> --k 5 --round <轮次>`，
     获得带理由的 shortlist，并落库 `exploration_shortlist` 证据事件
     （envelope 含 targets/round，payload 含 source_event_ids 精确溯源）。
4. **下轮生成**：shortlist 与经验偏好（`experience_applied` 事件，PR #44）
   共同驱动下一轮 design——"失败证据 → 策略改变"的可验证闭环。
5. **禁止事项**：不得为避免全灭而临时调宽阈值或手工放行。阈值出处状态在
   证据链中留痕，临时改动会被审计发现。

## 5. 答辩口径

- 推荐表述："绝对标准负责科学可信度，相对多目标排名负责失败后的探索决策。"
- 引用 co-scientist 时只说"相对排名负责探索"，**不说**"相对排名替代科学判定"。
- 被问"全灭怎么办"：展示 `0/N passed` + shortlist 的真实输出，强调入选者
  仍标注 not passed——这是 failure-aware exploration，不是保底放行。

## 6. 参考文献与成熟工程参照

1. Derringer, G., & Suich, R. (1980). Simultaneous optimization of several
   response variables. *Journal of Quality Technology*, 12(4), 214–219.
   —— desirability function 方法的原始文献，R2 软评分的理论基础。
2. Wager, T. T., et al. (2010). Defining desirable central nervous system drug
   space through the alignment of molecular properties, in vitro ADME, and
   safety attributes. *ACS Chemical Neuroscience*, 1(6), 435–449.
   —— Pfizer CNS MPO，药物化学中最知名的多参数满意度打分落地。
3. Deb, K., Pratap, A., Agarwal, S., & Meyarivan, T. (2002). A fast and elitist
   multiobjective genetic algorithm: NSGA-II. *IEEE Transactions on Evolutionary
   Computation*, 6(2), 182–197. —— Pareto 非支配保留语义的经典来源（R3）。
4. Gottweis, J., et al. (2025). Towards an AI co-scientist. arXiv:2502.18864.
   —— tournament 演化 + 相对排名筛选科学假设，"相对排名负责探索"的直接背书。
5. Daulton, S., Balandat, M., & Bakshy, E. (2020). Differentiable expected
   hypervolume improvement for parallel multi-objective Bayesian optimization.
   *NeurIPS 2020*；及 Meta 开源 Ax / BoTorch 平台。
   —— 多目标实验闭环的工业级成熟范式（方案 D 的参照，本系统列为后续方向）。

## 7. 代码锚点

| 规则 | 实现 | 证据事件 |
|---|---|---|
| R1 硬门槛 | `battery_evaluation.py::evaluate_battery` | `battery_evaluated` |
| R2 软评分 | `exploration.py::desirability` | （并入 shortlist） |
| R3 Pareto | `battery_evaluation.py::compute_pareto_front` | （并入 shortlist） |
| R4 shortlist | `exploration.py::exploration_shortlist` | `exploration_shortlist` |
| R5 标定边界 | `threshold_contract.py`（calibration_status 语义） | payload `calibration` 汇总 |
| 经验闭环 | `experience.py`（PR #44） | `experience_applied` |
| 阈值标定 | `threshold_calibration.py`（PR #48） | `threshold_calibration` |

规范来源：`openspec/specs/evaluation/exploration-shortlist/spec.md`。
