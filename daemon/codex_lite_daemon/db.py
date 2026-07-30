from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  path TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chats (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  title_override_at TEXT,
  codex_session_id TEXT,
  transcript_path TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  archived_at TEXT,
  can_continue INTEGER NOT NULL DEFAULT 1,
  continue_disabled_reason TEXT
);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system', 'tool')),
  content TEXT NOT NULL,
  run_id TEXT,
  created_at TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('instruction', 'work', 'conclusion', 'waiting', 'status', 'activity'))
);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
  pid INTEGER,
  exit_code INTEGER,
  started_at TEXT,
  finished_at TEXT,
  log_path TEXT,
  error TEXT
);

CREATE TABLE IF NOT EXISTS automations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  prompt TEXT NOT NULL,
  schedule_kind TEXT NOT NULL CHECK(schedule_kind IN ('interval_minutes', 'hourly_minute', 'daily_time')),
  interval_minutes INTEGER NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  running INTEGER NOT NULL DEFAULT 0,
  next_run_at TEXT,
  last_run_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chats_project ON chats(project_id);
CREATE INDEX IF NOT EXISTS idx_messages_chat_created ON messages(chat_id, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_chat_started ON runs(chat_id, started_at);
CREATE INDEX IF NOT EXISTS idx_automations_chat ON automations(project_id, chat_id);
CREATE INDEX IF NOT EXISTS idx_automations_due ON automations(enabled, running, next_run_at);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    def migrate(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(chats)").fetchall()}
            if "transcript_path" not in columns:
                self._conn.execute("ALTER TABLE chats ADD COLUMN transcript_path TEXT")
            if "title_override_at" not in columns:
                self._conn.execute("ALTER TABLE chats ADD COLUMN title_override_at TEXT")
            if "can_continue" not in columns:
                self._conn.execute("ALTER TABLE chats ADD COLUMN can_continue INTEGER NOT NULL DEFAULT 1")
            if "continue_disabled_reason" not in columns:
                self._conn.execute("ALTER TABLE chats ADD COLUMN continue_disabled_reason TEXT")
            message_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(messages)").fetchall()}
            if "kind" not in message_columns:
                self._conn.execute("ALTER TABLE messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'conclusion'")
                self._conn.execute("UPDATE messages SET kind = 'instruction' WHERE role = 'user'")
            automation_sql_row = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'automations'"
            ).fetchone()
            automation_sql = str(automation_sql_row["sql"] or "") if automation_sql_row else ""
            if "hourly_minute" not in automation_sql or "daily_time" not in automation_sql:
                self._migrate_automation_schedule_kinds()
            if self.fetchone("SELECT version FROM schema_version WHERE version = 1") is None:
                self.execute("INSERT INTO schema_version(version, applied_at) VALUES (1, datetime('now'))")
            self._conn.commit()

    def _migrate_automation_schedule_kinds(self) -> None:
        self._conn.executescript(
            """
            PRAGMA foreign_keys = OFF;
            ALTER TABLE automations RENAME TO automations_legacy;
            CREATE TABLE automations (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
              chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
              name TEXT NOT NULL,
              prompt TEXT NOT NULL,
              schedule_kind TEXT NOT NULL CHECK(schedule_kind IN ('interval_minutes', 'hourly_minute', 'daily_time')),
              interval_minutes INTEGER NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              running INTEGER NOT NULL DEFAULT 0,
              next_run_at TEXT,
              last_run_at TEXT,
              last_error TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            INSERT INTO automations
            SELECT id, project_id, chat_id, name, prompt, schedule_kind, interval_minutes,
                   enabled, running, next_run_at, last_run_at, last_error, created_at, updated_at
            FROM automations_legacy;
            DROP TABLE automations_legacy;
            CREATE INDEX idx_automations_chat ON automations(project_id, chat_id);
            CREATE INDEX idx_automations_due ON automations(enabled, running, next_run_at);
            PRAGMA foreign_keys = ON;
            """
        )

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def executemany(self, sql: str, params: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, params)
            self._conn.commit()

    def fetchone(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

    def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
            return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
