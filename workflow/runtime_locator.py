"""Durable internal runtime-location reconstruction for Launcher commands."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable

from core.context import ProjectContext

from .errors import DiagnosticContractError
from .models import DiagnosticReport, RuntimeLocatorBinding


ContextLoader = Callable[[str | Path], ProjectContext]
ContextRestorer = Callable[[RuntimeLocatorBinding], ProjectContext]


def require_runtime_locator(report: DiagnosticReport) -> RuntimeLocatorBinding:
    """Return a complete report-bound locator or fail without ambient fallback."""

    binding = report.runtime_locator_binding
    if binding is None:
        raise DiagnosticContractError(
            "launcher_runtime_locator_unavailable",
            "The original Launcher runtime locator is unavailable.",
        )
    if _canonical(binding.project_locator) != _canonical(report.project_locator):
        raise DiagnosticContractError(
            "launcher_runtime_locator_conflict",
            "The approved-project locator conflicts with this launcher run.",
        )
    return binding


def restore_project_context(
    binding: RuntimeLocatorBinding,
    *,
    loader: ContextLoader | None = None,
) -> ProjectContext:
    """Load project content and inject only the original durable path set."""

    try:
        context = (
            ProjectContext.load(path=binding.project_locator)
            if loader is None
            else loader(binding.project_locator)
        )
        return replace(context, paths=binding.project_paths())
    except DiagnosticContractError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise DiagnosticContractError(
            "launcher_runtime_locator_unavailable",
            "The original Launcher runtime locator cannot be restored.",
        ) from error


def require_formal_store(
    binding: RuntimeLocatorBinding, context: ProjectContext
) -> None:
    """Fail closed unless the original formal Store validates read-only."""

    from data_layer import validate_storage_backend
    from storage import StorageUnavailableError

    try:
        validate_storage_backend(
            binding.database_path, project_id=context.project_id
        )
    except (OSError, StorageUnavailableError) as error:
        raise DiagnosticContractError(
            "launcher_runtime_locator_unavailable",
            "The original Launcher formal Store is unavailable.",
        ) from error


def _canonical(value: str) -> Path:
    return Path(value).expanduser().resolve()


__all__ = [
    "ContextLoader",
    "ContextRestorer",
    "require_formal_store",
    "require_runtime_locator",
    "restore_project_context",
]
