"""Idempotent legacy JSON/CSV/JSONL migration into SQLite."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

from .sqlite_store import SQLiteStore


def migrate_json_to_sqlite(*, db_path: str | Path, state_path: str | Path | None = None,
                           candidate_path: str | Path | None = None,
                           evidence_path: str | Path | None = None,
                           project_id: str = "default") -> dict[str, int]:
    store = SQLiteStore(db_path, project_id=project_id)
    stats = {"states": 0, "candidates": 0, "events": 0}
    if state_path and Path(state_path).exists():
        payload = json.loads(Path(state_path).read_text(encoding="utf-8"))
        store.update_state(project_id, payload)
        stats["states"] = 1
    if candidate_path and Path(candidate_path).exists():
        with Path(candidate_path).open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                if row.get("candidate_id") and row.get("sequence"):
                    store.upsert(row)
                    stats["candidates"] += 1
    if evidence_path and Path(evidence_path).exists():
        with Path(evidence_path).open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                event: dict[str, Any] = json.loads(line)
                try:
                    store.append(event)
                    stats["events"] += 1
                except sqlite3.IntegrityError as exc:
                    if "UNIQUE constraint failed: evidence_events.event_id" not in str(exc):
                        raise
    return stats
