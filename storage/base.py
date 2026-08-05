"""Backend-neutral storage contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterable, Mapping


class StateStore(ABC):
    @abstractmethod
    def get_state(self, project_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def update_state(self, project_id: str, patches: Mapping[str, Any]) -> dict[str, Any]: ...


class CandidateStore(ABC):
    @abstractmethod
    def get(self, candidate_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def upsert(self, candidate: Mapping[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def list(self, *, status: str | None = None) -> list[dict[str, Any]]: ...


class EvidenceStore(ABC):
    @abstractmethod
    def append(self, event: Mapping[str, Any]) -> str: ...

    @abstractmethod
    def query(self, **filters: Any) -> list[dict[str, Any]]: ...


class Store(StateStore, CandidateStore, EvidenceStore):
    """Business-facing store contract; no SQL operations are exposed."""

    def append_many(self, events: Iterable[Mapping[str, Any]]) -> list[str]:
        return [self.append(event) for event in events]
