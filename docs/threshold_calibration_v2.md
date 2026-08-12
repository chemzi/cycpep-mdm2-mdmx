# 阈值标定 v2 契约与科学可信度（P0-C）

本文档对应 v3 冲刺方案 P0-C「阈值标定与科学可信度」（D1–D4）与 OpenSpec change
`p0c-threshold-calibration`，说明 control dataset 契约、核心标定范围、正式持久化语义、
软偏好视图与硬清关的区分，以及从对照数据集到标定阈值的完整接入流程。

## 1. 控制数据集契约 v2（D1）

- 数据集声明 `schema_version`，当前标定产出为 `2`；v1 数据集保持可加载（legacy 行为，
  不强制逐条 provenance）。
- v2 数据集要求顶层绑定字段：`project_id`、`approved_digest`、`protocol`/`protocol_hash`、
  `schema_version`，缺失即被 `ControlDataError` 拒绝。
- v2 每条记录要求 `label`（`positive` / `negative`）、`role` 与 `source` 引用
  （`pdb_id` 和/或 `doi`）。缺 role 或缺引用的记录被拒绝，不会替换既有阈值。
- 绑定语义：交付的 manifest（如 `benchmarks/keap1/calibration/control_manifest_v2.json`）
  有意保持 provenance-only、不带 `project_id`/`approved_digest`/`protocol` 绑定，避免静默
  覆盖阈值；绑定在评分/标定时刻由服务端按当前批准的 project config 完成
  （`scripts/score_control_dataset.py` 产出绑定后的 v2 数据集）。

## 2. 核心标定范围（D2）

- 校准范围收敛为 `CALIBRATION_METRIC_KEYS`：`L2_ipsae`、`L4_nc_term_dist`、
  `L5_hotspot_coverage`、`L6_pose_rmsd`、`L7_scrmsd`（决定硬清关的核心 5 指标）。
- 其余指标（含 L1/L3 系列）保留文献/团队值，无论对照是否分离，`calibrate_thresholds`
  都不会用对照结果替换它们（audit 记为 `not_calibration_eligible`）。
- `calibrate_thresholds` 新增可选参数 `metric_keys`，默认即核心集合；调用方保持兼容。

## 3. 正式持久化语义（D3）

- 标定成功后在 `agents/research.py::_apply_control_calibration` 内：
  1. 写入标定审计产物 `_threshold_calibration.json`（`_cache_meta` + `source_metadata` + `audit`）；
  2. 注册 artifact（确定性 `artifact_id`，重跑幂等，INSERT OR IGNORE 去重）；
  3. 记录 `threshold_calibration` formal evidence 事件。
- 标定阈值写入 `_thresholds_cache.json`（Research 层 durable 恢复源），随后经
  `State.sync_thresholds_from_cache` 合并并通过 SQLite `replace_state` 事务路径更新
  `state.json`（后者是其持久化投影）。
- 因此正式记录 = artifact 行 + evidence 事件 + SQLite state；`_thresholds_cache.json`
  是 Research 层阈值恢复源，不是唯一正式记录。JSON 写与 artifact 注册不是单事务：
  注册失败只记 evidence、不影响标定（幂等注册缓解了重复执行）。

## 4. 软偏好视图与硬清关（D4）

- `calibration_status` 取值：`calibrated` / `pending` / `unavailable` / `not_separated`。
- `soft_desirability(candidate, thresholds, target_ids)` 提供只读软偏好视图，逐指标返回
  `{value, desirability, calibration_status, hard_eligible, reason}`；`hard_eligible`
  镜像电池判据（有值、有来源、已标定或可信证据等级）。
- 未标定指标只出现在软视图，绝不参与 `competition_clearance` 硬清关。
- NaN/Inf 指标不伪造分数：`_desirability` 对非有限值返回 `None`（desirability 不会是 1.0）。
- `target_ids=()` 时，target-scoped 指标（L2/L5/L6）显式标记
  `unavailable` + `missing_target_ids`，不会从视图消失。
- 命名区分：本视图是 per-指标「metric soft view」（0..1 比率）；P0-E shortlist
  的 per-候选「exploration desirability」（-1..1 边距）语义不同，前端展示请区分命名。

## 5. 对照评分与接入流程

1) 生成 manifest：`python -m scripts.prepare_keap1_controls`（确定性序列置换负对照，
   找不到完全错位置换时跳过该负对照，不产出近似 decoy）。
2) 用 prediction pipeline 对 manifest 中的对照计算核心指标值，产出 `scores.json`
   （需 GPU 服务器；`_control_value` 按 `METRIC_SPECS` 提取指标）。
3) 绑定 v2 数据集：`python -m scripts.score_control_dataset` 将 manifest + scores
   与当前批准的 project config 绑定（`project_id` / `approved_digest` / `protocol_hash`）。
4) 接入 Research：配置 `selection.calibration_controls_path`（或环境变量
   `CYCPEP_CONTROL_DATA`）指向绑定后的数据集，Research 流程内
   `_apply_control_calibration` 完成标定并走上述正式持久化路径。
5) 真实标定值由服务端产出；本仓库交付的是工程路径与数据资产（6 个实验阳性对照 +
   18 个 in-silico 负对照）。

## 风险与限制

- 负对照为 in-silico decoy，正式标定前需团队确认阴性集。
- 对照评分依赖 GPU 服务器，本地仅做语法/单元校验。
- 标定写入与 artifact 注册非单事务（崩溃可能留下「有 JSON 无 artifact 行」，已幂等化
  并记录错误）；如需强原子性，应另开 change 走 store 事务路径。

## E1 CalibrationBaseline publication

E1 增加的是发布与消费 authority，不改变上述 control 规则、标定算法、FPR、recall 或
样本量要求。阈值条目的算法状态（例如 `calibration_status=calibrated`）和发布 authority
是两个独立维度：

- `calibration_authority=simulation_only` 只允许显式标记为 synthetic/simulation 的 controls，
  用于验证完整工程链路；它不表示实验或科学验证，也不能转换或解释成 `approved_real`。
- `calibration_authority=approved_real` 拒绝 synthetic controls。现有 MDM manifest 继续是
  provenance-only；其中 presumed negatives 不构成真实 validated controls。

`CalibrationBaseline` v1 把批准项目、Prediction protocol、scoring implementation、control
dataset、threshold snapshot、calibration audit 与 calibration artifact 绑定。自然发布 ID
由这些 scientific/binding 内容确定：完全相同的重放返回幂等结果；相同 ID 但阈值、artifact
或其他绑定内容不同则 fail closed。

正式发布通过 Store 的单个 SQLite 事务完成 artifact 注册、active thresholds + binding 替换
以及 `threshold_calibration_published` Evidence 追加。`state.json` 仍只是 SQLite 状态投影，
不是并行 authority。Prediction 在构造 pipeline 前从 Store 读取 artifact 并验证项目批准、
protocol/scoring identity、threshold snapshot、artifact 行与文件内容；验证后的 exact binding
进入 cache identity、run manifest/summary/handoff、candidate record/metadata 和正式 Evidence。
任何 mismatch 或 tamper 都会在评分前拒绝。

E1 的 deterministic simulation fixture 仅标定 `L7_scrmsd`，目的是证明：simulation controls
→ existing calibrator → simulation-only artifact → atomic Store publication → exact Prediction
consumption。它没有为 MDM2/MDMX 建立真实 scientific calibration baseline。
