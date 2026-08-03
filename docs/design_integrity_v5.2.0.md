# Design v5.2.0：Route C 独立 L7 reference 合同

发布日期：2026-08-03

## 修复目标

Route A/B 的序列来自 RFdiffusion backbone 上的 LigandMPNN 设计，manifest 会保存
对应 backbone。历史 Route C 直接对 scaffold-derived 序列执行 fixed-sequence refold，
manifest 中没有 `backbone_pdb`。这会让 ColabDesign、Boltz、Rosetta 与 post-relax
全部完成后，Prediction L7 仍因缺少 reference 无法计算 scRMSD。

v5.2.0 为每条即将进入 Route C refold 的序列先生成一份独立、靶标结合态的
RFdiffusion backbone。输出必须满足：

- binder-first cyclic contig；
- 实际 PDB 中存在已审批 receptor chain；
- 恰好一条非 receptor 链与候选长度相同；
- 每条候选使用独立 backbone 文件；
- reference 生成或链识别失败时，该序列不会获得 candidate ID，也不会写入
  CandidateIndex。

Route C 的 scaffold/linker/mutation 序列逻辑保持不变。新增 RFdiffusion backbone
用于给出独立结构假设和 L7 对照；fixed-sequence refold 继续承担序列完整性与预松弛
闭环几何检查。

## Manifest v5.2 字段

新 manifest 写入：

```json
{
  "design_reference_pdb": "/absolute/path/to/bb_0.pdb",
  "design_reference_pdb_hash": "...",
  "design_reference_role": "rfdiffusion_target_bound_backbone",
  "backbone_pdb": "/absolute/path/to/bb_0.pdb",
  "backbone_pdb_hash": "..."
}
```

`backbone_*` 是兼容旧 Prediction reader 的别名。写 manifest 与读取 Prediction
合同时都会拒绝 reference 和 refold 指向同一路径或具有相同 SHA-256，防止循环证据。

## 历史候选与 C1250

历史 manifest 保持不可变。缺 reference 的 C1250 不做伪 backfill，也不把 refold
复制成 backbone。Critic v1.1 会将其识别为 `design_reference_missing`，Planner v1.1
再将 `regenerate_design_reference` 放入 Design 迭代，追加新 candidate ID。已有
Prediction record 继续作为历史证据保存。

## 生产前置门禁

Prediction v1.5.1 的 predictor runner 和 evidence enrichment 在创建模型输出目录前
检查整批候选的独立 reference。任一候选缺失时返回
`design_reference_missing_preflight`，从而避免六层重计算完成后才在 L7 pending。

## 本地回归

- Route C 按长度分组生成 reference，并验证每条序列获得不同文件；
- manifest 显式记录 reference role/hash；
- refold 复用为 reference 时 fail closed；
- Critic 将 L7 缺失归因给 Design；
- Planner 将 L7 修复与完整证据下的 L6 失败合并进 Design 迭代；
- `test_design.py` 21 组及 Critic/Planner/Prediction 63 项单元测试通过。

本地测试不调用 RFdiffusion、ColabDesign、Boltz 或 Rosetta；真实 GPU Route C 小批量
回归需要在部署后单独执行并保存输出 manifest。
