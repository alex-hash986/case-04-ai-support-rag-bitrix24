import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    request_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, session_id TEXT NOT NULL,
    response TEXT, cost_rub TEXT NOT NULL DEFAULT '0', cost_complete INTEGER NOT NULL DEFAULT 1,
    tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
    processing_time_sec REAL, ab_variant TEXT NOT NULL, mode TEXT NOT NULL,
    channel TEXT NOT NULL, is_after_hours INTEGER NOT NULL, need_human INTEGER,
    saved_minutes REAL
);
CREATE INDEX IF NOT EXISTS cycles_session ON cycles(session_id);
CREATE TABLE IF NOT EXISTS bitrix_steps (
    operation_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES cycles(request_id),
    ordinal INTEGER NOT NULL, method TEXT NOT NULL, payload TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT, UNIQUE(request_id, ordinal)
);
CREATE TABLE IF NOT EXISTS mock_receipts (
    operation_id TEXT PRIMARY KEY REFERENCES bitrix_steps(operation_id),
    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS embedding_cache (
    cache_key TEXT PRIMARY KEY, vector TEXT NOT NULL
);
"""


class Database:
    """Short, explicit transactions. Network I/O happens outside transactions."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(bitrix_steps)")}
            if "receipt" not in columns:
                connection.execute("ALTER TABLE bitrix_steps ADD COLUMN receipt TEXT")
            if "adapter_mode" not in columns:
                connection.execute("ALTER TABLE bitrix_steps ADD COLUMN adapter_mode TEXT NOT NULL DEFAULT 'mock'")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            with connection:
                yield connection
        finally:
            connection.close()

