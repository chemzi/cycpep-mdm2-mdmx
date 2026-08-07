"""Design Agent configuration and versioned scientific constants.

Single home for runtime paths, tool locations, and design protocol constants.

Lazy resolution contract
------------------------
Values that were previously resolved at import time (project config, tool
paths, output directory) are now resolved on first attribute access via
module ``__getattr__`` (PEP 562).  This removes import-time project global
state and import-time I/O side effects (Engineering Standard §7) while keeping
the legacy module-level names importable unchanged for backward compatibility.
:class:`DesignContext` is the forward-looking dependency-injected interface.

Reproducibility contract
------------------------
Scientific parameters live in ``protocols/design_v1.json``
(:data:`DESIGN_PROTOCOL`); every manifest records ``protocol_sha256`` of that
file so results stay reproducible (Engineering Standard §8 / Roadmap PR7).
Tool paths default to the deployment layout under ``/root/...`` and can be
overridden with ``CYCPEP_CONDA`` / ``RFDIFF_CONDA`` / ``RFDIFF_DIR`` /
``LIGANDMPNN_DIR`` / ``COLABDESIGN_DIR`` / ``CYCPEP_DESIGN_ROOT``.
"""

from __future__ import annotations

import functools
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from project_config import load_project_config  # noqa: E402
from core.context import ProjectContext  # noqa: E402
from core.protocol import load_protocol  # noqa: E402

# ============================================================
# Versioned scientific protocol (Engineering Standard §8 / Roadmap PR7)
# ============================================================
# Scientific parameters no longer live as Magic Numbers in handlers; they are
# read from protocols/design_v1.json.  Every plan/approval artifact binds the
# file's SHA-256 via DESIGN_PROTOCOL_SHA256 so results are reproducible and a
# parameter change forces a protocol version bump.

DESIGN_PROTOCOL_PATH = ROOT / "protocols" / "design_v1.json"
DESIGN_PROTOCOL, DESIGN_PROTOCOL_SHA256 = load_protocol(
    DESIGN_PROTOCOL_PATH,
    required_sections={
        "cheap_filter": dict,
        "ligandmpnn": dict,
        "mutation": dict,
        "rfdiff": dict,
        "refold": dict,
    },
)

# ============================================================
# Lazy runtime environment (Engineering Standard §7)
# ============================================================
# Project config and tool-path constants are resolved on first access (and
# cached) instead of at import time, so importing the package performs no
# project-config / env side effects.  Legacy module-level names are served
# through module ``__getattr__`` (PEP 562), so ``from agents.design.config
# import CYCPEP_PYTHON`` keeps working unchanged.

# Deferred warning flag for an invalid RFDIFF_TIMESTEPS env value; consumed by
# runtime._run_rfdiff before the first RFdiffusion invocation.
_RFDIFF_TIMESTEPS_INVALID = None


@functools.lru_cache(maxsize=1)
def _get_active_project_config():
    """Load the approved project config (cached on first access)."""
    return load_project_config()


@functools.lru_cache(maxsize=1)
def _get_cycpep_conda():
    return os.environ.get("CYCPEP_CONDA") or "/root/damodel-tmp/envs/cycpep-prediction"


@functools.lru_cache(maxsize=1)
def _get_cycpep_python():
    return os.environ.get("CYCPEP_PYTHON") or f"{_get_cycpep_conda()}/bin/python"


@functools.lru_cache(maxsize=1)
def _get_rfdiff_conda():
    return os.environ.get("RFDIFF_CONDA") or "/root/damodel-tmp/envs/rfdiffusion-design"


@functools.lru_cache(maxsize=1)
def _get_rfdiff_python():
    return os.environ.get("RFDIFF_PYTHON") or f"{_get_rfdiff_conda()}/bin/python"


@functools.lru_cache(maxsize=1)
def _get_rfdiff_dir():
    return os.environ.get("RFDIFF_DIR") or "/root/workspace/NovaPeptide/tools/RFdiffusion"


@functools.lru_cache(maxsize=1)
def _get_ligandmpnn_dir():
    return os.environ.get("LIGANDMPNN_DIR") or "/root/workspace/NovaPeptide/tools/LigandMPNN"


@functools.lru_cache(maxsize=1)
def _get_colabdesign_dir():
    return os.environ.get("COLABDESIGN_DIR") or "/root/workspace/NovaPeptide/tools/ColabDesign"


@functools.lru_cache(maxsize=1)
def _get_colabdesign_params():
    return os.environ.get("COLABDESIGN_PARAMS") or f"{_get_colabdesign_dir()}/params"


@functools.lru_cache(maxsize=1)
def _get_se3_root():
    return os.environ.get("SE3_ROOT") or f"{_get_rfdiff_dir()}/env/SE3Transformer"


@functools.lru_cache(maxsize=1)
def _get_cuda_data_dir():
    return os.environ.get(
        "CUDA_DATA_DIR"
    ) or f"{_get_cycpep_conda()}/lib/python3.10/site-packages/nvidia/cuda_nvcc"


@functools.lru_cache(maxsize=1)
def _get_default_output_dir():
    """Resolve the writable design root (cached on first access)."""
    return _resolve_output_dir()


@functools.lru_cache(maxsize=1)
def _get_output_dir():
    return str(_get_default_output_dir())


@functools.lru_cache(maxsize=1)
def _get_rfdiff_timesteps():
    """RFdiffusion timesteps from env or protocol; invalid env is deferred-logged."""
    raw = os.environ.get("RFDIFF_TIMESTEPS") or str(DESIGN_PROTOCOL["parameters"]["rfdiff"]["timesteps"])
    try:
        return max(1, int(raw))
    except (ValueError, TypeError):
        # Defer the warning until runtime._run_rfdiff consumes the value (no
        # EvidenceLogger side effects at import time).
        global _RFDIFF_TIMESTEPS_INVALID
        _RFDIFF_TIMESTEPS_INVALID = os.environ.get("RFDIFF_TIMESTEPS")
        return DESIGN_PROTOCOL["parameters"]["rfdiff"]["timesteps"]


@functools.lru_cache(maxsize=1)
def _get_ligandmpnn_model_type():
    return os.environ.get("LIGANDMPNN_MODEL_TYPE") or DESIGN_PROTOCOL["parameters"]["ligandmpnn"]["model_type"]


@functools.lru_cache(maxsize=1)
def _get_ligandmpnn_checkpoint():
    return os.environ.get(
        "LIGANDMPNN_CHECKPOINT"
    ) or f"{_get_ligandmpnn_dir()}/model_params/{DESIGN_PROTOCOL['parameters']['ligandmpnn']['checkpoint']}"


@functools.lru_cache(maxsize=1)
def _get_cheap_filter_max_keep():
    try:
        return max(1, int(
            os.environ.get("CHEAP_FILTER_MAX_KEEP")
            or os.environ.get("CHEAP_FILTER_TOP_K")
            or str(DESIGN_PROTOCOL["parameters"]["cheap_filter"]["max_keep_per_backbone"])))
    except (ValueError, TypeError):
        return DESIGN_PROTOCOL["parameters"]["cheap_filter"]["max_keep_per_backbone"]


_LAZY_ATTRIBUTES = {
    "ACTIVE_PROJECT_CONFIG": _get_active_project_config,
    "CYCPEP_CONDA": _get_cycpep_conda,
    "CYCPEP_PYTHON": _get_cycpep_python,
    "RFDIFF_CONDA": _get_rfdiff_conda,
    "RFDIFF_PYTHON": _get_rfdiff_python,
    "RFDIFF_DIR": _get_rfdiff_dir,
    "LIGANDMPNN_DIR": _get_ligandmpnn_dir,
    "COLABDESIGN_DIR": _get_colabdesign_dir,
    "COLABDESIGN_PARAMS": _get_colabdesign_params,
    "SE3_ROOT": _get_se3_root,
    "CUDA_DATA_DIR": _get_cuda_data_dir,
    "DEFAULT_OUTPUT_DIR": _get_default_output_dir,
    "OUTPUT_DIR": _get_output_dir,
    "RFDIFF_TIMESTEPS": _get_rfdiff_timesteps,
    "LIGANDMPNN_MODEL_TYPE": _get_ligandmpnn_model_type,
    "LIGANDMPNN_CHECKPOINT": _get_ligandmpnn_checkpoint,
    "CHEAP_FILTER_MAX_KEEP": _get_cheap_filter_max_keep,
}


def __getattr__(name):
    """PEP 562: serve legacy config names lazily on first access."""
    getter = _LAZY_ATTRIBUTES.get(name)
    if getter is not None:
        return getter()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _module_attr(name):
    """Read a config value through the module object.

    Bare global lookups inside this module do NOT trigger the PEP 562 module
    ``__getattr__``, so lazy names must be fetched explicitly.  Reading through
    the module object also honours runtime overrides (e.g. tests patching
    ``agents.design.config.ACTIVE_PROJECT_CONFIG``).
    """
    return getattr(sys.modules[__name__], name)


# ============================================================
# Static deployment paths / pins (immutable literals)
# ============================================================

DAMODEL_DATA_ROOT = Path("/root/damodel-tmp/novapeptide")
COLABDESIGN_COMMIT = "094e2cb3603dee7d99846e0977736bd943c830c2"


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
        # /root.  Fall through to next candidate without logging - resolution
        # is deferred to first access and must not produce side effects.
        pass

    runner_temp = env.get("RUNNER_TEMP")
    if runner_temp:
        return Path(runner_temp) / "novapeptide" / "designs"
    return ROOT / "data" / "designs"


# ============================================================
# Mutable runtime flags (shared by submodules; not project state)
# ============================================================
# Cached runtime state exposed through explicit accessors so importing the
# module has no side effects.

_RUNTIME_FLAGS = {
    "verified_runtime_signature": None,
    "skip_evidence_logged": False,
}


def get_verified_runtime_signature():
    """Return the cached ColabDesign verification signature (None until set)."""
    return _RUNTIME_FLAGS["verified_runtime_signature"]


def set_verified_runtime_signature(value):
    """Cache a successful ColabDesign environment signature (P1-3)."""
    _RUNTIME_FLAGS["verified_runtime_signature"] = value


def get_skip_evidence_logged():
    """Return whether the skip-verification evidence log already fired."""
    return _RUNTIME_FLAGS["skip_evidence_logged"]


def set_skip_evidence_logged(value):
    """Set whether the skip-verification evidence log already fired."""
    _RUNTIME_FLAGS["skip_evidence_logged"] = value


# ============================================================
# Static design constants (immutable literals; not project state)
# ============================================================

# Geometry gates are deliberately labelled as compatibility checks.  A model
# whose terminal atoms are close enough for a covalent bond is suitable for
# downstream relaxation/validation; coordinates alone do not prove that the
# bond has been chemically formed.
CLOSURE_GEOMETRY = {
    "head-to-tail_amide": {
        "atom_1": "last:C",
        "atom_2": "first:N",
        # The wwPDB validation range for a peptide C-N bond is 1.30-1.45 A.
        # Design uses a wider pre-relax screen and records ideal-range status.
        "screen_range_angstrom": (1.15, 2.00),
        "ideal_range_angstrom": (1.30, 1.45),
    },
    "Cys-Cys_disulfide": {
        "atom_1": "first:SG",
        "atom_2": "last:SG",
        # Typical protein disulfides are close to 2.03 A.  The wider screen
        # tolerates an unrelaxed predictor output without accepting CA proxies.
        "screen_range_angstrom": (1.80, 2.30),
        "ideal_range_angstrom": (1.90, 2.15),
    },
}

# All design constants are read from Research State (via _load_target_spec).
_LOCK = threading.Lock()
CYCLIZATION_PAIRS = [("C", "C"), ("", "")]
LINKER_MATRIX = ["GGGGS", "GGGS", "GGS", "GS", ""]
SCAFFOLD_MUTABLE_AA = "ACDEFGHIKLMNPQRSTVWY"

# Cheap-prefilter cap: controls the number of sequences kept per backbone
# during the global prefilter; does not limit the final candidate count.
# (Route A/B use a global two-phase collect-sort-top-K design.)
HYDROPHOBIC = set("AILMFWV")
POS_CHARGED = set("KR")
NEG_CHARGED = set("DE")
DESIGN_PIPELINE_VERSION = "5.2.1"


# ============================================================
# DesignContext (Engineering Standard §7 / P1-1)
# ============================================================
# Design(context) is the dependency-injected entry point; legacy import-time
# globals (ACTIVE_PROJECT_CONFIG / DEFAULT_OUTPUT_DIR) are now resolved lazily
# by this module on first access.

@dataclass(frozen=True)
class DesignContext:
    """Per-project context injected into :class:`~agents.design.agent.Design`.

    ``project_config`` is the approved target/project config and ``output_dir``
    is where design artifacts are written.  Runtime tool paths
    (CYCPEP_PYTHON / RFDIFF_DIR / ...) are resolved lazily by this module on
    first access; the legacy module-level names remain importable unchanged.

    Examples
    --------
    >>> DesignContext(project_config=approved_config, output_dir="/tmp/x")
    >>> DesignContext.default()  # legacy defaults from ACTIVE_PROJECT_CONFIG
    >>> DesignContext.from_project_context(ProjectContext.default())
    """

    project_config: dict
    output_dir: str = ""

    def __post_init__(self):
        # frozen dataclass forbids normal attribute writes in __post_init__.
        # Lazy module names must be read through the module object (PEP 562
        # __getattr__ does not apply to bare globals inside this module).
        object.__setattr__(
            self, "output_dir",
            self.output_dir or str(_module_attr("DEFAULT_OUTPUT_DIR")),
        )

    @classmethod
    def default(cls) -> "DesignContext":
        """Build the legacy default context from the lazy module state."""
        return cls(
            project_config=_module_attr("ACTIVE_PROJECT_CONFIG"),
            output_dir=str(_module_attr("DEFAULT_OUTPUT_DIR")),
        )

    @classmethod
    def from_project_context(
        cls, project_context: ProjectContext, output_dir: str = ""
    ) -> "DesignContext":
        """Build a Design view over the unified :class:`ProjectContext`.

        ``output_dir`` defaults to the design output root when empty; pass an
        explicit value to isolate artifacts per project.
        """
        return cls(project_config=project_context.config, output_dir=output_dir)
