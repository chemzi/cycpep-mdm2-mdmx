"""Executable action registry.

``contracts.action.ACTION_CATALOG`` is the vocabulary/capability contract;
this module binds its executable entries to the repository's existing handler
map.  Keeping the binding here preserves ``handlers.HANDLERS`` compatibility
while giving Planner, Orchestrator and Worker one capability query.
"""

from __future__ import annotations

from typing import Callable

from contracts.action import ACTION_CATALOG, ActionSpec, ActionType, get_action_spec
from .handlers import HANDLERS


ACTION_REGISTRY: dict[ActionType, ActionSpec] = dict(ACTION_CATALOG)
REGISTRY = ACTION_REGISTRY


def handler_for(action: str | ActionType) -> Callable | None:
    spec = get_action_spec(action)
    if not spec.executable or spec.handler_name is None:
        return None
    return HANDLERS.get(spec.handler_name)


def executable_actions() -> frozenset[ActionType]:
    return frozenset(action for action, spec in ACTION_REGISTRY.items() if spec.executable)


def validate_registry() -> None:
    """Fail fast if a typed executable capability lacks its real handler."""

    missing = [
        action.value
        for action, spec in ACTION_REGISTRY.items()
        if spec.executable and (spec.handler_name is None or HANDLERS.get(spec.handler_name) is None)
    ]
    if missing:
        raise RuntimeError(f"executable action registry entries lack handlers: {missing}")


validate_registry()

__all__ = [
    "ACTION_REGISTRY",
    "REGISTRY",
    "executable_actions",
    "get_action_spec",
    "handler_for",
    "validate_registry",
]
