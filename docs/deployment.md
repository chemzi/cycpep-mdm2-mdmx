# AutoDL 部署记录

> 记录人：于嘉乐  
> 最后更新：2026-07-24

---

## 环境概览

| 环境 | 激活方式 | 位置 | 用途 |
|------|---------|------|------|
| cycpep (conda) | `source ~/miniconda3/etc/profile.d/conda.sh && conda activate cycpep` | `/root/miniconda3/envs/cycpep/` | ColabDesign refold / JAX |
| rfdiff_env (venv) | `source /root/autodl-tmp/rfdiff_env/bin/activate` | `/root/autodl-tmp/rfdiff_env/` | RFdiffusion 生成环肽 |

两个环境不共存是因为 dgl 2.x 只支持到 PyTorch 2.2，而 JAX/ColabDesign 需要 PyTorch ≥ 2.13。

---

## cycpep 环境 (ColabDesign)

### 安装记录

```bash
conda create -n cycpep python=3.10 -y
conda activate cycpep
pip install jax==0.4.35 jaxlib==0.4.35
pip install torch jax flax optax dm-haiku
pip install nvidia-cuda-nvcc-cu12==12.1.105
```

### 关键依赖

| 包 | 版本 |
|---|------|
| Python | 3.10 |
| JAX | 0.4.35 |
| PyTorch | 2.13.0 |
| CUDA | 12.1 |

### AlphaFold2 参数

```
/root/autodl-tmp/params/  # 约 5.3GB，从 ~/.macromnex/cache/model/alphafold2/params/ 复制
软链接: ~/ColabDesign/params → /root/autodl-tmp/params
```

---

## rfdiff_env 环境 (RFdiffusion)

### 创建

```bash
python3.10 -m venv /root/autodl-tmp/rfdiff_env
source /root/autodl-tmp/rfdiff_env/bin/activate
pip install torch==2.2.0
pip install dgl==2.2.0+cu121 -f https://data.dgl.ai/wheels/torch-2.2/cu121/repo.html
pip install torchdata==0.7.1 "numpy<2" omegaconf hydra-core pyrsistent e3nn pydantic
pip install --no-deps -e /root/RFdiffusion
```

### 关键依赖

| 包 | 版本 |
|---|------|
| Python | 3.10 |
| PyTorch | 2.2.0 |
| dgl | 2.2.0+cu121 |
| numpy | < 2 (1.26.4) |
| torchdata | 0.7.1 |

### SE3-Transformer

不在 PyPI 上，从 NVIDIA DeepLearningExamples 提取：

```bash
# 本地下载 https://github.com/NVIDIA/DeepLearningExamples/archive/master.zip
# 解压后提取 DGLPyTorch/DrugDiscovery/SE3Transformer/se3_transformer/
# 上传到 /root/autodl-tmp/se3_transformer/
export PYTHONPATH=/root/autodl-tmp:$PYTHONPATH  # 必须设置
```

### RFdiffusion 模型权重

- **Base_ckpt.pt** (Base model)：`http://files.ipd.uw.edu/pub/RFdiffusion/6f5902ac237024bdd0c176cb93063dc4/Base_ckpt.pt`
- **Complex_base_ckpt.pt** (Complex model)：`http://files.ipd.uw.edu/pub/RFdiffusion/e29311f6f1bf1af907f9ef9f44b8328b/Complex_base_ckpt.pt`

存放路径：`/root/autodl-tmp/rfdiffusion_models/`，软链接到 `/root/RFdiffusion/models`。

---

## 目录结构

```
/root/
├── ColabDesign/          # ColabDesign v1.1.2
│   └── params → /root/autodl-tmp/params
├── RFdiffusion → /root/autodl-tmp/RFdiffusion
├── targets/              # 靶点 PDB (1YCR, 3DAB, etc.)
├── designs/              # 设计输出
├── scaffolds/            # 支架库
├── cycpep-mdm2-mdmx/     # 项目代码
├── miniconda3/           # conda 安装
└── autodl-tmp/           # 数据盘 (50GB)
    ├── rfdiff_env/       # RFdiffusion venv
    ├── RFdiffusion/      # RFdiffusion 代码
    ├── params/           # AlphaFold2 参数
    ├── rfdiffusion_models/  # RFdiffusion 权重
    └── se3_transformer/  # SE3-Transformer 代码
```

---

## 冒烟测试 (RFpeptides cyclic)

```bash
# 激活环境
source /root/autodl-tmp/rfdiff_env/bin/activate
cd /root/RFdiffusion

# 生成 2 个 10 残基环肽骨架 (1YCR MDM2)
python scripts/run_inference.py \
  inference.input_pdb=/root/targets/1YCR.pdb \
  inference.cyclic=True \
  inference.num_designs=2 \
  inference.output_prefix=/root/designs/smoke_test/cyclic \
  contigmap.contigs='["A25-109,0 10-10"]' \
  diffuser.T=25 \
  2>&1 | tail -20

# contig 格式说明:
#   A25-109  = MDM2 链A残基25-109（靶点）
#   0        = 不插入长度
#   10-10    = 设计10残基环肽
```

---

## LigandMPNN 权重

LigandMPNN 克隆后不含模型权重，必须手动下载。

```bash
# 运行官方下载脚本（一次性下全，约 30MB）
bash /root/LigandMPNN/get_model_params.sh /root/autodl-tmp/ligandmpnn_model_params
ln -sf /root/autodl-tmp/ligandmpnn_model_params /root/LigandMPNN/model_params
```

如果 `files.ipd.uw.edu` 下载慢/截断，只下必需的两个：
- `proteinmpnn_v_48_020.pt` (~6.5MB)
- `ligandmpnn_v_32_010_25.pt` (~10MB)

然后 scp 上传到 `/root/autodl-tmp/ligandmpnn_model_params/`。

### 关键：LigandMPNN 调用参数（v5 修正记录）

| 错误参数 | 正确参数 | 说明 |
|---------|---------|------|
| `--num_seq_per_target=8` | `--batch_size=4 --number_of_batches=2` | 序列数 = batch_size × number_of_batches |
| `--sampling_temp=0.1` | `--temperature=0.1` | 采样温度 |
| 无 | `--chains_to_design=B` | **必须指定！** 否则会把受体也重新设计 |
| 无 | `cwd=LIGANDMPNN_DIR` | **必须指定！** 否则相对路径 `./model_params/` 找不到 |

### LigandMPNN FASTA 解析注意事项

1. **输出路径**：FASTA 文件在 `seqs/` 子目录下（如 `mpnn_bb_0/seqs/bb_0.fa`），不能用 `*.fa` 而要用 `**/*.fa` 递归 glob
2. **序列格式**：每条序列行是 `受体序列:设计序列`（如 `ETLV...:GLITPEGFSK`），必须取 `:` 后面的 binder 部分
3. **全 G baseline**：第一条序列通常是全 G（`GGGGGGGGGG`），是 LigandMPNN 的 baseline 输出，需过滤掉
4. **单文件多条**：一个 `.fa` 文件包含全部 8 条序列（每条约 ~100 字符的受体+设计拼接），不是每序列一个文件

## 测试记录

| 日期 | 路线 | 命令 | 结果 |
|------|------|------|------|
| 2026-07-27 | Route A | `--route A --target 1YCR --n 10 --lengths 10,12,14` | 35/35 通过 |
| 2026-07-25 | Route C | `--route C --n 5` | 5/5 通过 |
| - | Route B | 待测 | - |

## 克隆/迁移注意事项

1. **数据盘不自动克隆**：克隆时选择「克隆数据盘」，否则 `/root/autodl-tmp/` 为空
2. **端口会变**：每次重启/克隆，SSH 端口不同
3. **GPU 需要手动开**：Clone 出的新机子默认无 GPU

### 最小迁移清单（如果数据盘没跟上）

```bash
scp -r -P <新端口> <本地> root@region-9.autodl.pro:/root/autodl-tmp/
```

需要搬的目录：
- `rfdiff_env/` — 整个 venv (约 2-3GB)
- `RFdiffusion/` — 代码
- `rfdiffusion_models/` — 模型权重 (~500MB)
- `se3_transformer/` — SE3 代码
- `params/` — AlphaFold2 参数 (~5.3GB，可选)
