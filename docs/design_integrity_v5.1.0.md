# Design v5.1.0：固定序列与闭环几何完整性门禁

发布日期：2026-07-29

## 版本目标

本版本关闭两个会污染 Design → Prediction 交接的高优先级风险：

1. LigandMPNN 已确定的环肽序列在 AfCycDesign refold 阶段发生漂移；
2. 使用首尾 CA–CA 距离代替真实成键原子判断闭环。

`agents/design.py` 通过 `DESIGN_PIPELINE_VERSION = "5.1.0"` 标识本版本，
每个新 manifest 记录 `design_pipeline_version`。

## 固定序列 refold 不变量

固定序列 refold 使用：

```python
model.predict(
    seq=requested_sequence,
    hard=True,
    soft=False,
    ...
)
```

序列优化入口 `model.design_3stage(...)` 不再参与 refold。一个候选只有同时满足
以下条件才可能进入 manifest：

1. ColabDesign 内部 hard sequence 与请求序列完全相同；
2. 保存后的 PDB 只有一条蛋白链；
3. 从该 PDB 重新提取的逐位序列与请求序列完全相同；
4. 子进程成功退出并同时生成 PDB 与 pLDDT 文件；
5. 宿主进程再次独立解析 PDB 并核对序列；
6. 重跑前删除同路径旧 PDB 与旧 pLDDT，失败重跑无法继承陈旧产物。

仓库提供两层测试：

- CPU/CI 回归：脚本结构、PDB 序列漂移、额外链和陈旧产物均 fail closed；
- 真实 GPU 回归：`test_design_gpu.py` 在部署环境运行一次完整 AfCycDesign
  refold，同时核对保存序列和末端 C–首端 N 几何。

GPU 门禁命令：

```bash
CYCPEP_RUN_GPU_TESTS=1 \
/root/damodel-tmp/envs/cycpep-prediction/bin/python \
-m unittest -v test_design_gpu.py
```

未设置 `CYCPEP_RUN_GPU_TESTS=1` 时该测试会显式显示 `skipped`，不能将 skipped
报告为 GPU 通过。

## 闭环几何门禁

闭环结果现在描述为 `pre_relax_geometry_compatibility`。它证明模型坐标与预期
成键几何兼容，不能代替化学合成、拓扑声明或 post-relax 验证。

### 首尾酰胺键

- 原子：末位残基 `C` → 首位残基 `N`
- pre-relax 筛选窗口：1.15–2.00 Å
- 理想/验证参考窗口：1.30–1.45 Å

### 二硫键

- 原子：首端 Cys `SG` → 末端 Cys `SG`
- pre-relax 筛选窗口：1.80–2.30 Å
- 理想参考窗口：1.90–2.15 Å

输出的 `ring_closure` 记录：

- `cyclization_type`
- `atom_1` / `atom_2`
- `distance_angstrom`
- `screen_range_angstrom`
- `ideal_range_angstrom`
- `ideal_geometry`
- `reason`

缺少成键原子、多链 monomer、序列长度不一致、非 Cys 末端的二硫键请求、
未知环化类型和 PDB 解析错误均 fail closed。

距离依据：

- wwPDB validation 示例将肽键 C–N 的接受范围列为 1.30–1.45 Å：
  <https://www.wwpdb.org/docs/documentation/validationReport/pdbj_sample_validation_report.pdf>
- wwPDB SSBOND 文档给出的蛋白二硫键实例约为 2.03–2.07 Å：
  <https://www.wwpdb.org/documentation/file-format-content/format33/sect6.html>

较宽的 pre-relax 窗口用于筛选未经松弛的预测结构；Prediction L4 仍需基于
经批准阈值检查 relax 前后几何。

## Manifest 兼容性

旧 Route C 把 linker、mutation 等修饰拼接进 `cyclization_type`，可能让下游
无法识别。v5.1.0 将其拆分为：

- `cyclization_type`：稳定的规范值，例如 `head-to-tail_amide`
- `cyclization_description`：保留 linker、mutation 等完整原始描述

这样下游读取稳定字段，同时审计信息不会丢失。

## 当前下游边界

Prediction 当前生产契约仅完整支持 `head_to_tail_amide`。Design v5.1.0 已能
正确检查和记录二硫键候选，但二硫键候选在 Prediction 扩展相应 L4 契约和
post-relax 方法前，不得标记为最终清关。

Rosetta、post-relax 和第二 predictor 仍按项目决定暂缓，不属于本版本安装范围。

## 本地验证记录

本版本在隔离 `.venv` 中完成：

- `test_design.py`：18 组通过；
- `test_reliability_regressions.py`：9 项通过；
- `test_prediction_pipeline.py`：17 项通过；
- target bootstrap + threshold research：19 项通过；
- `test_data_layer.py`：180 项通过；
- `pip check`：无依赖冲突。

真实 GPU 测试已提交但本次本地验证没有服务器权限，因此等待部署负责人在
4090 环境执行并保存完整命令、提交哈希、GPU 型号和输出日志。
