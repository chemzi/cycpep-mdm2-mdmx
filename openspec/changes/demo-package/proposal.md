## Why

终期答辩最大风险不是工程不够，而是缺少"真实证据 + 可讲创新 + 可演示产品"。方案 v3 §11 要求演示包与研发并行：demo snapshot、one-command startup、protocol trace、candidate detail、Evidence timeline、transaction/artifact provenance、screenshots、recording、FAQ。本 change 建立演示包骨架：可复现的启动/验证脚本、读模型链路核查、FAQ 与演示叙事，作为后续 UI 截图与真实样本接入的底座。

## What Changes

- 新增 `demo/README.md`：演示包总览、one-command startup、§15 五段答辩故事、当前数据基线诚实清单、roadmap。
- 新增 `demo/faq.md`：答辩 FAQ 初稿（软/硬判定分离、阈值标定与负对照、协议化可复现、learning 边界、旧阈值数据说明）。
- 新增 `demo/scripts/verify_demo_stack.py`：一键验证后端读模型——启动 `web_api/server.py`，拉取 `/api/v2/workbench`，校验 `schema_version`，打印摘要并把快照写入 `demo/snapshot/`。
- `.gitignore` 追加忽略 `demo/snapshot/`（运行时产物）。

## Non-goals

- 不修改 `web-gui/` 生产代码；UI 截图/录屏等待 PR #55（workbench UI）合入后接入。
- 不伪造演示数据：快照只取自真实读模型输出；候选/协议绑定数据依赖 P0-D 真实小批量或明确标注的 fixture。
- 不改读模型/后端协议。

## Capabilities

### New Capabilities

- `demo/stack-verification`: 一条命令验证前端读模型链路并生成工作台快照。

### Modified Capabilities

None.

## Impact

- 新增文件：`demo/`（README、FAQ、scripts、snapshot 目录）、`openspec/changes/demo-package/`。
- `.gitignore` 追加一行。
- 无 public interface 破坏、无数据格式迁移、无后端行为改动。