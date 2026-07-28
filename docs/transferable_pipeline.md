# 可迁移环肽 Binder 流程

MDM2/MDMX 是本仓库的参考项目，不是评估器中的特殊靶点。新项目通过
`CYCPEP_PROJECT_CONFIG` 指向一个 JSON 配置。最小输入先由 Target Bootstrapper
补全成草稿，用户检查、修改并显式批准后，Research/Design Agent 才能启动。

## 启动前自动补全与人工闸门

配置 LLM（兼容 OpenAI chat-completions 接口）：

```powershell
$env:OPENAI_API_KEY='...'
$env:OPENAI_BASE_URL='https://api.openai.com/v1'
$env:LLM_MODEL='step-3.7-flash'
```

从 gene、UniProt 或 PDB 生成草稿。Bootstrapper 先用 UniProt/RCSB 确认身份，
再让 LLM 仅依据返回的 evidence ID 补全功能、机制、结合位点假设、检索词和不确定性：

```powershell
python -m target_bootstrap draft --identifier NOVEL1 --type gene --epitope "domain X pocket" --output projects/novel1.draft.json
python -m target_bootstrap show --draft projects/novel1.draft.json
```

修改使用 JSON Merge Patch，因此以后 UI 只需调用同一逻辑接口：

```powershell
python -m target_bootstrap edit --draft projects/novel1.draft.json --patch review_patch.json
python -m target_bootstrap approve --draft projects/novel1.draft.json --output projects/novel1.json
$env:CYCPEP_PROJECT_CONFIG=(Resolve-Path projects/novel1.json)
python -m agents.research
```

对应的 Python 接口是 `TargetBootstrapper.create_draft()`、`edit_draft()`、
`approve_draft()` 和 `assert_project_approved()`。审批会绑定配置内容摘要；审批后再手改
靶点、表位或结构会自动失效，必须重新检查。身份歧义属于阻断项；缺少表位或预测结构
属于明确警告，允许用户在知情后批准 Research，但 Design 仍需通过结构就绪检查。

## 结构来源和构象质量

`structure_resolution.resolve_project_structures()` 执行“实验结构优先，预测结构降级”：

- RCSB 实验结构按分辨率和复合物信息分级 A/B/C；
- 无可用实验结构时查询 AlphaFold DB，结合 pLDDT/PAE 分级 B/C/D；
- 预测结构或 C 级结构标记 `needs_ensemble`，不能被描述成已验证结合构象；
- 没有结构时返回 `prediction_required` 和 `required_next_step`，而非静默继续设计。
- 选中结构元数据后还要由后端落盘并校验坐标 artifact；只有 artifact、target chain 和
  表位残基都通过检查，`ready_for_design` 才为 true。
- target identity 或结构来源变化会使旧坐标 artifact 和身份相关研究内容失效，必须重新
  物化坐标并审核表位。

本阶段已经定义 `ExperimentalStructureProvider` 和 `PredictedStructureProvider` 接口；
后续接本地 AlphaFold 3、Boltz 或其他预测服务时只需实现 `find(target)`，不需要改
Bootstrapper、审批或 Design 闸门。当前实现负责公开结构发现、质量分级和任务状态，
尚不负责提交昂贵的本地结构预测作业。

## 两种决策不得混用

- `triage_status` 服务于研发漏斗。环闭合等物理硬失败返回 `invalid`；软证据缺失
  返回 `needs_more_evidence`；软指标未达到暂定线返回 `needs_optimization`。
- `competition_clearance` 服务于最终汇报。它要求七层指标同时通过，而且每条阈值
  必须有可审计来源或同协议对照校准。
- `all_layers_pass`/`metric_clearance` 只表示数值过线。暂定阈值即使数值过线，也不能
  让 `competition_clearance` 为真。

## 新靶点最小配置

```json
{
  "project_id": "novel_target_demo",
  "modality": "head_to_tail_cyclic_peptide",
  "objective": "binder",
  "targets": [
    {
      "id": "NOVEL-1",
      "uniprot": "P00001",
      "required": true,
      "structure": {"pdb_id": "0ABC", "chain": "A"},
      "binding_site": {"residues": [10, 20, 35], "source": "research_agent"}
    }
  ]
}
```

PowerShell：

```powershell
$env:CYCPEP_PROJECT_CONFIG='C:\path\to\novel_target.json'
python -m scripts.search_pdb
python -m agents.research
```

手写配置也必须带有效的 `review.status=approved` 和 `approved_digest`；推荐统一走上面的
草稿/审批接口，避免摘要计算或审计记录不一致。

非 MDM 项目使用独立 research cache，且不会在检索失败时回退到 MDM2/MDMX
热点或已知 binder。默认运行数据和 evidence 也会按 `project_id` 自动进入独立子目录，
避免切换靶点时读取上一个项目的 state 或候选。

## 通用候选指标

```json
{
  "candidate_id": "C0001",
  "sequence": "GFEWALAAK",
  "metrics": {
    "global": {
      "plddt": 0.92,
      "nc_distance_pre": 1.35,
      "nc_distance_post": 1.38,
      "scrmsd": 0.8
    },
    "targets": {
      "NOVEL-1": {
        "ipsae": 0.68,
        "dg": -15.3,
        "sc": 0.72,
        "dsasa": 580,
        "hotspot_cov": 0.85,
        "site_consistency": true,
        "pose_rmsd": 1.2,
        "seed_convergence": 0.8
      }
    }
  }
}
```

`CandidateIndex` 将这部分保存到 `metrics_json`，旧 MDM2/MDMX 展示列继续兼容。

## 没有文献阈值时

使用 `threshold_calibration.calibrate_threshold()` 输入同一工具、同一版本、同一协议
得到的正负对照。它选择满足经验假阳性率上限且正对照召回最高的阈值，并记录工具、
协议哈希、正负样本数、观察 FPR 和召回率。只有负对照时，结果标为
`empirical_null`，含义仅为“优于背景”，不能冒充实验亲和力。

## 最终选择顺序

1. 物理和化学硬 QC；
2. 补齐所有七层指标；
3. 使用有证据的阈值完成 `competition_clearance`；
4. 仅在清关候选上计算混合方向 Pareto；
5. 聚类去冗余后按实验预算推荐。

当前 MDM 专属的 motif graft 和 ATSP 路线仍属于参考项目资产。新靶点默认只能启用
结构导向路线；Research Agent 未找到新靶点 motif 时，不允许静默复用 MDM motif。

面向 UI 的资源模型、REST adapter contract、请求/响应示例和审核/运行状态机见
[frontend_api_contract.md](frontend_api_contract.md)。

## 服务器实测（AutoDL A100）

2026-07-28 在 NVIDIA A100 40GB 环境完成了隔离冒烟测试：

- UniProt `Q00987` → RCSB → `step-3.7-flash` JSON Mode → draft 成功；
- draft → edit → approve 成功，审批后篡改会被摘要校验拦截；
- 未确认 target chain 和表位残基时，坐标即使是 A 级也不会进入 Design；
- MDM2 / 1YCR、长度 8、单候选 ColabDesign 运行成功，约 105 秒，生成有效 8 aa 序列及 PDB；
- 原有 180 项回归测试和新增 5 项 Bootstrap/结构测试全部通过。

真实测试还修复了两个只在服务器环境出现的问题：RCSB 条目串行请求导致初始化过慢，
现限制候选数并并行获取；Design 子进程原来硬编码调用 `python`，现改为
`sys.executable`，保证继承当前 Conda/CUDA 环境。

## 密钥安全

- API Key 只能通过环境变量或受权限保护的 secret 管理器提供；
- 不得把 Key 写入诊断脚本、项目配置、日志、draft 或 evidence；
- 若 Key 曾进入脚本、聊天记录或终端日志，应立即吊销并轮换；
- `.env` 已被 Git 忽略，但生产环境仍建议使用平台 Secret 管理能力。

建议在独立虚拟环境中安装 `requirements.txt`。依赖将 NumPy 限制在 1.x，避免现有
Matplotlib/科学计算二进制扩展与 NumPy 2.x ABI 不兼容。
