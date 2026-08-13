"""Launcher-scoped Workbench reads over the exact bound formal Store."""

from __future__ import annotations

from typing import Any, Callable

from data_layer import get_storage_backend
from workflow.operator_control import bound_launcher_project
from workflow.service import LauncherServiceDependencies

from .workbench import DEFAULT_COLLECTION_LIMIT, WorkbenchReader


StoreFactory = Callable[..., Any]
ReaderFactory = Callable[[Any], WorkbenchReader]


def read_launcher_workbench(
    *,
    launcher_run_id: str,
    limit: int = DEFAULT_COLLECTION_LIMIT,
    launcher_dependencies: LauncherServiceDependencies | None = None,
    store_factory: StoreFactory | None = None,
    reader_factory: ReaderFactory = WorkbenchReader,
) -> dict[str, Any]:
    """Read one run's project without consulting an adapter startup Store."""

    create_store = store_factory or get_storage_backend
    with bound_launcher_project(launcher_run_id, launcher_dependencies):
        store = create_store(read_only=True)
        return reader_factory(store).read(limit=limit)


__all__ = ["read_launcher_workbench"]
