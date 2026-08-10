## Context

- integration 分支 `agents/design/validation.py` 中 `_binder_first_contig`（单段 `10-10 A25-109/0`）与 `_pdb_residue_range`（gap>50 分段、取最长段或 hotspot 覆盖段）是三个 Route 的唯一 contig 来源。
- 22XC 残基 69 未建模：`A25-109` 这类跨度会把缺失残基写进 contig，RFdiffusion `get_idx0` 断言崩溃。

## Goals / Non-Goals

**Goals：**
- 无 hotspot 时按真实建模段（gap>1）生成多段 contig，缺失残基不进 contig。
- 单段情形输出逐字节等于旧逻辑，MDM2/MDMX 等完整结构行为不变。
- 有 hotspot 时保留原单段+校验语义。

**Non-Goals：**
- 不改 hotspot 语义、不动执行链、不加新依赖。

## Decisions

1. `_pdb_receptor_contig_segments` 用 gap>1 而非 gap>50：contig 需要逐残基存在，任何缺失都必须断段。
2. 多段连接用 `/`（同一输出链续段），末尾 `/0` 断开链，与 `_binder_first_contig` 的链语义一致。
3. hotspot 路径保持 `_pdb_residue_range`：结合位点必须完整落在单一 gap>50 段内；段内残留缺口属既有行为，不在本 change 扩大范围。
4. `_binder_first_contig` 保留（单段路径与测试仍用），route 统一切到 `_binder_first_contig_segmented`。