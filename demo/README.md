# 产品化演示包（Demo Package）

> 与研发并行维护。目标：让评委直接看到「项目 → 任务 → Action → 执行 → Candidate → 科学评分 → Evidence → Artifact → 决策理由」，而不是只看后台代码（方案 v3 §11/§15）。

## One-command startup

后端读模型（不依赖 UI / GPU / npm）：

```powershell
python demo/scripts/verify_demo_stack.py        # workbench + results 两个读模型
python demo/scripts/verify_protocol_trace.py    # artifact -> task -> protocol -> sha256 可复现链路
python demo/scripts/stress_demo_data.py --n 1500  # 大数据量压测（结束后自动恢复 fixture）
```

完整栈（adapter + 前端 UI，需 Node）：

```powershell
cd web-gui
.\start-local.ps1
```

浏览器打开 http://localhost:3000，连接页填 http://127.0.0.1:8765（same-host 模式）。

## 当前数据基线（诚实清单）

- 读模型：`/api/v2/workbench`（schema `frontend.workbench.v2`）与 `/api/v2/results`（schema `frontend.results.v1`）都可跑；`verify_demo_stack.py` 一条命令同时校验两者。
- 结果页：前端 workbench 顶部新增 **Results digest** 面板——硬清关汇总、finalists 排名、七层统计、阈值标定计数与结论，数据全部来自只读读模型，不伪造分数。
- 本地演示数据：`python demo/scripts/seed_demo_fixture.py` 可一键写入 4 个合成候选 + 16 条 evidence + 4 artifact/transaction + 1 个绑定 run（仅写本地 `data/store.db` 与 `demo/snapshot/demo_run/`，不碰服务器/git）。七层裁决由真实 `evaluate_battery` 按当前阈值实时计算、不硬编码：C0101/C0102 指标真实通过全部七层、C0103 多项不达标（L3/L4/L5/L6）、C0104 待预测（结果页显示为 pending 而非 passed）。运行后 results 页显示 `data_basis=demo_fixture`。
- 可复现链路：每个 artifact 都有磁盘 sha256，且内嵌 `protocol` + `protocol_sha256`；`verify_protocol_trace.py` 对全部 4 个 artifact 重算哈希并与库内记录比对（design 2.1 / prediction 1.3 / critic 1.0 / calibration 1.2）。
- 大数据量表现（本地压测）：1500 条候选时 workbench 读模型约 55ms、results 读模型约 486ms；3000 条时约 113ms / 983ms；前端集合按 100 条截断显示（`total=… returned=… truncated`），读模型不做全量回传。
- 结论：当前是**流程证明级数据**（合成 fixture 或旧阈值真实数据），适合演示管线与交互，**不适合作为最终科学结论**。真实样本依赖 P0-D（标定后阈值重跑小批量）。

## 答辩故事（方案 v3 §15 五段）

1. **Scientific credibility**：真实靶点 + 候选 + 可追溯评估。
2. **Agent decision intelligence**：evidence → Planner → typed action → execution → transaction → 新 evidence → 下个决策。
3. **Failure-aware exploration**：hard clearance 全灭时仍能 Pareto + relative ranking → 下一轮 shortlist，但不伪造 scientific pass。
4. **Reproducibility**：每个 artifact 有 protocol / version / hash / trace，可审计、可复现。
5. **Productization**：Frontend V2 直接展示全链路，而不是只看到后台代码。

## Roadmap

- [x] 读模型链路验证 + 演示骨架
- [x] workbench UI 合入（PR #55）+ Results digest 结果页（`/api/v2/results`，含前端面板与契约测试）
- [x] 本地 demo fixture（`seed_demo_fixture.py`）跑通完整结果链路
- [x] Protocol trace 奖励分演示：`verify_protocol_trace.py` 校验 artifact → task → protocol → sha256 全链路，4 个 artifact 全部通过
- [x] 大数据量前端验证：`stress_demo_data.py`（1500/3000 条压测，读模型线性扩展、截断语义正确）
- [x] FAQ 定稿（`demo/faq.md`，10 问）
- [ ] 页面截图与录屏（UI 已就绪，待彩排时采集）
- [ ] P0-D 真实小批量：标定后阈值重跑，产出 demo-quality 样本（依赖服务器，未做）