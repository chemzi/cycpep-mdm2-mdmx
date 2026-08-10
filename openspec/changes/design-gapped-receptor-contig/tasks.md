## 1. 分段 contig 实现

- [x] 1.1 新增 `_pdb_receptor_contig_segments`（gap>1 细分段，空输入报错）
- [x] 1.2 新增 `_binder_first_contig_segmented`（多段 `/0` 连接；单段输出与 `_binder_first_contig` 逐字节一致；校验链/长度/段序）
- [x] 1.3 新增 `_receptor_contig_segments`（无 hotspot 返回全部建模段；有 hotspot 走 `_pdb_residue_range`）
- [x] 1.4 Route A/B/C 三个调用点切换；`agents/design/__init__.py` 导出同步

## 2. 测试与验证

- [x] 2.1 `test_design.py` Test 18：单段一致、多段 `C26-68/C70-228/C235-306/0`、gap 分段、空/反向输入报错、缺口 PDB 端到端（无 hotspot 多段、有 hotspot 单段、hotspot 落在缺口报错）
- [x] 2.2 `python test_design.py` 18 组全过；`test_design_behavior.py` 13 通过；`scripts/architecture_gate.py` 零新违规