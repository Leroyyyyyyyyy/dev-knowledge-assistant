"""
SQLite: index version records, the active-version pointer, and retrieval runs.

Concurrency rules live in the schema, not in application checks, so they hold
even when two processes race:
- at most one index build can be in state 'building' (partial unique index);
- the active pointer is a single row that can only name a 'ready' version
  (enforced in activate_version inside one transaction).
"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS index_versions (
    version      TEXT PRIMARY KEY,
    status       TEXT NOT NULL CHECK (status IN ('building', 'ready', 'failed')),
    manifest     TEXT NOT NULL,          -- JSON: repo_id -> {commit, remote, exclude}
    embed_model  TEXT NOT NULL,
    chunk_count  INTEGER,
    created_at   TEXT NOT NULL,
    finished_at  TEXT,
    error        TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_build_at_a_time
    ON index_versions (status) WHERE status = 'building';

CREATE TABLE IF NOT EXISTS active_index (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    version     TEXT NOT NULL REFERENCES index_versions (version),
    switched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    request_id    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    query         TEXT NOT NULL,
    repo_filter   TEXT,                  -- NULL means all repos in the index version
    index_version TEXT NOT NULL REFERENCES index_versions (version),
    embed_model   TEXT NOT NULL,
    top_k         INTEGER NOT NULL,
    status        TEXT NOT NULL,
    latency_ms    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS run_chunks (
    run_id   TEXT NOT NULL REFERENCES runs (run_id),
    rank     INTEGER NOT NULL,
    chunk_id TEXT NOT NULL,
    distance REAL NOT NULL,
    PRIMARY KEY (run_id, rank)
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(db_path: Path) -> None:
    connection = connect(db_path)
    try:
        connection.executescript(SCHEMA)
        connection.commit()
    finally:
        connection.close()


def active_version(connection: sqlite3.Connection) -> sqlite3.Row | None:
    """The active index version's full record, or None before the first activation."""
    return connection.execute(
        """
        SELECT v.* FROM active_index a
        JOIN index_versions v ON v.version = a.version
        WHERE a.id = 1
        """
    ).fetchone()
