## 1. 演示包骨架

- [x] 1.1 `demo/README.md`：one-command startup、答辩叙事、诚实清单、roadmap
- [x] 1.2 `demo/faq.md`：FAQ 初稿
- [x] 1.3 `demo/scripts/verify_demo_stack.py`：起读模型、拉 workbench、校验 schema、输出快照
- [x] 1.4 `.gitignore` 忽略 `demo/snapshot/`

## 2. 验证

- [x] 2.1 本地实跑 `verify_demo_stack.py`，workbench v2 + results v1 均返回正常、快照生成
- [x] 2.2 `scripts/architecture_gate.py` 零新违规

## 3. Results digest（并入演示包）

- [x] 3.1 `web_api/results.py`：只读 results 读模型（硬清关汇总、finalists、layer 统计、阈值计数、结论；NaN/Inf 视为缺失、demo_fixture 显式标注）
- [x] 3.2 `web_api/server.py` 暴露 `/api/v2/results`（schema `frontend.results.v1`）
- [x] 3.3 后端测试 `test_results.py`（9 个用例：空态/排序/NaN/阈值/per-target/pending/real/trace）
- [x] 3.4 前端 `web-gui/app/workbench/results-*.ts(x)`：契约校验 client + Results digest 面板接入 workbench 页
- [x] 3.5 前端测试：results-client（5 用例）+ results-render（3 用例）
- [x] 3.6 `verify_demo_stack.py` 同时校验 `/api/v2/results`
- [x] 3.7 `exploration.split_layer_key` 公开化（替代私有 `_split_layer_key`，通过架构门禁私有导入检查）