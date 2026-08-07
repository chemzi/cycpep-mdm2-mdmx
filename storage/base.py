"""Backend-neutral storage contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterable, Literal, Mapping


class StateStore(ABC):
    @abstractmethod
    def get_state(self, project_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def update_state(self, project_id: str, patches: Mapping[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def append_state_item_if_absent(
        self,
        project_id: str,
        key: str,
        item: Mapping[str, Any],
        *,
        identity_path: Iterable[str],
        identity_value: Any,
    ) -> dict[str, Any]: ...


class CandidateStore(ABC):
    @abstractmethod
    def get(self, candidate_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def upsert(
        self,
        candidate: Mapping[str, Any],
        *,
        duplicate_policy: Literal["update", "insert_only", "raise_duplicate"] = "update",
    ) -> dict[str, Any]: ...

    @abstractmethod
    def list(self, *, status: str | None = None) -> list[dict[str, Any]]: ...


class EvidenceStore(ABC):
    @abstractmethod
    def append(self, event: Mapping[str, Any]) -> str: ...

    @abstractmethod
    def query(self, **filters: Any) -> list[dict[str, Any]]: ...


class TransactionStore(ABC):
    @abstractmethod
    def reserve_candidate_ids(self, count: int = 1) -> list[str]: ...

    @abstractmethod
    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def get_transaction_status(self, transaction_id: str) -> str | None: ...

    @abstractmethod
    def commit_transaction(
        self,
        *,
        context: Mapping[str, Any],
        candidate_updates: Iterable[Mapping[str, Any]],
        state_updates: Mapping[str, Any],
        state_appends: Iterable[Mapping[str, Any]],
        artifacts: Iterable[Mapping[str, Any]],
        evidence_events: Iterable[Mapping[str, Any]] = (),
    ) -> list[str]: ...

    @abstractmethod
    def record_task_failure(
        self, *, context: Mapping[str, Any], error: Mapping[str, Any]
    ) -> None: ...

    @abstractmethod
    def rollback_transaction(self, transaction_id: str) -> list[dict[str, Any]]: ...

class Store(StateStore, CandidateStore, EvidenceStore, TransactionStore):
    """Business-facing store contract; no SQL operations are exposed."""

    def append_many(self, events: Iterable[Mapping[str, Any]]) -> list[str]:
        return [self.append(event) for event in events]
