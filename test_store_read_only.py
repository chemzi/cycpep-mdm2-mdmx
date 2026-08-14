"""Read-only formal Store contract tests."""

from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path
from unittest.mock import patch

from data_layer import validate_storage_backend
from storage import SQLiteStore, StorageUnavailableError


class SQLiteStoreReadOnlyTests(unittest.TestCase):
    def test_missing_read_only_store_is_not_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "missing" / "store.db"

            with self.assertRaises(StorageUnavailableError):
                SQLiteStore(database, project_id="project-1", read_only=True)

            self.assertFalse(database.exists())
            self.assertFalse(database.parent.exists())

    def test_empty_or_wrong_project_store_is_rejected_without_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            empty = root / "empty.db"
            empty.write_bytes(b"")

            with self.assertRaises(StorageUnavailableError):
                SQLiteStore(empty, project_id="project-1", read_only=True)

            self.assertEqual(empty.read_bytes(), b"")

            database = root / "wrong-project.db"
            SQLiteStore(database, project_id="project-other")
            before = database.read_bytes()
            with self.assertRaises(StorageUnavailableError):
                SQLiteStore(database, project_id="project-1", read_only=True)
            self.assertEqual(database.read_bytes(), before)

    def test_schema_migration_is_required_before_read_only_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "incomplete.db"
            SQLiteStore(database, project_id="project-1")
            connection = sqlite3.connect(database)
            try:
                connection.execute("DROP INDEX idx_evidence_transaction")
                connection.execute(
                    "ALTER TABLE evidence_events DROP COLUMN transaction_id"
                )
                connection.commit()
            finally:
                connection.close()
            before = database.read_bytes()

            with self.assertRaises(StorageUnavailableError):
                SQLiteStore(database, project_id="project-1", read_only=True)

            self.assertEqual(database.read_bytes(), before)

    def test_missing_required_index_is_rejected_without_recreation(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "missing-index.db"
            SQLiteStore(database, project_id="project-1")
            connection = sqlite3.connect(database)
            try:
                connection.execute("DROP INDEX idx_evidence_transaction")
                connection.commit()
            finally:
                connection.close()
            before = database.read_bytes()

            with self.assertRaises(StorageUnavailableError):
                SQLiteStore(database, project_id="project-1", read_only=True)

            self.assertEqual(database.read_bytes(), before)

    def test_read_only_queries_do_not_change_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "store.db"
            writable = SQLiteStore(database, project_id="project-1")
            writable.append(
                {
                    "event_id": "EV-read-only",
                    "timestamp": "2026-08-11T00:00:00+00:00",
                    "agent": "research",
                    "event_type": "research_completion_receipt",
                    "phase": "research",
                    "project_id": "project-1",
                }
            )
            before = database.read_bytes()

            read_only = SQLiteStore(database, project_id="project-1", read_only=True)
            events = read_only.query(project_id="project-1", agent="research")

            self.assertEqual([event["event_id"] for event in events], ["EV-read-only"])
            self.assertEqual(database.read_bytes(), before)
            with self.assertRaises(RuntimeError):
                read_only.append(
                    {
                        "event_id": "EV-forbidden",
                        "timestamp": "2026-08-11T00:00:01+00:00",
                        "agent": "research",
                        "event_type": "research_completion_receipt",
                        "phase": "research",
                        "project_id": "project-1",
                    }
                )

    def test_doctor_validation_uses_immutable_snapshot_without_touching_wal_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "store.db"
            SQLiteStore(database, project_id="project-1")
            connection = sqlite3.connect(database)
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("SELECT 1").fetchone()
                observed = tuple(
                    path for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm"))
                    if path.exists()
                )
                before = {
                    path: (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in observed
                }
                with patch("storage.sqlite_store.sqlite3.connect", wraps=sqlite3.connect) as connect:
                    validate_storage_backend(database, project_id="project-1")
                uri = str(connect.call_args_list[0].args[0])
                self.assertIn("mode=ro", uri)
                self.assertIn("immutable=1", uri)
                self.assertEqual(
                    before,
                    {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in observed},
                )
            finally:
                connection.close()

    def test_doctor_validation_fails_closed_on_uncheckpointed_wal_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "store.db"
            SQLiteStore(database, project_id="project-1")
            connection = sqlite3.connect(database)
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA wal_autocheckpoint = 0")
                connection.execute(
                    "INSERT INTO projects(project_id, created_at, updated_at) VALUES (?, ?, ?)",
                    ("project-wal", "2026-08-14T00:00:00Z", "2026-08-14T00:00:00Z"),
                )
                connection.commit()
                wal = Path(str(database) + "-wal")
                self.assertGreater(wal.stat().st_size, 0)
                observed = tuple(
                    path for path in (database, wal, Path(str(database) + "-shm"))
                    if path.exists()
                )
                before = {
                    path: (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in observed
                }
                with self.assertRaisesRegex(StorageUnavailableError, "quiescent TRUNCATE checkpoint"):
                    validate_storage_backend(database, project_id="project-wal")
                self.assertEqual(
                    before,
                    {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in observed},
                )
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                self.assertEqual(wal.stat().st_size, 0)
                checkpointed = {
                    path: (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in observed
                }
                validate_storage_backend(database, project_id="project-wal")
                self.assertEqual(
                    checkpointed,
                    {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in observed},
                )
            finally:
                connection.close()

    def test_immutable_snapshot_requires_read_only_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "requires read_only"):
                SQLiteStore(Path(tmp) / "store.db", immutable_snapshot=True)


if __name__ == "__main__":
    unittest.main()
