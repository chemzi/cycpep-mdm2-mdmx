"""SQLite transactional kernel backing the shared data layer.

The database file (``DATA_DIR / "store.db"``) is the single source of truth
for State, CandidateIndex and Evidence.  The legacy ``state.json`` /
``candidate_index.csv`` / ``evidence_log.jsonl`` files remain as projections
that the data layer re-exports after every successful commit, so evidence-chain
hashes, spreadsheet inspection and existing tests keep working unchanged.

Concurrency model: WAL mode plus ``BEGIN IMMEDIATE`` gives a single-writer /
multi-reader store; cross-process writers serialize on the database lock with a
30s busy timeout.  Candidate IDs come from a sequence row updated inside the
write transaction, so ID allocation cannot collide across processes.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

_BUSY_TIMEOUT_MS = 30_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL UNIQUE,
    row_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_json TEXT NOT NULL
);
"""

_LOCAL = threading.local()


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection, creating the database and schema if needed.

    ``isolation_level=None`` selects autocommit mode: statements outside an
    explicit BEGIN take effect immediately, and transaction boundaries are
    controlled only by the explicit BEGIN IMMEDIATE / COMMIT / ROLLBACK in
    ``transaction``.  This keeps post-commit projection bookkeeping (meta
    updates during export) durable instead of silently rolled back on close.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db_path), timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    conn.executescript(_SCHEMA)
    return conn


def current(db_path: Path) -> sqlite3.Connection | None:
    """Return this thread's open transaction connection for ``db_path``, if any."""
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None and getattr(_LOCAL, "db_path", None) == str(Path(db_path)):
        return conn
    return None


def mark_dirty(db_path: Path, domain: str) -> None:
    """Declare that a projection domain must be re-exported after commit."""
    if current(db_path) is not None:
        _LOCAL.dirty.add(domain)


@contextmanager
def transaction(db_path: Path, export=None):
    """Run one atomic write transaction against the store.

    Nested calls on the same thread reuse the enclosing transaction, so public
    data-layer methods compose into a single commit.  ``export(conn, domains)``
    runs once after the outermost COMMIT with the accumulated dirty domains;
    on any exception the transaction rolls back and nothing is exported.
    """
    db_path = Path(db_path)
    existing = current(db_path)
    if existing is not None:
        yield existing
        return
    conn = connect(db_path)
    _LOCAL.conn = conn
    _LOCAL.db_path = str(db_path)
    _LOCAL.dirty = set()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
        if export is not None and _LOCAL.dirty:
            export(conn, set(_LOCAL.dirty))
    finally:
        _LOCAL.conn = None
        _LOCAL.db_path = None
        _LOCAL.dirty = set()
        conn.close()


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
    )
