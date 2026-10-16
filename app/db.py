"""Mała baza SQLite: sesje i liczniki nieudanych logowań (przetrwają restart)."""

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    created    INTEGER NOT NULL,
    expires    INTEGER NOT NULL,
    ip         TEXT,
    user_agent TEXT
);
CREATE TABLE IF NOT EXISTS login_ip (
    ip           TEXT PRIMARY KEY,
    fails        INTEGER NOT NULL DEFAULT 0,
    lock_level   INTEGER NOT NULL DEFAULT 0,
    locked_until INTEGER NOT NULL DEFAULT 0,
    last_fail    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS login_fail_log (
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_fail_log_ts ON login_fail_log(ts);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS favorites (
    channel_id TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    added      INTEGER NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            return self._conn.execute(sql, params).rowcount

    @contextmanager
    def transaction(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
