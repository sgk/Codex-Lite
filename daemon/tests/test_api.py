from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from codex_lite_daemon.app_server import AppServerClient, AppServerClientPool, AppServerNotification
from codex_lite_daemon.app_services import AppServerRunService, AppServerRuntimeSettings, AppServerThreadService, AppServerUsageService, _content_with_attachment_summary, _is_reasoning_delta_notification, _merge_messages, _notification_details, _notification_summary, _reasoning_delta, _reasoning_item_id
from codex_lite_daemon.automation_service import AutomationService, _run_due_automations
from codex_lite_daemon.codex_state import CodexStateService
from codex_lite_daemon.config import Config, default_config
from codex_lite_daemon.db import Database
from codex_lite_daemon.errors import AppError
from codex_lite_daemon.main import _attachment_path_to_wsl, _model_list_out, create_app, message_page
import codex_lite_daemon.process_env as process_env
import codex_lite_daemon.deepseek as deepseek
from codex_lite_daemon.process_env import codex_process_env
from codex_lite_daemon.run_service import EventHub
from codex_lite_daemon.runner.codex_runner import CodexRunner
from codex_lite_daemon.services import ChatService, MessageService, ProjectService
from codex_lite_daemon.transcript_import import TranscriptImportService
from codex_lite_daemon.util.ids import new_id
from codex_lite_daemon.util.time import utc_now


@pytest.fixture
def linux_tmp_path() -> Path:
    path = Path(tempfile.mkdtemp(prefix="codex-lite-test-", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def make_test_config(tmp_path: Path) -> Config:
    base = default_config()
    return Config(
        host="127.0.0.1",
        port=8765,
        wsl_distro_name="test-distro",
        app_data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "codex-lite.db",
        run_log_dir=tmp_path / "data" / "runs",
        codex_home=tmp_path / "codex-home",
        codex_sqlite_home=tmp_path / "codex-home" / "sqlite",
        codex_path=base.codex_path,
        max_concurrent_runs=1,
        allow_mnt_c_projects=False,
        runner_mode="fake",
        permission_profile=base.permission_profile,
        approval_policy=base.approval_policy,
        model=base.model,
        auto_compact_token_limit=base.auto_compact_token_limit,
        auto_compact_token_limit_scope=base.auto_compact_token_limit_scope,
    )


def make_runtime_settings(permission_profile: str = ":danger-full-access", approval_policy: str = "never", model: str = "", reasoning_effort: str = "", approvals_reviewer: str = "user") -> AppServerRuntimeSettings:
    return AppServerRuntimeSettings(permission_profile=permission_profile, approval_policy=approval_policy, model=model, reasoning_effort=reasoning_effort, approvals_reviewer=approvals_reviewer)


def test_codex_runner_prefers_desktop_bundle_then_vscode_before_path(linux_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    windows_home = linux_tmp_path / "mnt" / "c" / "Users" / "tester"
    codex_home_codex = windows_home / ".codex" / "bin" / "wsl" / "abc123" / "codex"
    desktop_codex = windows_home / "AppData" / "Local" / "Programs" / "Codex" / "resources" / "app" / "bin" / "linux-x86_64" / "codex"
    vscode_codex = linux_tmp_path / "home" / "tester" / ".vscode-server" / "extensions" / "openai.chatgpt-test" / "bin" / "linux-x86_64" / "codex"
    path_codex = linux_tmp_path / "bin" / "codex"
    explicit_codex = linux_tmp_path / "explicit" / "codex"
    for path in (codex_home_codex, desktop_codex, vscode_codex, path_codex, explicit_codex):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    cfg = make_test_config(linux_tmp_path)
    cfg = Config(
        **{
            **cfg.__dict__,
            "codex_home": windows_home / ".codex",
            "codex_path": str(explicit_codex),
        }
    )
    runner = CodexRunner(cfg)
    monkeypatch.setattr("codex_lite_daemon.runner.codex_runner.shutil.which", lambda name: str(path_codex) if name == "codex" else None)
    monkeypatch.setattr("codex_lite_daemon.runner.codex_runner.Path.home", lambda: linux_tmp_path / "home" / "tester")
    monkeypatch.setattr("codex_lite_daemon.runner.codex_runner._windows_home_from_codex_home", lambda _: windows_home)

    candidates = runner._candidate_paths()

    assert candidates[:5] == [str(explicit_codex), str(codex_home_codex), str(desktop_codex), str(vscode_codex), str(path_codex)]


def test_app_server_command_enables_auto_compaction(linux_tmp_path: Path) -> None:
    cfg = make_test_config(linux_tmp_path)
    client = AppServerClient(cfg, CodexRunner(cfg))

    args = client._command_args("/opt/codex")

    assert args == [
        "/opt/codex",
        "-c",
        "model_auto_compact_token_limit=100000",
        "-c",
        'model_auto_compact_token_limit_scope="total"',
        "app-server",
        "--listen",
        "stdio://",
    ]


def test_app_server_command_can_disable_auto_compaction(linux_tmp_path: Path) -> None:
    cfg = make_test_config(linux_tmp_path)
    cfg = Config(**{**cfg.__dict__, "auto_compact_token_limit": 0})
    client = AppServerClient(cfg, CodexRunner(cfg))

    args = client._command_args("/opt/codex")

    assert args == ["/opt/codex", "app-server", "--listen", "stdio://"]


@pytest.mark.asyncio
async def test_codex_runner_resolve_uses_candidate_order(linux_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_test_config(linux_tmp_path)
    runner = CodexRunner(cfg)
    candidates = ["/desktop/codex", "/path/codex", "/vscode/codex"]
    monkeypatch.setattr(runner, "_candidate_paths", lambda: candidates)

    async def fake_version(candidate: str) -> str | None:
        return f"version for {candidate}"

    monkeypatch.setattr(runner, "_try_version", fake_version)

    assert await runner.resolve() == "/desktop/codex"
    assert runner.codex_version == "version for /desktop/codex"


def test_codex_process_env_uses_safe_login_shell_values(linux_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_test_config(linux_tmp_path)

    class Result:
        returncode = 0
        stdout = "__CODEX_LITE_ENV__" + json.dumps(
            {
                "PATH": "/opt/codex-lite-test/bin:/mnt/c/Windows/System32:/usr/bin",
                "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
                "GPG_TTY": "/dev/pts/1",
                "OPENAI_API_KEY": "secret",
                "SOME_TOKEN": "secret",
                "CODEX_HOME": "/wrong",
            }
        )

    calls: list[list[str]] = []

    def fake_run(args, *rest, **kwargs):
        calls.append(args)
        return Result()

    monkeypatch.setattr(process_env, "_LOGIN_ENV", None)
    monkeypatch.setattr(process_env.subprocess, "run", fake_run)

    env = codex_process_env(cfg)

    assert env["SSH_AUTH_SOCK"] == "/tmp/ssh-agent.sock"
    assert env["GPG_TTY"] == "/dev/pts/1"
    assert env["PATH"] == "/opt/codex-lite-test/bin:/usr/bin"
    assert env["CODEX_HOME"] == str(cfg.codex_home)
    assert env["CODEX_SQLITE_HOME"] == str(cfg.codex_sqlite_home)
    assert "OPENAI_API_KEY" not in env
    assert "SOME_TOKEN" not in env
    assert calls[0][1] == "-lic"


def test_codex_process_env_falls_back_when_login_shell_fails(linux_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_test_config(linux_tmp_path)

    class Result:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(process_env, "_LOGIN_ENV", None)
    monkeypatch.setattr(process_env.subprocess, "run", lambda *args, **kwargs: Result())

    env = codex_process_env(cfg)

    assert env["PATH"] == process_env.DEFAULT_PATH
    assert env["CODEX_HOME"] == str(cfg.codex_home)


def test_codex_process_env_loads_deepseek_key_from_user_file(linux_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = linux_tmp_path / "home"
    secret_path = home / ".config" / "codex-lite" / "deepseek.env"
    secret_path.parent.mkdir(parents=True)
    secret_path.write_text("DEEPSEEK_API_KEY=sk-test-only\n", encoding="utf-8")
    monkeypatch.setattr(deepseek.Path, "home", lambda: home)
    monkeypatch.setattr(process_env, "_LOGIN_ENV", {"PATH": process_env.DEFAULT_PATH})

    env = codex_process_env(make_test_config(linux_tmp_path))

    assert env["DEEPSEEK_API_KEY"] == "sk-test-only"


def test_deepseek_app_server_command_uses_responses_provider(linux_tmp_path: Path) -> None:
    cfg = make_test_config(linux_tmp_path)
    client = AppServerClient(cfg, CodexRunner(cfg))

    args = client._command_args("/opt/codex", "deepseek")

    assert 'model_provider="deepseek"' in args
    assert 'model="deepseek-v4-flash"' in args
    assert 'model_providers.deepseek.wire_api="responses"' in args
    assert (cfg.app_data_dir / "deepseek-models.json").exists()


def test_app_server_pool_keeps_provider_processes_independent(linux_tmp_path: Path) -> None:
    cfg = make_test_config(linux_tmp_path)
    pool = AppServerClientPool(cfg, CodexRunner(cfg))

    openai = pool.client_for_provider("openai")
    deepseek = pool.client_for_provider("deepseek")

    assert openai is not deepseek
    assert openai.provider == "openai"
    assert deepseek.provider == "deepseek"
    assert 'model_provider="deepseek"' in deepseek._command_args("/opt/codex")
    assert 'model_provider="deepseek"' not in openai._command_args("/opt/codex")


@pytest.mark.asyncio
async def test_app_server_client_closes_after_idle_run_window(linux_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_test_config(linux_tmp_path)
    client = AppServerClient(cfg, CodexRunner(cfg), "deepseek")
    closed = False

    async def fake_close() -> None:
        nonlocal closed
        closed = True
        client._idle_shutdown_task = None

    monkeypatch.setattr(type(client), "is_running", property(lambda self: True))
    monkeypatch.setattr(client, "close", fake_close)
    client.IDLE_SHUTDOWN_SECONDS = 0.01

    await client.release_run()
    await asyncio.sleep(0.03)

    assert closed is True


def test_model_list_adds_configured_deepseek_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deepseek, "deepseek_api_key_configured", lambda: True)

    result = _model_list_out(["gpt-5.6-sol"], {}, "", dynamic=True)

    assert "gpt-5.6-sol" in result["availableModels"]
    assert "deepseek-v4-flash" in result["availableModels"]
    assert result["reasoningEffortsByModel"]["deepseek-v4-flash"] == ["low", "high", "max"]


def test_app_server_progress_notifications_have_readable_summaries() -> None:
    assert _notification_summary(AppServerNotification("exec_command_begin", {"command": "dotnet build windows/CodexLite.sln"})) == "コマンドを開始しました: dotnet build windows/CodexLite.sln"
    assert _notification_summary(AppServerNotification("exec_command_output_delta", {"stream": "stdout", "delta": "x"})) == "コマンド出力: stdout"
    assert _notification_summary(AppServerNotification("exec_command_end", {"exitCode": 0})) == "コマンドが終了しました: exit 0"
    assert _notification_summary(AppServerNotification("mcp_tool_call_begin", {"name": "filesystem.read"})) == "ファイルを読み取っています"
    assert _notification_summary(AppServerNotification("item/tool_call_begin", {"item": {"type": "read_file", "path": "README.md"}})) == "ファイルを読み取っています: README.md"
    assert _notification_summary(AppServerNotification("item/tool_call_end", {"item": {"type": "write_file", "path": "README.md"}})) == "ファイルを編集しました: README.md"
    assert _notification_summary(AppServerNotification("apply_patch_begin", {"path": "windows/CodexLite/MainWindow.xaml"})) == "ファイルを編集しています: windows/CodexLite/MainWindow.xaml"
    assert _notification_summary(AppServerNotification("exec_command_begin", {"arguments": {"cmd": "git status --short"}})) == "コマンドを開始しました: git status --short"
    assert _notification_summary(AppServerNotification("exec_command_begin", {"cmd": "git", "args": ["status", "--short"]})) == "コマンドを開始しました: git status --short"
    assert _notification_summary(AppServerNotification("exec_command_begin", {"command": {"cmd": "bash", "args": ["-lc", "git status --short"]}})) == "コマンドを開始しました: bash -lc 'git status --short'"
    assert _notification_summary(AppServerNotification("exec_command_begin", {"arguments": "{\"cmd\":\"dotnet\",\"args\":[\"build\",\"windows/CodexLite.sln\"]}"})) == "コマンドを開始しました: dotnet build windows/CodexLite.sln"
    assert _notification_summary(AppServerNotification("mcp_tool_call_begin", {"name": "filesystem.read", "arguments": {"path": "AGENTS.md"}})) == "ファイルを読み取っています: AGENTS.md"
    assert _notification_summary(AppServerNotification("mcp_tool_call_end", {"name": "filesystem.write", "arguments": "{\"path\":\"notes.md\"}"})) == "ファイルを編集しました: notes.md"
    assert _notification_summary(AppServerNotification("exec_command_begin", {"command": "curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz' https://example.test"})) == "コマンドを開始しました: curl -H 'Authorization=<redacted>' https://example.test"


def test_reasoning_delta_notifications_are_identified_and_read_without_fallback() -> None:
    notification = AppServerNotification("item/reasoning/textDelta", {"itemId": "reasoning-1", "delta": "考"})

    assert _is_reasoning_delta_notification(notification)
    assert _reasoning_delta(notification) == "考"
    assert _reasoning_item_id(notification) == "reasoning-1"
    assert not _is_reasoning_delta_notification(AppServerNotification("item/reasoning/started", {"itemId": "reasoning-1"}))
    assert _reasoning_delta(AppServerNotification("item/reasoning/textDelta", {"delta": 42})) == ""


def test_app_server_progress_notification_details_include_output_and_redact_secrets() -> None:
    assert _notification_details(AppServerNotification("exec_command_output_delta", {"stream": "stdout", "delta": "line one\nline two\n"})) == "line one\nline two\n"
    details = _notification_details(
        AppServerNotification(
            "mcp_tool_call_begin",
            {"name": "example.tool", "arguments": {"path": "README.md", "api_key": "do-not-show"}},
        )
    )
    assert '"path": "README.md"' in details
    assert '"api_key": "<redacted>"' in details
    assert "do-not-show" not in details


def test_clipboard_attachment_summary_hides_path() -> None:
    summary = _content_with_attachment_summary(
        "確認して",
        [
            {"name": "clipboard-20260713-174210-abcd.png", "path": "/mnt/c/Users/sgk/AppData/Local/CodexLite/attachments/clipboard-20260713-174210-abcd.png"},
            {"name": "note.txt", "path": "/home/sgk/project/note.txt"},
        ],
    )

    assert summary == "確認して\n\nAttachments:\n- clipboard-20260713-174210-abcd.png\n- note.txt: /home/sgk/project/note.txt"


def test_attachment_path_to_wsl_converts_windows_and_wsl_unc_paths() -> None:
    assert _attachment_path_to_wsl(r"C:\Users\sgk\note.txt") == "/mnt/c/Users/sgk/note.txt"
    assert _attachment_path_to_wsl("//wsl.localhost/Ubuntu-24.04/home/sgk/note.txt") == "/home/sgk/note.txt"
    assert _attachment_path_to_wsl("/home/sgk/note.txt") == "/home/sgk/note.txt"


def test_merge_messages_dedupes_adjacent_same_text_at_same_second_and_sorts_by_time() -> None:
    transcript_messages = [
        {"id": "assistant", "role": "assistant", "content": "done", "createdAt": "2026-06-30T03:04:58.9215662+00:00"},
        {"id": "user-transcript", "role": "user", "content": "確認して", "createdAt": "2026-06-30T03:04:40.0758495+00:00"},
    ]
    local_messages = [
        {
            "id": "user-local",
            "role": "user",
            "content": "確認して",
            "createdAt": "2026-06-30T03:04:40Z",
        }
    ]

    merged = _merge_messages(transcript_messages, local_messages)

    assert [message["id"] for message in merged] == ["user-transcript", "assistant"]


def test_merge_messages_keeps_same_text_at_different_seconds() -> None:
    transcript_messages = [
        {"id": "user-transcript", "role": "user", "content": "確認して", "createdAt": "2026-06-30T03:04:41Z"},
    ]
    local_messages = [
        {"id": "user-local", "role": "user", "content": "確認して", "createdAt": "2026-06-30T03:04:40Z"},
    ]

    merged = _merge_messages(transcript_messages, local_messages)

    assert [message["id"] for message in merged] == ["user-local", "user-transcript"]


def test_merge_messages_skips_exact_same_id() -> None:
    transcript_messages = [
        {"id": "msg-1", "role": "user", "content": "確認して", "createdAt": "2026-06-30T03:04:40Z"},
    ]
    local_messages = [
        {"id": "msg-1", "role": "user", "content": "確認して", "createdAt": "2026-06-30T03:04:41Z"},
    ]

    merged = _merge_messages(transcript_messages, local_messages)

    assert [message["id"] for message in merged] == ["msg-1"]


def test_merge_messages_keeps_repeated_automation_prompts() -> None:
    transcript_messages = [
        {"id": "assistant-1", "role": "assistant", "content": "done 1", "createdAt": "2026-06-30T03:00:10Z"},
        {"id": "assistant-2", "role": "assistant", "content": "done 2", "createdAt": "2026-06-30T03:10:10Z"},
    ]
    local_messages = [
        {"id": "automation-1", "role": "user", "content": "定期確認してください", "createdAt": "2026-06-30T03:00:00Z"},
        {"id": "automation-2", "role": "user", "content": "定期確認してください", "createdAt": "2026-06-30T03:10:00Z"},
    ]

    merged = _merge_messages(transcript_messages, local_messages)

    assert [message["id"] for message in merged] == ["automation-1", "assistant-1", "automation-2", "assistant-2"]


def test_merge_messages_preserves_repeated_transcript_prompts_without_local_echoes() -> None:
    transcript_messages = [
        {"id": "user-transcript-1", "role": "user", "content": "確認して", "createdAt": "2026-06-30T03:00:00Z"},
        {"id": "user-transcript-2", "role": "user", "content": "確認して", "createdAt": "2026-06-30T03:10:00Z"},
    ]
    local_messages = [
        {"id": "user-local-1", "role": "user", "content": "確認して", "createdAt": "2026-06-30T03:00:00Z"},
        {"id": "user-local-2", "role": "user", "content": "確認して", "createdAt": "2026-06-30T03:10:00Z"},
    ]

    merged = _merge_messages(transcript_messages, local_messages)

    assert [message["id"] for message in merged] == ["user-transcript-1", "user-transcript-2"]


def test_message_page_returns_latest_and_before_cursor() -> None:
    messages = [
        {"id": f"msg_{index}", "createdAt": f"2026-06-27T00:00:0{index}Z", "content": str(index)}
        for index in range(5)
    ]

    latest = message_page(messages, limit=2, before_created_at=None, before_id=None)
    older = message_page(messages, limit=2, before_created_at="2026-06-27T00:00:03Z", before_id="msg_3")

    assert latest["totalCount"] == 5
    assert latest["hasMoreBefore"] is True
    assert [message["id"] for message in latest["messages"]] == ["msg_3", "msg_4"]
    assert older["hasMoreBefore"] is True
    assert [message["id"] for message in older["messages"]] == ["msg_1", "msg_2"]


@pytest.mark.asyncio
async def test_event_hub_late_subscriber_stops_after_terminal_history() -> None:
    hub = EventHub()
    await hub.publish("run_1", "status", {"status": "running"})
    await hub.publish("run_1", "done", {"status": "succeeded"})

    events = []
    async for item in hub.subscribe("run_1"):
        events.append(item["event"])

    assert events == ["status", "done"]


@pytest.mark.asyncio
async def test_transcript_index_does_not_infer_title_from_history_text(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    sessions = cfg.codex_home / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "title.jsonl").write_text(
        json_line({"type": "session_meta", "payload": {"id": "thr_title", "cwd": str(project_dir), "timestamp": "2026-06-27T00:00:00Z"}})
        + json_line({"type": "event_msg", "payload": {"type": "user_message", "message": "<user_instructions>ignore this</user_instructions>"}})
        + json_line({"type": "event_msg", "payload": {"type": "user_message", "message": "Generate a concise UI title for this task."}})
        + json_line({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Indexed chat title"}]}}),
        encoding="utf-8",
    )
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    transcripts = TranscriptImportService(cfg, projects, chats)

    session = transcripts._read_transcript_session(sessions / "title.jsonl")

    assert session is not None
    assert session.title == "New Chat"
    db.close()


@pytest.mark.asyncio
async def test_transcript_index_uses_late_history_only_for_timestamp(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    sessions = cfg.codex_home / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "late-title.jsonl").write_text(
        json_line({"timestamp": "2026-06-27T00:00:00Z", "type": "session_meta", "payload": {"id": "thr_late", "cwd": str(project_dir), "timestamp": "2026-06-27T00:00:00Z"}})
        + json_line({"timestamp": "2026-06-27T00:00:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": "Initial task"}})
        + json_line({"timestamp": "2026-06-27T00:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Long assistant answer"}]}})
        + json_line({"timestamp": "2026-06-27T00:00:03Z", "type": "event_msg", "payload": {"type": "user_message", "message": "Final follow-up task"}}),
        encoding="utf-8",
    )
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    transcripts = TranscriptImportService(cfg, projects, chats)

    session = transcripts._read_transcript_session(sessions / "late-title.jsonl")

    assert session is not None
    assert session.title == "New Chat"
    assert session.timestamp == "2026-06-27T00:00:03Z"
    db.close()


@pytest.mark.asyncio
async def test_health_and_diagnostics(linux_tmp_path: Path) -> None:
    app = create_app(make_test_config(linux_tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json()["ok"] is True

        diagnostics = await client.get("/diagnostics")
        assert diagnostics.status_code == 200
        body = diagnostics.json()
        assert "auth" not in str(body).lower()
        assert body["databaseFileExists"] is True
        assert body["permissionProfile"] == ":danger-full-access"
        assert body["activeRunIds"] == []
        assert body["activeRuns"] == []

        shutdown = await client.post("/shutdown")
        assert shutdown.status_code == 200
        assert shutdown.json()["ok"] is True


@pytest.mark.asyncio
async def test_chat_automation_crud(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    app = create_app(make_test_config(linux_tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        project = (await client.post("/projects", json={"path": str(project_dir)})).json()
        chat = (await client.post(f"/projects/{project['id']}/chats", json={"title": "daily"})).json()
        prompt = "## Check\n\n- build\n- test\n\nReport with `git status`."

        created = await client.post(
            f"/projects/{project['id']}/chats/{chat['id']}/automations",
            json={"name": "check", "prompt": f"\n{prompt}\n", "interval_minutes": 5, "enabled": True},
        )
        assert created.status_code == 200
        automation = created.json()
        assert automation["chatId"] == chat["id"]
        assert automation["prompt"] == prompt
        assert automation["enabled"] is True
        assert automation["nextRunAt"] is not None

        listed = (await client.get(f"/projects/{project['id']}/chats/{chat['id']}/automations")).json()
        assert [item["id"] for item in listed] == [automation["id"]]
        assert listed[0]["prompt"] == prompt

        updated = await client.patch(
            f"/projects/{project['id']}/chats/{chat['id']}/automations/{automation['id']}",
            json={"prompt": "Line 1\r\n\r\nLine 2", "enabled": False},
        )
        assert updated.status_code == 200
        assert updated.json()["prompt"] == "Line 1\n\nLine 2"
        assert updated.json()["enabled"] is False
        assert updated.json()["nextRunAt"] is None

        deleted = await client.delete(f"/projects/{project['id']}/chats/{chat['id']}/automations/{automation['id']}")
        assert deleted.status_code == 204
        assert (await client.get(f"/projects/{project['id']}/chats/{chat['id']}/automations")).json() == []


@pytest.mark.asyncio
async def test_chat_automation_supports_hourly_minute_and_daily_time(linux_tmp_path: Path) -> None:
    app = create_app(make_test_config(linux_tmp_path))
    project_path = linux_tmp_path / "project"
    project_path.mkdir()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        project = (await client.post("/projects", json={"path": str(project_path), "name": "project"})).json()
        chat = (await client.post(f"/projects/{project['id']}/chats", json={"title": "automation schedules"})).json()

        hourly = (
            await client.post(
                f"/projects/{project['id']}/chats/{chat['id']}/automations",
                json={"name": "hourly", "prompt": "check", "schedule_kind": "hourly_minute", "interval_minutes": 15, "enabled": True},
            )
        ).json()
        assert hourly["scheduleKind"] == "hourly_minute"
        assert hourly["intervalMinutes"] == 15
        assert hourly["nextRunAt"] is not None

        daily = (
            await client.patch(
                f"/projects/{project['id']}/chats/{chat['id']}/automations/{hourly['id']}",
                json={"schedule_kind": "daily_time", "interval_minutes": 9 * 60 + 30},
            )
        ).json()
        assert daily["scheduleKind"] == "daily_time"
        assert daily["intervalMinutes"] == 570
        assert daily["nextRunAt"] is not None

        invalid = await client.patch(
            f"/projects/{project['id']}/chats/{chat['id']}/automations/{hourly['id']}",
            json={"schedule_kind": "hourly_minute", "interval_minutes": 60},
        )
        assert invalid.status_code == 400


@pytest.mark.asyncio
async def test_chat_automation_rejects_archived_chat(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    app = create_app(make_test_config(linux_tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        project = (await client.post("/projects", json={"path": str(project_dir)})).json()
        chat = (await client.post(f"/projects/{project['id']}/chats", json={"title": "daily"})).json()

        archive = await client.post(f"/projects/{project['id']}/chats/{chat['id']}/archive")
        assert archive.status_code == 200

        created = await client.post(
            f"/projects/{project['id']}/chats/{chat['id']}/automations",
            json={"name": "check", "prompt": "check status", "interval_minutes": 5, "enabled": True},
        )

        assert created.status_code == 409
        assert created.json()["error"]["code"] == "automation_chat_archived"


@pytest.mark.asyncio
async def test_chat_automation_due_run_uses_existing_chat(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    automations = AutomationService(db, projects, chats)
    project = projects.create_project(str(project_dir))
    chat = chats.create_chat(project["id"], "daily")
    prompt = "## Check\n\n- status\n- tests"
    automation = automations.create_automation(project["id"], chat["id"], "check", prompt, 5, enabled=True)
    db.execute("UPDATE automations SET next_run_at = ? WHERE id = ?", ("2026-01-01T00:00:00Z", automation["id"]))

    class RecordingRunner:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str]] = []

        async def start_message_run(self, project_id: str, chat_id: str, content: str) -> dict:
            self.calls.append((project_id, chat_id, content))
            return {"id": "run_1"}

    runner = RecordingRunner()

    await _run_due_automations(automations, runner)

    assert runner.calls == [(project["id"], chat["id"], prompt)]
    row = db.fetchone("SELECT running, enabled, last_run_at, next_run_at, last_error FROM automations WHERE id = ?", (automation["id"],))
    assert row is not None
    assert row["running"] == 0
    assert row["enabled"] == 1
    assert row["last_run_at"] is not None
    assert row["next_run_at"] is not None
    assert row["last_error"] is None
    db.close()


@pytest.mark.asyncio
async def test_chat_automation_run_now_updates_next_run(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    app = create_app(make_test_config(linux_tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        project = (await client.post("/projects", json={"path": str(project_dir)})).json()
        chat = (await client.post(f"/projects/{project['id']}/chats", json={"title": "daily"})).json()
        automation = (
            await client.post(
                f"/projects/{project['id']}/chats/{chat['id']}/automations",
                json={"name": "check", "prompt": "check status", "interval_minutes": 5, "enabled": True},
            )
        ).json()

        run = await client.post(f"/projects/{project['id']}/chats/{chat['id']}/automations/{automation['id']}/run")

        assert run.status_code == 200
        body = run.json()
        assert body["automation"]["enabled"] is True
        assert body["automation"]["running"] is False
        assert body["automation"]["lastRunAt"] is not None
        assert body["automation"]["nextRunAt"] is not None
        assert body["run"] is not None
        assert body["run"]["runId"]


@pytest.mark.asyncio
async def test_runtime_settings_endpoint(linux_tmp_path: Path) -> None:
    cfg = make_test_config(linux_tmp_path)
    app = create_app(cfg)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        initial = await client.get("/settings")
        assert initial.status_code == 200
        assert initial.json()["permissionProfile"] == ":danger-full-access"
        assert initial.json()["approvalPolicy"] == "never"
        assert initial.json()["approvalsReviewer"] == "user"
        assert initial.json()["model"] == ""
        assert initial.json()["reasoningEffort"] == ""
        assert initial.json()["recentModelReasoningChoices"] == []
        assert "gpt-5.6-luna" in initial.json()["availableModels"]

        models = await client.get("/models")
        assert models.status_code == 200
        assert models.json()["dynamic"] is False
        assert "gpt-5.6-sol" in models.json()["availableModels"]

        updated = await client.patch("/settings", json={"permissionProfile": ":workspace", "approvalPolicy": "on-request", "approvalsReviewer": "auto_review", "model": "gpt-5-codex", "reasoningEffort": "high"})
        assert updated.status_code == 200
        assert updated.json()["permissionProfile"] == ":workspace"
        assert updated.json()["approvalPolicy"] == "on-request"
        assert updated.json()["approvalsReviewer"] == "auto_review"
        assert updated.json()["model"] == "gpt-5-codex"
        assert updated.json()["reasoningEffort"] == "high"
        assert updated.json()["recentModelReasoningChoices"][0] == {"model": "gpt-5-codex", "reasoningEffort": "high"}

        invalid = await client.patch("/settings", json={"permissionProfile": "invalid"})
        assert invalid.status_code == 400
        assert invalid.json()["error"]["code"] == "validation_error"

        invalid_approval = await client.patch("/settings", json={"approvalPolicy": "invalid"})
        assert invalid_approval.status_code == 400
        assert invalid_approval.json()["error"]["code"] == "validation_error"

        invalid_reviewer = await client.patch("/settings", json={"approvalsReviewer": "invalid"})
        assert invalid_reviewer.status_code == 400
        assert invalid_reviewer.json()["error"]["code"] == "validation_error"

    restarted = create_app(cfg)
    async with AsyncClient(transport=ASGITransport(app=restarted), base_url="http://test") as client:
        persisted = await client.get("/settings")
        assert persisted.status_code == 200
        assert persisted.json()["permissionProfile"] == ":workspace"
        assert persisted.json()["approvalPolicy"] == "on-request"
        assert persisted.json()["approvalsReviewer"] == "auto_review"
        assert persisted.json()["model"] == "gpt-5-codex"
        assert persisted.json()["reasoningEffort"] == "high"
        assert persisted.json()["recentModelReasoningChoices"][0] == {"model": "gpt-5-codex", "reasoningEffort": "high"}


@pytest.mark.asyncio
async def test_chat_runtime_settings_are_saved_per_chat(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    app = create_app(make_test_config(linux_tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        project = (await client.post("/projects", json={"path": str(project_dir), "name": "settings"})).json()
        chat_one = (
            await client.post(
                f"/projects/{project['id']}/chats",
                json={
                    "title": "one",
                    "permissionProfile": ":workspace",
                    "approvalPolicy": "on-request",
                    "approvalsReviewer": "auto_review",
                    "model": "gpt-5.6-sol",
                    "reasoningEffort": "high",
                },
            )
        ).json()
        chat_two = (await client.post(f"/projects/{project['id']}/chats", json={"title": "two"})).json()

        one_settings = await client.get(f"/projects/{project['id']}/chats/{chat_one['id']}/settings")
        two_settings = await client.get(f"/projects/{project['id']}/chats/{chat_two['id']}/settings")
        assert one_settings.status_code == 200
        assert one_settings.json()["permissionProfile"] == ":workspace"
        assert one_settings.json()["approvalsReviewer"] == "auto_review"
        assert one_settings.json()["model"] == "gpt-5.6-sol"
        assert one_settings.json()["reasoningEffort"] == "high"
        assert two_settings.json()["permissionProfile"] == ":danger-full-access"
        assert two_settings.json()["model"] == ""

        updated = await client.patch(
            f"/projects/{project['id']}/chats/{chat_two['id']}/settings",
            json={
                "permissionProfile": ":read-only",
                "approvalPolicy": "on-failure",
                "approvalsReviewer": "user",
                "model": "deepseek-v4-flash",
                "reasoningEffort": "max",
            },
        )
        assert updated.status_code == 200
        assert updated.json()["model"] == "deepseek-v4-flash"
        assert updated.json()["recentModelReasoningChoices"][0] == {"model": "deepseek-v4-flash", "reasoningEffort": "max"}
        assert (await client.get(f"/projects/{project['id']}/chats/{chat_one['id']}/settings")).json()["model"] == "gpt-5.6-sol"
        assert (await client.get(f"/projects/{project['id']}/chats/{chat_two['id']}/settings")).json()["model"] == "deepseek-v4-flash"

    restarted = create_app(make_test_config(linux_tmp_path))
    async with AsyncClient(transport=ASGITransport(app=restarted), base_url="http://test") as client:
        recent = (await client.get("/settings")).json()["recentModelReasoningChoices"]
        assert recent[0] == {"model": "deepseek-v4-flash", "reasoningEffort": "max"}
        assert recent[1] == {"model": "gpt-5.6-sol", "reasoningEffort": "high"}


@pytest.mark.asyncio
async def test_app_server_mode_does_not_start_app_server_during_lifespan(linux_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def record_start(self) -> None:  # type: ignore[no-untyped-def]
        calls.append("start")

    async def record_close(self) -> None:  # type: ignore[no-untyped-def]
        calls.append("close")

    monkeypatch.setattr("codex_lite_daemon.app_server.AppServerClient.ensure_started", record_start)
    monkeypatch.setattr("codex_lite_daemon.app_server.AppServerClient.close", record_close)
    base_cfg = make_test_config(linux_tmp_path)
    cfg = base_cfg.__class__(**(base_cfg.__dict__ | {"runner_mode": "app-server"}))
    app = create_app(cfg)

    async with app.router.lifespan_context(app):
        assert calls == []

    assert calls == ["close"]


@pytest.mark.asyncio
async def test_fake_mode_does_not_start_app_server_during_lifespan(linux_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def record_start(self) -> None:  # type: ignore[no-untyped-def]
        calls.append("start")

    async def record_close(self) -> None:  # type: ignore[no-untyped-def]
        calls.append("close")

    monkeypatch.setattr("codex_lite_daemon.app_server.AppServerClient.ensure_started", record_start)
    monkeypatch.setattr("codex_lite_daemon.app_server.AppServerClient.close", record_close)
    app = create_app(make_test_config(linux_tmp_path))

    async with app.router.lifespan_context(app):
        assert calls == []

    assert calls == ["close"]


@pytest.mark.asyncio
async def test_create_project_does_not_import_transcript_chats_automatically(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    sessions = cfg.codex_home / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "session.jsonl").write_text(
        json_line({"type": "session_meta", "payload": {"id": "thr_auto", "cwd": str(project_dir), "timestamp": "2026-06-27T00:00:00Z"}})
        + json_line({"type": "event_msg", "payload": {"type": "user_message", "message": "should not appear automatically"}}),
        encoding="utf-8",
    )

    app = create_app(cfg)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        project = (await client.post("/projects", json={"path": str(project_dir)})).json()
        chats = (await client.get(f"/projects/{project['id']}/chats")).json()
        assert chats == []


def test_delete_project_removes_local_chat_projection(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)

    project = projects.create_project(str(project_dir))
    chat = chats.create_chat(project["id"], "local")
    messages.insert_message(chat["id"], "user", "hello", kind="instruction")

    projects.delete_project(project["id"])

    assert db.fetchall("SELECT * FROM projects WHERE id = ?", (project["id"],)) == []
    assert db.fetchall("SELECT * FROM chats WHERE project_id = ?", (project["id"],)) == []
    assert db.fetchall("SELECT * FROM messages WHERE chat_id = ?", (chat["id"],)) == []
    db.close()


@pytest.mark.asyncio
async def test_startup_does_not_index_registered_project_transcripts(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    sessions = cfg.codex_home / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "session.jsonl").write_text(
        json_line({"type": "session_meta", "payload": {"id": "thr_startup", "cwd": str(project_dir), "timestamp": "2026-06-27T00:00:00Z"}}),
        encoding="utf-8",
    )
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    project = projects.create_project(str(project_dir))
    db.close()

    app = create_app(cfg)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        chats = (await client.get(f"/projects/{project['id']}/chats")).json()
        assert chats == []


@pytest.mark.asyncio
async def test_project_chat_message_run_flow(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "README.md").write_text("# Hello\n", encoding="utf-8")

    app = create_app(make_test_config(linux_tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        project_resp = await client.post("/projects", json={"path": str(project_dir), "name": "demo"})
        assert project_resp.status_code == 200
        project_id = project_resp.json()["id"]

        chat_resp = await client.post(f"/projects/{project_id}/chats", json={"title": "test"})
        assert chat_resp.status_code == 200
        chat_id = chat_resp.json()["id"]

        msg_resp = await client.post(
            f"/projects/{project_id}/chats/{chat_id}/messages",
            json={"content": "say hello"},
        )
        assert msg_resp.status_code == 200
        assert msg_resp.json()["messageId"].startswith("msg_")
        run_id = msg_resp.json()["runId"]

        for _ in range(100):
            run = (await client.get(f"/runs/{run_id}")).json()
            if run["status"] == "succeeded":
                break
            await asyncio.sleep(0.02)
        assert run["status"] == "succeeded"

        messages = (await client.get(f"/projects/{project_id}/chats/{chat_id}/messages")).json()
        assert [message["role"] for message in messages] == ["user", "assistant"]
        assert "Fake Codex run" in messages[1]["content"]


@pytest.mark.asyncio
async def test_archive_chat_hides_it_from_chat_list(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()

    app = create_app(make_test_config(linux_tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        project_id = (await client.post("/projects", json={"path": str(project_dir)})).json()["id"]
        keep_id = (await client.post(f"/projects/{project_id}/chats", json={"title": "keep"})).json()["id"]
        archive_id = (await client.post(f"/projects/{project_id}/chats", json={"title": "archive"})).json()["id"]

        archive = await client.post(f"/projects/{project_id}/chats/{archive_id}/archive")
        assert archive.status_code == 200
        assert archive.json()["id"] == archive_id

        chats = (await client.get(f"/projects/{project_id}/chats")).json()
        assert [chat["id"] for chat in chats] == [keep_id]

        direct = await client.get(f"/projects/{project_id}/chats/{archive_id}")
        assert direct.status_code == 200


def test_insert_message_updates_chat_sort_time(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)

    project_id = projects.create_project(str(project_dir))["id"]
    older = chats.create_chat(project_id, "older")
    newer = chats.create_chat(project_id, "newer")
    db.execute("UPDATE chats SET updated_at = ? WHERE id = ?", ("2026-06-27T00:00:00Z", older["id"]))
    db.execute("UPDATE chats SET updated_at = ? WHERE id = ?", ("2026-06-27T00:00:01Z", newer["id"]))

    messages.insert_message(older["id"], "user", "bump older", kind="instruction")

    assert [chat["id"] for chat in chats.list_chats(project_id)] == [older["id"], newer["id"]]
    db.close()


@pytest.mark.asyncio
async def test_app_server_delete_chat_is_local_index_only(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    app_server = RecordingAppServer()
    service = AppServerThreadService(projects, chats, messages, transcripts, app_server, make_runtime_settings())  # type: ignore[arg-type]

    project_id = projects.create_project(str(project_dir))["id"]
    chat_id = chats.upsert_chat_index(project_id, "thr_delete", "delete me", "thr_delete", utc_now(), utc_now())["id"]

    await service.delete_chat(project_id, chat_id)

    assert app_server.requests == []
    assert chats.list_chats(project_id) == []
    db.close()


@pytest.mark.asyncio
async def test_app_server_archive_chat_archives_thread(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    app_server = RecordingAppServer()
    service = AppServerThreadService(projects, chats, messages, transcripts, app_server, make_runtime_settings())  # type: ignore[arg-type]

    project_id = projects.create_project(str(project_dir))["id"]
    chat_id = chats.upsert_chat_index(project_id, "thr_archive", "archive me", "thr_archive", utc_now(), utc_now())["id"]

    archived = await service.archive_chat(project_id, chat_id)

    assert archived["id"] == chat_id
    assert app_server.requests == [("thread/archive", {"threadId": "thr_archive"})]
    assert chats.list_chats(project_id) == []
    db.close()


@pytest.mark.asyncio
async def test_app_server_update_chat_uses_codex_session_id(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    app_server = RecordingAppServer()
    service = AppServerThreadService(projects, chats, messages, transcripts, app_server, make_runtime_settings())  # type: ignore[arg-type]

    project_id = projects.create_project(str(project_dir))["id"]
    chats.upsert_chat_index(project_id, "local_chat", "old title", "actual_thread", utc_now(), utc_now())

    updated = await service.update_chat(project_id, "local_chat", "local title")

    assert updated["title"] == "local title"
    assert app_server.requests == [("thread/name/set", {"threadId": "actual_thread", "name": "local title"})]
    assert chats.get_chat(project_id, "local_chat")["title"] == "local title"
    db.close()


@pytest.mark.asyncio
async def test_app_server_update_chat_keeps_local_title_when_app_server_fails(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    app_server = FailingNameAppServer()
    service = AppServerThreadService(projects, chats, messages, transcripts, app_server, make_runtime_settings())  # type: ignore[arg-type]

    project_id = projects.create_project(str(project_dir))["id"]
    chats.upsert_chat_index(project_id, "thr_rename", "old title", "thr_rename", utc_now(), utc_now())

    updated = await service.update_chat(project_id, "thr_rename", "local title")

    assert updated["title"] == "local title"
    assert app_server.requests == [("thread/name/set", {"threadId": "thr_rename", "name": "local title"})]
    assert chats.get_chat(project_id, "thr_rename")["title"] == "local title"
    db.close()


@pytest.mark.asyncio
async def test_app_server_usage_capacity_reads_rate_limits() -> None:
    app_server = UsageAppServer()
    service = AppServerUsageService(app_server)  # type: ignore[arg-type]

    capacity = await service.read_capacity()

    assert app_server.requests == [("account/rateLimits/read", {})]
    assert capacity["fiveHour"] == {
        "usedPercent": 40.0,
        "remainingPercent": 60.0,
        "windowMinutes": 300,
        "resetsAt": "2026-07-01T08:00:00Z",
    }
    assert capacity["weekly"] == {
        "usedPercent": 89.0,
        "remainingPercent": 11.0,
        "windowMinutes": 10080,
        "resetsAt": "2026-07-07T08:00:00Z",
    }
    assert capacity["planType"] == "prolite"
    assert capacity["resetCredits"] == {"availableCount": 1}
    assert capacity["codexCredits"] == {"hasCredits": False, "unlimited": False, "balance": "0"}


@pytest.mark.asyncio
async def test_app_server_usage_capacity_reads_deepseek_balance_without_app_server() -> None:
    async def fake_balance() -> dict:
        return {
            "status": "ok",
            "isAvailable": True,
            "balanceInfos": [
                {
                    "currency": "USD",
                    "totalBalance": "12.50",
                    "grantedBalance": "2.50",
                    "toppedUpBalance": "10.00",
                }
            ],
        }

    app_server = UsageAppServer()
    service = AppServerUsageService(app_server, fake_balance)  # type: ignore[arg-type]

    capacity = await service.read_capacity("deepseek")

    assert app_server.requests == []
    assert capacity["provider"] == "deepseek"
    assert capacity["fiveHour"] is None
    assert capacity["weekly"] is None
    assert capacity["deepseekBalance"]["balanceInfos"][0]["totalBalance"] == "12.50"


@pytest.mark.asyncio
async def test_app_server_archive_chat_falls_back_for_missing_thread(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    app_server = MissingArchiveAppServer()
    service = AppServerThreadService(projects, chats, messages, transcripts, app_server, make_runtime_settings())  # type: ignore[arg-type]

    project_id = projects.create_project(str(project_dir))["id"]
    chat_id = chats.upsert_chat_index(project_id, "missing_thread", "archive me", "missing_thread", utc_now(), utc_now())["id"]

    archived = await service.archive_chat(project_id, chat_id)

    assert archived["id"] == chat_id
    assert app_server.requests == [("thread/archive", {"threadId": "missing_thread"})]
    assert chats.list_chats(project_id) == []
    db.close()


@pytest.mark.asyncio
async def test_app_server_archive_chat_hides_row_when_app_server_fails(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    app_server = FailingArchiveAppServer()
    service = AppServerThreadService(projects, chats, messages, transcripts, app_server, make_runtime_settings())  # type: ignore[arg-type]

    project_id = projects.create_project(str(project_dir))["id"]
    chat_id = chats.upsert_chat_index(project_id, "thr_archive", "archive me", "thr_archive", utc_now(), utc_now())["id"]

    archived = await service.archive_chat(project_id, chat_id)

    assert archived["id"] == chat_id
    assert app_server.requests == [("thread/archive", {"threadId": "thr_archive"})]
    assert chats.list_chats(project_id) == []
    db.close()


@pytest.mark.asyncio
async def test_app_server_messages_are_loaded_from_transcript_jsonl(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    sessions = cfg.codex_home / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "thread.jsonl").write_text(
        json_line({"timestamp": "2026-06-27T00:00:00Z", "type": "session_meta", "payload": {"id": "thr_messages", "cwd": str(project_dir), "timestamp": "2026-06-27T00:00:00Z"}})
        + json_line({"timestamp": "2026-06-27T00:00:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": "hello"}})
        + json_line({"timestamp": "2026-06-27T00:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "working"}], "phase": "commentary"}})
        + json_line({"timestamp": "2026-06-27T00:00:03Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}], "phase": "final_answer"}}),
        encoding="utf-8",
    )
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    app_server = RecordingAppServer()
    service = AppServerThreadService(projects, chats, messages, transcripts, app_server, make_runtime_settings())  # type: ignore[arg-type]

    project = projects.create_project(str(project_dir))
    chats.upsert_chat_index(project["id"], "thr_messages", "messages", "thr_messages", utc_now(), utc_now(), str(sessions / "thread.jsonl"))
    messages.insert_message("thr_messages", "assistant", "workingdone", kind="conclusion")

    loaded = await service.list_messages(project["id"], "thr_messages")

    assert app_server.requests == []
    assert [(message["role"], message["content"], message["kind"]) for message in loaded] == [
        ("user", "hello", "instruction"),
        ("assistant", "working", "work"),
        ("assistant", "done", "conclusion"),
    ]
    db.close()


@pytest.mark.asyncio
async def test_transcript_user_message_file_mentions_become_image_attachments(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    sessions = cfg.codex_home / "sessions"
    sessions.mkdir(parents=True)
    message = "\n".join(
        [
            "Files mentioned by the user:",
            "codex-clipboard-image.png:",
            "C:/Users/sgk/AppData/Local/Temp/codex-clipboard-image.png",
            "",
            "My request for Codex:",
            "画像を貼るとこうなっちゃうんだよ。",
            "Codex could not read the local image at `C:\\Users\\sgk\\AppData\\Local\\Temp\\codex-clipboard-image.png`: No such file or directory (os error 2)",
        ]
    )
    (sessions / "thread.jsonl").write_text(
        json_line({"timestamp": "2026-06-27T00:00:00Z", "type": "session_meta", "payload": {"id": "thr_image", "cwd": str(project_dir), "timestamp": "2026-06-27T00:00:00Z"}})
        + json_line({"timestamp": "2026-06-27T00:00:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": message}}),
        encoding="utf-8",
    )
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    service = AppServerThreadService(projects, chats, messages, transcripts, RecordingAppServer(), make_runtime_settings())  # type: ignore[arg-type]

    project = projects.create_project(str(project_dir))
    chats.upsert_chat_index(project["id"], "thr_image", "image", "thr_image", utc_now(), utc_now(), str(sessions / "thread.jsonl"))

    loaded = await service.list_messages(project["id"], "thr_image")

    assert len(loaded) == 1
    assert loaded[0]["content"] == "画像を貼るとこうなっちゃうんだよ。"
    assert loaded[0]["attachments"] == [
        {
            "path": "/mnt/c/Users/sgk/AppData/Local/Temp/codex-clipboard-image.png",
            "name": "codex-clipboard-image.png",
            "kind": "image",
            "uri": "file:///C:/Users/sgk/AppData/Local/Temp/codex-clipboard-image.png",
        }
    ]
    db.close()


@pytest.mark.asyncio
async def test_app_server_messages_include_transcript_command_activity(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    sessions = cfg.codex_home / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "thread.jsonl").write_text(
        json_line({"timestamp": "2026-06-27T00:00:00Z", "type": "session_meta", "payload": {"id": "thr_commands", "cwd": str(project_dir), "timestamp": "2026-06-27T00:00:00Z"}})
        + json_line({"timestamp": "2026-06-27T00:00:01Z", "type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "arguments": "{\"cmd\":\"git status --short\",\"workdir\":\"/repo\"}", "call_id": "call_1"}})
        + json_line({"timestamp": "2026-06-27T00:00:02Z", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "call_1", "output": "Chunk ID: abc\nOutput:\n M file.txt\n"}}),
        encoding="utf-8",
    )
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    service = AppServerThreadService(projects, chats, messages, transcripts, RecordingAppServer(), make_runtime_settings())  # type: ignore[arg-type]

    project = projects.create_project(str(project_dir))
    chats.upsert_chat_index(project["id"], "thr_commands", "commands", "thr_commands", utc_now(), utc_now(), str(sessions / "thread.jsonl"))

    loaded = await service.list_messages(project["id"], "thr_commands")

    assert len(loaded) == 1
    assert loaded[0]["role"] == "status"
    assert loaded[0]["kind"] == "status"
    assert loaded[0]["content"] == "コマンドを実行しました: git status --short"
    assert "$ cd /repo" in loaded[0]["activityDetails"]
    assert " M file.txt" in loaded[0]["activityDetails"]
    db.close()


@pytest.mark.asyncio
async def test_app_server_messages_include_reasoning_summary_and_tool_arguments(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    sessions = cfg.codex_home / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "thread.jsonl").write_text(
        json_line({"timestamp": "2026-06-27T00:00:00Z", "type": "session_meta", "payload": {"id": "thr_details", "cwd": str(project_dir), "timestamp": "2026-06-27T00:00:00Z"}})
        + json_line({"timestamp": "2026-06-27T00:00:01Z", "type": "response_item", "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "調査対象を絞り込みます。"}], "encrypted_content": "not-for-display"}})
        + json_line({"timestamp": "2026-06-27T00:00:02Z", "type": "response_item", "payload": {"type": "function_call", "name": "filesystem.read", "arguments": "{\"path\":\"README.md\"}", "call_id": "call_1"}})
        + json_line({"timestamp": "2026-06-27T00:00:03Z", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "call_1", "output": "read complete"}}),
        encoding="utf-8",
    )
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    service = AppServerThreadService(projects, chats, messages, transcripts, RecordingAppServer(), make_runtime_settings())  # type: ignore[arg-type]

    project = projects.create_project(str(project_dir))
    chats.upsert_chat_index(project["id"], "thr_details", "details", "thr_details", utc_now(), utc_now(), str(sessions / "thread.jsonl"))

    loaded = await service.list_messages(project["id"], "thr_details")

    assert len(loaded) == 2
    assert loaded[0]["content"] == "推論の要約"
    assert loaded[0]["activityDetails"] == "調査対象を絞り込みます。"
    assert "not-for-display" not in loaded[0]["activityDetails"]
    assert '"path": "README.md"' in loaded[1]["activityDetails"]
    assert "read complete" in loaded[1]["activityDetails"]
    db.close()


@pytest.mark.asyncio
async def test_app_server_messages_use_saved_transcript_path_without_scanning(linux_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    sessions = cfg.codex_home / "sessions"
    sessions.mkdir(parents=True)
    transcript_path = sessions / "thread.jsonl"
    transcript_path.write_text(
        json_line({"timestamp": "2026-06-27T00:00:00Z", "type": "session_meta", "payload": {"id": "thr_saved", "cwd": str(project_dir), "timestamp": "2026-06-27T00:00:00Z"}})
        + json_line({"timestamp": "2026-06-27T00:00:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": "hello from saved path"}}),
        encoding="utf-8",
    )
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    app_server = RecordingAppServer()
    service = AppServerThreadService(projects, chats, messages, transcripts, app_server, make_runtime_settings())  # type: ignore[arg-type]

    project = projects.create_project(str(project_dir))
    chats.upsert_chat_index(project["id"], "thr_saved", "saved", "thr_saved", utc_now(), utc_now(), str(transcript_path))
    monkeypatch.setattr(transcripts, "_transcript_sessions", lambda: (_ for _ in ()).throw(AssertionError("unexpected transcript scan")))

    loaded = await service.list_messages(project["id"], "thr_saved")

    assert [(message["role"], message["content"]) for message in loaded] == [("user", "hello from saved path")]
    db.close()


@pytest.mark.asyncio
async def test_app_server_messages_backfills_missing_transcript_path(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    sessions = cfg.codex_home / "sessions"
    sessions.mkdir(parents=True)
    transcript_path = sessions / "thread.jsonl"
    transcript_path.write_text(
        json_line({"timestamp": "2026-06-27T00:00:00Z", "type": "session_meta", "payload": {"id": "thr_backfill", "cwd": str(project_dir), "timestamp": "2026-06-27T00:00:00Z"}})
        + json_line({"timestamp": "2026-06-27T00:00:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": "hello from backfill"}}),
        encoding="utf-8",
    )
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    app_server = RecordingAppServer()
    service = AppServerThreadService(projects, chats, messages, transcripts, app_server, make_runtime_settings())  # type: ignore[arg-type]

    project = projects.create_project(str(project_dir))
    chats.upsert_chat_index(project["id"], "thr_backfill", "backfill", "thr_backfill", utc_now(), utc_now())

    loaded = await service.list_messages(project["id"], "thr_backfill")

    row = chats.get_chat_row(project["id"], "thr_backfill")
    assert [(message["role"], message["content"]) for message in loaded] == [("user", "hello from backfill")]
    assert row["transcript_path"] == str(transcript_path.resolve())
    db.close()


@pytest.mark.asyncio
async def test_transcript_index_reuses_existing_chat_by_codex_session_id(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    sessions = cfg.codex_home / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "session.jsonl").write_text(
        json_line({"timestamp": "2026-06-27T00:00:00Z", "type": "session_meta", "payload": {"id": "session_1", "cwd": str(project_dir), "timestamp": "2026-06-27T00:00:00Z"}}),
        encoding="utf-8",
    )
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    transcripts = TranscriptImportService(cfg, projects, chats)

    project = projects.create_project(str(project_dir))
    chats.upsert_chat_index(project["id"], "thread_1", "existing", "session_1", utc_now(), utc_now())

    transcripts.index_project(project)

    rows = db.fetchall("SELECT id, codex_session_id FROM chats WHERE project_id = ?", (project["id"],))
    assert rows == [{"id": "thread_1", "codex_session_id": "session_1"}]
    db.close()


def test_chat_index_preserves_newer_local_title(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    project_id = projects.create_project(str(project_dir))["id"]

    chats.upsert_chat_index(project_id, "thread_1", "DB title", "thread_1", "2026-06-30T00:00:00Z", "2026-06-30T00:00:00Z")
    chats.update_chat(project_id, "thread_1", "Local renamed title")
    chats.upsert_chat_index(project_id, "thread_1", "Old DB title", "thread_1", "2026-06-30T00:00:00Z", "2026-06-30T00:00:00Z")

    row = chats.get_chat(project_id, "thread_1")
    assert row["title"] == "Local renamed title"
    db.close()


def test_chat_index_preserves_local_title_override_against_newer_sync(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    project_id = projects.create_project(str(project_dir))["id"]

    chats.upsert_chat_index(project_id, "thread_1", "DB title", "thread_1", "2026-06-30T00:00:00Z", "2026-06-30T00:00:00Z")
    chats.update_chat(project_id, "thread_1", "Local renamed title")
    chats.upsert_chat_index(project_id, "thread_1", "Newer DB title", "thread_1", "2026-06-30T00:00:00Z", "2026-07-01T00:00:00Z")

    row = chats.get_chat(project_id, "thread_1")
    assert row["title"] == "Local renamed title"
    db.close()


def test_chat_index_keeps_local_title_override_when_sync_matches(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    project_id = projects.create_project(str(project_dir))["id"]

    chats.upsert_chat_index(project_id, "thread_1", "DB title", "thread_1", "2026-06-30T00:00:00Z", "2026-06-30T00:00:00Z")
    chats.update_chat(project_id, "thread_1", "Local renamed title")
    chats.upsert_chat_index(project_id, "thread_1", "Local renamed title", "thread_1", "2026-06-30T00:00:00Z", "2026-07-01T00:00:00Z")
    chats.upsert_chat_index(project_id, "thread_1", "Newer DB title", "thread_1", "2026-06-30T00:00:00Z", "2026-07-02T00:00:00Z")

    row = chats.get_chat(project_id, "thread_1")
    assert row["title"] == "Local renamed title"
    db.close()


def test_chat_index_compares_fractional_timestamps(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    project_id = projects.create_project(str(project_dir))["id"]

    chats.upsert_chat_index(project_id, "thread_1", "Local title", "thread_1", "2026-06-30T00:00:00Z", "2026-06-30T00:00:00.500Z")
    chats.upsert_chat_index(project_id, "thread_1", "Older DB title", "thread_1", "2026-06-30T00:00:00Z", "2026-06-30T00:00:00Z")

    row = chats.get_chat(project_id, "thread_1")
    assert row["title"] == "Local title"
    assert row["updatedAt"] == "2026-06-30T00:00:00.500Z"
    db.close()


def test_chat_index_accepts_newer_codex_title(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    project_id = projects.create_project(str(project_dir))["id"]

    chats.upsert_chat_index(project_id, "thread_1", "Local title", "thread_1", "2026-06-30T00:00:00Z", "2026-06-30T00:00:00Z")
    chats.upsert_chat_index(project_id, "thread_1", "New DB title", "thread_1", "2026-06-30T00:00:00Z", "2026-07-01T00:00:00Z")

    row = chats.get_chat(project_id, "thread_1")
    assert row["title"] == "New DB title"
    assert row["updatedAt"] == "2026-07-01T00:00:00Z"
    db.close()


@pytest.mark.asyncio
async def test_list_chats_dedupes_existing_duplicate_codex_session_id(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    project_id = projects.create_project(str(project_dir))["id"]
    now = utc_now()
    db.execute(
        "INSERT INTO chats(id, project_id, title, codex_session_id, created_at, updated_at, archived_at) VALUES (?, ?, ?, ?, ?, ?, NULL)",
        ("old_index_id", project_id, "duplicate old", "session_1", now, "2026-06-27T00:00:00Z"),
    )
    db.execute(
        "INSERT INTO chats(id, project_id, title, codex_session_id, created_at, updated_at, archived_at) VALUES (?, ?, ?, ?, ?, ?, NULL)",
        ("session_1", project_id, "duplicate exact", "session_1", now, "2026-06-27T00:00:01Z"),
    )

    indexed_chats = chats.list_chats(project_id)

    assert [(chat["id"], chat["title"]) for chat in indexed_chats] == [("session_1", "duplicate exact")]
    db.close()


@pytest.mark.asyncio
async def test_app_server_run_uses_chat_id_before_codex_session_id(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    app_server = TurnStartAppServer()
    threads = AppServerThreadService(projects, chats, messages, transcripts, app_server, make_runtime_settings())  # type: ignore[arg-type]
    runs = AppServerRunService(projects, threads, messages, app_server, max_concurrent_runs=1, settings=make_runtime_settings())  # type: ignore[arg-type]

    project_id = projects.create_project(str(project_dir))["id"]
    chat_id = chats.upsert_chat_index(project_id, "thread_1", "existing", "session_1", utc_now(), utc_now())["id"]

    result = await runs.start_message_run(project_id, chat_id, "hello")

    assert result["messageId"].startswith("msg_")
    assert app_server.requests == [
        ("thread/settings/update", {"threadId": "thread_1", "approvalPolicy": "never", "approvalsReviewer": "user", "permissions": ":danger-full-access"}),
        ("turn/start", {"threadId": "thread_1", "cwd": str(project_dir), "input": [{"type": "text", "text": "hello"}]}),
    ]
    db.close()


@pytest.mark.asyncio
async def test_app_server_run_allows_parallel_different_chats(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    app_server = TurnStartAppServer()
    threads = AppServerThreadService(projects, chats, messages, transcripts, app_server, make_runtime_settings())  # type: ignore[arg-type]
    runs = AppServerRunService(projects, threads, messages, app_server, max_concurrent_runs=2, settings=make_runtime_settings())  # type: ignore[arg-type]

    project_id = projects.create_project(str(project_dir))["id"]
    chat_1 = chats.upsert_chat_index(project_id, "thread_1", "one", "thread_1", utc_now(), utc_now())["id"]
    chat_2 = chats.upsert_chat_index(project_id, "thread_2", "two", "thread_2", utc_now(), utc_now())["id"]

    first = await runs.start_message_run(project_id, chat_1, "hello 1")
    second = await runs.start_message_run(project_id, chat_2, "hello 2")

    assert first["runId"] != second["runId"]
    active = runs.list_run_diagnostics()
    assert sorted(run["chatId"] for run in active) == [chat_1, chat_2]
    db.close()


@pytest.mark.asyncio
async def test_app_server_run_rejects_parallel_same_chat(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    app_server = TurnStartAppServer()
    threads = AppServerThreadService(projects, chats, messages, transcripts, app_server, make_runtime_settings())  # type: ignore[arg-type]
    runs = AppServerRunService(projects, threads, messages, app_server, max_concurrent_runs=2, settings=make_runtime_settings())  # type: ignore[arg-type]

    project_id = projects.create_project(str(project_dir))["id"]
    chat_id = chats.upsert_chat_index(project_id, "thread_1", "one", "thread_1", utc_now(), utc_now())["id"]

    await runs.start_message_run(project_id, chat_id, "hello 1")
    with pytest.raises(AppError) as exc:
        await runs.start_message_run(project_id, chat_id, "hello 2")

    assert exc.value.code == "run_already_active_in_chat"
    db.close()


@pytest.mark.asyncio
async def test_app_server_run_uses_runtime_settings(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    app_server = TurnStartAppServer()
    settings = make_runtime_settings(":workspace", "on-request", "gpt-5-codex")
    threads = AppServerThreadService(projects, chats, messages, transcripts, app_server, settings)  # type: ignore[arg-type]
    runs = AppServerRunService(projects, threads, messages, app_server, max_concurrent_runs=1, settings=settings)  # type: ignore[arg-type]

    project_id = projects.create_project(str(project_dir))["id"]
    chat_id = chats.upsert_chat_index(project_id, "thread_1", "existing", "thread_1", utc_now(), utc_now())["id"]

    await runs.start_message_run(project_id, chat_id, "hello")

    assert app_server.requests[0] == ("thread/settings/update", {"threadId": "thread_1", "approvalPolicy": "on-request", "approvalsReviewer": "user", "permissions": ":workspace", "model": "gpt-5-codex"})
    db.close()


@pytest.mark.asyncio
async def test_app_server_run_uses_chat_specific_runtime_settings(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    app_server = TurnStartAppServer()
    defaults = make_runtime_settings(":workspace", "on-request", "gpt-5-codex")
    threads = AppServerThreadService(projects, chats, messages, transcripts, app_server, defaults)  # type: ignore[arg-type]
    runs = AppServerRunService(projects, threads, messages, app_server, max_concurrent_runs=1, settings=defaults)  # type: ignore[arg-type]

    project_id = projects.create_project(str(project_dir))["id"]
    chat_id = chats.upsert_chat_index(project_id, "thread_1", "existing", "thread_1", utc_now(), utc_now())["id"]
    chats.update_chat_settings(
        project_id,
        chat_id,
        {
            "permission_profile": ":read-only",
            "approval_policy": "never",
            "approvals_reviewer": "user",
            "model": "deepseek-v4-flash",
            "reasoning_effort": "max",
        },
    )

    await runs.start_message_run(project_id, chat_id, "hello")

    assert chats.list_model_reasoning_history()[0] == {
        "model": "deepseek-v4-flash",
        "reasoning_effort": "max",
    }

    assert app_server.requests[0] == (
        "thread/settings/update",
        {
            "threadId": "thread_1",
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "permissions": ":read-only",
            "model": "deepseek-v4-flash",
            "effort": "max",
        },
    )
    db.close()


@pytest.mark.asyncio
async def test_app_server_run_uses_runtime_model_and_approval_policy(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    app_server = TurnStartAppServer()
    settings = make_runtime_settings(":workspace", "on-request", "gpt-5-codex", "high", "auto_review")
    threads = AppServerThreadService(projects, chats, messages, transcripts, app_server, settings)  # type: ignore[arg-type]
    runs = AppServerRunService(projects, threads, messages, app_server, max_concurrent_runs=1, settings=settings)  # type: ignore[arg-type]

    project_id = projects.create_project(str(project_dir))["id"]
    chat_id = chats.upsert_chat_index(project_id, "thread_1", "existing", "thread_1", utc_now(), utc_now())["id"]

    await runs.start_message_run(project_id, chat_id, "hello")

    assert app_server.requests[0] == (
        "thread/settings/update",
        {"threadId": "thread_1", "approvalPolicy": "on-request", "approvalsReviewer": "auto_review", "permissions": ":workspace", "model": "gpt-5-codex", "effort": "high"},
    )
    db.close()


@pytest.mark.asyncio
async def test_app_server_run_routes_each_run_to_selected_provider(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    openai = TurnStartAppServer()
    deepseek = TurnStartAppServer()

    class ProviderPool:
        def client_for_provider(self, provider: str):
            return deepseek if provider == "deepseek" else openai

    settings = make_runtime_settings(model="deepseek-v4-flash")
    threads = AppServerThreadService(projects, chats, messages, transcripts, ProviderPool(), settings)  # type: ignore[arg-type]
    runs = AppServerRunService(projects, threads, messages, ProviderPool(), max_concurrent_runs=2, settings=settings)  # type: ignore[arg-type]

    project_id = projects.create_project(str(project_dir))["id"]
    deepseek_chat = chats.upsert_chat_index(project_id, "deepseek_thread", "deepseek", "deepseek_thread", utc_now(), utc_now())["id"]
    openai_chat = chats.upsert_chat_index(project_id, "openai_thread", "openai", "openai_thread", utc_now(), utc_now())["id"]

    deepseek_result = await runs.start_message_run(project_id, deepseek_chat, "use deepseek")
    settings.model = "gpt-5-codex"
    openai_result = await runs.start_message_run(project_id, openai_chat, "use openai")

    assert deepseek.requests[0][0] == "thread/settings/update"
    assert openai.requests[0][0] == "thread/settings/update"
    assert runs.get_run(deepseek_result["runId"])["provider"] == "deepseek"
    assert runs.get_run(openai_result["runId"])["provider"] == "openai"
    db.close()


@pytest.mark.asyncio
async def test_app_server_run_resumes_not_loaded_thread_before_turn(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    app_server = NotLoadedThreadAppServer()
    threads = AppServerThreadService(projects, chats, messages, transcripts, app_server, make_runtime_settings())  # type: ignore[arg-type]
    runs = AppServerRunService(projects, threads, messages, app_server, max_concurrent_runs=1, settings=make_runtime_settings())  # type: ignore[arg-type]

    project_id = projects.create_project(str(project_dir))["id"]
    chat_id = chats.upsert_chat_index(project_id, "thread_1", "existing", "thread_1", utc_now(), utc_now())["id"]

    await runs.start_message_run(project_id, chat_id, "hello")

    assert app_server.requests == [
        ("thread/settings/update", {"threadId": "thread_1", "approvalPolicy": "never", "approvalsReviewer": "user", "permissions": ":danger-full-access"}),
        ("thread/read", {"threadId": "thread_1"}),
        ("thread/resume", {"threadId": "thread_1"}),
        ("thread/settings/update", {"threadId": "thread_1", "approvalPolicy": "never", "approvalsReviewer": "user", "permissions": ":danger-full-access"}),
        ("turn/start", {"threadId": "thread_1", "cwd": str(project_dir), "input": [{"type": "text", "text": "hello"}]}),
    ]
    db.close()


@pytest.mark.asyncio
async def test_app_server_cancel_treats_already_finished_turn_as_cancelled(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    app_server = InterruptAlreadyFinishedAppServer()
    threads = AppServerThreadService(projects, chats, messages, transcripts, app_server, make_runtime_settings())  # type: ignore[arg-type]
    runs = AppServerRunService(projects, threads, messages, app_server, max_concurrent_runs=1, settings=make_runtime_settings())  # type: ignore[arg-type]

    project_id = projects.create_project(str(project_dir))["id"]
    chat_id = chats.upsert_chat_index(project_id, "thread_1", "existing", "thread_1", utc_now(), utc_now())["id"]
    result = await runs.start_message_run(project_id, chat_id, "hello")

    cancelled = await runs.cancel_run(result["runId"])

    assert cancelled["status"] == "cancelled"
    assert app_server.notifications[-1] == ("turn/interrupt", {"threadId": "thread_1", "turnId": "turn_1"})
    db.close()


@pytest.mark.asyncio
async def test_app_server_cancel_waits_until_thread_is_idle(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    app_server = InterruptThenIdleAppServer()
    threads = AppServerThreadService(projects, chats, messages, transcripts, app_server, make_runtime_settings())  # type: ignore[arg-type]
    runs = AppServerRunService(projects, threads, messages, app_server, max_concurrent_runs=1, settings=make_runtime_settings())  # type: ignore[arg-type]

    project_id = projects.create_project(str(project_dir))["id"]
    chat_id = chats.upsert_chat_index(project_id, "thread_1", "existing", "thread_1", utc_now(), utc_now())["id"]
    result = await runs.start_message_run(project_id, chat_id, "hello")

    cancelled = await runs.cancel_run(result["runId"])

    assert cancelled["status"] == "cancelled"
    assert app_server.thread_read_count == 2
    db.close()


@pytest.mark.asyncio
async def test_app_server_run_can_be_steered(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    image_path = linux_tmp_path / "image.png"
    image_path.write_bytes(b"png")
    note_path = linux_tmp_path / "note.txt"
    note_path.write_text("hello", encoding="utf-8")
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    app_server = TurnStartAppServer()
    threads = AppServerThreadService(projects, chats, messages, transcripts, app_server, make_runtime_settings())  # type: ignore[arg-type]
    runs = AppServerRunService(projects, threads, messages, app_server, max_concurrent_runs=1, settings=make_runtime_settings())  # type: ignore[arg-type]

    project_id = projects.create_project(str(project_dir))["id"]
    chat_id = chats.upsert_chat_index(project_id, "thread_1", "existing", "thread_1", utc_now(), utc_now())["id"]
    result = await runs.start_message_run(project_id, chat_id, "hello")

    steered = await runs.steer_run(
        result["runId"],
        "please continue with more detail",
        [
            {"path": str(image_path), "name": "image.png", "kind": "image"},
            {"path": str(note_path), "name": "note.txt", "kind": "file"},
        ],
    )

    assert steered["status"] == "running"
    assert app_server.requests[-1] == (
        "turn/steer",
        {
            "threadId": "thread_1",
            "expectedTurnId": "turn_1",
            "input": [
                {
                    "type": "text",
                    "text": (
                        "please continue with more detail\n\n"
                        "添付ファイルは次の絶対パスにあります。記載されたファイルを直接確認し、"
                        "同名ファイルをプロジェクト内や他の場所から探さないでください。\n"
                        f"- {note_path.resolve()}"
                    ),
                },
                {"type": "localImage", "path": str(image_path.resolve())},
                {"type": "mention", "name": "note.txt", "path": str(note_path.resolve())},
            ],
        },
    )
    stored_messages = messages.list_messages(project_id, chat_id)
    assert stored_messages[-1]["content"] == f"please continue with more detail\n\nAttachments:\n- image.png: {image_path}\n- note.txt: {note_path}"
    db.close()


@pytest.mark.asyncio
async def test_app_server_run_keeps_active_turn_id_after_steer(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    app_server = SteerReturnsTurnAppServer()
    threads = AppServerThreadService(projects, chats, messages, transcripts, app_server, make_runtime_settings())  # type: ignore[arg-type]
    runs = AppServerRunService(projects, threads, messages, app_server, max_concurrent_runs=1, settings=make_runtime_settings())  # type: ignore[arg-type]

    project_id = projects.create_project(str(project_dir))["id"]
    chat_id = chats.upsert_chat_index(project_id, "thread_1", "existing", "thread_1", utc_now(), utc_now())["id"]
    result = await runs.start_message_run(project_id, chat_id, "hello")
    await runs.steer_run(result["runId"], "please continue")
    await runs.steer_run(result["runId"], "and keep going")
    assert app_server.requests[-1] == (
        "turn/steer",
        {
            "threadId": "thread_1",
            "expectedTurnId": "turn_1",
            "input": [{"type": "text", "text": "and keep going"}],
        },
    )
    await app_server.subscribers[-1].put(AppServerNotification("item/agentMessage/delta", {"threadId": "thread_1", "turnId": "turn_1", "delta": "continued"}))

    events = []
    async for event in runs.stream_events(result["runId"]):
        events.append(event)
        if "continued" in event:
            break

    assert any("continued" in event for event in events)
    assert runs.get_run(result["runId"])["turnId"] == "turn_1"
    db.close()


@pytest.mark.asyncio
async def test_app_server_agent_message_boundary_is_published(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    app_server = TurnStartAppServer()
    threads = AppServerThreadService(projects, chats, messages, transcripts, app_server, make_runtime_settings())  # type: ignore[arg-type]
    runs = AppServerRunService(projects, threads, messages, app_server, max_concurrent_runs=1, settings=make_runtime_settings())  # type: ignore[arg-type]

    project_id = projects.create_project(str(project_dir))["id"]
    chat_id = chats.upsert_chat_index(project_id, "thread_1", "existing", "thread_1", utc_now(), utc_now())["id"]
    result = await runs.start_message_run(project_id, chat_id, "hello")
    await app_server.subscribers[-1].put(AppServerNotification("item/agentMessage", {"threadId": "thread_1", "turnId": "turn_1"}))

    events = []
    async for event in runs.stream_events(result["runId"]):
        events.append(event)
        if "event: progress" in event:
            break

    assert any("item/agentMessage" in event for event in events)
    db.close()


@pytest.mark.asyncio
async def test_app_server_run_steer_failure_does_not_store_message(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    app_server = FailingSteerAppServer()
    threads = AppServerThreadService(projects, chats, messages, transcripts, app_server, make_runtime_settings())  # type: ignore[arg-type]
    runs = AppServerRunService(projects, threads, messages, app_server, max_concurrent_runs=1, settings=make_runtime_settings())  # type: ignore[arg-type]

    project_id = projects.create_project(str(project_dir))["id"]
    chat_id = chats.upsert_chat_index(project_id, "thread_1", "existing", "thread_1", utc_now(), utc_now())["id"]
    result = await runs.start_message_run(project_id, chat_id, "hello")

    with pytest.raises(AppError):
        await runs.steer_run(result["runId"], "this should fail")

    assert app_server.requests[-1][0] == "turn/steer"
    stored_messages = messages.list_messages(project_id, chat_id)
    assert [message["content"] for message in stored_messages] == ["hello"]
    db.close()


@pytest.mark.asyncio
async def test_app_server_steer_starts_next_turn_when_previous_turn_is_already_idle(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    app_server = NoActiveSteerAppServer()
    threads = AppServerThreadService(projects, chats, messages, transcripts, app_server, make_runtime_settings())  # type: ignore[arg-type]
    runs = AppServerRunService(projects, threads, messages, app_server, max_concurrent_runs=1, settings=make_runtime_settings())  # type: ignore[arg-type]

    project_id = projects.create_project(str(project_dir))["id"]
    chat_id = chats.upsert_chat_index(project_id, "thread_1", "existing", "thread_1", utc_now(), utc_now())["id"]
    result = await runs.start_message_run(project_id, chat_id, "hello")

    steered = await runs.steer_run(result["runId"], "continue")

    assert steered["status"] == "running"
    assert steered["turnId"] == "turn_2"
    assert app_server.requests[-1] == (
        "turn/start",
        {"threadId": "thread_1", "cwd": str(project_dir), "input": [{"type": "text", "text": "continue"}]},
    )
    stored_messages = messages.list_messages(project_id, chat_id)
    assert [message["content"] for message in stored_messages] == ["hello", "continue"]
    db.close()


@pytest.mark.asyncio
async def test_app_server_run_sends_attachments_to_turn(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    image_path = linux_tmp_path / "image.png"
    image_path.write_bytes(b"png")
    note_path = linux_tmp_path / "note.txt"
    note_path.write_text("hello", encoding="utf-8")
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    app_server = TurnStartAppServer()
    threads = AppServerThreadService(projects, chats, messages, transcripts, app_server, make_runtime_settings())  # type: ignore[arg-type]
    runs = AppServerRunService(projects, threads, messages, app_server, max_concurrent_runs=1, settings=make_runtime_settings())  # type: ignore[arg-type]

    project_id = projects.create_project(str(project_dir))["id"]
    chat_id = chats.upsert_chat_index(project_id, "thread_1", "existing", "thread_1", utc_now(), utc_now())["id"]

    await runs.start_message_run(
        project_id,
        chat_id,
        "hello",
        [
            {"path": str(image_path), "name": "image.png", "kind": "image"},
            {"path": str(note_path), "name": "note.txt", "kind": "file"},
        ],
    )

    assert app_server.requests == [
        ("thread/settings/update", {"threadId": "thread_1", "approvalPolicy": "never", "approvalsReviewer": "user", "permissions": ":danger-full-access"}),
        (
            "turn/start",
            {
                "threadId": "thread_1",
                "cwd": str(project_dir),
                "input": [
                    {
                        "type": "text",
                        "text": (
                            "hello\n\n"
                            "添付ファイルは次の絶対パスにあります。記載されたファイルを直接確認し、"
                            "同名ファイルをプロジェクト内や他の場所から探さないでください。\n"
                            f"- {note_path.resolve()}"
                        ),
                    },
                    {"type": "localImage", "path": str(image_path.resolve())},
                    {"type": "mention", "name": "note.txt", "path": str(note_path.resolve())},
                ],
            },
        ),
    ]
    db.close()


@pytest.mark.asyncio
async def test_app_server_run_does_not_replace_missing_imported_thread(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats)
    app_server = ReplacingAppServer()
    threads = AppServerThreadService(projects, chats, messages, transcripts, app_server, make_runtime_settings())  # type: ignore[arg-type]
    runs = AppServerRunService(projects, threads, messages, app_server, max_concurrent_runs=1, settings=make_runtime_settings())  # type: ignore[arg-type]

    project_id = projects.create_project(str(project_dir))["id"]
    chat_id = chats.upsert_chat_index(project_id, "old_thread", "imported", "old_thread", utc_now(), utc_now())["id"]

    with pytest.raises(AppError) as exc_info:
        await runs.start_message_run(project_id, chat_id, "hello")

    assert exc_info.value.code == "thread_not_found"
    assert chats.get_chat(project_id, chat_id)["codexSessionId"] == "old_thread"
    assert app_server.requests == [
        ("thread/settings/update", {"threadId": "old_thread", "approvalPolicy": "never", "approvalsReviewer": "user", "permissions": ":danger-full-access"}),
        ("turn/start", {"threadId": "old_thread", "cwd": str(project_dir), "input": [{"type": "text", "text": "hello"}]}),
        ("thread/read", {"threadId": "old_thread"}),
        ("turn/start", {"threadId": "old_thread", "cwd": str(project_dir), "input": [{"type": "text", "text": "hello"}]}),
    ]
    db.close()


@pytest.mark.asyncio
async def test_sse_streams_run_events(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()

    app = create_app(make_test_config(linux_tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        project_id = (await client.post("/projects", json={"path": str(project_dir)})).json()["id"]
        chat_id = (await client.post(f"/projects/{project_id}/chats", json={"title": "sse"})).json()["id"]
        run_id = (await client.post(f"/projects/{project_id}/chats/{chat_id}/messages", json={"content": "stream"})).json()["runId"]

        async with client.stream("GET", f"/runs/{run_id}/events") as response:
            assert response.status_code == 200
            chunks = []
            async for text in response.aiter_text():
                chunks.append(text)
                if "event: done" in "".join(chunks):
                    break
        stream_text = "".join(chunks)
        assert "event: output" in stream_text
        assert "event: done" in stream_text


@pytest.mark.asyncio
async def test_reject_invalid_project_paths(linux_tmp_path: Path) -> None:
    app = create_app(make_test_config(linux_tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        win = await client.post("/projects", json={"path": "C:/Users/user/project"})
        assert win.status_code == 400
        assert win.json()["error"]["code"] == "project_path_invalid"

        missing = await client.post("/projects", json={"path": str(linux_tmp_path / "missing")})
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "project_path_not_directory"


@pytest.mark.asyncio
async def test_allow_mnt_c_project_paths_when_enabled(linux_tmp_path: Path) -> None:
    cfg = make_test_config(linux_tmp_path)
    cfg = cfg.__class__(**(cfg.__dict__ | {"allow_mnt_c_projects": True}))
    mnt_c_dir = Path("/mnt/c")
    if not mnt_c_dir.is_dir():
        pytest.skip("Windows drive mount is not available")

    app = create_app(cfg)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/projects", json={"path": str(mnt_c_dir), "name": "windows home"})
        assert response.status_code == 200
        assert response.json()["path"] == str(mnt_c_dir.resolve())


@pytest.mark.asyncio
async def test_readonly_files_api(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "README.md").write_text("# Title\n", encoding="utf-8")
    (project_dir / "bin.dat").write_bytes(b"\x00\x01")
    (project_dir / "pixel.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (project_dir / "photo.jpg").write_bytes(b"\xff\xd8\xff")
    (project_dir / "manual.pdf").write_bytes(b"%PDF-1.7\n")
    (project_dir / "large.pdf").write_bytes(b"%PDF-1.7\n" + b"0" * (1024 * 1024 + 1))
    (project_dir / "report.docx").write_bytes(b"PK\x03\x04")
    (project_dir / "book.xlsx").write_bytes(b"PK\x03\x04")

    app = create_app(make_test_config(linux_tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        project = (await client.post("/projects", json={"path": str(project_dir)})).json()
        project_id = project["id"]

        listing = await client.get(f"/projects/{project_id}/files")
        assert listing.status_code == 200
        names = {entry["name"]: entry for entry in listing.json()["entries"]}
        assert names["README.md"]["viewerKind"] == "markdown"
        assert names["bin.dat"]["viewerKind"] == "binary"
        assert names["pixel.png"]["viewerKind"] == "png"
        assert names["photo.jpg"]["viewerKind"] == "jpeg"
        assert names["manual.pdf"]["viewerKind"] == "pdf"
        assert names["large.pdf"]["viewerKind"] == "pdf"
        assert names["report.docx"]["viewerKind"] == "word"
        assert names["book.xlsx"]["viewerKind"] == "excel"

        content = await client.get(f"/projects/{project_id}/files/content", params={"path": "README.md"})
        assert content.status_code == 200
        assert content.json()["content"] == "# Title\n"

        escape = await client.get(f"/projects/{project_id}/files/content", params={"path": "../secret"})
        assert escape.status_code == 400
        assert escape.json()["error"]["code"] == "file_path_invalid"

        pdf_preview = await client.get(f"/projects/{project_id}/files/content", params={"path": "large.pdf"})
        assert pdf_preview.status_code == 415
        assert pdf_preview.json()["error"]["code"] == "file_not_previewable"


@pytest.mark.asyncio
async def test_files_api_rejects_symlink(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    outside = linux_tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    (project_dir / "link.txt").symlink_to(outside)

    app = create_app(make_test_config(linux_tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        project_id = (await client.post("/projects", json={"path": str(project_dir)})).json()["id"]
        response = await client.get(f"/projects/{project_id}/files/content", params={"path": "link.txt"})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "file_path_invalid"


@pytest.mark.asyncio
async def test_validation_errors_use_error_envelope(linux_tmp_path: Path) -> None:
    app = create_app(make_test_config(linux_tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/projects", json={"name": "missing path"})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.asyncio
async def test_project_candidates_are_loaded_from_codex_state(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    newer_project_dir = linux_tmp_path / "newer-project"
    newer_project_dir.mkdir()
    missing_dir = linux_tmp_path / "missing"
    cfg = make_test_config(linux_tmp_path)
    sessions = cfg.codex_home / "sessions" / "2026" / "06" / "27"
    sessions.mkdir(parents=True)
    existing = sessions / "rollout-existing.jsonl"
    newer = sessions / "rollout-newer.jsonl"
    missing = sessions / "rollout-missing.jsonl"
    existing.write_text("", encoding="utf-8")
    newer.write_text("", encoding="utf-8")
    missing.write_text("", encoding="utf-8")
    write_codex_state_db(
        cfg,
        [
            ("thr_1", str(project_dir), "Implement the project index", str(existing), 0, 1_782_518_400_000, 1_782_518_400_000, None),
            ("thr_newer", str(newer_project_dir), "Newer project task", str(newer), 0, 1_782_518_400_000, 1_782_525_600_000, None),
            ("thr_2", str(missing_dir), "Missing", str(missing), 0, 1_782_522_000_000, 1_782_522_000_000, None),
        ],
    )

    app = create_app(cfg)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        candidates = (await client.get("/project-candidates")).json()
        assert candidates == [
            {"path": str(newer_project_dir), "name": "newer-project", "threadCount": 1, "lastUsedAt": "2026-06-27T02:00:00Z"},
            {"path": str(project_dir), "name": "project", "threadCount": 1, "lastUsedAt": "2026-06-27T00:00:00Z"},
        ]

        imported = (await client.post("/project-candidates/import")).json()
        assert [project["path"] for project in imported] == [str(newer_project_dir), str(project_dir)]

        project = next(item for item in imported if item["path"] == str(project_dir))
        chats = (await client.get(f"/projects/{project['id']}/chats")).json()
        assert [(chat["id"], chat["title"]) for chat in chats] == [("thr_1", "Implement the project index")]

        after_import = (await client.get("/project-candidates")).json()
        assert after_import == []


@pytest.mark.asyncio
async def test_project_candidates_import_accepts_selected_paths(linux_tmp_path: Path) -> None:
    first_dir = linux_tmp_path / "first"
    second_dir = linux_tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    sessions = cfg.codex_home / "sessions"
    sessions.mkdir(parents=True)
    first = sessions / "first.jsonl"
    second = sessions / "second.jsonl"
    first.write_text("", encoding="utf-8")
    second.write_text("", encoding="utf-8")
    write_codex_state_db(
        cfg,
        [
            ("thr_1", str(first_dir), "First", str(first), 0, 1_782_518_400_000, 1_782_518_400_000, None),
            ("thr_2", str(second_dir), "Second", str(second), 0, 1_782_522_000_000, 1_782_522_000_000, None),
        ],
    )

    app = create_app(cfg)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        imported = (await client.post("/project-candidates/import", json={"paths": [str(first_dir)]})).json()
        assert [project["path"] for project in imported] == [str(first_dir)]
        chats = (await client.get(f"/projects/{imported[0]['id']}/chats")).json()
        assert [(chat["id"], chat["title"]) for chat in chats] == [("thr_1", "First")]

        candidates = (await client.get("/project-candidates")).json()
        assert [candidate["path"] for candidate in candidates] == [str(second_dir)]


@pytest.mark.asyncio
async def test_project_candidates_ignore_archived_sessions(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "archived-project"
    project_dir.mkdir()
    archived = linux_tmp_path / "codex-home" / "archived_sessions"
    archived.mkdir(parents=True)
    (archived / "archived.jsonl").write_text(
        json_line({"type": "session_meta", "payload": {"id": "thr_archived", "cwd": str(project_dir), "timestamp": "2026-06-27T00:00:00Z"}}),
        encoding="utf-8",
    )

    app = create_app(make_test_config(linux_tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        candidates = (await client.get("/project-candidates")).json()
        assert candidates == []


@pytest.mark.asyncio
async def test_project_candidates_use_configured_codex_home_only(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "windows-project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    configured_codex_home = linux_tmp_path / "windows-home" / ".codex"
    cfg = cfg.__class__(**(cfg.__dict__ | {"codex_home": configured_codex_home}))
    sessions = configured_codex_home / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "rollout-windows-home.jsonl").write_text(
        "",
        encoding="utf-8",
    )
    ignored_sessions = linux_tmp_path / "ignored-home" / ".codex" / "sessions"
    ignored_sessions.mkdir(parents=True)
    (ignored_sessions / "ignored.jsonl").write_text(
        json_line({"type": "session_meta", "payload": {"id": "thr_ignored", "cwd": str(project_dir), "timestamp": "2026-06-28T00:00:00Z"}}),
        encoding="utf-8",
    )
    write_codex_state_db(
        cfg,
        [("thr_1", str(project_dir), "Windows project", str(sessions / "rollout-windows-home.jsonl"), 0, 1_782_518_400_000, 1_782_518_400_000, None)],
    )

    app = create_app(cfg)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        candidates = (await client.get("/project-candidates")).json()
        assert candidates == [{"path": str(project_dir), "name": "windows-project", "threadCount": 1, "lastUsedAt": "2026-06-27T00:00:00Z"}]


def test_codex_state_threads_are_indexed_and_archived_threads_are_hidden(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    active_transcript = cfg.codex_home / "sessions" / "active.jsonl"
    archived_transcript = cfg.codex_home / "archived_sessions" / "archived.jsonl"
    active_transcript.parent.mkdir(parents=True)
    archived_transcript.parent.mkdir(parents=True)
    active_transcript.write_text(
        json_line({"type": "session_meta", "payload": {"id": "thr_active", "cwd": str(project_dir), "timestamp": "2026-06-27T00:00:00Z"}}),
        encoding="utf-8",
    )
    archived_transcript.write_text(
        json_line({"type": "session_meta", "payload": {"id": "thr_archived", "cwd": str(project_dir), "timestamp": "2026-06-26T00:00:00Z"}}),
        encoding="utf-8",
    )
    write_codex_state_db(
        cfg,
        [
            ("thr_active", str(project_dir), "Active from DB", str(active_transcript), 0, 1_782_550_800_000, 1_782_550_801_000, None),
            ("thr_archived", str(project_dir), "Archived from DB", str(archived_transcript), 1, 1_782_400_000_000, 1_782_400_001_000, 1_782_400_002_000),
        ],
    )
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    transcripts = TranscriptImportService(cfg, projects, chats, CodexStateService(cfg))

    candidates = transcripts.list_project_candidates()
    assert candidates == [{"path": str(project_dir), "name": "project", "threadCount": 1, "lastUsedAt": "2026-06-27T09:00:01Z"}]

    project = projects.create_project(str(project_dir))
    transcripts.index_project(project)

    visible = chats.list_chats(project["id"])
    all_rows = db.fetchall("SELECT id, title, archived_at, transcript_path, can_continue FROM chats WHERE project_id = ? ORDER BY id", (project["id"],))
    assert [(chat["id"], chat["title"], chat["canContinue"]) for chat in visible] == [("thr_active", "Active from DB", True)]
    assert [(row["id"], row["archived_at"] is not None, row["can_continue"]) for row in all_rows] == [("thr_active", False, 1)]
    assert all_rows[0]["transcript_path"] == str(active_transcript.resolve())
    db.close()


def test_codex_state_sync_hides_guardian_sessions_only(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    sessions = cfg.codex_home / "sessions"
    sessions.mkdir(parents=True)
    guardian_transcript = sessions / "guardian.jsonl"
    worker_transcript = sessions / "worker.jsonl"
    guardian_transcript.write_text(
        json_line(
            {
                "type": "session_meta",
                "payload": {
                    "id": "thr_guardian",
                    "cwd": str(project_dir),
                    "source": {"subagent": {"other": "guardian"}},
                },
            }
        ),
        encoding="utf-8",
    )
    worker_transcript.write_text(
        json_line(
            {
                "type": "session_meta",
                "payload": {
                    "id": "thr_worker",
                    "cwd": str(project_dir),
                    "source": {"subagent": {"other": "worker"}},
                },
            }
        ),
        encoding="utf-8",
    )
    write_codex_state_db(
        cfg,
        [
            ("thr_guardian", str(project_dir), "Approval review", str(guardian_transcript), 0, 1_782_550_800_000, 1_782_550_801_000, None),
            ("thr_worker", str(project_dir), "Worker task", str(worker_transcript), 0, 1_782_550_800_000, 1_782_550_801_000, None),
        ],
    )
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    transcripts = TranscriptImportService(cfg, projects, chats, CodexStateService(cfg))
    project = projects.create_project(str(project_dir))
    chats.upsert_chat_index(
        project["id"],
        "thr_guardian",
        "Previously imported guardian",
        "thr_guardian",
        "2026-06-26T00:00:00Z",
        "2026-06-26T00:00:00Z",
        str(guardian_transcript),
    )

    transcripts.index_project(project)

    visible = chats.list_chats(project["id"])
    assert [(chat["id"], chat["title"]) for chat in visible] == [("thr_worker", "Worker task")]
    guardian = db.fetchone("SELECT archived_at FROM chats WHERE id = ?", ("thr_guardian",))
    assert guardian is not None and guardian["archived_at"] is not None
    db.close()


def test_codex_state_sync_ignores_jsonl_only_sessions_when_db_is_available(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    active_transcript = cfg.codex_home / "sessions" / "active.jsonl"
    old_transcript = cfg.codex_home / "sessions" / "old-jsonl-only.jsonl"
    active_transcript.parent.mkdir(parents=True)
    active_transcript.write_text(
        json_line({"type": "session_meta", "payload": {"id": "thr_active", "cwd": str(project_dir), "timestamp": "2026-06-27T00:00:00Z"}}),
        encoding="utf-8",
    )
    old_transcript.write_text(
        json_line({"type": "session_meta", "payload": {"id": "thr_old_jsonl", "cwd": str(project_dir), "timestamp": "2026-06-26T00:00:00Z"}}),
        encoding="utf-8",
    )
    write_codex_state_db(
        cfg,
        [("thr_active", str(project_dir), "Active from DB", str(active_transcript), 0, 1_782_550_800_000, 1_782_550_801_000, None)],
    )
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    transcripts = TranscriptImportService(cfg, projects, chats, CodexStateService(cfg))

    project = projects.create_project(str(project_dir))
    transcripts.index_project(project)

    visible = chats.list_chats(project["id"])
    assert [chat["id"] for chat in visible] == ["thr_active"]
    db.close()


def test_codex_state_sync_archives_stale_imported_rows(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    active_transcript = cfg.codex_home / "sessions" / "active.jsonl"
    active_transcript.parent.mkdir(parents=True)
    active_transcript.write_text(
        json_line({"type": "session_meta", "payload": {"id": "thr_active", "cwd": str(project_dir), "timestamp": "2026-06-27T00:00:00Z"}}),
        encoding="utf-8",
    )
    write_codex_state_db(
        cfg,
        [("thr_active", str(project_dir), "Active from DB", str(active_transcript), 0, 1_782_550_800_000, 1_782_550_801_000, None)],
    )
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    transcripts = TranscriptImportService(cfg, projects, chats, CodexStateService(cfg))
    project = projects.create_project(str(project_dir))
    chats.upsert_chat_index(
        project["id"],
        "thr_old_jsonl",
        "old imported",
        "thr_old_jsonl",
        "2026-06-26T00:00:00Z",
        "2026-06-26T00:00:00Z",
        str(cfg.codex_home / "sessions" / "old.jsonl"),
        None,
        False,
        False,
        "imported",
    )

    transcripts.index_project(project)

    visible = chats.list_chats(project["id"])
    assert [chat["id"] for chat in visible] == ["thr_active"]
    stale = db.fetchone("SELECT archived_at FROM chats WHERE id = ?", ("thr_old_jsonl",))
    assert stale is not None and stale["archived_at"] is not None
    db.close()


def test_codex_state_sync_archives_stale_continuable_rows(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    active_transcript = cfg.codex_home / "sessions" / "active.jsonl"
    active_transcript.parent.mkdir(parents=True)
    active_transcript.write_text(
        json_line({"type": "session_meta", "payload": {"id": "thr_active", "cwd": str(project_dir), "timestamp": "2026-06-27T00:00:00Z"}}),
        encoding="utf-8",
    )
    write_codex_state_db(
        cfg,
        [("thr_active", str(project_dir), "Active from DB", str(active_transcript), 0, 1_782_550_800_000, 1_782_550_801_000, None)],
    )
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    transcripts = TranscriptImportService(cfg, projects, chats, CodexStateService(cfg))
    project = projects.create_project(str(project_dir))
    chats.upsert_chat_index(
        project["id"],
        "thr_stale",
        "stale continuable",
        "thr_stale",
        "2026-06-26T00:00:00Z",
        "2026-06-26T00:00:00Z",
        str(cfg.codex_home / "sessions" / "stale.jsonl"),
        None,
        False,
        True,
        None,
    )

    transcripts.index_project(project)

    visible = chats.list_chats(project["id"])
    assert [chat["id"] for chat in visible] == ["thr_active"]
    stale = db.fetchone("SELECT archived_at FROM chats WHERE id = ?", ("thr_stale",))
    assert stale is not None and stale["archived_at"] is not None
    db.close()


def test_codex_state_sync_does_not_archive_when_current_home_has_no_project_sessions(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    write_codex_state_db(cfg, [])
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    transcripts = TranscriptImportService(cfg, projects, chats, CodexStateService(cfg))
    project = projects.create_project(str(project_dir))
    chats.upsert_chat_index(
        project["id"],
        "thr_existing",
        "existing continuable",
        "thr_existing",
        "2026-06-26T00:00:00Z",
        "2026-06-26T00:00:00Z",
        str(cfg.codex_home / "sessions" / "existing.jsonl"),
        None,
        False,
        True,
        None,
    )

    transcripts.index_project(project)

    existing = db.fetchone("SELECT archived_at FROM chats WHERE id = ?", ("thr_existing",))
    assert existing is not None and existing["archived_at"] is None
    assert [chat["id"] for chat in chats.list_chats(project["id"])] == ["thr_existing"]
    db.close()


def test_codex_state_sync_restores_active_thread_after_local_stale_archive(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    active_transcript = cfg.codex_home / "sessions" / "active.jsonl"
    active_transcript.parent.mkdir(parents=True)
    active_transcript.write_text(
        json_line({"type": "session_meta", "payload": {"id": "thr_active", "cwd": str(project_dir), "timestamp": "2026-06-27T00:00:00Z"}}),
        encoding="utf-8",
    )
    write_codex_state_db(
        cfg,
        [("thr_active", str(project_dir), "Active from DB", str(active_transcript), 0, 1_782_550_800_000, 1_782_550_801_000, None)],
    )
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    transcripts = TranscriptImportService(cfg, projects, chats, CodexStateService(cfg))
    project = projects.create_project(str(project_dir))
    chats.upsert_chat_index(
        project["id"],
        "thr_active",
        "active but locally archived",
        "thr_active",
        "2026-06-26T00:00:00Z",
        "2026-06-26T00:00:00Z",
        str(active_transcript),
        "2026-06-28T00:00:00Z",
        True,
        True,
        None,
    )

    transcripts.index_project(project)

    restored = db.fetchone("SELECT title, archived_at FROM chats WHERE id = ?", ("thr_active",))
    assert restored is not None
    assert restored["title"] == "Active from DB"
    assert restored["archived_at"] is None
    assert [chat["id"] for chat in chats.list_chats(project["id"])] == ["thr_active"]
    db.close()


def test_codex_state_thread_without_transcript_is_hidden_from_chat_list(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    write_codex_state_db(
        cfg,
        [("thr_no_transcript", str(project_dir), "Missing rollout", str(cfg.codex_home / "sessions" / "missing.jsonl"), 0, 1_782_550_800_000, 1_782_550_801_000, None)],
    )
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    transcripts = TranscriptImportService(cfg, projects, chats, CodexStateService(cfg))

    project = projects.create_project(str(project_dir))
    transcripts.index_project(project)

    visible = chats.list_chats(project["id"])
    all_rows = db.fetchall("SELECT id, can_continue FROM chats WHERE project_id = ?", (project["id"],))
    assert visible == []
    assert all_rows == []
    db.close()


def test_codex_state_uses_preview_for_generic_titles(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    transcript = cfg.codex_home / "sessions" / "thread.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("", encoding="utf-8")
    cfg.codex_sqlite_home.mkdir(parents=True)
    conn = sqlite3.connect(cfg.codex_sqlite_home / "state_5.sqlite")
    conn.execute(
        """
        CREATE TABLE threads(
          id TEXT PRIMARY KEY,
          cwd TEXT NOT NULL,
          title TEXT,
          preview TEXT,
          archived INTEGER NOT NULL,
          rollout_path TEXT,
          created_at_ms INTEGER,
          updated_at_ms INTEGER,
          recency_at_ms INTEGER
        )
        """
    )
    conn.execute(
        """
        INSERT INTO threads(id, cwd, title, preview, rollout_path, archived, created_at_ms, updated_at_ms, recency_at_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("thr_preview", str(project_dir), "New Chat", "適切な復元タイトル", str(transcript), 0, 1_782_486_000_000, 1_782_486_001_000, 1_782_486_001_000),
    )
    conn.commit()
    conn.close()
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    transcripts = TranscriptImportService(cfg, projects, chats, CodexStateService(cfg))

    project = projects.create_project(str(project_dir))
    transcripts.index_project(project)

    visible = chats.list_chats(project["id"])
    assert [(chat["id"], chat["title"]) for chat in visible] == [("thr_preview", "適切な復元タイトル")]
    db.close()


def test_codex_state_uses_preview_when_title_matches_first_user_message(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    transcript = cfg.codex_home / "sessions" / "thread.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("", encoding="utf-8")
    cfg.codex_sqlite_home.mkdir(parents=True)
    conn = sqlite3.connect(cfg.codex_sqlite_home / "state_5.sqlite")
    conn.execute(
        """
        CREATE TABLE threads(
          id TEXT PRIMARY KEY,
          cwd TEXT NOT NULL,
          title TEXT,
          preview TEXT,
          first_user_message TEXT,
          archived INTEGER NOT NULL,
          rollout_path TEXT,
          created_at_ms INTEGER,
          updated_at_ms INTEGER,
          recency_at_ms INTEGER
        )
        """
    )
    conn.execute(
        """
        INSERT INTO threads(id, cwd, title, preview, first_user_message, rollout_path, archived, created_at_ms, updated_at_ms, recency_at_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("thr_first_user", str(project_dir), "この長い依頼を処理してください", "短い復元タイトル", "この長い依頼を処理してください", str(transcript), 0, 1_782_486_000_000, 1_782_486_001_000, 1_782_486_001_000),
    )
    conn.commit()
    conn.close()
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    transcripts = TranscriptImportService(cfg, projects, chats, CodexStateService(cfg))

    project = projects.create_project(str(project_dir))
    transcripts.index_project(project)

    visible = chats.list_chats(project["id"])
    assert [(chat["id"], chat["title"]) for chat in visible] == [("thr_first_user", "短い復元タイトル")]
    db.close()


@pytest.mark.asyncio
async def test_app_server_list_chats_syncs_codex_state(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    transcript = cfg.codex_home / "sessions" / "thread.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json_line({"type": "session_meta", "payload": {"id": "thr_from_db", "cwd": str(project_dir), "timestamp": "2026-06-27T00:00:00Z"}}),
        encoding="utf-8",
    )
    write_codex_state_db(
        cfg,
        [("thr_from_db", str(project_dir), "Synced from DB\nwith a very long title that should be collapsed before it reaches the project tree", str(transcript), 0, 1_782_486_000_000, 1_782_486_001_000, None)],
    )
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats, CodexStateService(cfg))
    service = AppServerThreadService(projects, chats, messages, transcripts, RecordingAppServer(), make_runtime_settings())  # type: ignore[arg-type]

    project = projects.create_project(str(project_dir))
    cached = await service.list_chats(project["id"])
    listed = await service.list_chats(project["id"], sync=True)

    assert cached == []
    assert [(chat["id"], chat["title"], chat["canContinue"]) for chat in listed] == [("thr_from_db", "Synced from DB with a very long title that should be collapsed before it reac...", True)]
    db.close()


def test_codex_state_sync_restores_locally_archived_active_thread(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    transcript = cfg.codex_home / "sessions" / "thread.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json_line({"type": "session_meta", "payload": {"id": "thr_local_archived", "cwd": str(project_dir), "timestamp": "2026-06-27T00:00:00Z"}}),
        encoding="utf-8",
    )
    write_codex_state_db(
        cfg,
        [("thr_local_archived", str(project_dir), "Active in Codex DB", str(transcript), 0, 1_782_486_000_000, 1_782_486_001_000, None)],
    )
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    transcripts = TranscriptImportService(cfg, projects, chats, CodexStateService(cfg))

    project = projects.create_project(str(project_dir))
    chats.upsert_chat_index(project["id"], "thr_local_archived", "archived locally", "thr_local_archived", utc_now(), utc_now())
    chats.archive_chat(project["id"], "thr_local_archived")
    transcripts.index_project(project)

    assert [chat["id"] for chat in chats.list_chats(project["id"])] == ["thr_local_archived"]
    row = chats.get_chat_row(project["id"], "thr_local_archived")
    assert row["archived_at"] is None
    db.close()


def test_codex_state_sync_skips_jsonl_scan_when_db_service_is_configured(linux_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    transcript = cfg.codex_home / "sessions" / "thread.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json_line({"type": "session_meta", "payload": {"id": "thr_from_db", "cwd": str(project_dir), "timestamp": "2026-06-27T00:00:00Z"}}),
        encoding="utf-8",
    )
    write_codex_state_db(
        cfg,
        [("thr_from_db", str(project_dir), "From DB", str(transcript), 0, 1_782_486_000_000, 1_782_486_001_000, None)],
    )
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    transcripts = TranscriptImportService(cfg, projects, chats, CodexStateService(cfg))
    monkeypatch.setattr(transcripts, "_transcript_files", lambda: (_ for _ in ()).throw(AssertionError("unexpected jsonl scan")))

    project = projects.create_project(str(project_dir))
    transcripts.index_project(project)

    assert [chat["id"] for chat in chats.list_chats(project["id"])] == ["thr_from_db"]
    db.close()


def test_codex_state_sync_does_not_fall_back_to_jsonl_when_db_is_missing(linux_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    sessions = cfg.codex_home / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "jsonl_only.jsonl").write_text(
        json_line({"type": "session_meta", "payload": {"id": "thr_jsonl_only", "cwd": str(project_dir), "timestamp": "2026-06-27T00:00:00Z"}}),
        encoding="utf-8",
    )
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    transcripts = TranscriptImportService(cfg, projects, chats, CodexStateService(cfg))
    monkeypatch.setattr(transcripts, "_transcript_files", lambda: (_ for _ in ()).throw(AssertionError("unexpected jsonl scan")))

    project = projects.create_project(str(project_dir))
    transcripts.index_project(project)

    assert chats.list_chats(project["id"]) == []
    assert transcripts.codex_state is not None
    assert transcripts.codex_state.diagnostics()["ok"] is False
    db.close()


@pytest.mark.asyncio
async def test_configured_codex_home_threads_are_continuable(linux_tmp_path: Path) -> None:
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    cfg = make_test_config(linux_tmp_path)
    configured_codex_home = linux_tmp_path / "windows-home" / ".codex"
    cfg = cfg.__class__(**(cfg.__dict__ | {"codex_home": configured_codex_home}))
    transcript = configured_codex_home / "sessions" / "thread.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json_line({"type": "session_meta", "payload": {"id": "thr_windows", "cwd": str(project_dir), "timestamp": "2026-06-27T00:00:00Z"}}),
        encoding="utf-8",
    )
    write_codex_state_db(
        cfg,
        [("thr_windows", str(project_dir), "Windows thread", str(transcript), 0, 1_782_486_000_000, 1_782_486_001_000, None)],
    )
    db = Database(cfg.database_path)
    db.migrate()
    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    transcripts = TranscriptImportService(cfg, projects, chats, CodexStateService(cfg))
    app_server = TurnStartAppServer()
    threads = AppServerThreadService(projects, chats, messages, transcripts, app_server, make_runtime_settings())  # type: ignore[arg-type]
    runs = AppServerRunService(projects, threads, messages, app_server, max_concurrent_runs=1, settings=make_runtime_settings())  # type: ignore[arg-type]

    project = projects.create_project(str(project_dir))
    listed = await threads.list_chats(project["id"], sync=True)

    assert listed[0]["canContinue"] is True
    await runs.start_message_run(project["id"], "thr_windows", "hello")
    assert app_server.requests[-1] == ("turn/start", {"threadId": "thr_windows", "cwd": str(project_dir), "input": [{"type": "text", "text": "hello"}]})
    db.close()


def test_codex_state_schema_mismatch_is_reported_without_crashing(linux_tmp_path: Path) -> None:
    cfg = make_test_config(linux_tmp_path)
    cfg.codex_sqlite_home.mkdir(parents=True)
    db_path = cfg.codex_sqlite_home / "state_5.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE threads(id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    state = CodexStateService(cfg)

    assert state.list_threads() == []
    diagnostics = state.diagnostics()
    assert diagnostics["ok"] is False
    assert diagnostics["path"] == str(db_path.resolve())
    assert "missing columns" in diagnostics["error"]


def json_line(value: dict) -> str:
    return json.dumps(value) + "\n"


def write_codex_state_db(cfg: Config, rows: list[tuple[str, str, str, str, int, int, int, int | None]]) -> None:
    cfg.codex_sqlite_home.mkdir(parents=True)
    conn = sqlite3.connect(cfg.codex_sqlite_home / "state_5.sqlite")
    conn.execute(
        """
        CREATE TABLE threads(
          id TEXT PRIMARY KEY,
          cwd TEXT NOT NULL,
          title TEXT,
          archived INTEGER NOT NULL,
          archived_at_ms INTEGER,
          rollout_path TEXT,
          created_at_ms INTEGER,
          updated_at_ms INTEGER,
          recency_at_ms INTEGER
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO threads(id, cwd, title, rollout_path, archived, created_at_ms, updated_at_ms, recency_at_ms, archived_at_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [(thread_id, cwd, title, rollout_path, archived, created_at_ms, updated_at_ms, updated_at_ms, archived_at_ms) for thread_id, cwd, title, rollout_path, archived, created_at_ms, updated_at_ms, archived_at_ms in rows],
    )
    conn.commit()
    conn.close()


class RecordingAppServer:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []

    async def request(self, method: str, params: dict) -> dict:
        self.requests.append((method, params))
        return {}


class MissingArchiveAppServer:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []

    async def request(self, method: str, params: dict) -> dict:
        self.requests.append((method, params))
        if method == "thread/archive":
            raise AppError("app_server_error", f"thread not found: {params['threadId']}", 502)
        return {}


class FailingArchiveAppServer:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []

    async def request(self, method: str, params: dict) -> dict:
        self.requests.append((method, params))
        if method == "thread/archive":
            raise AppError("app_server_error", "archive failed", 502)
        return {}


class FailingNameAppServer:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []

    async def request(self, method: str, params: dict) -> dict:
        self.requests.append((method, params))
        if method == "thread/name/set":
            raise AppError("app_server_error", "rename failed", 502)
        return {}


class UsageAppServer:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []

    async def request(self, method: str, params: dict | None = None) -> dict:
        params = params or {}
        self.requests.append((method, params))
        return {
            "rateLimits": {
                "limitId": "codex",
                "limitName": None,
                "primary": {"usedPercent": 40, "windowDurationMins": 300, "resetsAt": 1782892800},
                "secondary": {"usedPercent": 89, "windowDurationMins": 10080, "resetsAt": 1783411200},
                "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
                "individualLimit": None,
                "planType": "prolite",
                "rateLimitReachedType": None,
            },
            "rateLimitResetCredits": {"availableCount": 1},
        }


class TurnStartAppServer:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []
        self.notifications: list[tuple[str, dict]] = []
        self.subscribers = []

    async def ensure_started(self) -> None:
        return None

    def subscribe_queue(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self.subscribers.append(queue)
        return queue

    def unsubscribe_queue(self, queue: asyncio.Queue) -> None:
        if queue in self.subscribers:
            self.subscribers.remove(queue)

    async def request(self, method: str, params: dict) -> dict:
        self.requests.append((method, params))
        if method == "turn/start":
            return {"turn": {"id": "turn_1"}}
        return {}

    async def notify(self, method: str, params: dict) -> None:
        self.notifications.append((method, params))


class FailingSteerAppServer(TurnStartAppServer):
    async def request(self, method: str, params: dict) -> dict:
        self.requests.append((method, params))
        if method == "turn/start":
            return {"turn": {"id": "turn_1"}}
        if method == "turn/steer":
            raise AppError("app_server_error", "steering was rejected", 502)
        return {}


class NoActiveSteerAppServer(TurnStartAppServer):
    def __init__(self) -> None:
        super().__init__()
        self.turn_start_count = 0

    async def request(self, method: str, params: dict) -> dict:
        self.requests.append((method, params))
        if method == "turn/start":
            self.turn_start_count += 1
            if self.turn_start_count == 2:
                await self.subscribers[-1].put(
                    AppServerNotification(
                        "turn/completed",
                        {"threadId": "thread_1", "turn": {"id": "turn_1", "status": "completed"}},
                    )
                )
                await asyncio.sleep(0)
            return {"turn": {"id": f"turn_{self.turn_start_count}"}}
        if method == "turn/steer":
            raise AppError("app_server_error", "no active turn to steer", 502)
        return {}


class SteerReturnsTurnAppServer(TurnStartAppServer):
    async def request(self, method: str, params: dict) -> dict:
        self.requests.append((method, params))
        if method == "turn/start":
            return {"turn": {"id": "turn_1"}}
        if method == "turn/steer":
            return {"turn": {"id": "turn_2"}}
        return {}


class NotLoadedThreadAppServer(TurnStartAppServer):
    def __init__(self) -> None:
        super().__init__()
        self.loaded = False

    async def request(self, method: str, params: dict, timeout: float | None = None) -> dict:  # type: ignore[override]
        self.requests.append((method, params))
        if method == "thread/settings/update" and not self.loaded:
            raise AppError("app_server_error", f"thread not found: {params['threadId']}", 502)
        if method == "thread/read":
            return {"thread": {"id": params["threadId"], "status": {"type": "notLoaded"}}}
        if method == "thread/resume":
            self.loaded = True
            return {"thread": {"id": params["threadId"], "status": {"type": "loaded"}}}
        if method == "turn/start":
            return {"turn": {"id": "turn_1"}}
        return {}


class InterruptAlreadyFinishedAppServer(TurnStartAppServer):
    async def notify(self, method: str, params: dict) -> None:
        self.notifications.append((method, params))
        if method == "turn/interrupt":
            raise AppError("app_server_error", "no active turn to interrupt", 502)


class InterruptThenIdleAppServer(TurnStartAppServer):
    def __init__(self) -> None:
        super().__init__()
        self.thread_read_count = 0

    async def request(self, method: str, params: dict) -> dict:
        self.requests.append((method, params))
        if method == "turn/start":
            return {"turn": {"id": "turn_1"}}
        if method == "thread/read":
            self.thread_read_count += 1
            status_type = "active" if self.thread_read_count == 1 else "idle"
            status = {"type": status_type}
            if status_type == "active":
                status["activeFlags"] = []
            return {"thread": {"id": params["threadId"], "status": status}}
        return {}


class ReplacingAppServer:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []
        self.subscribers = []

    async def ensure_started(self) -> None:
        return None

    def subscribe_queue(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self.subscribers.append(queue)
        return queue

    def unsubscribe_queue(self, queue: asyncio.Queue) -> None:
        if queue in self.subscribers:
            self.subscribers.remove(queue)

    async def request(self, method: str, params: dict) -> dict:
        self.requests.append((method, params))
        if method == "turn/start" and params["threadId"] == "old_thread":
            raise AppError("app_server_error", "thread not found: old_thread", 502)
        if method == "thread/start":
            return {"thread": {"id": "new_thread"}}
        if method == "turn/start":
            return {"turn": {"id": "turn_new"}}
        return {}


def test_startup_recovers_stale_runs(linux_tmp_path: Path) -> None:
    cfg = make_test_config(linux_tmp_path)
    db = Database(cfg.database_path)
    db.migrate()
    now = utc_now()
    project_id = new_id("prj")
    chat_id = new_id("cht")
    run_id = new_id("run")
    project_dir = linux_tmp_path / "project"
    project_dir.mkdir()
    db.execute(
        "INSERT INTO projects(id, name, path, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (project_id, "demo", str(project_dir), now, now),
    )
    db.execute(
        "INSERT INTO chats(id, project_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (chat_id, project_id, "demo", now, now),
    )
    db.execute("INSERT INTO runs(id, chat_id, status) VALUES (?, ?, 'running')", (run_id, chat_id))
    db.close()

    create_app(cfg)

    db = Database(cfg.database_path)
    row = db.fetchone("SELECT status, error FROM runs WHERE id = ?", (run_id,))
    assert row is not None
    assert row["status"] == "failed"
    assert row["error"] == "daemon started while this run was not completed"
    db.close()
