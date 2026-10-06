"""SQLite storage layer for the Repo Analysis Tool (RAT).

The database holds, per repository:
  * repos          -- registry + ingest status
  * commits        -- every parsed non-merge commit (sha, committer date, raw author)
  * changes        -- per commit, one row per changed file AND one row per ancestor
                      directory (incl. the root ''), so directory/repository metrics
                      are plain SUM()s (see metrics.py)
  * raw_authors    -- the raw (name, email) identities found in the log
  * author_groups  -- canonical (merged) authors
  * group_members  -- maps each raw identity to exactly one canonical group
"""
from __future__ import annotations

import contextlib
import sqlite3
import threading
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "rat.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    source_type  TEXT NOT NULL,                 -- 'zip' | 'url'
    source       TEXT NOT NULL,                 -- upload file name or clone URL
    path         TEXT NOT NULL,                 -- on-disk git directory
    ref          TEXT NOT NULL DEFAULT 'HEAD',  -- active reference
    resolved_sha TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    progress     REAL NOT NULL DEFAULT 0,       -- 0..1 for the active phase
    phase        TEXT NOT NULL DEFAULT '',
    message      TEXT NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS raw_authors (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL,
    name    TEXT NOT NULL,
    email   TEXT NOT NULL,
    UNIQUE(repo_id, name, email)
);

CREATE TABLE IF NOT EXISTS author_groups (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id      INTEGER NOT NULL,
    display_name TEXT NOT NULL,
    email        TEXT NOT NULL DEFAULT '',
    origin       TEXT NOT NULL DEFAULT 'raw'    -- 'raw' | 'mailmap' | 'manual'
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id INTEGER NOT NULL,
    raw_id   INTEGER NOT NULL UNIQUE,           -- a raw identity belongs to one group
    PRIMARY KEY (group_id, raw_id)
);

CREATE TABLE IF NOT EXISTS commits (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id   INTEGER NOT NULL,
    sha       TEXT NOT NULL,
    ts        INTEGER NOT NULL,                 -- committer date (unix seconds)
    author_id INTEGER NOT NULL,                 -- raw author id
    subject   TEXT NOT NULL DEFAULT '',
    added     INTEGER NOT NULL DEFAULT 0,       -- total added lines (non-binary)
    deleted   INTEGER NOT NULL DEFAULT 0,       -- total removed lines (non-binary)
    UNIQUE(repo_id, sha)
);
CREATE INDEX IF NOT EXISTS idx_commits_ts     ON commits(repo_id, ts);
CREATE INDEX IF NOT EXISTS idx_commits_author ON commits(repo_id, author_id);

CREATE TABLE IF NOT EXISTS changes (
    repo_id   INTEGER NOT NULL,
    commit_id INTEGER NOT NULL,
    path      TEXT NOT NULL,                    -- object path ('' = root dir)
    parent    TEXT NOT NULL DEFAULT '',         -- immediate parent directory
    is_dir    INTEGER NOT NULL,
    added     INTEGER NOT NULL,
    deleted   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_changes_object  ON changes(repo_id, is_dir, path);
CREATE INDEX IF NOT EXISTS idx_changes_children ON changes(repo_id, parent, is_dir, path);
CREATE INDEX IF NOT EXISTS idx_changes_commit  ON changes(commit_id);
"""


class _Shared:
    """One process-wide connection for API reads/small writes (WAL, lock-guarded)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.conn = self._open()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=60.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=60000")
        return conn

    def init_schema(self) -> None:
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # ---- locking helpers used by the API layer -------------------------------
    def lock(self) -> threading.RLock:
        return self._lock

    @contextlib.contextmanager
    def session(self):
        """Hold the connection lock across several statements (e.g. temp tables
        that must survive between a fill and the main query)."""
        with self._lock:
            yield self.conn

    @contextlib.contextmanager
    def transaction(self):
        """Atomic multi-statement write."""
        with self._lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()


db = _Shared(str(DB_PATH))


def new_connection() -> sqlite3.Connection:
    """A fresh connection for background/ingest threads (independent WAL writer)."""
    conn = sqlite3.connect(str(DB_PATH), timeout=60.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn
