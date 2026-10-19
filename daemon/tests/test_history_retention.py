from __future__ import annotations

import json
import sqlite3

import pytest

from codex_lite_daemon.db import Database
from codex_lite_daemon.errors import AppError
from codex_lite_daemon.history_retention import HISTORY_TABLES, HistoryRetention
from codex_lite_daemon.run_service import EventHub
from test_history_tail import assistant, history, write_lines


def thought(when, value="old detail"):
    return {"timestamp": when, "type": "response_item", "payload": {
        "type": "reasoning", "summary": [{"type": "summary_text", "text": value}]}}


@pytest.mark.asyncio
async def test_compaction_keeps_prose_but_prunes_activity_and_does_not_resurrect(history):
    service, _, messages, db, project_id, path, meta = history
    records = [meta, assistant(1), thought("2026-10-01T00:00:02Z"),
               {"type": "compacted", "timestamp": "2026-10-01T00:00:03Z", "payload": {}},
               thought("2026-10-01T00:00:04Z", "new detail")]
    write_lines(path, records)
    messages.insert_message("thread-tail", "user", "keep instruction", kind="instruction", created_at="2026-10-01T00:00:00Z")
    db.execute("INSERT INTO runs(id,chat_id,status) VALUES ('r','thread-tail','succeeded')")
    db.execute("INSERT INTO run_events VALUES ('r',1,'progress',?,?)", (json.dumps({"method": "old", "details": "large old detail"}), "2026-10-01T00:00:02Z"))
    page = await service.list_message_page(project_id, "thread-tail", 100, None, None)
    assert any(m["content"] == "回答1" for m in page["messages"])
    assert any(m["content"] == "keep instruction" for m in page["messages"])
    assert not db.fetchone("SELECT 1 FROM run_events WHERE event='progress'")
    assert all("old detail" not in row["payload"] for row in db.fetchall("SELECT payload FROM history_transcript_messages"))
    assert any("new detail" in row["payload"] for row in db.fetchall("SELECT payload FROM history_transcript_messages"))
    # Even a rewritten file containing old detail must respect the watermark.
    write_lines(path, [*records, assistant(5)])
    await service.list_message_page(project_id, "thread-tail", 100, None, None)
    assert all("old detail" not in row["payload"] for row in db.fetchall("SELECT payload FROM history_messages"))
    assert all("old detail" not in row["payload"] for row in db.fetchall("SELECT payload FROM history_transcript_messages"))


@pytest.mark.asyncio
async def test_archival_purges_history_but_not_jsonl_or_chat_metadata(history):
    service, _, messages, db, project_id, path, meta = history
    write_lines(path, [meta, assistant(1)])
    original = path.read_bytes()
    messages.insert_message("thread-tail", "user", "local history", kind="instruction")
    await service.list_message_page(project_id, "thread-tail", 5, None, None)
    service.chats.archive_chat(project_id, "thread-tail")
    retention = HistoryRetention(db)
    assert retention.purge_archived() == 1
    for table in (*HISTORY_TABLES, "messages"):
        assert db.fetchone(f"SELECT COUNT(*) AS n FROM {table}")["n"] == 0
    assert db.fetchone("SELECT archived_at FROM chats WHERE id='thread-tail'")["archived_at"]
    assert path.read_bytes() == original
    with pytest.raises(AppError) as exc:
        await service.list_message_page(project_id, "thread-tail", 5, None, None)
    assert exc.value.code == "chat_archived"
    assert retention.purge_archived() == 0
    # Restoring the chat explicitly permits reimport from the untouched JSONL.
    db.execute("UPDATE chats SET archived_at=NULL WHERE id='thread-tail'")
    assert (await service.list_message_page(project_id, "thread-tail", 5, None, None))["totalCount"] == 1


@pytest.mark.asyncio
async def test_archived_active_run_is_not_purged_until_finished(history):
    service, _, messages, db, project_id, path, meta = history
    write_lines(path, [meta])
    messages.insert_message("thread-tail", "user", "keep while running", kind="instruction")
    db.execute("INSERT INTO runs(id,chat_id,status) VALUES ('r','thread-tail','running')")
    service.chats.archive_chat(project_id, "thread-tail")
    retention = HistoryRetention(db)
    assert retention.purge_archived() == 0
    db.execute("UPDATE runs SET status='succeeded' WHERE id='r'")
    assert retention.purge_archived() == 1


@pytest.mark.asyncio
async def test_delayed_progress_flush_cannot_restore_compacted_or_archived_data(history):
    service, _, _, db, project_id, path, meta = history
    db.execute("INSERT INTO runs(id,chat_id,status) VALUES ('r','thread-tail','running')")
    hub = EventHub(db)
    await hub.publish("r", "progress", {"method": "reasoning"})
    HistoryRetention(db).record_compaction("thread-tail", "2099-01-01T00:00:00Z")
    await hub.flush()
    assert db.fetchone("SELECT COUNT(*) AS n FROM run_events")["n"] == 0
    service.chats.archive_chat(project_id, "thread-tail")
    await hub.publish("r", "output", {"text": "archived"})
    await hub.flush()
    assert db.fetchone("SELECT COUNT(*) AS n FROM run_events")["n"] == 0


def test_incremental_reclamation_reduces_file_and_waits_for_readers(history):
    _, _, _, db, _, _, _ = history
    db.execute("CREATE TABLE bulk(value BLOB)")
    db.executemany("INSERT INTO bulk VALUES (?)", [(b"x" * 65536,)] * 40)
    size = db.path.stat().st_size
    db.execute("DELETE FROM bulk")
    with db.history_reader():
        assert not db.reclaim_unused_space(pages=10000)
    assert db.reclaim_unused_space(pages=10000)
    assert db.path.stat().st_size < size


def test_existing_database_vacuum_invalidates_event_offsets(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE padding(value BLOB)")
    conn.executemany("INSERT INTO padding VALUES (?)", [(b"x" * 65536,)] * 8)
    conn.commit()
    conn.close()
    db = Database(path)
    try:
        db.migrate()
        db.execute("INSERT INTO projects VALUES ('p','p','/p','now','now')")
        db.execute("INSERT INTO chats(id,project_id,title,created_at,updated_at) VALUES ('c','p','c','now','now')")
        db.execute("INSERT INTO history_checkpoint VALUES ('c',0,100,0)")
        db.execute("DELETE FROM padding")
        assert db.reclaim_unused_space()
        assert db.fetchone("PRAGMA auto_vacuum")["auto_vacuum"] == 2
        checkpoint = db.fetchone("SELECT * FROM history_checkpoint")
        assert checkpoint["event_offset"] == 0 and checkpoint["event_reset"] == -1
    finally:
        db.close()


def test_deleting_highest_event_resets_all_chat_highwater_marks(history):
    _, _, _, db, _, _, _ = history
    db.execute("INSERT INTO runs(id,chat_id,status) VALUES ('r','thread-tail','succeeded')")
    db.execute("INSERT INTO run_events VALUES ('r',1,'done','{}','2026-10-01T00:00:00Z')")
    db.execute("INSERT INTO history_checkpoint VALUES ('thread-tail',0,1,0)")
    db.execute("DELETE FROM run_events")
    row = db.fetchone("SELECT * FROM history_checkpoint")
    assert row["event_offset"] == 0 and row["event_reset"] == -1


@pytest.mark.asyncio
async def test_compaction_marker_and_import_cursor_commit_atomically(history, monkeypatch):
    service, _, _, db, project_id, path, meta = history
    write_lines(path, [meta, assistant(1), {"type": "compacted", "timestamp": "2026-10-01T00:00:03Z"}])

    def fail(*args):
        raise RuntimeError("interrupted compaction checkpoint")

    monkeypatch.setattr(service.history.retention, "record_compaction", fail)
    with pytest.raises(RuntimeError, match="interrupted compaction checkpoint"):
        await service.list_message_page(project_id, "thread-tail", 5, None, None)
    assert db.fetchone("SELECT COUNT(*) AS n FROM history_sources")["n"] == 0
    assert db.fetchone("SELECT COUNT(*) AS n FROM history_transcript_messages")["n"] == 0


def test_maintenance_skips_running_run_and_active_history_reader(history):
    _, _, _, db, _, _, _ = history
    db.execute("CREATE TABLE padding(value BLOB)")
    db.executemany("INSERT INTO padding VALUES (?)", [(b"x" * 65536,)] * 8)
    db.execute("DELETE FROM padding")
    db.execute("INSERT INTO runs(id,chat_id,status) VALUES ('r','thread-tail','running')")
    assert not db.reclaim_unused_space()
    with db.history_reader():
        HistoryRetention(db).maintain()
        assert db.fetchone("PRAGMA freelist_count")["freelist_count"] > 0


def test_partial_archive_import_is_also_purged(history):
    service, _, _, db, project_id, _, _ = history
    db.execute("INSERT INTO history_sources VALUES ('thread-tail','p','i',0,0,0,'h','{}')")
    service.chats.archive_chat(project_id, "thread-tail")
    assert HistoryRetention(db).purge_archived() == 1
    assert db.fetchone("SELECT COUNT(*) AS n FROM history_sources")["n"] == 0


@pytest.mark.asyncio
async def test_chat_deletion_cascades_to_all_history_tables(history):
    service, _, messages, db, project_id, path, meta = history
    write_lines(path, [meta, assistant(1), thought("2026-10-01T00:00:02Z")])
    messages.insert_message("thread-tail", "user", "instruction", kind="instruction")
    db.execute("INSERT INTO runs(id,chat_id,status) VALUES ('r','thread-tail','succeeded')")
    db.execute("INSERT INTO run_events VALUES ('r',1,'done','{}','2026-10-01T00:00:03Z')")
    await service.list_message_page(project_id, "thread-tail", 10, None, None)
    HistoryRetention(db).record_compaction("thread-tail", "2026-10-01T00:00:03Z")
    service.chats.delete_chat(project_id, "thread-tail")
    for table in (*HISTORY_TABLES, "messages", "runs", "run_events"):
        assert db.fetchone(f"SELECT COUNT(*) AS n FROM {table}")["n"] == 0
    assert path.exists()


def test_archive_cleanup_is_bounded_and_preserves_usage(history):
    service, _, messages, db, project_id, _, _ = history
    db.execute("INSERT INTO chats(id,project_id,title,created_at,updated_at,archived_at) VALUES ('other',?,'other','now','now','now')", (project_id,))
    messages.insert_message("thread-tail", "user", "instruction", kind="instruction")
    db.execute("INSERT INTO messages VALUES ('m','other','user','other',NULL,'now','instruction')")
    db.execute("""INSERT INTO token_usage_samples(chat_id,provider,thread_id,turn_id,recorded_at,
        input_tokens,cached_input_tokens,output_tokens,reasoning_output_tokens,total_tokens,thread_total_signature)
        VALUES ('thread-tail','openai','t','turn','now',10,0,2,0,12,'signature')""")
    service.chats.archive_chat(project_id, "thread-tail")
    retention = HistoryRetention(db)
    assert retention.purge_archived() == 1
    assert db.fetchone("SELECT COUNT(*) AS n FROM messages")["n"] == 1
    assert retention.purge_archived() == 1
    assert db.fetchone("SELECT COUNT(*) AS n FROM messages")["n"] == 0
    assert db.fetchone("SELECT total_tokens FROM token_usage_samples")["total_tokens"] == 12
