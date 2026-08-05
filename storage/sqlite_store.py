"""SQLite storage backend using one connection per operation."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from .base import Store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("store records must be mappings")


class SQLiteStore(Store):
    """Transactional SQLite backend for state, candidates and append-only evidence."""

    def __init__(
        self,
        path: str | Path,
        *,
        project_id: str = "default",
        duplicate_policy: Literal["update", "insert_only", "raise_duplicate"] = "update",
    ):
        self.path = Path(path)
        self.project_id = project_id
        if duplicate_policy not in {"update", "insert_only", "raise_duplicate"}:
            raise ValueError(f"unsupported duplicate_policy: {duplicate_policy}")
        self.duplicate_policy = duplicate_policy
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS projects (
            project_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS states (
            project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
            phase TEXT,
            round INTEGER,
            active_workflow_id TEXT,
            updated_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS candidates (
            candidate_id TEXT PRIMARY KEY,
            sequence TEXT NOT NULL,
            status TEXT,
            metrics_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS evidence_events (
            event_id TEXT PRIMARY KEY,
            workflow_id TEXT,
            run_id TEXT,
            task_id TEXT,
            candidate_id TEXT,
            agent TEXT,
            event_type TEXT,
            timestamp TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS artifacts (
            artifact_id TEXT PRIMARY KEY,
            artifact_type TEXT,
            path TEXT,
            sha256 TEXT,
            producer_task_id TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workflow_runs (
            run_id TEXT PRIMARY KEY,
            workflow_id TEXT,
            status TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            workflow_id TEXT,
            action TEXT,
            status TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_evidence_workflow ON evidence_events(workflow_id);
        CREATE INDEX IF NOT EXISTS idx_evidence_task ON evidence_events(task_id);
        CREATE INDEX IF NOT EXISTS idx_evidence_candidate ON evidence_events(candidate_id);
        """
        with self._connect() as connection:
            connection.executescript(schema)
            now = _now()
            connection.execute(
                "INSERT OR IGNORE INTO projects(project_id, created_at, updated_at) VALUES (?, ?, ?)",
                (self.project_id, now, now),
            )

    def get_state(self, project_id: str | None = None) -> dict[str, Any]:
        project_id = project_id or self.project_id
        with self._connect() as connection:
            row = connection.execute("SELECT payload_json FROM states WHERE project_id = ?", (project_id,)).fetchone()
        return json.loads(row["payload_json"]) if row else {}

    def update_state(self, project_id: str, patches: Mapping[str, Any]) -> dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            row = connection.execute("SELECT payload_json FROM states WHERE project_id = ?", (project_id,)).fetchone()
            state = json.loads(row["payload_json"]) if row else {}
            state.update(dict(patches))
            connection.execute(
                "INSERT INTO projects(project_id, created_at, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(project_id) DO UPDATE SET updated_at=excluded.updated_at",
                (project_id, now, now),
            )
            connection.execute(
                "INSERT INTO states(project_id, phase, round, active_workflow_id, updated_at, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(project_id) DO UPDATE SET "
                "phase=excluded.phase, round=excluded.round, active_workflow_id=excluded.active_workflow_id, "
                "updated_at=excluded.updated_at, payload_json=excluded.payload_json",
                (project_id, state.get("phase"), state.get("round"), state.get("active_workflow_id"), now, _json(state)),
            )
        return state

    def get(self, candidate_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT payload_json FROM candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def upsert(
        self,
        candidate: Mapping[str, Any],
        *,
        duplicate_policy: Literal["update", "insert_only", "raise_duplicate"] | None = None,
    ) -> dict[str, Any]:
        value = _mapping(candidate)
        candidate_id = value.get("candidate_id")
        sequence = value.get("sequence")
        if not candidate_id or not sequence:
            raise ValueError("candidate_id and sequence are required")
        now = _now()
        policy = duplicate_policy or self.duplicate_policy
        if policy not in {"update", "insert_only", "raise_duplicate"}:
            raise ValueError(f"unsupported duplicate_policy: {policy}")
        existing = self.get(str(candidate_id))
        if existing is not None and policy == "raise_duplicate":
            raise ValueError(f"duplicate candidate_id: {candidate_id}")
        if existing is not None and policy == "insert_only":
            return existing
        merged = {**(existing or {}), **value}
        created_at = (existing or {}).get("created_at", now)
        metrics = merged.get("metrics") if isinstance(merged.get("metrics"), dict) else merged.get("metrics_json", {})
        if isinstance(metrics, str):
            try:
                metrics = json.loads(metrics)
            except json.JSONDecodeError:
                metrics = {}
        merged["created_at"] = created_at
        merged["updated_at"] = now
        with self._connect() as connection:
            self._write_candidate(connection, merged, created_at=created_at, updated_at=now)
        return merged

    @staticmethod
    def _write_candidate(
        connection: sqlite3.Connection,
        candidate: Mapping[str, Any],
        *,
        created_at: str,
        updated_at: str,
    ) -> None:
        metrics = candidate.get("metrics") if isinstance(candidate.get("metrics"), dict) else candidate.get("metrics_json", {})
        if isinstance(metrics, str):
            try:
                metrics = json.loads(metrics)
            except json.JSONDecodeError:
                metrics = {}
        connection.execute(
            "INSERT INTO candidates(candidate_id, sequence, status, metrics_json, created_at, updated_at, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(candidate_id) DO UPDATE SET sequence=excluded.sequence, "
            "status=excluded.status, metrics_json=excluded.metrics_json, updated_at=excluded.updated_at, payload_json=excluded.payload_json",
            (
                str(candidate["candidate_id"]),
                str(candidate["sequence"]),
                candidate.get("status") or candidate.get("final_status"),
                _json(metrics),
                created_at,
                updated_at,
                _json(dict(candidate)),
            ),
        )

    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT payload_json FROM candidates"
        params: tuple[Any, ...] = ()
        if status is not None:
            query += " WHERE status = ? OR json_extract(payload_json, '$.final_status') = ?"
            params = (status, status)
        query += " ORDER BY candidate_id"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def append(self, event: Mapping[str, Any]) -> str:
        value = _mapping(event)
        event_id = str(value.get("event_id") or uuid.uuid4())
        timestamp = str(value.get("timestamp") or _now())
        payload = dict(value)
        for key in ("event_id", "timestamp", "workflow_id", "run_id", "task_id", "candidate_id", "agent", "event_type"):
            payload.pop(key, None)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO evidence_events(event_id, workflow_id, run_id, task_id, candidate_id, agent, event_type, timestamp, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, value.get("workflow_id"), value.get("run_id"), value.get("task_id"), value.get("candidate_id"),
                 value.get("agent"), value.get("event_type"), timestamp, _json(payload)),
            )
        return event_id

    def query(self, **filters: Any) -> list[dict[str, Any]]:
        clauses, params = [], []
        for key in ("workflow_id", "run_id", "task_id", "candidate_id", "agent", "event_type"):
            if filters.get(key) is not None:
                clauses.append(f"{key} = ?")
                params.append(filters[key])
        sql = "SELECT * FROM evidence_events" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY timestamp, rowid"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        events = []
        for row in rows:
            event = {key: row[key] for key in ("event_id", "workflow_id", "run_id", "task_id", "candidate_id", "agent", "event_type", "timestamp") if row[key] is not None}
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
        with self._connect() as connection:
            connection.execute("INSERT OR IGNORE INTO artifacts(artifact_id, artifact_type, path, sha256, producer_task_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                               (artifact_id, value.get("artifact_type"), value.get("path"), value.get("sha256"), value.get("producer_task_id"), value.get("created_at") or _now()))
        return artifact_id

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
        return dict(row) if row else None

    def commit_transaction(
        self,
        *,
        context: Mapping[str, Any],
        candidate_updates: list[Mapping[str, Any]],
        state_updates: Mapping[str, Any],
        artifacts: list[Mapping[str, Any]],
        completed_event: Mapping[str, Any],
    ) -> list[str]:
        """Commit all formal execution effects in one SQLite transaction."""

        now = _now()
        event_value = _mapping(completed_event)
        event_id = str(event_value.get("event_id") or uuid.uuid4())
        payload = dict(event_value)
        for key in (
            "event_id", "timestamp", "workflow_id", "run_id", "task_id",
            "candidate_id", "agent", "event_type",
        ):
            payload.pop(key, None)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO projects(project_id, created_at, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(project_id) DO UPDATE SET updated_at=excluded.updated_at",
                (self.project_id, now, now),
            )
            if state_updates:
                row = connection.execute(
                    "SELECT payload_json FROM states WHERE project_id = ?", (self.project_id,)
                ).fetchone()
                state = json.loads(row["payload_json"]) if row else {}
                state.update(dict(state_updates))
                connection.execute(
                    "INSERT INTO states(project_id, phase, round, active_workflow_id, updated_at, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(project_id) DO UPDATE SET "
                    "phase=excluded.phase, round=excluded.round, active_workflow_id=excluded.active_workflow_id, "
                    "updated_at=excluded.updated_at, payload_json=excluded.payload_json",
                    (
                        self.project_id, state.get("phase"), state.get("round"),
                        state.get("active_workflow_id"), now, _json(state),
                    ),
                )
            for candidate in candidate_updates:
                value = _mapping(candidate)
                candidate_id = value.get("candidate_id")
                sequence = value.get("sequence")
                if not candidate_id or not sequence:
                    raise ValueError("candidate_id and sequence are required")
                row = connection.execute(
                    "SELECT payload_json FROM candidates WHERE candidate_id = ?",
                    (str(candidate_id),),
                ).fetchone()
                existing = json.loads(row["payload_json"]) if row else None
                if existing is not None and self.duplicate_policy == "raise_duplicate":
                    raise ValueError(f"duplicate candidate_id: {candidate_id}")
                if existing is not None and self.duplicate_policy == "insert_only":
                    continue
                merged = {**(existing or {}), **value}
                merged["created_at"] = (existing or {}).get("created_at", now)
                merged["updated_at"] = now
                self._write_candidate(
                    connection,
                    merged,
                    created_at=str(merged["created_at"]),
                    updated_at=now,
                )
            for artifact in artifacts:
                value = _mapping(artifact)
                connection.execute(
                    "INSERT OR IGNORE INTO artifacts(artifact_id, artifact_type, path, sha256, producer_task_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(value["artifact_id"]), value.get("artifact_type"), value.get("path"),
                        value.get("sha256"), value.get("producer_task_id"), value.get("created_at") or now,
                    ),
                )
            task_id = str(context["task_id"])
            connection.execute(
                "INSERT INTO tasks(task_id, workflow_id, action, status, created_at, updated_at, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(task_id) DO UPDATE SET "
                "status=excluded.status, updated_at=excluded.updated_at, payload_json=excluded.payload_json",
                (
                    task_id, context.get("workflow_id"), context.get("action"), "SUCCEEDED",
                    context.get("created_at", now), now,
                    _json(dict(context, status="SUCCEEDED")),
                ),
            )
            connection.execute(
                "INSERT INTO evidence_events(event_id, workflow_id, run_id, task_id, candidate_id, agent, event_type, timestamp, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id, event_value.get("workflow_id"), event_value.get("run_id"),
                    event_value.get("task_id"), event_value.get("candidate_id"),
                    event_value.get("agent"), event_value.get("event_type"),
                    event_value.get("timestamp", now), _json(payload),
                ),
            )
        return [event_id]

    def record_task_failure(
        self, *, context: Mapping[str, Any], error: Mapping[str, Any]
    ) -> None:
        now = _now()
        status = "FAILED_RETRYABLE" if error.get("retryable") else "FAILED_FINAL"
        payload = dict(context)
        payload.update({"status": status, "error": dict(error)})
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO tasks(task_id, workflow_id, action, status, created_at, updated_at, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(task_id) DO UPDATE SET "
                "status=excluded.status, updated_at=excluded.updated_at, payload_json=excluded.payload_json",
                (
                    str(context["task_id"]), context.get("workflow_id"), context.get("action"),
                    status, context.get("created_at", now), now, _json(payload),
                ),
            )
