"""Process-scoped compatibility binding for legacy Data Layer callers.

New Launcher code passes :class:`core.context.ProjectContext` explicitly.
Several existing public Agent seams still reach the legacy module-level Data
Layer facade internally, however.  This adapter binds that facade to the same
project for the duration of one Launcher command and restores every changed
value afterwards.  It owns no state and adds no persistence format.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import data_layer
from core.context import ProjectContext


_PROCESS_CONTEXT_LOCK = threading.RLock()
_MISSING = object()
_DATA_LAYER_KEYS = (
    "ACTIVE_PROJECT_CONFIG",
    "DATA_DIR",
    "EVIDENCE_DIR",
    "STATE_PATH",
    "LOG_PATH",
    "INDEX_PATH",
    "SQLITE_DB_PATH",
)
_RESEARCH_KEYS = (
    "PROJECT_CONFIG",
    "DATA_DIR",
    "EVIDENCE_DIR",
    "CACHE_PATH",
    "THRESHOLDS_CACHE",
)


@contextmanager
def bind_project_context(context: ProjectContext) -> Iterator[None]:
    """Temporarily bind legacy facades to one explicit project context.

    The in-process lock prevents two Launcher commands from switching the
    compatibility facade underneath each other.  The formal SQLite Store is
    still the sole authority; this function merely selects its existing
    project/path context.
    """
    if not isinstance(context, ProjectContext):
        raise TypeError("context must be a ProjectContext")
    resolved = context.resolve_paths()
    if resolved.data_dir is None or resolved.evidence_dir is None:
        raise ValueError("ProjectContext must resolve data and evidence paths")
    data_dir = Path(resolved.data_dir).expanduser().resolve()
    evidence_dir = Path(resolved.evidence_dir).expanduser().resolve()

    with _PROCESS_CONTEXT_LOCK:
        from agents import research

        data_snapshot = _snapshot_module(data_layer, _DATA_LAYER_KEYS)
        research_snapshot = _snapshot_module(research, _RESEARCH_KEYS)
        runtime_paths = data_layer._runtime_paths
        state_project = data_layer.State.__dict__["_project_config"]
        state_default = data_layer.State.__dict__["_default"]
        try:
            bindings = {
                "ACTIVE_PROJECT_CONFIG": dict(context.config),
                "DATA_DIR": data_dir,
                "EVIDENCE_DIR": evidence_dir,
                "STATE_PATH": data_dir / "state.json",
                "LOG_PATH": evidence_dir / "evidence_log.jsonl",
                "INDEX_PATH": data_dir / "candidate_index.csv",
            }
            for name, value in bindings.items():
                setattr(data_layer, name, value)
            # Data Layer owns formal database selection.  Leaving its public
            # SQLITE_DB_PATH binding untouched preserves an explicit runtime
            # selection; when absent, its lazy resolver applies the documented
            # CYCPEP_DB_PATH override and only then falls back to the bound
            # DATA_DIR/store.db.
            data_layer._runtime_paths = {
                "data_dir": data_dir,
                "evidence_dir": evidence_dir,
                "state_path": bindings["STATE_PATH"],
                "log_path": bindings["LOG_PATH"],
                "index_path": bindings["INDEX_PATH"],
            }
            data_layer.State._project_config = dict(context.config)
            data_layer.State._default = data_layer.default_state(dict(context.config))

            research.PROJECT_CONFIG = dict(context.config)
            research.DATA_DIR = data_dir
            research.EVIDENCE_DIR = evidence_dir
            research.CACHE_PATH = data_dir / (
                "_research_cache.json"
                if set(context.targets) == {"MDM2", "MDMX"}
                else f"_research_cache_{_safe_project_slug(context.project_id)}.json"
            )
            research.THRESHOLDS_CACHE = data_dir / "_thresholds_cache.json"
            yield
        finally:
            data_layer.State._project_config = state_project
            data_layer.State._default = state_default
            _restore_module(research, research_snapshot)
            _restore_module(data_layer, data_snapshot)
            data_layer._runtime_paths = runtime_paths


def _safe_project_slug(project_id: str) -> str:
    from project_config import target_slug

    return target_slug(project_id)


def _snapshot_module(module, names: tuple[str, ...]) -> dict[str, object]:
    namespace = vars(module)
    return {name: namespace.get(name, _MISSING) for name in names}


def _restore_module(module, snapshot: dict[str, object]) -> None:
    for name, value in snapshot.items():
        if value is _MISSING:
            vars(module).pop(name, None)
        else:
            setattr(module, name, value)


__all__ = ["bind_project_context"]
