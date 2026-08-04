"""Closed-world execution worker for NovaPeptide Planner tasks.

Contract symbols are loaded lazily so project-scoped command entry points can
set ``CYCPEP_*`` paths before importing modules that bind ``data_layer`` paths.
"""

from importlib import import_module

__all__ = [
    "CORE_ACTIONS",
    "EXECUTION_SCHEMA_VERSION",
    "V2_RESERVED_ACTIONS",
    "ExecutionContractError",
]


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(name)
    return getattr(import_module(".contracts", __name__), name)
