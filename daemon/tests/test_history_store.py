from __future__ import annotations

import asyncio
import json

import pytest

from codex_lite_daemon import history_store
from codex_lite_daemon.app_services import AppServerThreadService
from codex_lite_daemon.db import Database
from codex_lite_daemon.errors import AppError
from codex_lite_daemon.services import ChatService, MessageService, ProjectService
from codex_lite_daemon.transcript_import import TranscriptImportService
from test_history_tail import assistant, history, write_lines


def reopened(service):
    cfg = service.transcripts.config
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    return AppServerThreadService(projects, chats, MessageService(db, chats), TranscriptImportService(cfg, projects, chats), service.app_server, service.settings)


def append(path, records):
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write("\n" + json.dumps(record, ensure_ascii=False))


@pytest.mark.asyncio
async def test_restart_and_append_only_parse_new_records(history, monkeypatch):
    service, _, _, _, project_id, path, meta = history
    write_lines(path, [meta, *[assistant(i) for i in range(100)]])
    await service.list_message_page(project_id, "thread-tail", 5, None, None)
    second = reopened(service)
    parsed = []
    original = history_store._transcript_message

    def record(*args):
        parsed.append(args[1])
        return original(*args)

    monkeypatch.setattr(history_store, "_transcript_message", record)
    try:
        await second.list_message_page(project_id, "thread-tail", 5, None, None)
        assert not parsed
        append(path, [assistant(100)])
        page = await second.list_message_page(project_id, "thread-tail", 5, None, None)
        assert len(parsed) == 1
        assert page["messages"][-1]["content"] == "回答100"
        assert page["totalCount"] == 101
    finally:
        second.messages.db.close()


@pytest.mark.asyncio
async def test_partial_final_line_and_pending_tool_survive_restart(history):
    service, _, _, db, project_id, path, meta = history
    call = {"timestamp": "2026-10-01T00:00:01Z", "type": "response_item", "payload": {
        "type": "function_call", "call_id": "call-1", "name": "exec_command", "arguments": json.dumps({"cmd": "pwd"})}}
    output = {"timestamp": "2026-10-01T00:00:02Z", "type": "response_item", "payload": {
        "type": "function_call_output", "call_id": "call-1", "output": "project-dir"}}
    write_lines(path, [meta, call])
    fragment = json.dumps(output).encode()
    with path.open("ab") as handle:
        handle.write(b"\n" + fragment[:31])
    await service.list_message_page(project_id, "thread-tail", 5, None, None)
    checkpoint = db.fetchone("SELECT * FROM history_sources")
    assert checkpoint["byte_offset"] < path.stat().st_size
    second = reopened(service)
    try:
        with path.open("ab") as handle:
            handle.write(fragment[31:])
        page = await second.list_message_page(project_id, "thread-tail", 5, None, None)
        tool = next(message for message in page["messages"] if message["role"] == "status")
        assert "pwd" in tool["content"] and "project-dir" in tool["activityDetails"]
        assert second.messages.db.fetchone("SELECT * FROM history_sources")["byte_offset"] == path.stat().st_size
    finally:
        second.messages.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["replace", "truncate", "rewrite", "grow_rewrite"])
async def test_source_replacement_reindexes_without_stale_rows(history, change):
    service, _, _, _, project_id, path, meta = history
    write_lines(path, [meta, assistant(1), assistant(2)])
    await service.list_message_page(project_id, "thread-tail", 5, None, None)
    if change == "replace":
        other = path.with_suffix(".new")
        write_lines(other, [meta, assistant(3)])
        other.replace(path)
    else:
        records = [assistant(3)] if change == "truncate" else [assistant(3), assistant(4)]
        if change == "grow_rewrite":
            records.extend([assistant(i) for i in range(5, 10)])
        write_lines(path, [meta, *records])
    page = await service.list_message_page(project_id, "thread-tail", 20, None, None)
    contents = [message["content"] for message in page["messages"]]
    assert "回答1" not in contents and "回答2" not in contents and "回答3" in contents


@pytest.mark.asyncio
async def test_local_insert_update_delete_invalidate_projection(history):
    service, _, messages, db, project_id, path, meta = history
    write_lines(path, [meta])
    await service.list_message_page(project_id, "thread-tail", 5, None, None)
    item = messages.insert_message("thread-tail", "user", "new instruction", kind="instruction")
    assert (await service.list_message_page(project_id, "thread-tail", 5, None, None))["totalCount"] == 1
    db.execute("UPDATE messages SET content = 'updated' WHERE id = ?", (item["id"],))
    assert (await service.list_messages(project_id, "thread-tail"))[0]["content"] == "updated"
    db.execute("DELETE FROM messages WHERE id = ?", (item["id"],))
    assert not (await service.list_message_page(project_id, "thread-tail", 5, None, None))["messages"]


@pytest.mark.asyncio
async def test_persisted_activity_state_only_decodes_appended_events(history, monkeypatch):
    service, _, _, db, project_id, path, meta = history
    write_lines(path, [meta])
    db.execute("INSERT INTO runs(id, chat_id, status) VALUES ('r', 'thread-tail', 'running')")

    def event(sequence, text):
        data = json.dumps({"method": "item/reasoning/textDelta", "summary": "reasoning", "details": json.dumps({"itemId": "thought", "delta": text})})
        db.execute("INSERT INTO run_events VALUES ('r', ?, 'progress', ?, ?)", (sequence, data, f"2026-10-01T00:00:0{sequence}Z"))
        return data

    old = event(1, "first ")
    await service.list_message_page(project_id, "thread-tail", 5, None, None)
    second = reopened(service)
    original = json.loads

    def reject_old(value, *args, **kwargs):
        assert value != old, "already-imported event was decoded again"
        return original(value, *args, **kwargs)

    monkeypatch.setattr(json, "loads", reject_old)
    try:
        event(2, "second")
        page = await second.list_message_page(project_id, "thread-tail", 5, None, None)
        assert page["messages"][0]["activityDetails"] == "first second"
        assert page["messages"][0]["id"] == "run-activity-r-1"
    finally:
        second.messages.db.close()


@pytest.mark.asyncio
async def test_changed_event_rebuilds_aggregated_state(history):
    service, _, _, db, project_id, path, meta = history
    write_lines(path, [meta])
    db.execute("INSERT INTO runs(id, chat_id, status) VALUES ('r', 'thread-tail', 'running')")
    data = {"method": "item/reasoning/textDelta", "summary": "reasoning", "details": json.dumps({"itemId": "thought", "delta": "old"})}
    db.execute("INSERT INTO run_events VALUES ('r', 1, 'progress', ?, '2026-10-01T00:00:01Z')", (json.dumps(data),))
    await service.list_message_page(project_id, "thread-tail", 5, None, None)
    data["details"] = json.dumps({"itemId": "thought", "delta": "new"})
    db.execute("UPDATE run_events SET data_json = ? WHERE run_id = 'r'", (json.dumps(data),))
    assert (await service.list_message_page(project_id, "thread-tail", 5, None, None))["messages"][0]["activityDetails"] == "new"
    db.execute("DELETE FROM run_events WHERE run_id = 'r'")
    assert not (await service.list_message_page(project_id, "thread-tail", 5, None, None))["messages"]


@pytest.mark.asyncio
async def test_concurrent_pages_import_once_and_keep_timestamp_tie_order(history, monkeypatch):
    service, _, _, _, project_id, path, meta = history
    write_lines(path, [meta, *[assistant(i, "2026-10-01T00:00:00Z") for i in range(30)]])
    parsed = []
    original = history_store._transcript_message

    def record(*args):
        parsed.append(args[1])
        return original(*args)

    monkeypatch.setattr(history_store, "_transcript_message", record)
    pages = await asyncio.gather(*[service.list_message_page(project_id, "thread-tail", 10, None, None) for _ in range(6)])
    assert len(parsed) == 31
    assert all(page == pages[0] for page in pages)
    page = pages[0]
    assert [row["content"] for row in page["messages"]] == [f"回答{i}" for i in range(20, 30)]
    first = page["messages"][0]
    older = await service.list_message_page(project_id, "thread-tail", 10, first["createdAt"], first["id"])
    assert [row["content"] for row in older["messages"]] == [f"回答{i}" for i in range(10, 20)]
    assert older["messages"][-1]["historySequence"] < first["historySequence"]
    with pytest.raises(AppError, match="履歴が変更"):
        await service.list_message_page(project_id, "thread-tail", 10, first["createdAt"], "deleted-message")


@pytest.mark.asyncio
async def test_provider_mapping_reuses_saved_chat_path(history, monkeypatch):
    service, transcripts, _, db, project_id, path, meta = history
    write_lines(path, [meta, assistant(1)])
    service.chats.upsert_provider_thread(project_id, "thread-tail", "openai", "thread-tail", None, history_initialized=True)
    checkpoint = db.fetchone("SELECT updated_at FROM chat_provider_threads")["updated_at"]
    monkeypatch.setattr(transcripts, "_find_transcript_path", lambda *a, **kw: pytest.fail("unnecessary thread scan"))
    await service.list_message_page(project_id, "thread-tail", 5, None, None)
    assert db.fetchone("SELECT transcript_path FROM chat_provider_threads")["transcript_path"] == str(path)
    # Finding a filename must not advance the provider's conversation checkpoint.
    assert db.fetchone("SELECT updated_at FROM chat_provider_threads")["updated_at"] == checkpoint


@pytest.mark.asyncio
async def test_cascading_deletion_removes_projection_without_invalid_revision(history):
    service, _, messages, db, project_id, path, meta = history
    write_lines(path, [meta, assistant(1)])
    messages.insert_message("thread-tail", "user", "local", kind="instruction")
    await service.list_message_page(project_id, "thread-tail", 5, None, None)
    db.execute("DELETE FROM chats WHERE id = 'thread-tail'")
    for table in ("history_messages", "history_sources", "history_transcript_messages", "history_checkpoint", "history_revision"):
        assert db.fetchone(f"SELECT COUNT(*) AS n FROM {table}")["n"] == 0


@pytest.mark.asyncio
async def test_older_pages_use_indexed_sequence_range(history):
    service, _, _, db, project_id, path, meta = history
    write_lines(path, [meta, *[assistant(i) for i in range(100)]])
    await service.list_message_page(project_id, "thread-tail", 10, None, None)
    plan = db.fetchall("""EXPLAIN QUERY PLAN SELECT position, payload FROM history_messages
        WHERE chat_id = ? AND prose = 1 AND position < ? ORDER BY position DESC LIMIT ?""", ("thread-tail", 50, 11))
    assert any("idx_history_page" in row["detail"] and "position<?" in row["detail"] for row in plan)


@pytest.mark.asyncio
async def test_projection_refresh_does_not_lose_concurrent_local_insert(history, monkeypatch):
    service, _, messages, _, project_id, path, meta = history
    write_lines(path, [meta, assistant(1)])
    original = service.history.replace_projection
    inserted = False

    def insert_during_commit(*args):
        nonlocal inserted
        if not inserted:
            inserted = True
            messages.insert_message("thread-tail", "user", "arrived while indexing", kind="instruction")
        return original(*args)

    monkeypatch.setattr(service.history, "replace_projection", insert_during_commit)
    await service.list_message_page(project_id, "thread-tail", 5, None, None)
    page = await service.list_message_page(project_id, "thread-tail", 5, None, None)
    assert any(message["content"] == "arrived while indexing" for message in page["messages"])


@pytest.mark.asyncio
async def test_run_deletion_invalidates_aggregated_activity(history):
    service, _, _, db, project_id, path, meta = history
    write_lines(path, [meta])
    db.execute("INSERT INTO runs(id, chat_id, status) VALUES ('r', 'thread-tail', 'running')")
    data = {"method": "item/reasoning/textDelta", "summary": "reasoning", "details": json.dumps({"itemId": "thought", "delta": "old"})}
    db.execute("INSERT INTO run_events VALUES ('r', 1, 'progress', ?, '2026-10-01T00:00:01Z')", (json.dumps(data),))
    assert (await service.list_message_page(project_id, "thread-tail", 5, None, None))["messages"]
    db.execute("DELETE FROM runs WHERE id = 'r'")
    assert not (await service.list_message_page(project_id, "thread-tail", 5, None, None))["messages"]
