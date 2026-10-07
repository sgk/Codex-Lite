from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from codex_lite_daemon import transcript_import
from codex_lite_daemon.app_services import AppServerThreadService
from codex_lite_daemon.db import Database
from codex_lite_daemon.services import ChatService, MessageService, ProjectService
from codex_lite_daemon.transcript_import import TranscriptImportService, _reverse_lines
from test_api import RecordingAppServer, make_runtime_settings, make_test_config


@pytest.fixture
def history(tmp_path):
    cfg = make_test_config(tmp_path)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    service = AppServerThreadService(projects, chats, messages, transcripts, RecordingAppServer(), make_runtime_settings())
    project = projects.create_project(str(project_dir))
    path = cfg.codex_home / "sessions" / "history.jsonl"
    path.parent.mkdir(parents=True)
    chats.upsert_chat_index(project["id"], "thread-tail", "history", "thread-tail", "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z", str(path))
    meta = {"type": "session_meta", "payload": {"id": "thread-tail", "cwd": str(project_dir)}}
    yield service, transcripts, messages, db, project["id"], path, meta
    db.close()


def assistant(index, timestamp=None):
    return {"timestamp": timestamp or (datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(seconds=index)).isoformat(), "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": f"回答{index}"}]}}


def write_lines(path, records):
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in records), encoding="utf-8")


def test_reverse_lines_preserves_offsets_long_utf8_and_unterminated_lines(tmp_path):
    path = tmp_path / "lines.jsonl"
    lines = ["先頭", "あ" * 100000, "", "末尾"]
    data = "\n".join(lines).encode()
    path.write_bytes(data)
    expected = []
    offset = 0
    for line in lines:
        encoded = line.encode()
        if encoded:
            expected.append((offset, encoded))
        offset += len(encoded) + 1
    assert list(_reverse_lines(path, block_size=37)) == list(reversed(expected))
    path.write_bytes(data + b"\n")
    assert list(_reverse_lines(path, block_size=31)) == list(reversed(expected))


@pytest.mark.asyncio
async def test_initial_page_never_parses_old_bulk_or_calls_full_reader(history, monkeypatch):
    service, transcripts, _, _, project_id, path, meta = history
    old = {"type": "response_item", "payload": {"type": "function_call_output", "output": "x" * 4096}}
    write_lines(path, [meta, *[old for _ in range(2000)], *[assistant(i) for i in range(60)]])
    original = transcript_import.json.loads
    decoded = []

    def counted(value):
        decoded.append(len(value))
        return original(value)

    monkeypatch.setattr(transcript_import.json, "loads", counted)
    monkeypatch.setattr(transcripts, "list_messages", lambda *a, **k: pytest.fail("full transcript scan"))
    page = await service.list_message_page(project_id, "thread-tail", 10, None, None)
    assert [m["content"] for m in page["messages"]] == [f"回答{i}" for i in range(50, 60)]
    assert page["hasMoreBefore"] and not page["totalCountIsExact"]
    assert len(decoded) < 20


@pytest.mark.asyncio
async def test_pages_share_full_reader_ids_and_preserve_timestamp_ties(history):
    service, transcripts, _, _, project_id, path, meta = history
    timestamp = "2026-10-01T00:00:01.123456Z"
    write_lines(path, [meta, *[assistant(i, timestamp) for i in range(17)]])
    full = transcripts.list_messages(str(path.parent), "thread-tail", "thread-tail", str(path))
    found = []
    cursor = None
    for _ in range(10):
        page = await service.list_message_page(project_id, "thread-tail", 3, cursor[0] if cursor else None, cursor[1] if cursor else None)
        found.extend(page["messages"])
        if not page["hasMoreBefore"]:
            break
        first = page["messages"][0]
        cursor = first["createdAt"], first["id"]
    assert len(found) == len(full) == 17
    assert {m["id"] for m in found} == {m["id"] for m in full}


def test_older_page_seeks_to_cached_boundary_and_append_invalidates_cache(history, monkeypatch):
    _, transcripts, _, _, _, path, meta = history
    write_lines(path, [meta, *[assistant(i) for i in range(80)]])
    tail, _ = transcripts.read_message_tail(path, "thread-tail", 10)
    first = tail[0]
    cursor = datetime.fromisoformat(first["createdAt"]), first["id"]
    calls = []
    original = transcript_import._reverse_lines

    def recording(path, **kwargs):
        calls.append(kwargs.get("end_offset"))
        return original(path, **kwargs)

    monkeypatch.setattr(transcript_import, "_reverse_lines", recording)
    older, more = transcripts.read_message_tail(path, "thread-tail", 10, cursor)
    assert calls[-1] is not None and calls[-1] < path.stat().st_size
    assert [m["content"] for m in older] == [f"回答{i}" for i in range(60, 70)] and more
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n" + json.dumps(assistant(80), ensure_ascii=False))
    newest, _ = transcripts.read_message_tail(path, "thread-tail", 10)
    assert newest[-1]["content"] == "回答80"


@pytest.mark.asyncio
async def test_latest_activity_retained_without_historical_status_consuming_page(history):
    service, _, _, _, project_id, path, meta = history
    reasoning = {"timestamp": "2026-10-01T00:00:04Z", "type": "response_item", "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "current"}]}}
    write_lines(path, [meta, assistant(1), reasoning, assistant(3), reasoning])
    page = await service.list_message_page(project_id, "thread-tail", 2, None, None)
    assert [m["content"] for m in page["messages"] if m["role"] == "assistant"] == ["回答1", "回答3"]
    assert len([m for m in page["messages"] if m["role"] == "status"]) == 1


@pytest.mark.asyncio
async def test_page_file_work_runs_off_event_loop(history, monkeypatch):
    service, _, _, _, project_id, _, _ = history
    entered = threading.Event()
    release = threading.Event()

    def blocked(*args):
        entered.set()
        assert release.wait(2)
        return {"messages": []}

    monkeypatch.setattr(service, "_read_message_page", blocked)
    task = asyncio.create_task(service.list_message_page(project_id, "thread-tail", 10, None, None))
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set() and not task.done()
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_local_authority_repairs_selected_conclusion_and_loads_only_current_activity(history, monkeypatch):
    service, _, messages, db, project_id, path, meta = history
    write_lines(path, [meta, assistant(1), assistant(3)])
    db.execute("INSERT INTO runs(id, chat_id, status) VALUES ('run-tail', 'thread-tail', 'running')")
    messages.insert_message("thread-tail", "user", "指示", run_id="run-tail", kind="instruction", created_at="2026-10-01T00:00:00Z")
    messages.insert_message("thread-tail", "assistant", "回答", run_id="run-tail", kind="conclusion", created_at="2026-10-01T00:00:03Z")
    for sequence, timestamp in [(1, "2026-10-01T00:00:02Z"), (2, "2026-10-01T00:00:04Z")]:
        data = {"method": "item/reasoning/textDelta", "summary": "reasoning", "details": json.dumps({"itemId": f"reasoning-{sequence}", "delta": "途中経過"})}
        db.execute("INSERT INTO run_events(run_id, sequence, event, data_json, created_at) VALUES ('run-tail', ?, 'progress', ?, ?)", (sequence, json.dumps(data), timestamp))
    # Old malformed details would fail if the hidden row were decoded at all.
    db.execute("UPDATE run_events SET data_json = 'not-json' WHERE sequence = 1")
    original = json.loads

    def reject_old_details(value, *args, **kwargs):
        assert value != "not-json"
        return original(value, *args, **kwargs)

    monkeypatch.setattr(json, "loads", reject_old_details)
    page = await service.list_message_page(project_id, "thread-tail", 2, None, None)
    assert [m["content"] for m in page["messages"] if m["role"] != "status"] == ["指示", "回答3"]
    assert len([m for m in page["messages"] if m["role"] == "status"]) == 1
    assert page["totalCountIsExact"] and page["totalCount"] == 2
    first = page["messages"][0]
    older = await service.list_message_page(project_id, "thread-tail", 2, first["createdAt"], first["id"])
    assert older["messages"] == [] and not older["hasMoreBefore"]
