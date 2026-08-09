## Why

P0-C「阈值标定与科学可信度」来自 v3 冲刺方案：正式 threshold 尚未形成可信标定，直接影响答辩中“为什么这个 candidate 算通过？”的科学可信度。当前代码已有正负对照校准能力（`threshold_calibration.py` / `agents.research._apply_control_calibration`），但对照 v3 的 D1–D4 要求还有四个缺口：没有真实正负对照数据集；校准范围覆盖全部指标而非“决定生死”的核心 4–6 个；标定结果缺少正式可追溯记录（artifact 注册、formal evidence、事务路径）；未标定指标与 hard scientific clearance 的区分不显式。

## What Changes

- **D1 正负对照（新增）**
  - 落地正负对照数据集契约：复用现有 control dataset schema，纳入 `benchmarks/keap1` 的 6 个实验阳性对照（PDB 7K2E/7K2F/7K2G/7K2H/7K2I/7K2M，DOI 10.1021/jacs.0c09799），并要求每个对照带 provenance（来源、PDB、DOI、role）。
  - 新增对照评分脚本：复用现有 prediction pipeline 对对照集计算核心指标值，产出可直接喂给 `calibrate_thresholds` 的 control dataset。
- **D2 核心指标标定（修改）**
  - 将 `METRIC_SPECS` 的校准范围收敛为决定硬性清关的核心指标（约 4–6 个）；其余指标保留文献/团队值，不参与正负对照替换。
  - 校准配置显式声明指标 scope（global/target）与方向，删除隐式默认带来的歧义。
- **D3 Threshold persistence（修改）**
  - 标定结果通过正式可追溯记录持久化：标定审计产物（`_threshold_calibration.json`）注册到 `artifacts` 表（确定性 id 幂等），formal evidence 事件完整记录，阈值经 `_thresholds_cache.json`（Research 层 durable 恢复源）→ `State.sync_thresholds_from_cache` → SQLite `replace_state` 事务路径写入 `state.json`。
  - 正式记录 = artifact 行 + evidence 事件 + SQLite state；`_thresholds_cache.json` 不是唯一正式写入入口，JSON 写与 artifact 注册非单事务（注册失败仅记 evidence）。
- **D4 未标定指标（新增）**
  - 显式定义 `calibration_status` 语义（calibrated / pending / unavailable / not_separated 等），未标定指标绝不参与 hard scientific clearance 判定。
  - 提供未标定指标的 soft desirability / relative ranking 只读入口，并与 hard clearance 结果明确区分。

## Capabilities

### New Capabilities

- `scientific/threshold-calibration`: 正负对照数据集契约与 provenance、核心指标标定范围、标定结果经正式 Store/事务路径持久化、未标定指标的软偏好与硬清关区分。

### Modified Capabilities

- 无。`openspec/specs/` 下尚无阈值相关既有 capability。

## Impact

- 实现：`threshold_calibration.py`、`agents/research.py`、`data_layer.py`、`scripts/calibrate_thresholds.py`、新增对照评分脚本、`benchmarks/keap1`。
- 公开接口：`calibrate_thresholds` 保持兼容，新增可选参数 `metric_keys`（默认核心 5 指标）；control dataset schema 版本递增。默认标定范围收敛为核心指标属受控行为变更。**显式 BREAKING（§9 声明）**：`load_control_dataset` / `validate_control_metadata` 删除公开参数 `schema_version`（改为以数据集内声明的 `schema_version` 为准，仓库内调用方已全部更新）。
- 数据格式：control dataset schema 新版本要求 provenance 字段；阈值条目字段保持兼容。
- 遗留路径：`state.json` 是 SQLite 的持久化投影；`_thresholds_cache.json` 是 Research 层 durable 阈值恢复源；`_threshold_calibration.json` 是注册为 artifact 的标定审计产物。
- 非目标：不实现 P0-E 的完整 Pareto/tournament；不重写预测/评分算法；不在本 change 内跑 GPU 对照评分（真实标定值由服务端产出）；不新增 hash/SHA256 机制。
