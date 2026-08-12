## Development workflow

- For a trivial, low-risk change, implement it directly and run relevant tests.
- Before implementing any non-trivial feature, behavior change, architecture change, or substantial refactor, create and approve an OpenSpec change.
- For an approved OpenSpec change, use `$openspec-multi-agent` when multiple tasks can benefit from parallel execution and:
  - their dependencies are satisfied;
  - their expected write sets do not materially overlap;
  - no unresolved shared contract or interface change exists between them.
  Otherwise, execute the tasks sequentially.
- Use Matt skills only as engineering aids for TDD, debugging, code review, architecture, and domain modeling. Do not use `to-spec`, `to-tickets`, `implement`, or `wayfinder` for OpenSpec-managed work.
- Treat OpenSpec artifacts as the single source of truth for the scope, requirements, design, tasks, and progress of an OpenSpec-managed change. Do not create a second spec, task list, implementation plan, or progress tracker.
- Before completion, run relevant tests, the full test suite when applicable, lint and type checks, OpenSpec verification when available, and code review.

### Workflow visibility

At the start of implementation work, briefly state the selected workflow:
- `direct` for a trivial change;
- `OpenSpec: <change>` for an OpenSpec-managed change;
- `OpenSpec + multi-agent: <change>` when safe parallel execution is selected.

Before declaring completion, report the verification gates actually run and their results. Do not claim completion without evidence.

### Repository remediation

For repository-remediation work, read `docs/engineering/remediation-strategy.md` for long-term engineering direction.

OpenSpec remains authoritative for the scope, design, tasks, and progress of each individual change.

After a remediation change is verified and archived, reassess the remaining known debt against the remediation strategy and select the next smallest high-value problem. Do not restart a full repository redesign or create a separate remediation task tracker.
如果这是编程任务时必须严格读取并遵守仓库里ENGINEERING_STANDARD.md的规范
    如果没有此规范提示用户并且给出规范的草稿，请求提交

用户偏好：
		1.用于审查的子智能体think level设置为high 
		2.当且仅当用户明确批准“现在进入全自动模式”时，则进入全自动模式，即：在smoke过程中发现的blocker自动拟定修改方案，可以不依赖用户通过子智能体修改并批准方案，代码审查后自动提交pr，达到p0=0 p1=0后留着pr不与远端合并，但是本地归档，并继续使用修改好的新代码继续下一轮测试——知道smoke全通，最后向用户回报测试全通，退出全自动模式。

另外：
		1. 我们不是一篇安全攻防论文，你有权力进行校验，但是禁止过度防御
		2. 禁止为了过度防御新增无必要的哈希校验；已有协议、artifact、完整性 contract 明确要求的 hash/SHA256 必须保留并遵循现有设计。 
		3. 禁止反复的基本不可能出现的case写防御 
		4. 需要rubric的地方不要过度机械化
