"""SQLite revision ownership used by transaction compensation.

This helper has no public storage API. ``SQLiteStore`` remains the sole formal
mutation boundary; this module only keeps its revision/effect bookkeeping
cohesive enough to audit independently.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping


_MISSING = object()
TERMINAL_TRANSACTION_STATUSES = frozenset({
    "COMMITTED", "FAILED", "ROLLED_BACK", "COMPENSATION_CONFLICT",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _merge_mapping(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, item in source.items():
        if isinstance(item, Mapping) and isinstance(target.get(key), dict):
            _merge_mapping(target[key], item)
        else:
            target[key] = item


def patch_candidate_value(
    candidate: Mapping[str, Any], patches: Mapping[str, Any]
) -> dict[str, Any]:
    value = dict(candidate)
    updates = dict(patches)
    metrics_update = updates.pop("metrics", None)
    if isinstance(metrics_update, Mapping):
        encoded = value.get("metrics_json")
        if isinstance(encoded, str):
            try:
                metrics = json.loads(encoded or "{}")
            except json.JSONDecodeError:
                metrics = {}
        elif isinstance(value.get("metrics"), Mapping):
            metrics = deepcopy(dict(value["metrics"]))
        else:
            metrics = {}
        if not isinstance(metrics, dict):
            metrics = {}
        _merge_mapping(metrics, metrics_update)
        updates["metrics_json"] = _json(metrics)
        if isinstance(value.get("metrics"), Mapping):
            updates["metrics"] = metrics
    value.update(updates)
    return value


def _path_value(value: Any, path: Iterable[str]) -> Any:
    current = value
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return _MISSING
        current = current[key]
    return current


class SQLiteOwnership:
    """Revision tracking scoped to one SQLite connection and project."""

    def __init__(self, connection: sqlite3.Connection, project_id: str):
        self.connection = connection
        self.project_id = project_id

    def state_version(self, key: str) -> tuple[int, str | None]:
        row = self.connection.execute(
            "SELECT revision, last_writer_transaction_id FROM state_key_versions "
            "WHERE project_id = ? AND key = ?",
            (self.project_id, key),
        ).fetchone()
        if row is None:
            return 0, None
        return int(row["revision"]), row["last_writer_transaction_id"]

    def advance_state(
        self,
        key: str,
        writer_transaction_id: str | None,
        *,
        steps: int = 1,
    ) -> tuple[int, str | None, int, str | None]:
        before_revision, before_writer = self.state_version(key)
        after_revision = before_revision + steps
        self.connection.execute(
            "INSERT INTO state_key_versions(project_id, key, revision, last_writer_transaction_id) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(project_id, key) DO UPDATE SET "
            "revision=excluded.revision, "
            "last_writer_transaction_id=excluded.last_writer_transaction_id",
            (self.project_id, key, after_revision, writer_transaction_id),
        )
        return before_revision, before_writer, after_revision, writer_transaction_id

    def candidate_version(self, candidate_id: str) -> tuple[int, str | None]:
        row = self.connection.execute(
            "SELECT revision, last_writer_transaction_id FROM candidate_versions "
            "WHERE project_id = ? AND candidate_id = ?",
            (self.project_id, candidate_id),
        ).fetchone()
        if row is None:
            return 0, None
        return int(row["revision"]), row["last_writer_transaction_id"]

    def advance_candidate(
        self, candidate_id: str, writer_transaction_id: str | None
    ) -> tuple[int, str | None, int, str | None]:
        before_revision, before_writer = self.candidate_version(candidate_id)
        after_revision = before_revision + 1
        self.connection.execute(
            "INSERT INTO candidate_versions(project_id, candidate_id, revision, last_writer_transaction_id) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(project_id, candidate_id) DO UPDATE SET "
            "revision=excluded.revision, "
            "last_writer_transaction_id=excluded.last_writer_transaction_id",
            (self.project_id, candidate_id, after_revision, writer_transaction_id),
        )
        return before_revision, before_writer, after_revision, writer_transaction_id

    def apply_candidates(
        self,
        updates: Iterable[Mapping[str, Any]],
        patches: Iterable[Mapping[str, Any]],
        transaction_id: str,
        *,
        put_candidate: Callable[[Mapping[str, Any], str, str], dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        inserted: list[dict[str, Any]] = []
        effects: dict[str, dict[str, Any]] = {}

        def remember_before(candidate_id: str) -> None:
            if candidate_id in effects:
                return
            row = self.connection.execute(
                "SELECT payload_json FROM candidates WHERE project_id = ? AND candidate_id = ?",
                (self.project_id, candidate_id),
            ).fetchone()
            before_revision, before_writer = self.candidate_version(candidate_id)
            effects[candidate_id] = {
                "kind": "candidate",
                "candidate_id": candidate_id,
                "before_exists": row is not None,
                "before": json.loads(row["payload_json"]) if row else None,
                "before_revision": before_revision,
                "before_writer_transaction_id": before_writer,
            }

        for item in updates:
            value = dict(item)
            candidate_id = str(value.get("candidate_id") or "")
            remember_before(candidate_id)
            inserted.append(put_candidate(value, "raise_duplicate", transaction_id))
        for mutation in patches:
            candidate_id = str(mutation.get("candidate_id") or "")
            patch = mutation.get("patch")
            if not candidate_id or not isinstance(patch, Mapping):
                raise ValueError("candidate patch requires candidate_id and patch")
            row = self.connection.execute(
                "SELECT payload_json FROM candidates WHERE project_id = ? AND candidate_id = ?",
                (self.project_id, candidate_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"candidate_id not found: {candidate_id}")
            remember_before(candidate_id)
            value = patch_candidate_value(json.loads(row["payload_json"]), patch)
            if str(value.get("candidate_id") or "") != candidate_id:
                raise ValueError("candidate patch cannot change candidate_id")
            put_candidate(value, "update", transaction_id)
        for candidate_id, effect in effects.items():
            row = self.connection.execute(
                "SELECT payload_json FROM candidates WHERE project_id = ? AND candidate_id = ?",
                (self.project_id, candidate_id),
            ).fetchone()
            after_revision, after_writer = self.candidate_version(candidate_id)
            effect.update({
                "after": json.loads(row["payload_json"]),
                "after_revision": after_revision,
                "after_writer_transaction_id": after_writer,
            })
        return inserted, list(effects.values())

    def apply_state(
        self,
        state: dict[str, Any],
        updates: Mapping[str, Any],
        appends: Iterable[Mapping[str, Any]],
        candidate_ids: Iterable[str],
        transaction_id: str,
        *,
        write_state: Callable[[Mapping[str, Any]], None],
    ) -> list[dict[str, Any]]:
        before_values: dict[str, tuple[bool, Any]] = {}
        writes: Counter[str] = Counter()
        kinds: dict[str, str] = {}

        def touch(key: str, kind: str) -> None:
            if key not in before_values:
                before_values[key] = (key in state, deepcopy(state.get(key)))
                kinds[key] = kind
            writes[key] += 1

        numbers = [
            int(value[1:])
            for value in map(str, candidate_ids)
            if value.startswith("C") and value[1:].isdigit()
        ]
        if numbers:
            touch("candidate_count", "candidate_count_max")
            state["candidate_count"] = max(
                int(state.get("candidate_count") or 0), max(numbers)
            )
        for key, after in updates.items():
            touch(str(key), "set")
            state[key] = after
        for mutation in appends:
            if mutation.get("kind") != "append_if_absent":
                raise ValueError("unsupported state append mutation")
            key = str(mutation["key"])
            values = list(state.get(key) or [])
            identity_path = list(mutation["identity_path"])
            identity_value = mutation.get("identity_value")
            if not any(
                _path_value(item, identity_path) == identity_value for item in values
            ):
                touch(key, "append_if_absent")
                values.append(dict(mutation["item"]))
                state[key] = values
        if not writes:
            return []
        write_state(state)
        effects = []
        for key, write_count in writes.items():
            before_exists, before = before_values[key]
            before_revision, before_writer, after_revision, after_writer = (
                self.advance_state(key, transaction_id, steps=write_count)
            )
            effects.append({
                "kind": kinds[key],
                "key": key,
                "before_exists": before_exists,
                "before": before,
                "after": deepcopy(state.get(key)),
                "before_revision": before_revision,
                "before_writer_transaction_id": before_writer,
                "after_revision": after_revision,
                "after_writer_transaction_id": after_writer,
                "write_count": write_count,
            })
        return effects

    def advance_candidate_count(
        self,
        state: dict[str, Any],
        candidate_ids: Iterable[str],
        writer_transaction_id: str | None,
        *,
        write_state: Callable[[Mapping[str, Any]], None],
    ) -> list[dict[str, Any]]:
        numbers = [
            int(value[1:])
            for value in map(str, candidate_ids)
            if value.startswith("C") and value[1:].isdigit()
        ]
        if not numbers:
            return []
        before_exists = "candidate_count" in state
        before = state.get("candidate_count")
        after = max(int(before or 0), max(numbers))
        state["candidate_count"] = after
        write_state(state)
        before_revision, before_writer, after_revision, after_writer = (
            self.advance_state("candidate_count", writer_transaction_id)
        )
        return [{
            "kind": "candidate_count_max",
            "key": "candidate_count",
            "before_exists": before_exists,
            "before": before,
            "after": after,
            "before_revision": before_revision,
            "before_writer_transaction_id": before_writer,
            "after_revision": after_revision,
            "after_writer_transaction_id": after_writer,
        }]

    def state_conflicts(
        self, transaction_id: str, effects: Iterable[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        conflicts = []
        for effect in effects:
            key = str(effect["key"])
            revision, writer = self.state_version(key)
            if revision != effect.get("after_revision") or writer != transaction_id:
                conflicts.append({
                    "kind": effect.get("kind"),
                    "key": key,
                    "expected_revision": effect.get("after_revision"),
                    "current_revision": revision,
                    "current_writer_transaction_id": writer,
                })
        return conflicts

    def candidate_conflicts(
        self, transaction_id: str, effects: Iterable[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        conflicts = []
        for effect in effects:
            candidate_id = str(effect["candidate_id"])
            revision, writer = self.candidate_version(candidate_id)
            if revision != effect.get("after_revision") or writer != transaction_id:
                conflicts.append({
                    "kind": "candidate",
                    "candidate_id": candidate_id,
                    "expected_revision": effect.get("after_revision"),
                    "current_revision": revision,
                    "current_writer_transaction_id": writer,
                })
        return conflicts

    def compensate_state(
        self,
        state: dict[str, Any],
        transaction_id: str,
        effects: Iterable[Mapping[str, Any]],
        *,
        write_state: Callable[[Mapping[str, Any]], None],
    ) -> None:
        effects = list(effects)
        if not effects:
            return
        for effect in effects:
            key = str(effect["key"])
            if effect.get("before_exists"):
                state[key] = effect.get("before")
            else:
                state.pop(key, None)
        write_state(state)
        for effect in effects:
            self.advance_state(str(effect["key"]), transaction_id)

    def compensate_candidates(
        self,
        transaction_id: str,
        effects: Iterable[Mapping[str, Any]],
    ) -> None:
        for effect in effects:
            candidate_id = str(effect["candidate_id"])
            if effect.get("before_exists"):
                self._write_candidate(candidate_id, effect["before"])
            else:
                self.connection.execute(
                    "DELETE FROM candidates WHERE project_id = ? AND candidate_id = ?",
                    (self.project_id, candidate_id),
                )
            self.advance_candidate(candidate_id, transaction_id)

    def _write_candidate(
        self, candidate_id: str, candidate: Mapping[str, Any]
    ) -> None:
        value = dict(candidate)
        metrics = value.get("metrics_json", {})
        if isinstance(metrics, str):
            try:
                metrics = json.loads(metrics)
            except json.JSONDecodeError:
                metrics = {}
        self.connection.execute(
            "UPDATE candidates SET sequence = ?, status = ?, metrics_json = ?, "
            "created_at = ?, updated_at = ?, payload_json = ? "
            "WHERE project_id = ? AND candidate_id = ?",
            (
                str(value.get("sequence") or ""),
                value.get("status") or value.get("final_status"),
                _json(metrics),
                value.get("created_at") or _now(),
                value.get("updated_at") or _now(),
                _json(value),
                self.project_id,
                candidate_id,
            ),
        )


def assert_transaction_transition(current: str, next_status: str) -> None:
    allowed = {
        "COMMITTED": frozenset({"ROLLED_BACK", "COMPENSATION_CONFLICT"}),
        "COMPENSATION_CONFLICT": frozenset({"ROLLED_BACK"}),
        "FAILED": frozenset(),
        "ROLLED_BACK": frozenset(),
    }
    if next_status != current and next_status not in allowed.get(current, frozenset()):
        raise ValueError(f"invalid transaction transition: {current} -> {next_status}")
