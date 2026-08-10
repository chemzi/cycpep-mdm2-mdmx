# 产品化演示包（Demo Package）

> 与研发并行维护。目标：让评委直接看到「项目 → 任务 → Action → 执行 → Candidate → 科学评分 → Evidence → Artifact → 决策理由」，而不是只看后台代码（方案 v3 §11/§15）。

## One-command startup

后端读模型（不依赖 UI / GPU / npm）：

```powershell
python demo/scripts/verify_demo_stack.py
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
- 本地演示数据：`demo/snapshot/seed_demo_fixture.py` 可一键写入 4 个合成候选（C0101/C0102 硬清关通过、C0103 失败、C0104 待预测）+ 16 条 evidence + 4 artifact/transaction + 1 个绑定 run（仅写本地 `data/store.db` 与 `demo/snapshot/demo_run/`，不碰服务器/git）。运行后 results 页显示 `data_basis=demo_fixture`。
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
- [ ] 页面截图与录屏（UI 已就绪，待彩排时采集）
- [ ] P0-D 真实小批量：标定后阈值重跑，产出 demo-quality 样本（依赖服务器，未做）
- [ ] Protocol trace 奖励分演示：绑定 artifact 的 protocol name/version/sha256 链路（fixture 已带绑定，UI 已支持）
- [ ] FAQ 定稿 + 演示彩排