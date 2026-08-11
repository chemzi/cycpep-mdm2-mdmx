# 答辩 FAQ（定稿）

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

一条可验证链：artifact（文件 sha256）→ 产出任务 → protocol（name/version/integrity_identity），且 artifact 文件自身内嵌 `protocol` + `protocol_sha256`，脱离数据库也能自证来源。demo fixture 已按此绑定（design 2.1 / prediction 1.3 / critic 1.0 / calibration 1.2），一键校验：`python demo/scripts/verify_protocol_trace.py`——它对每个 artifact 重算磁盘 sha256 并与库内记录比对，全过才返回 0。真实数据接入后同一链路直接复用。

## 6. 演示数据为什么是合成的？

本地 fixture 是合成数据：七层裁决由真实 `evaluate_battery` 按当前 `state.json` 阈值实时计算（7 个有效标定项 + 8 个暂定项），C0101/C0102 真实通过全部七层、C0103 挂 L3/L4/L5/L6、C0104 待预测。它证明的是流程链路，不是最终科学结论；P0-D 会用标定后阈值重跑小批量真实样本，产出 demo-quality 数据后再用于正式演示。

## 7. 换靶点要重新登记吗？

不默认强制。Target Bootstrap / structure resolution 支持直接换靶；Target Registry 只在真实换靶失败证据表明需要时才引入（evidence-driven）。

## 8. 演示环境怎么启动？

后端读模型一条命令：`python demo/scripts/verify_demo_stack.py`（同时校验 workbench 与 results 两个读模型）；可复现链路：`python demo/scripts/verify_protocol_trace.py`；完整栈：`cd web-gui && .\start-local.ps1`（见 `demo/README.md`）。

## 9. 工作台和结果页有什么区别？

工作台（`/api/v2/workbench`）是证据链视角：任务图、执行、transaction、candidate workspace、evidence timeline、artifact trace。结果页（`/api/v2/results`，页面顶部 "Results digest"）是科学结论视角：硬清关汇总、finalists 排名、每层通过率、阈值标定计数与一句话结论。两者共用同一只读存储，`data_basis` 字段明确标注当前行是 `demo_fixture`（合成）还是 `real`（真实），页面结论也明确写"这是流程演示，不是最终科学结论"。

## 10. 演示数据是怎么来的？会不会污染真实结果？

`demo/scripts/seed_demo_fixture.py` 只写本地 `data/store.db` 与 `demo/snapshot/demo_run/`，所有行都带 `demo_fixture=true` 标记或 `DEMO` 前缀；重跑会先删除上次的 fixture 行。它不碰服务器、git 或任何公共资源。结果页通过 `data_basis=demo_fixture` 显式标注，不会被误认为真实科学结论。