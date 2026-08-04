"""Trusted server-side paths for Execution handlers.

Planner tasks cannot override these paths.  Production deployment may set the
documented environment variables in the Worker service environment; the web
API never forwards arbitrary environment variables into a task.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _path(name: str, default: str | Path) -> Path:
    return Path(os.environ.get(name, str(default))).expanduser().resolve()


def _optional_path(name: str, default: str | Path | None = None) -> Path | None:
    value = os.environ.get(name)
    if value:
        return Path(value).expanduser().resolve()
    if default is None:
        return None
    path = Path(default).expanduser().resolve()
    return path if path.exists() else None


@dataclass(frozen=True)
class ExecutionConfig:
    repo_root: Path
    execution_root: Path
    core_python: Path
    design_python: Path
    prediction_python: Path
    prediction_artifacts_root: Path
    prediction_runs_root: Path
    colabdesign_dir: Path
    colabdesign_params: Path
    cuda_data_dir: Path
    boltz_executable: Path | None
    boltz_cache: Path | None
    boltz_checkpoint: Path | None
    prodigy_executable: Path | None
    pyrosetta_python: Path | None
    control_data_path: Path | None
    project_config_path: Path | None
    target_structures_root: Path
    research_timeout_seconds: int = 3600
    design_timeout_seconds: int = 7200
    prediction_timeout_seconds: int = 3600
    rosetta_timeout_seconds: int = 1800
    post_relax_timeout_seconds: int = 3600

    @classmethod
    def from_environment(cls) -> "ExecutionConfig":
        data_root = _path(
            "NP_DATA",
            os.environ.get("CYCPEP_DATA_DIR", ROOT / "data"),
        )
        execution_root = _path(
            "CYCPEP_EXECUTION_ROOT", data_root / "execution_runs"
        )
        core_default = Path(sys.executable).resolve()
        prediction_default = Path(
            "/root/damodel-tmp/envs/cycpep-prediction/bin/python"
        )
        if not prediction_default.is_file():
            prediction_default = core_default
        colabdesign_dir = _path(
            "COLABDESIGN_DIR", "/root/workspace/NovaPeptide/tools/ColabDesign"
        )
        return cls(
            repo_root=ROOT,
            execution_root=execution_root,
            core_python=_path("CYCPEP_EXECUTION_PYTHON", core_default),
            design_python=_path("CYCPEP_DESIGN_AGENT_PYTHON", core_default),
            prediction_python=_path("CYCPEP_PREDICTION_PYTHON", prediction_default),
            prediction_artifacts_root=_path(
                "CYCPEP_PREDICTION_ARTIFACTS",
                data_root / "prediction_artifacts",
            ),
            prediction_runs_root=_path(
                "CYCPEP_PREDICTION_ROOT", data_root / "prediction_runs"
            ),
            colabdesign_dir=colabdesign_dir,
            colabdesign_params=_path(
                "COLABDESIGN_PARAMS", colabdesign_dir / "params"
            ),
            cuda_data_dir=_path("XLA_CUDA_DIR", "/usr/local/cuda"),
            boltz_executable=_optional_path(
                "CYCPEP_BOLTZ_EXECUTABLE",
                "/root/damodel-tmp/envs/boltz-2.2.1/bin/boltz",
            ),
            boltz_cache=_optional_path(
                "CYCPEP_BOLTZ_CACHE",
                "/root/damodel-tmp/novapeptide/boltz_cache",
            ),
            boltz_checkpoint=_optional_path(
                "CYCPEP_BOLTZ_CHECKPOINT",
                "/root/damodel-tmp/novapeptide/boltz_cache/boltz2_conf.ckpt",
            ),
            prodigy_executable=_optional_path(
                "CYCPEP_PRODIGY_EXECUTABLE",
                "/root/damodel-tmp/envs/cycpep-prediction/bin/prodigy",
            ),
            pyrosetta_python=_optional_path(
                "CYCPEP_PYROSETTA_PYTHON",
                "/root/damodel-tmp/envs/pyrosetta-2026.29-minsizerel/bin/python",
            ),
            control_data_path=_optional_path("CYCPEP_CONTROL_DATA"),
            project_config_path=_optional_path("CYCPEP_PROJECT_CONFIG"),
            target_structures_root=_path(
                "CYCPEP_TARGET_STRUCTURES_ROOT", data_root / "target_structures"
            ),
            research_timeout_seconds=int(
                os.environ.get("CYCPEP_EXECUTION_RESEARCH_TIMEOUT", "3600")
            ),
            design_timeout_seconds=int(
                os.environ.get("CYCPEP_EXECUTION_DESIGN_TIMEOUT", "7200")
            ),
            prediction_timeout_seconds=int(
                os.environ.get("CYCPEP_EXECUTION_PREDICTION_TIMEOUT", "3600")
            ),
            rosetta_timeout_seconds=int(
                os.environ.get("CYCPEP_EXECUTION_ROSETTA_TIMEOUT", "1800")
            ),
            post_relax_timeout_seconds=int(
                os.environ.get("CYCPEP_EXECUTION_POST_RELAX_TIMEOUT", "3600")
            ),
        )

    def task_dir(self, run_id: str, task_id: str, attempt: int) -> Path:
        return (
            self.execution_root
            / run_id
            / task_id
            / f"attempt_{attempt}"
        ).resolve()
