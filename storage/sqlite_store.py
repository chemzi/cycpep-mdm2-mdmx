"""SQLite source of truth for project state, candidates and evidence."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping

from contracts.event import EvidenceEvent

from .base import Store
from .sqlite_ownership import (
    TERMINAL_TRANSACTION_STATUSES,
    SQLiteOwnership,
    assert_transaction_transition,
    patch_candidate_value,
)
from .sqlite_schema import ensure_schema


_BUSY_TIMEOUT_MS = 30_000

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("store records must be mappings")
    return dict(value)


_MISSING = object()


def _path_value(value: Any, path: Iterable[str]) -> Any:
    current = value
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return _MISSING
        current = current[key]
    return current


class SQLiteStore(Store):
    """Single-writer/multi-reader store with explicit atomic write boundaries."""

    def __init__(self, path: str | Path, *, project_id: str = "default"):
        self.path = Path(path)
        self.project_id = project_id
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        return connection

    @contextmanager
    def _write(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    @contextmanager
    def _read(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._read() as connection:
            ensure_schema(connection)
        with self._write() as connection:
            self._ensure_project(connection)
            connection.execute(
                "UPDATE candidates SET project_id = ? WHERE project_id IS NULL",
                (self.project_id,),
            )
            sequence = max(
                (
                    int(row[0][1:])
                    for row in connection.execute(
                        "SELECT candidate_id FROM candidates WHERE project_id = ?",
                        (self.project_id,),
                    )
                    if str(row[0]).startswith("C") and str(row[0])[1:].isdigit()
                ),
                default=0,
            )
            connection.execute(
                "UPDATE candidate_sequences SET current_value = MAX(current_value, ?) WHERE project_id = ?",
                (sequence, self.project_id),
            )

    def _ensure_project(
        self, connection: sqlite3.Connection, project_id: str | None = None
    ) -> None:
        project_id = project_id or self.project_id
        now = _now()
        connection.execute(
            "INSERT INTO projects(project_id, created_at, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(project_id) DO UPDATE SET updated_at=excluded.updated_at",
            (project_id, now, now),
        )
        connection.execute(
            "INSERT OR IGNORE INTO candidate_sequences(project_id, current_value) VALUES (?, 0)",
            (project_id,),
        )

    def get_state(self, project_id: str | None = None) -> dict[str, Any]:
        project_id = project_id or self.project_id
        with self._read() as connection:
            row = connection.execute(
                "SELECT payload_json FROM states WHERE project_id = ?", (project_id,)
            ).fetchone()
        return json.loads(row["payload_json"]) if row else {}

    def update_state(self, project_id: str, patches: Mapping[str, Any]) -> dict[str, Any]:
        updates = dict(patches)
        with self._write() as connection:
            self._ensure_project(connection, project_id)
            state = self._state_in(connection, project_id)
            state.update(updates)
            self._write_state(connection, project_id, state)
            ownership = SQLiteOwnership(connection, project_id)
            for key in updates:
                ownership.advance_state(key, None)
        return state

    def replace_state(self, project_id: str, value: Mapping[str, Any]) -> dict[str, Any]:
        state = dict(value)
        with self._write() as connection:
            self._ensure_project(connection, project_id)
            previous = self._state_in(connection, project_id)
            self._write_state(connection, project_id, state)
            ownership = SQLiteOwnership(connection, project_id)
            for key in set(previous) | set(state):
                ownership.advance_state(key, None)
        return state

    def initialize_state(self, project_id: str, value: Mapping[str, Any]) -> dict[str, Any]:
        with self._write() as connection:
            self._ensure_project(connection, project_id)
            now = _now()
            state = dict(value)
            connection.execute(
                "INSERT OR IGNORE INTO states(project_id, phase, round, active_workflow_id, updated_at, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    state.get("phase"),
                    state.get("round"),
                    state.get("active_workflow_id"),
                    now,
                    _json(state),
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0]:
                ownership = SQLiteOwnership(connection, project_id)
                for key in state:
                    ownership.advance_state(key, None)
            return self._state_in(connection, project_id)

    def append_state_item(self, project_id: str, key: str, item: Mapping[str, Any]) -> dict[str, Any]:
        with self._write() as connection:
            self._ensure_project(connection, project_id)
            state = self._state_in(connection, project_id)
            values = list(state.get(key) or [])
            values.append(dict(item))
            state[key] = values
            self._write_state(connection, project_id, state)
            SQLiteOwnership(connection, project_id).advance_state(key, None)
            return state

    def append_state_item_if_absent(
        self,
        project_id: str,
        key: str,
        item: Mapping[str, Any],
        *,
        identity_path: Iterable[str],
        identity_value: Any,
    ) -> dict[str, Any]:
        with self._write() as connection:
            self._ensure_project(connection, project_id)
            state = self._state_in(connection, project_id)
            values = list(state.get(key) or [])
            path = tuple(identity_path)
            if not any(_path_value(value, path) == identity_value for value in values):
                values.append(dict(item))
                state[key] = values
                self._write_state(connection, project_id, state)
                SQLiteOwnership(connection, project_id).advance_state(key, None)
            return state

    @staticmethod
    def _state_in(connection: sqlite3.Connection, project_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT payload_json FROM states WHERE project_id = ?", (project_id,)
        ).fetchone()
        return json.loads(row["payload_json"]) if row else {}

    @staticmethod
    def _write_state(connection: sqlite3.Connection, project_id: str, state: Mapping[str, Any]) -> None:
        now = _now()
        connection.execute(
            "INSERT INTO states(project_id, phase, round, active_workflow_id, updated_at, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(project_id) DO UPDATE SET "
            "phase=excluded.phase, round=excluded.round, active_workflow_id=excluded.active_workflow_id, "
            "updated_at=excluded.updated_at, payload_json=excluded.payload_json",
            (
                project_id,
                state.get("phase"),
                state.get("round"),
                state.get("active_workflow_id"),
                now,
                _json(dict(state)),
            ),
        )

    def reserve_candidate_ids(self, count: int = 1) -> list[str]:
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("count must be a positive integer")
        with self._write() as connection:
            self._ensure_project(connection)
            row = connection.execute(
                "SELECT current_value FROM candidate_sequences WHERE project_id = ?",
                (self.project_id,),
            ).fetchone()
            start = int(row["current_value"]) + 1
            end = start + count - 1
            connection.execute(
                "UPDATE candidate_sequences SET current_value = ? WHERE project_id = ?",
                (end, self.project_id),
            )
        return [f"C{value:04d}" for value in range(start, end + 1)]

    def get(self, candidate_id: str) -> dict[str, Any] | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT payload_json FROM candidates WHERE project_id = ? AND candidate_id = ?",
                (self.project_id, candidate_id),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def upsert(
        self,
        candidate: Mapping[str, Any],
        *,
        duplicate_policy: Literal["update", "insert_only", "raise_duplicate"] = "update",
    ) -> dict[str, Any]:
        value = _mapping(candidate)
        with self._write() as connection:
            self._ensure_project(connection)
            result = self._put_candidate(connection, value, duplicate_policy)
            self._advance_candidate_count(connection, [result["candidate_id"]])
        return result

    def add_candidates(self, candidates: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        values = [_mapping(item) for item in candidates]
        with self._write() as connection:
            self._ensure_project(connection)
            results = [self._put_candidate(connection, item, "raise_duplicate") for item in values]
            self._advance_candidate_count(
                connection, [item["candidate_id"] for item in results]
            )
        return results

    def _advance_candidate_count(
        self,
        connection: sqlite3.Connection,
        candidate_ids: Iterable[str],
        writer_transaction_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return SQLiteOwnership(connection, self.project_id).advance_candidate_count(
            self._state_in(connection, self.project_id),
            candidate_ids,
            writer_transaction_id,
            write_state=lambda state: self._write_state(
                connection, self.project_id, state
            ),
        )

    def update_candidate(self, candidate_id: str, patches: Mapping[str, Any]) -> dict[str, Any]:
        with self._write() as connection:
            row = connection.execute(
                "SELECT payload_json FROM candidates WHERE project_id = ? AND candidate_id = ?",
                (self.project_id, candidate_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"candidate_id not found: {candidate_id}")
            value = json.loads(row["payload_json"])
            value = patch_candidate_value(value, patches)
            result = self._put_candidate(connection, value, "update")
        return result

    def _put_candidate(
        self,
        connection: sqlite3.Connection,
        value: Mapping[str, Any],
        policy: Literal["update", "insert_only", "raise_duplicate"],
        writer_transaction_id: str | None = None,
    ) -> dict[str, Any]:
        candidate_id = str(value.get("candidate_id") or "")
        sequence = str(value.get("sequence") or "")
        if not candidate_id or not sequence:
            raise ValueError("candidate_id and sequence are required")
        row = connection.execute(
            "SELECT payload_json FROM candidates WHERE project_id = ? AND candidate_id = ?",
            (self.project_id, candidate_id),
        ).fetchone()
        existing = json.loads(row["payload_json"]) if row else None
        if existing is not None and policy == "raise_duplicate":
            raise ValueError(f"duplicate candidate_id: {candidate_id}")
        if existing is not None and policy == "insert_only":
            return existing
        now = _now()
        merged = {**(existing or {}), **dict(value)}
        merged["created_at"] = (existing or {}).get("created_at", now)
        merged["updated_at"] = now
        metrics = merged.get("metrics_json", {})
        if isinstance(metrics, str):
            try:
                metrics = json.loads(metrics)
            except json.JSONDecodeError:
                metrics = {}
        connection.execute(
            "INSERT INTO candidates(candidate_id, project_id, sequence, status, metrics_json, created_at, updated_at, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(candidate_id) DO UPDATE SET "
            "sequence=excluded.sequence, status=excluded.status, metrics_json=excluded.metrics_json, "
            "updated_at=excluded.updated_at, payload_json=excluded.payload_json",
            (
                candidate_id,
                self.project_id,
                sequence,
                merged.get("status") or merged.get("final_status"),
                _json(metrics),
                merged["created_at"],
                now,
                _json(merged),
            ),
        )
        SQLiteOwnership(connection, self.project_id).advance_candidate(
            candidate_id, writer_transaction_id
        )
        return merged

    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT payload_json FROM candidates WHERE project_id = ?"
        params: list[Any] = [self.project_id]
        if status is not None:
            query += " AND (status = ? OR json_extract(payload_json, '$.final_status') = ?)"
            params.extend([status, status])
        query += " ORDER BY candidate_id"
        with self._read() as connection:
            rows = connection.execute(query, params).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def append(self, event: Mapping[str, Any]) -> str:
        with self._write() as connection:
            return self._append_event(connection, event)

    @staticmethod
    def _append_event(connection: sqlite3.Connection, event: Mapping[str, Any]) -> str:
        value = _mapping(event)
        event_id = str(value.get("event_id") or uuid.uuid4())
        timestamp = str(value.get("timestamp") or _now())
        payload = dict(value)
        for key in (
            "event_id", "timestamp", "transaction_id", "workflow_id", "run_id", "task_id",
            "candidate_id", "agent", "event_type",
        ):
            payload.pop(key, None)
        connection.execute(
            "INSERT INTO evidence_events(event_id, transaction_id, workflow_id, run_id, task_id, candidate_id, agent, event_type, timestamp, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                value.get("transaction_id"),
                value.get("workflow_id"),
                value.get("run_id"),
                value.get("task_id"),
                value.get("candidate_id"),
                value.get("agent"),
                value.get("event_type"),
                timestamp,
                _json(payload),
            ),
        )
        return event_id

    @staticmethod
    def _append_formal_event(
        connection: sqlite3.Connection, event: Mapping[str, Any]
    ) -> str:
        """Validate newly committed evidence without breaking legacy ingestion."""
        value = _mapping(event)
        value.setdefault("event_id", str(uuid.uuid4()))
        value.setdefault("timestamp", _now())
        EvidenceEvent.from_dict(value)
        return SQLiteStore._append_event(connection, value)

    def query(self, **filters: Any) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for key in (
            "transaction_id", "workflow_id", "run_id", "task_id", "candidate_id",
            "agent", "event_type",
        ):
            if filters.get(key) is not None:
                if key == "transaction_id":
                    clauses.append(
                        "(transaction_id = ? OR (transaction_id IS NULL AND "
                        "json_extract(payload_json, '$.transaction_id') = ?))"
                    )
                    params.append(filters[key])
                else:
                    clauses.append(f"{key} = ?")
                params.append(filters[key])
        sql = "SELECT * FROM evidence_events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY timestamp, rowid"
        with self._read() as connection:
            rows = connection.execute(sql, params).fetchall()
        events = []
        for row in rows:
            event = {
                key: row[key]
                for key in (
                    "event_id", "transaction_id", "workflow_id", "run_id", "task_id", "candidate_id",
                    "agent", "event_type", "timestamp",
                )
                if row[key] is not None
            }
            event.update(json.loads(row["payload_json"]))
            events.append(event)
        return events

    def trace_workflow(self, workflow_id: str) -> list[dict[str, Any]]:
        return self.query(workflow_id=workflow_id)

    def trace_task(self, task_id: str) -> list[dict[str, Any]]:
        return self.query(task_id=task_id)

    def trace_candidate(self, candidate_id: str) -> list[dict[str, Any]]:
        return self.query(candidate_id=candidate_id)

    def register_artifact(self, artifact: Mapping[str, Any]) -> str:
        value = _mapping(artifact)
        artifact_id = str(value.get("artifact_id") or uuid.uuid4())
        with self._write() as connection:
            connection.execute(
                "INSERT INTO artifacts(artifact_id, artifact_type, path, size_bytes, sha256, producer_task_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(artifact_id) DO UPDATE SET "
                "artifact_type=excluded.artifact_type, path=excluded.path, "
                "size_bytes=excluded.size_bytes, sha256=excluded.sha256, "
                "producer_task_id=excluded.producer_task_id",
                (
                    artifact_id,
                    value.get("artifact_type"),
                    value.get("path"),
                    value.get("size_bytes"),
                    value.get("sha256"),
                    value.get("producer_task_id"),
                    value.get("created_at") or _now(),
                ),
            )
        return artifact_id

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
        return dict(row) if row else None

    def commit_transaction(
        self,
        *,
        context: Mapping[str, Any],
        candidate_updates: Iterable[Mapping[str, Any]],
        candidate_patches: Iterable[Mapping[str, Any]] = (),
        state_updates: Mapping[str, Any],
        state_appends: Iterable[Mapping[str, Any]],
        artifacts: Iterable[Mapping[str, Any]],
        evidence_events: Iterable[Mapping[str, Any]] = (),
    ) -> list[str]:
        """Atomically publish one execution transaction and post-commit evidence."""
        context_value = _mapping(context)
        transaction_id = str(context_value["transaction_id"])
        candidate_updates = list(candidate_updates)
        candidate_patches = [_mapping(item) for item in candidate_patches]
        state_appends = [_mapping(item) for item in state_appends]
        artifacts = list(artifacts)
        evidence_events = list(evidence_events)
        now = _now()
        event_ids: list[str] = []
        with self._write() as connection:
            existing = connection.execute(
                "SELECT status, payload_json FROM execution_transactions WHERE transaction_id = ?",
                (transaction_id,),
            ).fetchone()
            if existing and existing["status"] == "COMMITTED":
                return list(json.loads(existing["payload_json"]).get("event_ids") or [])
            if existing:
                raise ValueError(
                    f"transaction {transaction_id} is already {existing['status']}"
                )
            self._ensure_project(connection)
            ownership = SQLiteOwnership(connection, self.project_id)
            candidates, candidate_effects = ownership.apply_candidates(
                candidate_updates,
                candidate_patches,
                transaction_id,
                put_candidate=lambda value, policy, writer: self._put_candidate(
                    connection, value, policy, writer
                ),
            )
            state_effects = ownership.apply_state(
                self._state_in(connection, self.project_id),
                state_updates,
                state_appends,
                [item["candidate_id"] for item in candidates],
                transaction_id,
                write_state=lambda state: self._write_state(
                    connection, self.project_id, state
                ),
            )
            for artifact in artifacts:
                value = _mapping(artifact)
                connection.execute(
                    "INSERT INTO artifacts(artifact_id, artifact_type, path, size_bytes, sha256, producer_task_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(value["artifact_id"]),
                        value.get("artifact_type"),
                        value.get("path"),
                        value.get("size_bytes"),
                        value.get("sha256"),
                        context_value.get("task_id"),
                        now,
                    ),
                )
            for candidate in candidates:
                event_ids.append(self._append_formal_event(connection, {
                    "workflow_id": context_value.get("workflow_id"),
                    "run_id": context_value.get("run_id"),
                    "task_id": context_value.get("task_id"),
                    "candidate_id": candidate["candidate_id"],
                    "agent": "design",
                    "event_type": "candidate_registered",
                    "transaction_id": transaction_id,
                    "attempt_id": context_value.get("attempt_id"),
                    "candidate": candidate,
                }))
            for event in evidence_events:
                formal_event = dict(_mapping(event))
                formal_event.update({
                    "workflow_id": context_value.get("workflow_id"),
                    "run_id": context_value.get("run_id"),
                    "task_id": context_value.get("task_id"),
                    "attempt_id": context_value.get("attempt_id"),
                    "transaction_id": transaction_id,
                })
                event_ids.append(self._append_formal_event(connection, formal_event))
            event_ids.append(self._append_formal_event(connection, {
                "workflow_id": context_value.get("workflow_id"),
                "run_id": context_value.get("run_id"),
                "task_id": context_value.get("task_id"),
                "agent": "execution",
                "event_type": "execution_transaction_committed",
                "transaction_id": transaction_id,
                "attempt_id": context_value.get("attempt_id"),
                "action": context_value.get("action"),
            }))
            transaction_payload = dict(
                context_value,
                event_ids=event_ids,
                candidate_ids=[item["candidate_id"] for item in candidates],
                candidate_effects=candidate_effects,
                artifact_ids=[str(item["artifact_id"]) for item in artifacts],
                state_effects=state_effects,
            )
            connection.execute(
                "INSERT INTO execution_transactions(transaction_id, task_id, attempt_id, status, created_at, updated_at, payload_json) "
                "VALUES (?, ?, ?, 'COMMITTED', ?, ?, ?)",
                (
                    transaction_id,
                    str(context_value["task_id"]),
                    str(context_value["attempt_id"]),
                    context_value.get("created_at", now),
                    now,
                    _json(transaction_payload),
                ),
            )
        return event_ids

    def rollback_transaction(self, transaction_id: str) -> list[dict[str, Any]]:
        """Compensate a committed effect set when orchestration cannot close."""
        with self._write() as connection:
            row = connection.execute(
                "SELECT status, payload_json FROM execution_transactions WHERE transaction_id = ?",
                (transaction_id,),
            ).fetchone()
            if row is None or row["status"] == "ROLLED_BACK":
                return []
            if row["status"] not in {"COMMITTED", "COMPENSATION_CONFLICT"}:
                return [{"kind": "transaction_status", "status": row["status"]}]
            payload = json.loads(row["payload_json"])
            compensation_event_ids = list(payload.get("compensation_event_ids") or [])
            compensation_event_ids.append(self._append_transaction_event(
                connection,
                payload,
                "execution_transaction_compensation_started",
            ))
            ownership = SQLiteOwnership(connection, self.project_id)
            state_effects = payload.get("state_effects")
            if state_effects is None and payload.get("previous_state") is not None:
                conflicts = [{"kind": "legacy_state_snapshot"}]
            else:
                conflicts = ownership.state_conflicts(transaction_id, state_effects or [])
            candidate_effects = payload.get("candidate_effects")
            if candidate_effects is None and payload.get("candidate_ids"):
                conflicts.append({"kind": "legacy_candidate_ownership"})
            else:
                conflicts.extend(
                    ownership.candidate_conflicts(transaction_id, candidate_effects or [])
                )
            payload["compensation_conflicts"] = conflicts
            payload["compensation_event_ids"] = compensation_event_ids
            if conflicts:
                compensation_event_ids.append(self._append_transaction_event(
                    connection,
                    payload,
                    "execution_transaction_compensation_conflict",
                    conflicts=conflicts,
                ))
                payload["compensation_event_ids"] = compensation_event_ids
                self._set_transaction_status(
                    connection, transaction_id, "COMPENSATION_CONFLICT", payload
                )
                return conflicts
            ownership.compensate_state(
                self._state_in(connection, self.project_id),
                transaction_id,
                state_effects or [],
                write_state=lambda state: self._write_state(
                    connection, self.project_id, state
                ),
            )
            ownership.compensate_candidates(
                transaction_id,
                candidate_effects or [],
            )
            self._delete_transaction_effects(connection, payload)
            compensation_event_ids.append(self._append_transaction_event(
                connection,
                payload,
                "execution_transaction_rolled_back",
            ))
            payload["compensation_event_ids"] = compensation_event_ids
            self._set_transaction_status(
                connection, transaction_id, "ROLLED_BACK", payload
            )
        return conflicts

    def get_transaction_status(self, transaction_id: str) -> str | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT status FROM execution_transactions WHERE transaction_id = ?",
                (transaction_id,),
            ).fetchone()
        return str(row["status"]) if row else None

    def _delete_transaction_effects(
        self, connection: sqlite3.Connection, payload: Mapping[str, Any]
    ) -> None:
        artifact_ids = list(payload.get("artifact_ids") or [])
        if artifact_ids:
            connection.executemany(
                "DELETE FROM artifacts WHERE artifact_id = ?",
                [(value,) for value in artifact_ids],
            )

    def _append_transaction_event(
        self,
        connection: sqlite3.Connection,
        transaction_payload: Mapping[str, Any],
        event_type: str,
        **details: Any,
    ) -> str:
        metadata = transaction_payload.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        event = {
            "event_id": uuid.uuid4().hex,
            "timestamp": _now(),
            "project_id": str(metadata.get("project_id") or self.project_id),
            "workflow_id": transaction_payload.get("workflow_id"),
            "run_id": transaction_payload.get("run_id"),
            "plan_id": metadata.get("plan_id"),
            "task_id": transaction_payload.get("task_id"),
            "attempt_id": transaction_payload.get("attempt_id"),
            "transaction_id": transaction_payload.get("transaction_id"),
            "agent": "execution",
            "event_type": event_type,
            **details,
        }
        event = {key: value for key, value in event.items() if value is not None}
        EvidenceEvent.from_dict(event)
        return self._append_event(connection, event)

    def _set_transaction_status(
        self,
        connection: sqlite3.Connection,
        transaction_id: str,
        status: str,
        payload: Mapping[str, Any],
    ) -> None:
        row = connection.execute(
            "SELECT status FROM execution_transactions WHERE transaction_id = ?",
            (transaction_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"transaction not found: {transaction_id}")
        current = str(row["status"])
        assert_transaction_transition(current, status)
        connection.execute(
            "UPDATE execution_transactions SET status = ?, updated_at = ?, payload_json = ? "
            "WHERE transaction_id = ?",
            (status, _now(), _json(payload), transaction_id),
        )

    def record_task_failure(
        self, *, context: Mapping[str, Any], error: Mapping[str, Any]
    ) -> None:
        context_value = _mapping(context)
        now = _now()
        payload = dict(context_value, error=dict(error))
        transaction_id = str(context_value["transaction_id"])
        context_status = str(context_value.get("status") or "")
        with self._write() as connection:
            row = connection.execute(
                "SELECT status FROM execution_transactions WHERE transaction_id = ?",
                (transaction_id,),
            ).fetchone()
            missing_transaction_row = row is None
            if row is None:
                stored_status = (
                    context_status
                    if context_status in TERMINAL_TRANSACTION_STATUSES
                    else "FAILED"
                )
                connection.execute(
                    "INSERT INTO execution_transactions(transaction_id, task_id, attempt_id, status, created_at, updated_at, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        transaction_id,
                        str(context_value["task_id"]),
                        str(context_value["attempt_id"]),
                        stored_status,
                        context_value.get("created_at", now),
                        now,
                        _json(payload),
                    ),
                )
            else:
                stored_status = str(row["status"])
                if stored_status not in TERMINAL_TRANSACTION_STATUSES:
                    connection.execute(
                        "UPDATE execution_transactions SET status = 'FAILED', updated_at = ?, payload_json = ? "
                        "WHERE transaction_id = ? AND status = ?",
                        (now, _json(payload), transaction_id, stored_status),
                    )
                    stored_status = "FAILED"
            event_type = (
                "execution_transaction_failed"
                if stored_status == "FAILED"
                or (missing_transaction_row and context_status == "ROLLED_BACK")
                else "execution_transaction_post_commit_failure"
            )
            self._append_transaction_event(
                connection, context_value, event_type, **dict(error)
            )
