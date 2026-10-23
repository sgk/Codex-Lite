from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
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
  auto_title_set_at TEXT,
  codex_session_id TEXT,
  transcript_path TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  archived_at TEXT,
  can_continue INTEGER NOT NULL DEFAULT 1,
  continue_disabled_reason TEXT,
  permission_profile TEXT,
  approval_policy TEXT,
  approvals_reviewer TEXT,
  model TEXT,
  reasoning_effort TEXT,
  resume_on_start INTEGER NOT NULL DEFAULT 0,
  resume_requested_at TEXT
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

CREATE TABLE IF NOT EXISTS submission_receipts (
  request_id TEXT PRIMARY KEY,
  payload_hash TEXT NOT NULL,
  run_id TEXT NOT NULL,
  message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL
);

-- One Codex Lite chat may have a separate internal thread per provider.
-- Keeping this mapping in the Lite DB prevents provider-specific JSONL
-- histories from being mixed while the visible chat remains one session.
CREATE TABLE IF NOT EXISTS chat_provider_threads (
  chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  transcript_path TEXT,
  history_initialized INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(chat_id, provider)
);

CREATE TABLE IF NOT EXISTS model_reasoning_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model TEXT NOT NULL,
  reasoning_effort TEXT NOT NULL,
  used_at TEXT NOT NULL,
  UNIQUE(model, reasoning_effort)
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
  error TEXT,
  provider TEXT,
  thread_id TEXT,
  turn_id TEXT,
  last_event_at TEXT,
  last_event_method TEXT,
  last_reconcile_at TEXT,
  watcher_state TEXT,
  terminal_reason TEXT,
  last_thread_status TEXT,
  last_reconcile_error TEXT,
  adopted INTEGER NOT NULL DEFAULT 0,
  revision INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS run_events (
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  sequence INTEGER NOT NULL,
  event TEXT NOT NULL,
  data_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(run_id, sequence)
);

CREATE TABLE IF NOT EXISTS token_usage_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  input_tokens INTEGER NOT NULL,
  cached_input_tokens INTEGER NOT NULL,
  cache_write_input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL,
  reasoning_output_tokens INTEGER NOT NULL,
  total_tokens INTEGER NOT NULL,
  thread_total_signature TEXT NOT NULL,
  model_context_window INTEGER,
  UNIQUE(provider, thread_id, thread_total_signature)
);

CREATE TABLE IF NOT EXISTS usage_windows (
  provider TEXT NOT NULL,
  window_kind TEXT NOT NULL CHECK(window_kind IN ('five_hour', 'weekly')),
  window_minutes INTEGER NOT NULL,
  start_at TEXT NOT NULL,
  resets_at TEXT NOT NULL,
  used_percent REAL,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY(provider, window_kind)
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

CREATE TABLE IF NOT EXISTS remote_tasks (
  task_id TEXT PRIMARY KEY,
  operation TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed')),
  run_id TEXT,
  result_json TEXT,
  error_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chats_project ON chats(project_id);
CREATE INDEX IF NOT EXISTS idx_messages_chat_created ON messages(chat_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_run ON messages(run_id);
CREATE INDEX IF NOT EXISTS idx_submission_receipts_message ON submission_receipts(message_id);
CREATE INDEX IF NOT EXISTS idx_chat_provider_threads_thread ON chat_provider_threads(provider, thread_id);
CREATE INDEX IF NOT EXISTS idx_model_reasoning_history_used ON model_reasoning_history(used_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_runs_chat_started ON runs(chat_id, started_at);
CREATE INDEX IF NOT EXISTS idx_run_events_run_sequence ON run_events(run_id, sequence);
CREATE INDEX IF NOT EXISTS idx_run_events_run_event ON run_events(run_id, event);
CREATE INDEX IF NOT EXISTS idx_token_usage_samples_recorded ON token_usage_samples(provider, recorded_at);
CREATE INDEX IF NOT EXISTS idx_token_usage_samples_chat ON token_usage_samples(chat_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_automations_chat ON automations(project_id, chat_id);
CREATE INDEX IF NOT EXISTS idx_automations_due ON automations(enabled, running, next_run_at);
CREATE INDEX IF NOT EXISTS idx_remote_tasks_updated ON remote_tasks(updated_at);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._in_transaction = False
        self.history_readers = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_database = not self.path.exists() or self.path.stat().st_size == 0
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if new_database:
            self._conn.execute("PRAGMA auto_vacuum = INCREMENTAL")

    def migrate(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(chats)").fetchall()}
            if "transcript_path" not in columns:
                self._conn.execute("ALTER TABLE chats ADD COLUMN transcript_path TEXT")
            if "title_override_at" not in columns:
                self._conn.execute("ALTER TABLE chats ADD COLUMN title_override_at TEXT")
            if "auto_title_set_at" not in columns:
                self._conn.execute("ALTER TABLE chats ADD COLUMN auto_title_set_at TEXT")
            if "can_continue" not in columns:
                self._conn.execute("ALTER TABLE chats ADD COLUMN can_continue INTEGER NOT NULL DEFAULT 1")
            if "continue_disabled_reason" not in columns:
                self._conn.execute("ALTER TABLE chats ADD COLUMN continue_disabled_reason TEXT")
            if "permission_profile" not in columns:
                self._conn.execute("ALTER TABLE chats ADD COLUMN permission_profile TEXT")
            if "approval_policy" not in columns:
                self._conn.execute("ALTER TABLE chats ADD COLUMN approval_policy TEXT")
            if "approvals_reviewer" not in columns:
                self._conn.execute("ALTER TABLE chats ADD COLUMN approvals_reviewer TEXT")
            if "model" not in columns:
                self._conn.execute("ALTER TABLE chats ADD COLUMN model TEXT")
            if "reasoning_effort" not in columns:
                self._conn.execute("ALTER TABLE chats ADD COLUMN reasoning_effort TEXT")
            if "resume_on_start" not in columns:
                self._conn.execute("ALTER TABLE chats ADD COLUMN resume_on_start INTEGER NOT NULL DEFAULT 0")
            if "resume_requested_at" not in columns:
                self._conn.execute("ALTER TABLE chats ADD COLUMN resume_requested_at TEXT")
            message_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(messages)").fetchall()}
            if "kind" not in message_columns:
                self._conn.execute("ALTER TABLE messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'conclusion'")
                self._conn.execute("UPDATE messages SET kind = 'instruction' WHERE role = 'user'")
            run_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(runs)").fetchall()}
            for name, declaration in {
                "provider": "TEXT",
                "thread_id": "TEXT",
                "turn_id": "TEXT",
                "last_event_at": "TEXT",
                "last_event_method": "TEXT",
                "last_reconcile_at": "TEXT",
                "watcher_state": "TEXT",
                "terminal_reason": "TEXT",
                "last_thread_status": "TEXT",
                "last_reconcile_error": "TEXT",
                "adopted": "INTEGER NOT NULL DEFAULT 0",
                "revision": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in run_columns:
                    self._conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {declaration}")
            automation_sql_row = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'automations'"
            ).fetchone()
            automation_sql = str(automation_sql_row["sql"] or "") if automation_sql_row else ""
            if "hourly_minute" not in automation_sql or "daily_time" not in automation_sql:
                self._migrate_automation_schedule_kinds()
            if self.fetchone("SELECT version FROM schema_version WHERE version = 1") is None:
                self.execute("INSERT INTO schema_version(version, applied_at) VALUES (1, datetime('now'))")
            self._conn.commit()
            from .history_store import HISTORY_SCHEMA
            self._conn.executescript(HISTORY_SCHEMA)
            from .history_retention import SCHEMA as RETENTION_SCHEMA
            self._conn.executescript(RETENTION_SCHEMA)

    @contextmanager
    def history_reader(self):
        with self._lock:
            self.history_readers += 1
        try:
            yield
        finally:
            with self._lock:
                self.history_readers -= 1

    def reclaim_unused_space(self, pages: int = 4096) -> bool:
        """Reclaim disk pages only between runs and history readers.

        Existing non-auto-vacuum databases require one VACUUM to enable
        incremental reclamation. This is never called by startup or a request.
        """
        with self._lock:
            if self._in_transaction or self.history_readers:
                return False
            if self.fetchone("SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1"):
                return False
            free = self._conn.execute("PRAGMA freelist_count").fetchone()[0]
            if not free:
                return False
            mode = self._conn.execute("PRAGMA auto_vacuum").fetchone()[0]
            if mode != 2:
                self._conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
                self._conn.execute("VACUUM")
                # VACUUM may renumber implicit run_events rowids. Invalidate
                # all high-water marks, not just the chat that freed space.
                self._conn.execute("UPDATE history_checkpoint SET event_offset=0, event_reset=-1")
                self._conn.commit()
            else:
                self._conn.execute(f"PRAGMA incremental_vacuum({max(1, int(pages))})").fetchall()
            return True

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

    @contextmanager
    def transaction(self):
        """Group synchronous service writes into one acceptance commit."""
        with self._lock:
            if self._in_transaction:
                raise RuntimeError("Nested database transactions are not supported.")
            self._in_transaction = True
            try:
                self._conn.execute("BEGIN")
                yield
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
            finally:
                self._in_transaction = False

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            if not self._in_transaction:
                self._conn.commit()
            return cur

    def executemany(self, sql: str, params: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, params)
            if not self._in_transaction:
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
