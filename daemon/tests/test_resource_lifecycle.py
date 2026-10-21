from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from codex_lite_daemon.app_server import AppServerClient
from codex_lite_daemon.config import default_config
from codex_lite_daemon.db import Database
from codex_lite_daemon.errors import AppError
from codex_lite_daemon.run_service import EventHub
from codex_lite_daemon.runner.codex_runner import CodexRunner


@pytest.fixture
def resource_db(tmp_path: Path):
    db = Database(tmp_path / "resources.sqlite")
    db.migrate()
    db.execute("INSERT INTO projects(id,name,path,created_at,updated_at) VALUES('p','p',?,'now','now')", (str(tmp_path),))
    db.execute("INSERT INTO chats(id,project_id,title,created_at,updated_at) VALUES('c','p','c','now','now')")
    yield db
    db.close()


@pytest.mark.asyncio
async def test_event_memory_bounded_across_completed_runs(resource_db):
    hub = EventHub(resource_db)
    hub.MAX_MEMORY_BYTES = 16_000
    hub.MAX_MEMORY_RUNS = 3
    hub.MAX_SEQUENCE_CACHE = 4
    for index in range(40):
        run_id = str(index)
        resource_db.execute("INSERT INTO runs(id,chat_id,status) VALUES(?,'c','running')", (run_id,))
        await hub.publish(run_id, "progress", {"text": "x" * 10_000})
        await hub.publish(run_id, "done", {"status": "succeeded"})
        assert hub.diagnostics()["cachedBytes"] <= hub.MAX_MEMORY_BYTES
        assert hub.diagnostics()["cachedRuns"] <= 3
        assert hub.diagnostics()["sequenceCacheSize"] <= 4
    assert len(hub.replay_history("0")) == 2
    assert hub.current_sequence("0") == 2


@pytest.mark.asyncio
async def test_slow_subscriber_has_one_wakeup_and_complete_paged_replay(resource_db):
    resource_db.execute("INSERT INTO runs(id,chat_id,status) VALUES('r','c','running')")
    hub = EventHub(resource_db)
    hub.REPLAY_PAGE_SIZE = 2
    hub.MAX_MEMORY_BYTES = 300
    hub.MAX_PENDING_BYTES = 500
    await hub.publish("r", "status", {})
    stream = hub.subscribe("r")
    first = await anext(stream)
    assert first["sequence"] == 1
    for _ in range(40):
        await hub.publish("r", "progress", {"text": "x" * 100})
    await hub.publish("r", "done", {})
    assert hub._subscribers["r"][0].qsize() == 1
    assert [item["sequence"] async for item in stream] == list(range(2, 43))
    assert hub.diagnostics()["subscriberCount"] == 0
    assert not hub._subscribers


def client_for_test(tmp_path):
    config = replace(default_config(), codex_home=tmp_path / "home", codex_sqlite_home=tmp_path / "home/sqlite")
    return AppServerClient(config, CodexRunner(config))


@pytest.mark.asyncio
@pytest.mark.parametrize("status,terminals,expected", [
    ("idle", [], True), ("notLoaded", [], True),
    ("active", [], False), ("systemError", [], False),
    ("idle", [{"processId": "42"}], False),
])
async def test_release_protects_active_approval_background_and_unknown(tmp_path, monkeypatch, status, terminals, expected):
    client = client_for_test(tmp_path)
    monkeypatch.setattr(type(client), "is_running", property(lambda self: True))
    calls = []

    async def request(method, params=None, timeout=None):
        calls.append(method)
        if method == "thread/read":
            return {"thread": {"status": {"type": status, "activeFlags": ["waitingOnApproval"]}}}
        if method == "thread/backgroundTerminals/list":
            return {"data": terminals, "nextCursor": None}
        assert method == "thread/unsubscribe"
        return {"status": "unsubscribed"}

    monkeypatch.setattr(client, "request_if_running", request)
    assert await client.release_thread_if_unused("thread") is expected
    assert ("thread/unsubscribe" in calls) is expected
    if expected:
        assert await client._thread_is_idle("thread")  # No claim that unsubscribe killed it.
    queue = client.subscribe_queue("thread")
    calls.clear()
    assert await client.release_thread_if_unused("thread") is False
    assert not calls
    client.unsubscribe_queue(queue)


@pytest.mark.asyncio
async def test_timeout_drops_pending_future(tmp_path, monkeypatch):
    client = client_for_test(tmp_path)
    class Process:
        stdin = object()
    client._process = Process()
    async def write(payload):
        pass
    monkeypatch.setattr(client, "_write", write)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(client._request_started("test"), .01)
    assert not client._pending


@pytest.mark.asyncio
async def test_metadata_polling_does_not_extend_idle_deadline(tmp_path, monkeypatch):
    client = client_for_test(tmp_path)
    monkeypatch.setattr(type(client), "is_running", property(lambda self: True))
    client.IDLE_SHUTDOWN_SECONDS = .03
    closed = asyncio.Event()
    async def close():
        closed.set()
    async def ensure():
        pass
    async def request(method, params):
        return {"data": [], "nextCursor": None}
    monkeypatch.setattr(client, "close", close)
    monkeypatch.setattr(client, "ensure_started", ensure)
    monkeypatch.setattr(client, "_request_started", request)
    await client.release_run()
    timer = client._idle_shutdown_task
    for _ in range(5):
        await client.request("model/list")
        assert client._idle_shutdown_task is timer
        await asyncio.sleep(.005)
    await asyncio.wait_for(closed.wait(), .2)


@pytest.mark.asyncio
async def test_idle_shutdown_fails_closed_and_protects_background_threads(tmp_path, monkeypatch):
    client = client_for_test(tmp_path)
    monkeypatch.setattr(type(client), "is_running", property(lambda self: True))
    async def request(method, params=None, timeout=None):
        if method == "thread/loaded/list":
            return {"data": ["background"]}
        if method == "thread/read":
            return {"thread": {"status": {"type": "idle"}}}
        return {"data": [{"processId": "1"}]}
    monkeypatch.setattr(client, "request_if_running", request)
    assert await client._can_close_idle() is False
    client.acquire_run()
    assert await client._can_close_idle() is False
    client._active_run_count = 0
    async def error(*args, **kwargs):
        raise AppError("unavailable", "explicit error", 503)
    monkeypatch.setattr(client, "request_if_running", error)
    assert await client.release_thread_if_unused("thread") is False
    assert "explicit error" in client.lifecycle_error


@pytest.mark.asyncio
async def test_next_turn_rejoins_unsubscribed_thread_without_full_history(tmp_path, monkeypatch):
    client = client_for_test(tmp_path)
    client._unsubscribed_threads.add("thread")
    calls = []
    async def request(method, params):
        calls.append((method, params))
        return {"turn": {"id": "new"}}
    monkeypatch.setattr(client, "_request_started", request)
    await client._request_subscribed("turn/start", {"threadId": "thread", "input": []})
    assert calls == [
        ("thread/resume", {"threadId": "thread", "excludeTurns": True}),
        ("turn/start", {"threadId": "thread", "input": []}),
    ]
    assert not client._unsubscribed_threads


@pytest.mark.asyncio
async def test_release_timeout_is_bounded_and_retains_failure_reason(tmp_path, monkeypatch):
    client = client_for_test(tmp_path)
    monkeypatch.setattr(type(client), "is_running", property(lambda self: True))
    client.THREAD_RELEASE_TIMEOUT_SECONDS = .01
    async def request(*args, **kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr(client, "request_if_running", request)
    assert await client.release_thread_if_unused("thread") is False
    assert "TimeoutError" in client.lifecycle_error


@pytest.mark.asyncio
async def test_unsubscribe_reply_timeout_still_requires_rejoin(tmp_path, monkeypatch):
    client = client_for_test(tmp_path)
    monkeypatch.setattr(type(client), "is_running", property(lambda self: True))
    client.THREAD_RELEASE_TIMEOUT_SECONDS = .01
    async def idle(thread_id):
        return True
    async def request(*args, **kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr(client, "_thread_is_idle", idle)
    monkeypatch.setattr(client, "request_if_running", request)
    assert await client.release_thread_if_unused("thread") is False
    assert "thread" in client._unsubscribed_threads
