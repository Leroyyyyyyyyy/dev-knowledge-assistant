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


# Each migration moves the schema from version N-1 to N. SCHEMA above is version 1.
# PRAGMA user_version records where a database is; a migration and its version
# bump commit together, so a crash cannot leave a half-applied step marked done.
MIGRATIONS = {
    2: """
    -- Citation metadata copied at retrieval time, so /api/answers can build
    -- canonical links from the run alone, even after that index is deleted.
    ALTER TABLE run_chunks ADD COLUMN repo_id TEXT;
    ALTER TABLE run_chunks ADD COLUMN commit_sha TEXT;
    ALTER TABLE run_chunks ADD COLUMN path TEXT;
    ALTER TABLE run_chunks ADD COLUMN start_line INTEGER;
    ALTER TABLE run_chunks ADD COLUMN end_line INTEGER;
    ALTER TABLE run_chunks ADD COLUMN url TEXT;

    CREATE TABLE answers (
        answer_id        TEXT PRIMARY KEY,
        run_id           TEXT NOT NULL REFERENCES runs (run_id),
        request_id       TEXT NOT NULL,
        created_at       TEXT NOT NULL,
        status           TEXT NOT NULL CHECK (status IN ('answered', 'insufficient_evidence')),
        answer_text      TEXT NOT NULL,
        cited_chunk_ids  TEXT NOT NULL,      -- JSON list, in the order the answer cites them
        model            TEXT NOT NULL,
        prompt_version   TEXT NOT NULL,
        content_hash     TEXT NOT NULL,      -- identifies a resubmission of the same answer
        validation       TEXT NOT NULL CHECK (validation IN ('accepted', 'rejected')),
        rejection_code   TEXT,
        rejection_detail TEXT
    );

    -- Rejected attempts are kept for evaluation; only one answer per run is accepted.
    CREATE UNIQUE INDEX one_accepted_answer_per_run
        ON answers (run_id) WHERE validation = 'accepted';
    """,
    3: """
    -- What the user said, kept apart from what the model claimed (answers.status).
    -- One row per run: a later submission replaces the earlier one.
    CREATE TABLE feedback (
        run_id      TEXT PRIMARY KEY REFERENCES runs (run_id),
        answer_id   TEXT NOT NULL REFERENCES answers (answer_id),
        resolved    INTEGER NOT NULL CHECK (resolved IN (0, 1)),
        reason      TEXT CHECK (reason IN ('wrong_answer', 'incomplete', 'wrong_citation', 'not_relevant', 'other')),
        comment     TEXT,
        request_id  TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        CHECK (resolved = 0 OR reason IS NULL)
    );
    """,
    4: """
    -- The chunk text as returned, so /api/answers can accept a URL that the
    -- answer quotes verbatim from a cited chunk (e.g. a docs URL in a README).
    ALTER TABLE run_chunks ADD COLUMN content TEXT;
    """,
}

SCHEMA_VERSION = max(MIGRATIONS)


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
        current = connection.execute("PRAGMA user_version").fetchone()[0]
        if current == 0:
            current = 1  # SCHEMA just created or already present: that is version 1
        for version in sorted(MIGRATIONS):
            if version <= current:
                continue
            connection.executescript(f"BEGIN; {MIGRATIONS[version]} PRAGMA user_version = {version}; COMMIT;")
            current = version
        connection.execute(f"PRAGMA user_version = {current}")
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
