"""Read-only formal Store contract tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
