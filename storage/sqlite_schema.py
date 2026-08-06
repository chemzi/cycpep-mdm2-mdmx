"""SQLite schema bootstrap and forward-compatible column upgrades."""

from __future__ import annotations

import sqlite3


BASE_SCHEMA = """
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
CREATE TABLE IF NOT EXISTS candidate_sequences (
    project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
    current_value INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS candidates (
    candidate_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
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
    size_bytes INTEGER,
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
CREATE TABLE IF NOT EXISTS execution_transactions (
    transaction_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
"""


INDEXES = """
CREATE INDEX IF NOT EXISTS idx_candidates_project ON candidates(project_id);
CREATE INDEX IF NOT EXISTS idx_evidence_workflow ON evidence_events(workflow_id);
CREATE INDEX IF NOT EXISTS idx_evidence_task ON evidence_events(task_id);
CREATE INDEX IF NOT EXISTS idx_evidence_candidate ON evidence_events(candidate_id);
"""


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Create current tables, upgrade old columns, then create dependent indexes."""
    connection.executescript(BASE_SCHEMA)
    candidate_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(candidates)")
    }
    if "project_id" not in candidate_columns:
        connection.execute("ALTER TABLE candidates ADD COLUMN project_id TEXT")
    artifact_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(artifacts)")
    }
    if "size_bytes" not in artifact_columns:
        connection.execute("ALTER TABLE artifacts ADD COLUMN size_bytes INTEGER")
    connection.executescript(INDEXES)
