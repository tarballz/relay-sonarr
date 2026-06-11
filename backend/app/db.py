"""SQLite persistence: one connection, lock-serialized, run off the event loop.

Deliberately stdlib-only (`sqlite3`) — no ORM, no async-DB dependency — matching
the project's minimalism and single-container, single-writer reality. The one
connection is shared across threads (``check_same_thread=False``) and every access
is guarded by a ``threading.Lock``; reads/writes run in the default executor so
they never block the event loop. WAL + ``busy_timeout`` keep the rare concurrent
access safe. The schema is applied idempotently at construction.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- One row per series the user wants Relay to own (Phase 5 fills policy_json).
CREATE TABLE IF NOT EXISTS series_intent (
  tvdb_id     INTEGER PRIMARY KEY,
  title       TEXT,
  chain_key   TEXT,
  paused      INTEGER NOT NULL DEFAULT 0,
  policy_json TEXT,
  created_at  TEXT,
  updated_at  TEXT
);

-- Per-episode desired-vs-obtained state. Keyed on (tvdb_id, season, episode) so
-- it is stable across instances (episode ids differ per Sonarr).
CREATE TABLE IF NOT EXISTS placement (
  tvdb_id        INTEGER NOT NULL,
  season         INTEGER NOT NULL,
  episode        INTEGER NOT NULL,
  desired_tier   TEXT,
  obtained_tier  TEXT,
  state          TEXT NOT NULL DEFAULT 'wanted',
  download_id    TEXT,
  last_search_at TEXT,
  next_retry_at  TEXT,
  attempts       INTEGER NOT NULL DEFAULT 0,
  locked_until   TEXT,
  reason         TEXT,
  wanted_since   TEXT,
  updated_at     TEXT,
  PRIMARY KEY (tvdb_id, season, episode)
);

-- Cached interactive-search verdict per (instance, episode), with a TTL via checked_at.
CREATE TABLE IF NOT EXISTS availability_cache (
  instance_id      TEXT NOT NULL,
  tvdb_id          INTEGER NOT NULL,
  season           INTEGER NOT NULL,
  episode          INTEGER NOT NULL,
  qualifies        INTEGER NOT NULL,
  total_releases   INTEGER,
  qualifying_count INTEGER,
  rejection_json   TEXT,
  checked_at       TEXT,
  PRIMARY KEY (instance_id, tvdb_id, season, episode)
);

-- Durable replacement for the in-memory OperationLog.
CREATE TABLE IF NOT EXISTS operation (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kind        TEXT,
  title       TEXT,
  tvdb_id     INTEGER,
  source      TEXT NOT NULL DEFAULT 'user',
  started_at  TEXT,
  finished_at TEXT,
  result_json TEXT,
  error       TEXT
);

CREATE TABLE IF NOT EXISTS operation_step (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  operation_id INTEGER NOT NULL REFERENCES operation(id) ON DELETE CASCADE,
  seq          INTEGER NOT NULL,
  event_json   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_step_op   ON operation_step(operation_id, seq);
CREATE INDEX IF NOT EXISTS idx_op_id     ON operation(id DESC);
CREATE INDEX IF NOT EXISTS idx_place_tvdb ON placement(tvdb_id);
"""


class Database:
    """A single SQLite connection with async, lock-serialized access."""

    def __init__(self, path: str):
        self._lock = threading.Lock()
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the connection is used from executor threads,
        # but every access is serialized by self._lock, so it's safe.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    def _ensure_column(self, table: str, column: str, decl: str) -> None:
        """Add a column to an existing table if it's missing (forward migration)."""
        cols = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def _migrate(self) -> None:
        # Columns added after the initial schema shipped — safe on fresh + existing DBs.
        self._ensure_column("placement", "wanted_since", "TEXT")
        # The seeder threshold a verdict was computed under; a mismatch with the
        # current minSeeders setting invalidates the row (NULL = legacy row).
        self._ensure_column("availability_cache", "min_seeders", "INTEGER")
        # Best grab candidate {guid, indexerId, seeders, title} captured during
        # the availability check, so the reconciler can grab without re-searching.
        self._ensure_column("availability_cache", "best_release_json", "TEXT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    async def _run(self, fn):
        loop = asyncio.get_running_loop()

        def job():
            with self._lock:
                return fn(self._conn)

        return await loop.run_in_executor(None, job)

    async def execute(self, sql: str, params: tuple = ()) -> int:
        """Run a write; returns lastrowid."""
        def fn(conn):
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.lastrowid
        return await self._run(fn)

    async def executemany(self, sql: str, seq_of_params) -> None:
        def fn(conn):
            conn.executemany(sql, seq_of_params)
            conn.commit()
        await self._run(fn)

    async def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return await self._run(lambda conn: conn.execute(sql, params).fetchall())

    async def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return await self._run(lambda conn: conn.execute(sql, params).fetchone())
