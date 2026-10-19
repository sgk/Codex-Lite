"""Retention for Lite-owned history. Codex JSONL and private DB are untouched."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .db import Database


SCHEMA = """
CREATE TABLE IF NOT EXISTS history_retention (
  chat_id TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
  compacted_at TEXT NOT NULL,
  pruned_at TEXT
);
"""

HISTORY_TABLES = (
    "history_messages", "history_transcript_messages", "history_activity_state",
    "history_sources", "history_checkpoint", "history_revision", "history_retention",
)


def timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Compaction timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


class HistoryRetention:
    def __init__(self, db: Database):
        self.db = db

    def record_compaction(self, chat_id: str, occurred_at: str) -> None:
        self.db.execute("""INSERT INTO history_retention(chat_id, compacted_at) VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET compacted_at=excluded.compacted_at, pruned_at=NULL
            WHERE excluded.compacted_at > history_retention.compacted_at""", (chat_id, timestamp(occurred_at)))

    def cutoff(self, chat_id: str) -> str | None:
        row = self.db.fetchone("SELECT compacted_at FROM history_retention WHERE chat_id=?", (chat_id,))
        return row["compacted_at"] if row else None

    def prune_chat(self, chat_id: str) -> None:
        with self.db.transaction():
            row = self.db.fetchone("SELECT * FROM history_retention WHERE chat_id=?", (chat_id,))
            if row is None or row["pruned_at"] == row["compacted_at"]:
                return
            cutoff = row["compacted_at"]
            # Retain all user/assistant prose and usage accounting. Only the
            # obsolete intermediate activity and its durable copies are removed.
            self.db.execute("""DELETE FROM history_transcript_messages WHERE chat_id=?
                AND json_extract(payload, '$.role')='status'
                AND julianday(json_extract(payload, '$.createdAt')) <= julianday(?)""", (chat_id, cutoff))
            self.db.execute("""DELETE FROM history_messages WHERE chat_id=? AND prose=0
                AND julianday(json_extract(payload, '$.createdAt')) <= julianday(?)""", (chat_id, cutoff))
            self.db.execute("""DELETE FROM history_activity_state WHERE chat_id=?
                AND julianday(json_extract(payload, '$.created_at')) <= julianday(?)""", (chat_id, cutoff))
            self.db.execute("""DELETE FROM run_events WHERE run_id IN (SELECT id FROM runs WHERE chat_id=?)
                AND event='progress' AND julianday(created_at) <= julianday(?)""", (chat_id, cutoff))
            self.db.execute("DELETE FROM messages WHERE chat_id=? AND kind IN ('status','activity') AND julianday(created_at)<=julianday(?)", (chat_id, cutoff))
            self.db.execute("UPDATE history_checkpoint SET revision=-1 WHERE chat_id=?", (chat_id,))
            self.db.execute("UPDATE history_retention SET pruned_at=compacted_at WHERE chat_id=?", (chat_id,))

    def purge_archived(self, limit: int = 1) -> int:
        # A chat can be archived while its final notifications are arriving.
        # Do not race those writers; the next maintenance pass handles it.
        cached = " OR ".join(f"EXISTS (SELECT 1 FROM {table} h WHERE h.chat_id=c.id)" for table in HISTORY_TABLES)
        chats = self.db.fetchall(f"""SELECT c.id FROM chats c WHERE c.archived_at IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.chat_id=c.id AND r.status IN ('queued','running'))
            AND ({cached}
              OR EXISTS (SELECT 1 FROM messages m WHERE m.chat_id=c.id)
              OR EXISTS (SELECT 1 FROM runs r CROSS JOIN run_events e ON e.run_id=r.id WHERE r.chat_id=c.id))
            ORDER BY c.id LIMIT ?""", (max(1, limit),))
        removed = 0
        for chat in chats:
            with self.db.transaction():
                # Recheck after obtaining the writer lock, including unarchive.
                eligible = self.db.fetchone("""SELECT id FROM chats WHERE id=? AND archived_at IS NOT NULL
                    AND NOT EXISTS (SELECT 1 FROM runs WHERE chat_id=? AND status IN ('queued','running'))""", (chat["id"], chat["id"]))
                if eligible is None:
                    continue
                self.db.execute("DELETE FROM run_events WHERE run_id IN (SELECT id FROM runs WHERE chat_id=?)", (chat["id"],))
                self.db.execute("DELETE FROM messages WHERE chat_id=?", (chat["id"],))
                for table in HISTORY_TABLES:
                    self.db.execute(f"DELETE FROM {table} WHERE chat_id=?", (chat["id"],))
                removed += 1
        return removed

    def maintain(self) -> None:
        # Never compact storage concurrently with a JSONL-to-SQLite refresh.
        with self.db._lock:
            if self.db.history_readers:
                return
            self.purge_archived()
            for row in self.db.fetchall("SELECT chat_id FROM history_retention WHERE pruned_at IS NULL OR pruned_at<>compacted_at ORDER BY chat_id LIMIT 1"):
                self.prune_chat(row["chat_id"])
        self.db.reclaim_unused_space()


async def run_history_maintenance(db: Database, stop: asyncio.Event) -> None:
    retention = HistoryRetention(db)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
        except asyncio.TimeoutError:
            try:
                await asyncio.to_thread(retention.maintain)
            except Exception:
                # Do not silently retry a broken maintenance operation.
                logging.getLogger(__name__).exception("History retention stopped after a maintenance failure")
                return
