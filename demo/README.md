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

- 读模型：`/api/v2/workbench` 可跑，schema `frontend.workbench.v2`。
- 证据：61 条 evidence（5 条 threshold_calibration + 39 tool_call + 16 error），仅 2 条带项目绑定进入工作台视图。
- 候选：0；artifact / transaction：0；protocol 绑定产物：0。
- 结论：当前是**流程证明级数据**，适合演示管线与交互，**不适合作为最终科学结论**。真实样本依赖 P0-D（标定后阈值重跑小批量）。

## 答辩故事（方案 v3 §15 五段）

1. **Scientific credibility**：真实靶点 + 候选 + 可追溯评估。
2. **Agent decision intelligence**：evidence → Planner → typed action → execution → transaction → 新 evidence → 下个决策。
3. **Failure-aware exploration**：hard clearance 全灭时仍能 Pareto + relative ranking → 下一轮 shortlist，但不伪造 scientific pass。
4. **Reproducibility**：每个 artifact 有 protocol / version / hash / trace，可审计、可复现。
5. **Productization**：Frontend V2 直接展示全链路，而不是只看到后台代码。

## Roadmap

- [x] 读模型链路验证 + 演示骨架（当前 PR）
- [ ] PR #55（workbench UI）合入后：candidate detail / Evidence timeline / transaction provenance 页面截图与录屏
- [ ] P0-D 真实小批量：标定后阈值重跑，产出 demo-quality 样本
- [ ] Protocol trace 奖励分演示：绑定 artifact 的 protocol name/version/sha256 链路
- [ ] FAQ 定稿 + 演示彩排