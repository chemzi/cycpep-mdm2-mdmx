# Research 调研流水线（可追溯 / 可复现）

本目录是 `agents/research.py` 背后的**调研过程**：从 PDB/PubMed 原始检索，到 biotite
界面/叠合计算，到文献人工提取，每一步的脚本、产物和指纹都在这里，供任何人核验与复算。

> 回应 review：结论不再硬编码在 `research.py` 里，而是**从下列产物派生**；每一步都挂
> `tool_trace`（`tool_name / tool_version / exit_code / output_sha256`）记进证据日志。

## 六步流水线

| # | 脚本 | 工具 | 产物 | 说明 |
|---|------|------|------|------|
| 1 | `search_pdb.py` | RCSB Search API v2 | `pdb_search_results.json` | UniProt 精确匹配 + 分辨率≤2.8Å + Protein(only)。MDM2 131 / MDMX 31 命中 |
| 2 | `enrich_pdb.py` | RCSB Data GraphQL | `pdb_enriched.json` | 判定肽复合物（靶点域实体 AND 独立 Protein 实体 ≤35aa）。MDM2 43 / MDMX 12 |
| 3 | `compute_interface.py` | biotite 1.2.0 | `interface_per_structure.json` | 重原子<4Å 界面残基 + 三锚点口袋；**强制分辨率≤2.8Å** |
| 4 | `aggregate_pockets.py` | stdlib | `pocket_report.json` | 共识口袋残基（出现在≥50% 规范结构） |
| 5 | `superpose_analyze.py` | biotite 1.2.0 | `pocket_differences.json` | 1YCR↔3DAB Cα 叠合（RMSD 1.88Å/85CA）；SASA/gatekeeper/floor/depth |
| 6 | `pubmed_search.py` | NCBI E-utilities | `pubmed_catalog.json` | 6 组关键词检索，109 个去重 PMID + 摘要 |

编排器：`run_pipeline.py` 按序跑完 6 步，捕获每步 `exit_code / 耗时 / 输出 sha256`，写 `run_report.json`。

## provenance/ 目录

- 6 个产物 JSON + `abstracts_batch1.txt` / `abstracts_batch2.txt`（PubMed 摘要原文）
- `MANIFEST.json`：每个产物的 **sha256** + 工具来源 + 过滤条件（供核验）
- `curation.json`：**人工**从上述 PMID 摘要提取的 8 个双靶 binder 表 + 三口袋设计解读。
  这部分是人工整理（非 LLM、非 API 自动产出），每条标注来源 PMID，且 `run()` 会校验该
  PMID 出现在 `pubmed_catalog.json` 内。

## 两种运行方式

```bash
# 1) 默认：校验已提交产物的 sha256 与 MANIFEST 一致，从产物派生结论写入 state.json
#    离线、秒级、无需 biotite/联网
PYTHONPATH=. python agents/research.py

# 2) 从零复算：真跑 6 步（需联网 + biotite），用新产物覆盖 provenance/ 后再派生
PYTHONPATH=. python agents/research.py --recompute
# 或只跑流水线、不写 state：
python scripts/run_pipeline.py --outdir scripts/provenance
```

两种模式都会为每一步在 `evidence/evidence_log.jsonl` 记一条 `research_tool_call`
（含完整 `tool_trace`），最后一条 `research_targets` 汇总全链 + 各产物 sha256。

## 派生 / 校验规则（哪些数字来自哪里）

- `targets[*].pocket_residues` ← `pocket_report.json` 的共识口袋（滤掉水）
- `pocket_differences[*].apo_sidechain_sasa_A2 / gatekeeper / leu26_floor / anchor_depth`
  ← `pocket_differences.json`
- `n_peptide_complexes`、PDB 列表 ← `pocket_report.json` / `interface_per_structure.json`
- `known_dual_binders`、`design_strategy_summary` ← `curation.json`（人工，PMID 可查）
- **强约束**：任一结构分辨率 >2.8Å、任一 binder 的 PMID 不在 catalog、任一产物 sha256 与
  MANIFEST 不符 → `run()` 直接报错（不会静默通过）。

## 复算依赖

```
pip install biotite numpy requests
```
`_structures/`（biotite 下载的 cif 缓存）与 `run_report.json` 不进版本控制。
