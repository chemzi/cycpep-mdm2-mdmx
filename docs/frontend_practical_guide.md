# CycPep Studio 前端实用指南

这份文档用于本地验收和连接 4090 服务器上的真实工作环境。前端只展示适配器返回的真实状态，不会用示例候选或假进度填充空白。

## 1. 启动服务

推荐使用仓库中的 `web-gui/start-local.ps1`。它会自动寻找能够导入 `paramiko` 的 Python，启动本地 JSON 适配器 `127.0.0.1:8765`，并启动 CycPep Studio `127.0.0.1:4173`。

如果脚本提示缺少依赖，在同一个 Python 环境安装：

```powershell
python -m pip install -r requirements.txt
```

浏览器打开 `http://127.0.0.1:4173/`。适配器健康检查地址是 `http://127.0.0.1:8765/api/v1/health`，返回 HTTP 200 且 `data.status` 为 `ok` 才表示适配器已启动。

## 2. 连接本地项目

在“连接工作环境”中选择“本地项目”，API 地址填写：

```text
http://127.0.0.1:8765/api/v1
```

如果之前使用过 `localhost:8765`，刷新页面后前端会自动迁移到 `127.0.0.1:8765`。

## 3. 连接 SSH 服务器

选择“SSH 服务器”，填写服务器地址、SSH 端口、用户名、密码和服务器上实际存在的项目代码目录。密码仅保存在当前适配器进程内存中。

登录成功后，页面会读取远程项目列表和运行状态。适配器不会把远程坐标路径当成本地文件读取；坐标仍由远端工作流负责验证和产出。

如果出现 `Failed to fetch`：

1. 确认浏览器打开的是 `127.0.0.1:4173`；
2. 确认 `8765/api/v1/health` 返回 200；
3. 强制刷新页面；
4. 重新登录 SSH。适配器重启后旧的 `connection_id` 会失效。

## 4. 新建项目和靶点解析

连接 SSH 后点击“新建项目”，输入 Gene、UniProt 或 PDB 标识。服务器会创建草稿并执行靶点解析、结构候选发现和审核检查。

如果看到 `ambiguous_identifier_requires_user_selection`，不需要更换靶点，而是要在候选列表中选择明确身份。优先选择唯一的 canonical UniProt 编号，例如 CXCR4 应确认 `P61073`，不要只选择可能重复的基因名。点击“选择此候选并继续”后，草稿会重新审核；阻塞项清空后才能批准。

## 5. 批准与完整运行

“批准并完整运行”会依次启动：

```text
Research → 结构准备 → Design → Prediction → Critic
```

底部的“运行日志”和“GPU 队列”展示真实阶段、进程和错误。Research 成功不等于整个工作流成功；结构准备、Design 或 Prediction 仍可能单独阻塞。

## 6. 结构链审核

如果看到：

```text
StructureNotReadyError: cannot identify one receptor chain ... tied chains=['A', 'B']
```

表示选中的 PDB 有多条并列受体链，系统拒绝自动猜链。需要在结构审核阶段明确选择受体链，或选择链身份明确的结构候选，再重新物化坐标。不要为了绕过闸门直接强行写入链名。

## 7. 日志和失败定位

`execution_task_failed` 和 `orchestrator_task_failed` 通常是汇总事件，第一条真实异常才是根因。Research 的 RCSB 失败要检查网络、HTTP 状态、JSON 解析和目标 UniProt；Research 成功但结构准备失败要检查 PDB、链、表位和坐标物化。GPU 使用量为 0 通常表示尚未进入 Design/Prediction。

服务器端每个项目的关键日志位于：

```text
data/projects/<project_slug>/autopilot/autopilot.log
data/projects/<project_slug>/autopilot/autopilot_status.json
data/projects/<project_slug>/evidence_log.jsonl
```

完整工作流还会在 `autopilot/sessions/` 和 `autopilot/execution/` 下保存计划、任务尝试、进程日志和失败原因。

## 8. 常见状态

| 页面状态 | 含义 |
|---|---|
| 未连接 | 尚未拿到真实数据源 |
| Research 运行中 | 正在检索结构、文献或提取知识 |
| 需要先处理 | 草稿审核闸门仍有阻塞项 |
| 结构未就绪 | 坐标、链、表位或 hash 尚未满足 Design 条件 |
| degraded | 使用了明确记录的降级路径，不等于完整成功 |
| failed | 当前阶段未满足完成契约，需要查看第一条真实异常 |

## 9. 验收清单

- 能打开 `127.0.0.1:4173`；
- `8765/api/v1/health` 返回 200；
- 本地项目可以加载真实 snapshot；
- SSH 可以读取远程项目列表；
- 新建项目可以生成草稿；
- 多候选身份可以选择 canonical UniProt；
- 审批前所有 blocking issue 已清除；
- Research、结构准备、Design、Prediction、Critic 的状态和日志可追踪；
- 失败时能在 evidence 日志中找到第一条根因，而不是只看到汇总错误。
