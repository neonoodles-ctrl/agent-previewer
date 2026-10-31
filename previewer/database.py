from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    project_run_id TEXT NOT NULL,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    expected_result TEXT NOT NULL,
    target_url TEXT,
    checks_json TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL CHECK (state IN ('candidate', 'approved', 'hidden', 'deleted')),
    verification_status TEXT NOT NULL CHECK (verification_status IN ('pending', 'ready', 'rejected', 'error')),
    staged_path TEXT,
    approved_path TEXT,
    sha256 TEXT,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    mime_type TEXT,
    receipt_json TEXT,
    created_at TEXT NOT NULL,
    verified_at TEXT,
    approved_at TEXT,
    updated_at TEXT NOT NULL,
    placed_by_agent INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS items_state_idx ON items(state, created_at DESC);
CREATE INDEX IF NOT EXISTS items_project_idx ON items(project_run_id, created_at DESC);

CREATE TABLE IF NOT EXISTS movie_edits (
    item_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 1,
    document_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (item_id) REFERENCES items(id)
);

CREATE TABLE IF NOT EXISTS movie_notes (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    at_seconds REAL NOT NULL,
    text TEXT NOT NULL,
    has_screenshot INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    view_json TEXT,
    FOREIGN KEY (item_id) REFERENCES items(id)
);

CREATE INDEX IF NOT EXISTS movie_notes_item_idx ON movie_notes(item_id, at_seconds);

CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    event TEXT NOT NULL,
    actor TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS audit_no_update
BEFORE UPDATE ON audit_events BEGIN
    SELECT RAISE(ABORT, 'audit events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_no_delete
BEFORE DELETE ON audit_events BEGIN
    SELECT RAISE(ABORT, 'audit events are append-only');
END;

CREATE TABLE IF NOT EXISTS auth_attempts (
    key TEXT NOT NULL,
    attempted_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS auth_attempts_key_idx ON auth_attempts(key, attempted_at);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(items)")}
            if "placed_by_agent" not in columns:
                # Databases made before agents could place items straight into Movies.
                connection.execute("ALTER TABLE items ADD COLUMN placed_by_agent INTEGER NOT NULL DEFAULT 0")
            note_columns = {row["name"] for row in connection.execute("PRAGMA table_info(movie_notes)")}
            if "view_json" not in note_columns:
                # Databases made before notes on 3D models kept a camera view.
                connection.execute("ALTER TABLE movie_notes ADD COLUMN view_json TEXT")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
