## Context

- `web_api/server.py` 提供只读 `/api/v2/workbench`（schema `frontend.workbench.v2`），`web-gui/start-local.ps1` 一键启动 adapter + UI。
- 本地数据基线：`data/store.db`（61 条 evidence 事件、0 候选、0 artifact）、`protocols/design_v1.json`、`protocols/prediction_v1.json`。旧 schema store 已通过仓库自带 `migrate_json_to_sqlite` 重建（本机维护操作，不入库）。
- 读模型当前诚实返回 `blockers=[no_current_run]`，`candidates/protocols/artifacts` 为空——流程证明级数据，非最终科学结论。

## Goals / Non-Goals

**Goals：**
- 一条命令可验证演示栈（后端读模型）并生成快照。
- 把答辩叙事、FAQ、数据限制写成仓库资产，供组内共用。

**Non-Goals：**
- 不动后端/前端生产代码；不造假数据；不做 UI 截图（等 #55）。

## Decisions

1. 验证脚本只依赖 Python 标准库 + `web_api`，不依赖 npm/GPU，任何机器可跑。
2. 快照写入 `demo/snapshot/` 并 gitignore，避免把易过期的本地数据提交进仓库。
3. 演示叙事与 FAQ 写入 `demo/`，与 openspec 分开管理（openspec 管范围/任务，demo 管答辩资产）。