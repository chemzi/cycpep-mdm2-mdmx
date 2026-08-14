"""Public, side-effect-free Prediction tool-path validation."""

from __future__ import annotations

from pathlib import Path

from .config import ExecutionConfig
from .contracts import ExecutionContractError


def validate_required_prediction_tool_paths(
    config: ExecutionConfig,
) -> dict[str, Path]:
    """Return required full-Prediction paths or fail with the existing contract."""

    required = {
        "boltz_executable": config.boltz_executable,
        "boltz_cache": config.boltz_cache,
        "boltz_checkpoint": config.boltz_checkpoint,
        "prodigy_executable": config.prodigy_executable,
        "pyrosetta_python": config.pyrosetta_python,
    }
    missing = sorted(
        name for name, path in required.items()
        if path is None or not Path(path).exists()
    )
    if missing:
        raise ExecutionContractError(
            "prediction_toolchain_incomplete",
            f"full Prediction requires configured tools: {missing}",
        )
    return {name: Path(path) for name, path in required.items() if path is not None}


__all__ = ["validate_required_prediction_tool_paths"]
