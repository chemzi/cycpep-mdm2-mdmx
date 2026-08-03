# Prediction v1.4.1：PyRosetta 服务器部署与真实双靶标验证

日期：2026-08-02  
服务器：RTX 4090 部署机（PyRosetta InterfaceAnalyzer 使用 CPU）  
候选：C0514，`TGTGETLEEFQE`  
许可场景：非商业学术、非营利或政府研究

## 1. 结论

Prediction 的独立第二 predictor 和 Rosetta 界面证据已经全部配置并跑通：

- Boltz-2 2.2.1 为 AlphaFold2 之外的第二模型家族，MDM2、MDMX 的 L6 均通过；
- PyRosetta 固定为长期保留的 2026.29 季度 MinSizeRel 构建；
- 对每个靶标的 3 个 AlphaFold2 模型和 1 个 Boltz-2 模型逐一运行 Rosetta；
- 8 份 Rosetta 结果全部与 predictor、model ID、seed 和输入 PDB SHA-256 绑定；
- L3 的 `sc`、`dSASA_int` 已不再缺失；
- 当前唯一缺失的七层原始证据是 L4 post-relax。

正负对照阈值标定继续跳过。该策略允许生产模型证据，但仍禁止候选进入
`finalized`。

## 2. 安装与版本

官方安装源：

```text
https://west.rosettacommons.org/pyrosetta/quarterly/minsizerel/
```

服务器固定环境：

```text
path: /root/damodel-tmp/envs/pyrosetta-2026.29-minsizerel
python: 3.11
package: pyrosetta==2026.29+releasequarterly.80a0635615
build: PyRosetta4.MinSizeRel.python311.ubuntu
Rosetta revision: 80a0635615099e1b918474a63acba7b1de6fd107
environment size: 3.3 GB
```

PyRosetta 已通过 `import`、数据库初始化和真实 PDB 协议执行测试。

## 3. 环肽界面协议

每个输入模型依次执行：

1. 验证候选序列、靶标链、环肽链和输入 PDB SHA-256；
2. 验证环肽末位 C 到首位 N 距离不大于 2.0 Å；
3. `DeclareBond` 声明环肽末端 C—N 共价键；
4. 使用 ref2015 `InterfaceAnalyzerMover`；
5. `pack_input=true`，预打包 AlphaFold2/Boltz 这类非 Rosetta 输入的侧链；
6. `pack_separated=true`，分离后重新打包界面侧链；
7. `interface_sc=true`，输出 shape complementarity；
8. 解析并记录 `dSASA_int`、`sc_value` 和 `dG_separated`。

最初工程 smoke 使用了 `pack_input=false`，AlphaFold2 模型出现数百 REU 的排斥能。
依据非 Rosetta 输入的协议需求，v1.4.1 改为 `pack_input=true` 并重新运行全部 8 个
模型。首轮结果只保留作调试记录，不进入 v1.4.1 正式 artifact。

## 4. C0514 最终回归结果

四模型中位数：

| 指标 | MDM2 | MDMX |
|---|---:|---:|
| ipSAE | 0.2988 | 0.2867 |
| ipTM | 0.6929 | 0.6512 |
| PRODIGY dG (kcal/mol) | -8.242 | -7.881 |
| Rosetta dSASA (Å²) | 1144.59 | 1012.83 |
| Rosetta SC | 0.5856 | 0.5277 |
| Rosetta dG_separated (REU) | +34.59 | -5.97 |
| pose RMSD (Å) | 1.4208 | 0.8752 |
| hotspot coverage | 1.0 | 1.0 |

MDM2 的 Rosetta `dG_separated` 中位数仍为正，主要来自两个 AlphaFold2 姿态残留的
不利骨架/界面几何；PyRosetta 的单次侧链预打包不能代替 post-relax。当前 L3 正式
电池使用 PRODIGY dG、Rosetta SC 和 dSASA，额外记录 Rosetta dG 作为诊断信息。

这批数值只证明评分链路跑通。C0514 的 L2 和 L3 按当前 team-provisional 数值门槛
均未通过，且阈值尚未经正负对照标定，因此不能宣称其为结合命中。

## 5. 状态与剩余事项

隔离 Prediction v1.4.1 结果：`prediction_pending`。

- 通过：L1、L5、L6、L7；
- 数值未通过：L2、L3；
- 缺证据：仅 L4 `nc_distance_post`；
- 缺阈值值：无；
- 缺最终阈值依据：L2、L3、L4、L6 仍为 `team_provisional`。

因此，Rosetta 和第二 predictor 已配置完成。后续技术路线只剩 post-relax，以及在
适当阶段恢复正负对照阈值标定。

## 6. 产物与数据保护

```text
artifact bundle:
/root/damodel-tmp/novapeptide/prediction_artifacts_v141_c0514_boltz_pyrosetta_prepack_20260802/C0514/artifacts.json

isolated run:
/root/damodel-tmp/novapeptide/prediction_v141_c0514_boltz_pyrosetta_prepack_20260802/runs/prediction_v141_c0514_prepack_20260802

artifact bundle SHA-256:
854545e1ee7cbc3a6d22993ea04dddde0617fdda5737f68e441850458760c3d7
```

正式文件在全部测试前后保持不变：

- `data/state.json`：`10c6fdf79b030e9693664cb53e1512522aaad6e1546d37664a9e1ad0825a457f`
- `data/candidate_index.csv`：`4e4b0a0e8be7a5e959262a3cc76db5e28f983076a7c3ce462b605eeab2e89c84`

## 7. 本次空间整理

安装前清除了 pip 缓存、Conda 可清包、Miniforge 安装程序、两个哈希相同的权重副本
和旧临时测试目录。可用空间从约 11 GB 增加到 14 GB；安装 PyRosetta 后剩余约
11 GB。当前模型、正式 Design/Prediction 数据、历史代码副本和 Git 回滚分支均未
删除。
