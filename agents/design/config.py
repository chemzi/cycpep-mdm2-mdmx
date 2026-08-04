"""Design Agent configuration and versioned scientific constants.

Single home for runtime paths, tool locations, and design protocol constants.
Module-level values are resolved at import time for backward compatibility;
:class:`DesignContext` replaces them without changing public behaviour.

Reproducibility contract
------------------------
- ????????????timesteps???????????? :data:`DESIGN_PROTOCOL`?
  ?? ``protocol_sha256`` ??????? manifest?Engineering Standard P1-4??
  ?????????? ``DESIGN_PROTOCOL["version"]``?
- ???????????????``/root/...``???????????????????
  ???????``CYCPEP_CONDA`` / ``RFDIFF_CONDA`` / ``RFDIFF_DIR`` /
  ``LIGANDMPNN_DIR`` / ``COLABDESIGN_DIR`` / ``CYCPEP_DESIGN_ROOT`` ??
  ?????????????????????
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from project_config import load_project_config  # noqa: E402

# ============================================================
# ????????Engineering Standard P1-4?
# ============================================================
#
# ??????????????timesteps?????????????? Magic
# Numbers??????????????/?????? protocol_sha256 ????
# ????????????????? version???? artifact ?????
# ?????????

DESIGN_PROTOCOL = {
    "version": "design_v1",
    "description": (
        "RFdiffusion backbone generation + LigandMPNN sequence sampling + "
        "AfCycDesign fixed-sequence refold, with global cheap pre-filter and "
        "Route C template cyclization/mutation expansion."
    ),
    "ligandmpnn": {
        "model_type": "protein_mpnn",
        "checkpoint": "proteinmpnn_v_48_020.pt",
        "n_seq_per_backbone": 8,
    },
    "rfdiff": {
        "timesteps": 50,
    },
    "cheap_filter": {
        "max_keep_per_backbone": 4,
    },
    "mutation": {
        "attempts_factor": 10,
        "protected_pharmacophore": "FWL",
    },
}

DESIGN_PROTOCOL_SHA256 = hashlib.sha256(
    json.dumps(DESIGN_PROTOCOL, sort_keys=True, ensure_ascii=True).encode("utf-8")
).hexdigest()



# ============================================================
# 环境路径
# ============================================================
#
# 警告：以下所有路径常量在 import 时解析。必须在 import design 之前
# 设置 CYCPEP_CONDA / RFDIFF_DIR / CYCPEP_DESIGN_ROOT 等环境变量；
# import 之后再修改这些变量不会生效。测试代码请参阅 test_design.py
# 的 stub 注入模式。

ACTIVE_PROJECT_CONFIG = load_project_config()

# 新服务器路径可全部通过环境变量覆盖；默认值对应 damodel 部署。
# os.environ.get(…) or default 确保空字符串不会静默损坏路径（P0-2）。
CYCPEP_CONDA = os.environ.get("CYCPEP_CONDA") or "/root/damodel-tmp/envs/cycpep-prediction"
CYCPEP_PYTHON = os.environ.get("CYCPEP_PYTHON") or f"{CYCPEP_CONDA}/bin/python"
RFDIFF_CONDA = os.environ.get("RFDIFF_CONDA") or "/root/damodel-tmp/envs/rfdiffusion-design"
RFDIFF_PYTHON = os.environ.get("RFDIFF_PYTHON") or f"{RFDIFF_CONDA}/bin/python"
RFDIFF_DIR = os.environ.get("RFDIFF_DIR") or "/root/workspace/NovaPeptide/tools/RFdiffusion"
LIGANDMPNN_DIR = os.environ.get("LIGANDMPNN_DIR") or "/root/workspace/NovaPeptide/tools/LigandMPNN"
COLABDESIGN_DIR = os.environ.get("COLABDESIGN_DIR") or "/root/workspace/NovaPeptide/tools/ColabDesign"
COLABDESIGN_PARAMS = os.environ.get("COLABDESIGN_PARAMS") or f"{COLABDESIGN_DIR}/params"
COLABDESIGN_COMMIT = "094e2cb3603dee7d99846e0977736bd943c830c2"
SE3_ROOT = os.environ.get("SE3_ROOT") or f"{RFDIFF_DIR}/env/SE3Transformer"
CUDA_DATA_DIR = os.environ.get("CUDA_DATA_DIR") or f"{CYCPEP_CONDA}/lib/python3.10/site-packages/nvidia/cuda_nvcc"
DAMODEL_DATA_ROOT = Path("/root/damodel-tmp/novapeptide")


def _resolve_output_dir(environ=None, damodel_data_root=None):
    """Resolve a writable design root without assuming /root is accessible."""
    env = os.environ if environ is None else environ
    explicit_root = env.get("CYCPEP_DESIGN_ROOT")
    if explicit_root:
        return Path(explicit_root)

    np_data_root = env.get("NP_DATA")
    if np_data_root:
        return Path(np_data_root) / "designs"

    damodel_root = DAMODEL_DATA_ROOT if damodel_data_root is None else damodel_data_root
    try:
        if damodel_root.is_dir():
            return damodel_root / "designs"
    except OSError:
        # GitHub runners and other non-root users cannot stat paths below
        # /root.  Fall through to next candidate without logging — this
        # function runs at import time and must not produce side effects (P2).
        pass

    runner_temp = env.get("RUNNER_TEMP")
    if runner_temp:
        return Path(runner_temp) / "novapeptide" / "designs"
    return ROOT / "data" / "designs"


DEFAULT_OUTPUT_DIR = _resolve_output_dir()
OUTPUT_DIR = str(DEFAULT_OUTPUT_DIR)
_raw_ts = os.environ.get("RFDIFF_TIMESTEPS") or str(DESIGN_PROTOCOL["rfdiff"]["timesteps"])
try:
    RFDIFF_TIMESTEPS = max(1, int(_raw_ts))
except (ValueError, TypeError):
    RFDIFF_TIMESTEPS = DESIGN_PROTOCOL["rfdiff"]["timesteps"]
    # Defer log until _run_rfdiff first consumes the value (P1: no
    # EvidenceLogger side-effects at import time).
    _RFDIFF_TIMESTEPS_INVALID = os.environ.get("RFDIFF_TIMESTEPS")
else:
    _RFDIFF_TIMESTEPS_INVALID = None
LIGANDMPNN_MODEL_TYPE = os.environ.get("LIGANDMPNN_MODEL_TYPE") or DESIGN_PROTOCOL["ligandmpnn"]["model_type"]
LIGANDMPNN_CHECKPOINT = os.environ.get("LIGANDMPNN_CHECKPOINT") or f"{LIGANDMPNN_DIR}/model_params/{DESIGN_PROTOCOL['ligandmpnn']['checkpoint']}"
DESIGN_PIPELINE_VERSION = "5.2.1"

# Module-level state for _verify_colabdesign_runtime() (P3-3).
# Only cache *success* — a transient failure (GPU OOM, env hiccup) must
# not permanently disable the check for the lifetime of the process (P1).
# Cached signature binds to the concrete ColabDesign environment so that
# switching CYCPEP_PYTHON / COLABDESIGN_DIR / COLABDESIGN_PARAMS
# mid-process triggers a re-verification (P1 reviewer feedback).
_VERIFIED_RUNTIME_SIGNATURE = None
_SKIP_EVIDENCE_LOGGED = False


# Geometry gates are deliberately labelled as compatibility checks.  A model
# whose terminal atoms are close enough for a covalent bond is suitable for
# downstream relaxation/validation; coordinates alone do not prove that the
# bond has been chemically formed.
CLOSURE_GEOMETRY = {
    "head-to-tail_amide": {
        "atom_1": "last:C",
        "atom_2": "first:N",
        # The wwPDB validation range for a peptide C-N bond is 1.30-1.45 Å.
        # Design uses a wider pre-relax screen and records ideal-range status.
        "screen_range_angstrom": (1.15, 2.00),
        "ideal_range_angstrom": (1.30, 1.45),
    },
    "Cys-Cys_disulfide": {
        "atom_1": "first:SG",
        "atom_2": "last:SG",
        # Typical protein disulfides are close to 2.03 Å.  The wider screen
        # tolerates an unrelaxed predictor output without accepting CA proxies.
        "screen_range_angstrom": (1.80, 2.30),
        "ideal_range_angstrom": (1.90, 2.15),
    },
}


# ============================================================
# 设计常量（Research 产出可覆盖）
# ============================================================

# 所有设计常量从 Research State 读取（_load_target_spec）。
_LOCK = threading.Lock()
CYCLIZATION_PAIRS = [("C", "C"), ("", "")]
LINKER_MATRIX = ["GGGGS", "GGGS", "GGS", "GS", ""]
SCAFFOLD_MUTABLE_AA = "ACDEFGHIKLMNPQRSTVWY"

# 便宜预筛参数
#
# Route A 已改为全局两阶段收集→排序→取 top K 条（Pass1: 收集所有 backbone
# 序列，Pass2: 全局 cheap filter）以避免 backbone 顺序偏差。Route B 同理。
# CHEAP_FILTER_MAX_KEEP 控制每个 backbone 内部预筛保留的序列数上限，不再约束
# 最终候选数。
try:
    CHEAP_FILTER_MAX_KEEP = max(1, int(
        os.environ.get("CHEAP_FILTER_MAX_KEEP")
        or os.environ.get("CHEAP_FILTER_TOP_K")
        or str(DESIGN_PROTOCOL["cheap_filter"]["max_keep_per_backbone"])))
except (ValueError, TypeError):
    CHEAP_FILTER_MAX_KEEP = DESIGN_PROTOCOL["cheap_filter"]["max_keep_per_backbone"]
HYDROPHOBIC = set("AILMFWV")
POS_CHARGED = set("KR")
NEG_CHARGED = set("DE")


# ============================================================
# ??????Engineering Standard P1-1?
# ============================================================
#
# ??????? Design(context) ????????????? import-time
# ??? ACTIVE_PROJECT_CONFIG????????????????????

@dataclass(frozen=True)
class DesignContext:
    """Per-project context injected into :class:`~agents.design.agent.Design`.

    ``project_config`` ????? target ?????``output_dir`` ?????
    ?????????????CYCPEP_PYTHON / RFDIFF_DIR / ...??????
    ??????????????????????

    Examples
    --------
    >>> DesignContext(project_config=approved_config, output_dir="/tmp/x")
    >>> DesignContext.default()  # ?????????? ACTIVE_PROJECT_CONFIG
    """

    project_config: dict
    output_dir: str = ""

    def __post_init__(self):
        # frozen dataclass ??? __post_init__ ??? setattr?
        object.__setattr__(self, "output_dir", self.output_dir or str(DEFAULT_OUTPUT_DIR))

    @classmethod
    def default(cls) -> "DesignContext":
        """Build the legacy default context from module-level state."""
        return cls(project_config=ACTIVE_PROJECT_CONFIG, output_dir=str(DEFAULT_OUTPUT_DIR))
