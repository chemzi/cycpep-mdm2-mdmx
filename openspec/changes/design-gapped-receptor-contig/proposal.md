## Why

RFdiffusion 的 contig 一旦引用 PDB 中不存在的残基号（未建模环区），就会在 `get_idx0` 处断言崩溃。CXCR4 22XC 的残基 69 未建模，导致整批 backbone 生成失败（已在 4090 服务器实测复现）。integration 分支现有 `_pdb_residue_range` 按 gap>50 分段并取最长段，带缺口的受体仍会生成跨越缺口的单段 contig。本 change 把已在 22XC/11QE 两个端到端流程验证过的本地补丁 2 移植到 integration 新代码结构。

## What Changes

- 新增 `_pdb_receptor_contig_segments(sorted_res)`：按 gap>1 拆分为真实建模段（比 `_pdb_contiguous_segments` 的 gap>50 更严格，专供 contig 使用）。
- 新增 `_binder_first_contig_segmented(target_chain, segments, binder_len)`：按建模段生成多段 contig（如 `10-10 C26-68/C70-228/C235-306/0`）；单段输入时输出与现有 `_binder_first_contig` 逐字节一致。
- 新增 `_receptor_contig_segments(pdb_path, chain, hotspot_residues=None)`：无 hotspot 时返回全部建模段；有 hotspot 时保留 `_pdb_residue_range` 的单段+校验窗口（absent/multi-segment 拒绝逻辑不变）。
- Route A/B/C 三个 RFdiffusion 调用点改用分段 contig 路径。

## Non-goals

- 不移植补丁 1（200KB 链统计）与补丁 3（越界热点过滤）：上游已通过 `chain_reviewed` 闸门与重构消除。
- 不移植补丁 4（前端选链 UI）：与上游 `chain_reviewed` 闸门互补，另行处理。
- 有 hotspot 时缺口 <50 的受体仍沿用原单段窗口（结合位点校验优先，行为不变）。

## Capabilities

### New Capabilities

- `design/gapped-receptor-contig`: 带未建模缺口的受体按真实建模段生成 RFdiffusion contig，避免 `get_idx0` 崩溃。

### Modified Capabilities

None.

## Impact

- 修改文件：`agents/design/validation.py`、`agents/design/route_a.py`、`agents/design/route_b.py`、`agents/design/route_c.py`、`agents/design/__init__.py`、`test_design.py`。
- 无 public interface 破坏（新增均为私有下划线函数）、无数据格式迁移、无 State/CandidateIndex/事务路径改动。
- 依赖方向不变：`validation.py` 仅新增纯函数，route 仍从 `.validation` 相对导入。