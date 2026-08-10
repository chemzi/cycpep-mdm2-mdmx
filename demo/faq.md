# 答辩 FAQ（初稿）

> 面向评委常见追问。原则：不宣称越跑越聪明、不伪造 scientific pass，只讲可验证的事实。

## 1. 为什么软视图满分但 hard clearance 没过？

软偏好视图（metric soft view / exploration desirability）是决策辅助，只做相对排序；hard clearance 是七层电池判定的二值结论，两者语义不同。NaN/Inf 数据曾被软视图误显为满分，已通过 `math.isfinite` 守卫修复并补测试；硬清关对 NaN 一律判失败。

## 2. 阈值是怎么标定的？

正负对照数据集（6 个实验正对照 PDB + 18 个 in-silico 负对照）+ `calibrate_thresholds` 按核心指标标定，输出带 provenance 的 control dataset，阈值经 SQLite 事务路径写入 state。未标定指标只参与 soft desirability，绝不参与 hard clearance。

## 3. in-silico 负对照集的科学性？

6 正 18 负的选择是作者声明的风险项，建议组会过一眼。代码层面：负对照由正对照序列确定性置换生成并打 `in_silico_sequence_negative_control` 标签，评分复用同一预测管线，不引入额外偏差。

## 4. 系统会"越跑越聪明"吗？

不宣称。可验证的闭环是：失败证据 → 结构化失败原因 → 下一轮生成偏好改变 → Evidence 记录变化原因。三轮成功率不下降不能作为学习证明，只能作为 demo observation。

## 5. 协议化可复现怎么证明？

protocol name/version/sha256 → 参数变更 → artifact 复用被拒。协议注册表（`protocols/design_v1.json`、`prediction_v1.json`）与 artifact 溯源（`contracts/artifact.py`）已有工程基础；等真实候选/artifact 数据接入后即可在 UI 演示完整链路。

## 6. 演示数据为什么是旧的？

当前两轮数据用的是旧阈值（L2/L3/L6 暂定、L5 缺失），适合做流程证明，不适合做最终科学结论。P0-D 会用标定后阈值重跑小批量，产出 demo-quality 样本后再用于正式演示。

## 7. 换靶点要重新登记吗？

不默认强制。Target Bootstrap / structure resolution 支持直接换靶；Target Registry 只在真实换靶失败证据表明需要时才引入（evidence-driven）。

## 8. 演示环境怎么启动？

后端读模型一条命令：`python demo/scripts/verify_demo_stack.py`；完整栈：`cd web-gui && .\start-local.ps1`（见 `demo/README.md`）。