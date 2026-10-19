"""Lite-owned, durable message projection. Never writes to Codex's database."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

from .db import Database
from .errors import AppError
from .transcript_import import _transcript_message


HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS history_revision (
  chat_id TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
  revision INTEGER NOT NULL DEFAULT 0,
  event_reset INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS history_checkpoint (
  chat_id TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
  revision INTEGER NOT NULL,
  event_offset INTEGER NOT NULL DEFAULT 0,
  event_reset INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS history_sources (
  chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  identity TEXT NOT NULL,
  byte_offset INTEGER NOT NULL,
  modified_ns INTEGER NOT NULL,
  observed_size INTEGER NOT NULL,
  boundary_hash TEXT NOT NULL,
  pending_json TEXT NOT NULL,
  PRIMARY KEY(chat_id, path)
);
CREATE TABLE IF NOT EXISTS history_transcript_messages (
  chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  id TEXT NOT NULL,
  byte_offset INTEGER NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY(chat_id, path, id)
);
CREATE INDEX IF NOT EXISTS idx_history_transcript_order
  ON history_transcript_messages(chat_id, path, byte_offset);
CREATE TABLE IF NOT EXISTS history_activity_state (
  chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  state_key TEXT NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY(chat_id, state_key)
);
CREATE TABLE IF NOT EXISTS history_messages (
  chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  id TEXT NOT NULL,
  position INTEGER NOT NULL,
  prose INTEGER NOT NULL,
  assistant INTEGER NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY(chat_id, id)
);
CREATE INDEX IF NOT EXISTS idx_history_page ON history_messages(chat_id, prose, position);
CREATE INDEX IF NOT EXISTS idx_history_order ON history_messages(chat_id, position);
CREATE INDEX IF NOT EXISTS idx_history_assistant ON history_messages(chat_id, assistant, position);
"""

# Changes made by every writer (including direct SQL updates) invalidate the
# projection. A completed refresh records the revision it actually observed.
for _table in ("messages", "run_events"):
    for _operation in ("INSERT", "UPDATE", "DELETE"):
        _row = "OLD" if _operation == "DELETE" else "NEW"
        _chat = f"{_row}.chat_id" if _table == "messages" else f"(SELECT chat_id FROM runs WHERE id = {_row}.run_id)"
        _reset = int(_table == "run_events" and _operation != "INSERT")
        HISTORY_SCHEMA += f"""
CREATE TRIGGER IF NOT EXISTS history_{_table}_{_operation.lower()} AFTER {_operation} ON {_table}
WHEN EXISTS (SELECT 1 FROM chats WHERE id = {_chat})
BEGIN
  INSERT INTO history_revision(chat_id, revision, event_reset) VALUES ({_chat}, 1, {_reset})
  ON CONFLICT(chat_id) DO UPDATE SET revision = revision + 1, event_reset = event_reset + {_reset};
END;
"""

HISTORY_SCHEMA += """
CREATE TRIGGER IF NOT EXISTS history_run_deleted BEFORE DELETE ON runs
WHEN EXISTS (SELECT 1 FROM chats WHERE id = OLD.chat_id)
BEGIN
  INSERT INTO history_revision(chat_id, revision, event_reset) VALUES (OLD.chat_id, 1, 1)
  ON CONFLICT(chat_id) DO UPDATE SET revision = revision + 1, event_reset = event_reset + 1;
END;
CREATE TRIGGER IF NOT EXISTS history_run_window_updated AFTER UPDATE OF started_at, finished_at, status ON runs
WHEN OLD.started_at IS NOT NEW.started_at OR OLD.finished_at IS NOT NEW.finished_at OR OLD.status IS NOT NEW.status
BEGIN
  INSERT INTO history_revision(chat_id, revision) VALUES (NEW.chat_id, 1)
  ON CONFLICT(chat_id) DO UPDATE SET revision = revision + 1;
END;
"""


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class HistoryStore:
    def __init__(self, db: Database):
        self.db = db
        self.locks = [threading.RLock() for _ in range(32)]

    def lock(self, chat_id: str):
        return self.locks[hash(chat_id) % len(self.locks)]

    def import_source(self, chat_id: str, path: Path) -> bool:
        """Read only appended bytes; commit messages and cursor atomically.

        A replaced/truncated/rewritten source is explicitly reindexed, not
        interpreted using an obsolete cursor. Partial final JSON is retried
        at the same byte position on the next refresh.
        """
        saved = self.db.fetchone("SELECT * FROM history_sources WHERE chat_id = ? AND path = ?", (chat_id, str(path)))
        with path.open("rb") as handle:
            stat = os.fstat(handle.fileno())
            identity = f"{stat.st_dev}:{stat.st_ino}"
            reset = saved is None or saved["identity"] != identity or stat.st_size < saved["byte_offset"]
            if saved and stat.st_size == saved["observed_size"] and stat.st_mtime_ns != saved["modified_ns"]:
                reset = True
            if saved and not reset:
                handle.seek(max(0, saved["byte_offset"] - 4096))
                boundary = handle.read(min(saved["byte_offset"], 4096))
                reset = hashlib.sha256(boundary).hexdigest() != saved["boundary_hash"]
            if saved and not reset and stat.st_size == saved["observed_size"] and stat.st_mtime_ns == saved["modified_ns"]:
                return False
            offset = 0 if reset else saved["byte_offset"]
            pending = {} if reset else json.loads(saved["pending_json"])
            handle.seek(offset)
            records = []
            # Snapshot the file length so a busy writer cannot extend this job
            # indefinitely. Subsequent appended bytes belong to the next pass.
            while handle.tell() < stat.st_size:
                start = handle.tell()
                raw = handle.readline(stat.st_size - start)
                try:
                    item = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    if not raw.endswith(b"\n"):
                        break
                    offset = handle.tell()
                    continue
                if isinstance(item, dict):
                    message = _transcript_message(chat_id, item, f"byte-{start}", pending)
                    if message is not None:
                        records.append((chat_id, str(path), message["id"], start, encoded(message)))
                offset = handle.tell()
            handle.seek(max(0, offset - 4096))
            boundary_hash = hashlib.sha256(handle.read(min(offset, 4096))).hexdigest()
        with self.db.transaction():
            if reset:
                self.db.execute("DELETE FROM history_transcript_messages WHERE chat_id = ? AND path = ?", (chat_id, str(path)))
            self.db.executemany("""INSERT INTO history_transcript_messages VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, path, id) DO UPDATE SET payload=excluded.payload""", records)
            self.db.execute("""INSERT OR REPLACE INTO history_sources VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (chat_id, str(path), identity, offset, stat.st_mtime_ns, stat.st_size, boundary_hash, encoded(pending)))
            self.db.execute("""INSERT INTO history_revision(chat_id, revision) VALUES (?, 1)
                ON CONFLICT(chat_id) DO UPDATE SET revision=revision+1""", (chat_id,))
        return True

    def transcripts(self, chat_id: str) -> list[dict]:
        return [json.loads(row["payload"]) for row in self.db.fetchall(
            "SELECT payload FROM history_transcript_messages WHERE chat_id = ? ORDER BY path, byte_offset", (chat_id,))]

    def replace_projection(self, chat_id: str, messages: list[dict], revision: int, event_offset: int, event_reset: int, states: dict) -> None:
        existing = {row["id"]: row for row in self.db.fetchall("SELECT id, payload FROM history_messages WHERE chat_id = ?", (chat_id,))}
        changed = []
        for position, message in enumerate(messages, 1):
            message = {**message, "historySequence": position}
            payload = encoded(message)
            previous = existing.pop(message["id"], None)
            if previous and previous["payload"] == payload:
                continue
            prose = message.get("role") != "status" and message.get("kind") not in {"status", "activity"}
            changed.append((chat_id, message["id"], position, int(prose), int(message.get("role") == "assistant"), payload))
        old_states = {row["state_key"]: row["payload"] for row in self.db.fetchall(
            "SELECT state_key, payload FROM history_activity_state WHERE chat_id = ?", (chat_id,))}
        changed_states = []
        for key, state in states.items():
            state_key, payload = encoded(key), encoded(state)
            if old_states.pop(state_key, None) != payload:
                changed_states.append((chat_id, state_key, payload))
        # Serialize outside the DB lock. Only changed rows are written, and the
        # projection and checkpoint become visible in the same short commit.
        with self.db.transaction():
            self.db.executemany("INSERT OR REPLACE INTO history_messages VALUES (?, ?, ?, ?, ?, ?)", changed)
            self.db.executemany("DELETE FROM history_messages WHERE chat_id = ? AND id = ?", [(chat_id, key) for key in existing])
            self.db.executemany("DELETE FROM history_activity_state WHERE chat_id = ? AND state_key = ?", [(chat_id, key) for key in old_states])
            self.db.executemany("INSERT OR REPLACE INTO history_activity_state VALUES (?, ?, ?)", changed_states)
            self.db.execute("INSERT OR REPLACE INTO history_checkpoint VALUES (?, ?, ?, ?)", (chat_id, revision, event_offset, event_reset))

    def all_messages(self, chat_id: str) -> list[dict]:
        return [json.loads(row["payload"]) for row in self.db.fetchall(
            "SELECT payload FROM history_messages WHERE chat_id = ? ORDER BY position", (chat_id,))]

    def page(self, chat_id: str, limit: int, before_id: str | None) -> dict:
        boundary = None
        if before_id:
            cursor = self.db.fetchone("SELECT position FROM history_messages WHERE chat_id = ? AND id = ?", (chat_id, before_id))
            if cursor is None:
                raise AppError("history_cursor_expired", "履歴が変更されました。チャットを開き直してください。", 409)
            boundary = cursor["position"]
        rows = self.db.fetchall("""SELECT position, payload FROM history_messages
            WHERE chat_id = ? AND prose = 1 AND position < ?
            ORDER BY position DESC LIMIT ?""", (chat_id, boundary if boundary is not None else 9223372036854775807, limit + 1))
        more = len(rows) > limit
        page = [json.loads(row["payload"]) for row in reversed(rows[:limit])]
        if boundary is None:
            page.extend(json.loads(row["payload"]) for row in self.db.fetchall("""
                SELECT payload FROM history_messages WHERE chat_id = ? AND prose = 0
                  AND position > COALESCE((SELECT MAX(position) FROM history_messages WHERE chat_id = ? AND assistant = 1), 0)
                ORDER BY position""", (chat_id, chat_id)))
            page.sort(key=lambda message: message["historySequence"])
        total = self.db.fetchone("SELECT COUNT(*) AS n FROM history_messages WHERE chat_id = ? AND prose = 1", (chat_id,))["n"]
        return {"messages": page, "totalCount": total, "totalCountIsExact": True, "hasMoreBefore": more}
