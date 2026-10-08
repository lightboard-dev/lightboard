"""SQLite storage, schema migration, and transaction boundaries."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from models import BUSY_TIMEOUT_MS, DB_SCHEMA_VERSION, GitContext, LightboardError


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    owner TEXT,
    task_type TEXT NOT NULL DEFAULT 'FEATURE' CHECK (task_type IN ('FEATURE', 'AUDIT', 'FIX', 'VERIFY')),
    status TEXT NOT NULL CHECK (status IN ('TODO', 'CLAIMED', 'IN_PROGRESS', 'BLOCKED', 'REVIEW', 'DONE')),
    branch TEXT,
    base_sha TEXT,
    head_sha TEXT,
    target_task_id TEXT REFERENCES tasks(id),
    target_base_sha TEXT,
    target_head_sha TEXT,
    reviewed_target_head_sha TEXT,
    lens TEXT CHECK (lens IS NULL OR lens IN ('GENERAL', 'STATE', 'OWNERSHIP', 'LIFECYCLE', 'CONCURRENCY', 'FAILURE', 'TRANSACTION', 'CRITICAL', 'TEST_ORACLE')),
    review_group_id TEXT,
    review_slot TEXT CHECK (review_slot IS NULL OR review_slot IN ('A', 'B')),
    fix_task_id TEXT REFERENCES tasks(id),
    fix_head_sha TEXT,
    scope_paths TEXT NOT NULL DEFAULT '[]',
    audit_verified TEXT NOT NULL DEFAULT '[]',
    audit_unverified TEXT NOT NULL DEFAULT '[]',
    audit_not_reviewed TEXT NOT NULL DEFAULT '[]',
    dependencies TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    follow_up_to TEXT,
    touches TEXT NOT NULL DEFAULT '[]',
    block_reason TEXT,
    started_at TEXT,
    verified_contract_ids TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('NOTE', 'CONTRACT', 'BLOCKER', 'REVIEW_FINDING')),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_task_id TEXT NOT NULL REFERENCES tasks(id),
    target_task_id TEXT NOT NULL REFERENCES tasks(id),
    target_base_sha TEXT,
    target_head_sha TEXT,
    reviewer TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW')),
    lens TEXT NOT NULL CHECK (lens IN ('GENERAL', 'STATE', 'OWNERSHIP', 'LIFECYCLE', 'CONCURRENCY', 'FAILURE', 'TRANSACTION', 'CRITICAL', 'TEST_ORACLE')),
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'FIXED', 'VERIFIED', 'REOPENED', 'REJECTED', 'DEFERRED')),
    claim TEXT NOT NULL,
    evidence TEXT NOT NULL,
    scenario TEXT NOT NULL,
    required_property TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_by_task_id TEXT REFERENCES tasks(id),
    verified_by_task_id TEXT REFERENCES tasks(id),
    verification_reason TEXT,
    CHECK (status != 'FIXED' OR resolved_by_task_id IS NOT NULL),
    CHECK (status != 'VERIFIED' OR verified_by_task_id IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS task_findings (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    finding_id INTEGER NOT NULL REFERENCES findings(id),
    relation TEXT NOT NULL CHECK (relation IN ('FIX', 'VERIFY')),
    PRIMARY KEY (task_id, finding_id, relation)
);
CREATE INDEX IF NOT EXISTS entries_task_kind ON entries(task_id, kind, id);
CREATE INDEX IF NOT EXISTS events_task_id ON events(task_id, id);
CREATE INDEX IF NOT EXISTS findings_target_id ON findings(target_task_id, id);
CREATE INDEX IF NOT EXISTS findings_audit_id ON findings(audit_task_id, id);
CREATE INDEX IF NOT EXISTS task_findings_finding_id ON task_findings(finding_id, relation, task_id);
"""

MIGRATION_COLUMNS = {
    "task_type": "TEXT NOT NULL DEFAULT 'FEATURE' CHECK (task_type IN ('FEATURE', 'AUDIT', 'FIX', 'VERIFY'))",
    "follow_up_to": "TEXT",
    "touches": "TEXT NOT NULL DEFAULT '[]'",
    "block_reason": "TEXT",
    "started_at": "TEXT",
    "verified_contract_ids": "TEXT NOT NULL DEFAULT '[]'",
    "target_task_id": "TEXT REFERENCES tasks(id)",
    "target_base_sha": "TEXT",
    "target_head_sha": "TEXT",
    "reviewed_target_head_sha": "TEXT",
    "lens": "TEXT",
    "review_group_id": "TEXT",
    "review_slot": "TEXT",
    "fix_task_id": "TEXT REFERENCES tasks(id)",
    "fix_head_sha": "TEXT",
    "scope_paths": "TEXT NOT NULL DEFAULT '[]'",
    "audit_verified": "TEXT NOT NULL DEFAULT '[]'",
    "audit_unverified": "TEXT NOT NULL DEFAULT '[]'",
    "audit_not_reviewed": "TEXT NOT NULL DEFAULT '[]'",
}

FINDING_MIGRATION_COLUMNS = {
    "target_base_sha": "TEXT",
    "target_head_sha": "TEXT",
    "verification_reason": "TEXT",
}


def _database_error(exc: sqlite3.Error) -> LightboardError:
    message = str(exc) or exc.__class__.__name__
    lowered = message.lower()
    if "locked" in lowered or "busy" in lowered:
        return LightboardError(f"database is locked: {message}")
    if "not a database" in lowered or "malformed" in lowered or "disk image" in lowered:
        return LightboardError(f"database is corrupt or unreadable: {message}")
    if "unable to open database file" in lowered:
        return LightboardError(
            f"database error: {message}; you may be trying to connect from a sandbox "
            "that does not have access to the database file"
        )
    return LightboardError(f"database error: {message}")


def _migrate(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
    for name, definition in MIGRATION_COLUMNS.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
    finding_columns = {row[1] for row in connection.execute("PRAGMA table_info(findings)")}
    for name, definition in FINDING_MIGRATION_COLUMNS.items():
        if name not in finding_columns:
            connection.execute(f"ALTER TABLE findings ADD COLUMN {name} {definition}")
    connection.execute(
        """
        UPDATE tasks
        SET block_reason = (
            SELECT body FROM entries
            WHERE entries.task_id = tasks.id AND entries.kind = 'BLOCKER'
            ORDER BY entries.id DESC LIMIT 1
        )
        WHERE status = 'BLOCKED' AND block_reason IS NULL
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS review_group_slot ON tasks(review_group_id, review_slot)
        WHERE review_group_id IS NOT NULL AND review_slot IS NOT NULL
        """
    )
    connection.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")


class Board:
    def __init__(self, context: GitContext):
        self.path = context.board_path

    def _connect(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_MS / 1000)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            _migrate(connection)
            connection.commit()
            return connection
        except OSError as exc:
            if connection is not None:
                connection.close()
            raise LightboardError(f"cannot create board storage: {exc}") from exc
        except sqlite3.Error as exc:
            if connection is not None:
                connection.rollback()
                connection.close()
            raise _database_error(exc) from exc

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except LightboardError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise _database_error(exc) from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        except sqlite3.Error as exc:
            raise _database_error(exc) from exc
        finally:
            connection.close()
