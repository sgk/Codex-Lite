from __future__ import annotations

import asyncio
import json
import re
import shlex
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .app_server import AppServerClient, AppServerNotification
from .errors import AppError
from .run_service import EventHub
from .services import ChatService, MessageService, ProjectService
from .transcript_import import TranscriptImportService
from .util.ids import new_id
from .util.time import utc_now


@dataclass
class AppServerRuntimeSettings:
    permission_profile: str
    approval_policy: str
    model: str


class AppServerThreadService:
    def __init__(self, projects: ProjectService, chats: ChatService, messages: MessageService, transcripts: TranscriptImportService, app_server: AppServerClient, settings: AppServerRuntimeSettings) -> None:
        self.projects = projects
        self.chats = chats
        self.messages = messages
        self.transcripts = transcripts
        self.app_server = app_server
        self.settings = settings

    async def list_chats(self, project_id: str, sync: bool = False) -> list[dict]:
        project = self.projects.get_project_row(project_id)
        if sync:
            self.transcripts.index_project(project)
        return self.chats.list_chats(project_id)

    async def get_chat(self, project_id: str, chat_id: str) -> dict:
        return self.chats.get_chat(project_id, chat_id)

    async def create_chat(self, project_id: str, title: str | None = None) -> dict:
        project = self.projects.get_project_row(project_id)
        response = await self.app_server.request("thread/start", {"cwd": project["path"]})
        thread = response["thread"]
        await _apply_codex_lite_thread_settings(self.app_server, thread["id"], self.settings)
        clean_title = (title or "New Chat").strip() or "New Chat"
        await self.app_server.request("thread/name/set", {"threadId": thread["id"], "name": clean_title})
        chat = _chat_out(project_id, thread) | {"title": clean_title}
        return self.chats.upsert_chat_index(
            project_id,
            chat["id"],
            chat["title"],
            chat["codexSessionId"],
            chat["createdAt"],
            chat["updatedAt"],
        )

    async def update_chat(self, project_id: str, chat_id: str, title: str | None) -> dict:
        chat = self.chats.get_chat_row(project_id, chat_id)
        clean_title = (title or "").strip()
        if not clean_title:
            raise AppError("validation_error", "Chat title must not be empty.")
        updated = self.chats.update_chat(project_id, chat_id, clean_title)
        thread_id = str(chat.get("codex_session_id") or chat_id)
        try:
            await self.app_server.request("thread/name/set", {"threadId": thread_id, "name": clean_title})
        except AppError:
            pass
        return updated

    async def archive_chat(self, project_id: str, chat_id: str) -> dict:
        chat = self.chats.get_chat_row(project_id, chat_id)
        for candidate_thread_id in _candidate_thread_ids(chat_id, chat.get("codex_session_id")):
            try:
                await self.app_server.request("thread/archive", {"threadId": candidate_thread_id})
                break
            except AppError as exc:
                if _is_thread_not_found(exc):
                    continue
                # Codex Lite hides the row locally when app-server cannot
                # archive it. A later Codex metadata sync may restore it if the
                # Codex state DB still reports the thread as active.
                continue
        # Imported JSONL sessions may no longer be known to app-server. They
        # still need to disappear from Codex Lite's active list.
        return self.chats.archive_chat(project_id, chat_id)

    async def delete_chat(self, project_id: str, chat_id: str) -> None:
        # Codex app surfaces archive as the normal way to remove a thread from
        # active lists. Keep this endpoint local-only instead of invoking
        # app-server's permanent delete operation.
        self.chats.get_chat_row(project_id, chat_id)
        self.chats.delete_chat(project_id, chat_id)

    async def list_messages(self, project_id: str, chat_id: str) -> list[dict]:
        project = self.projects.get_project_row(project_id)
        chat = self.chats.get_chat_row(project_id, chat_id)
        session_ids = [chat_id]
        codex_session_id = chat.get("codex_session_id")
        if codex_session_id and codex_session_id not in session_ids:
            session_ids.append(str(codex_session_id))
        transcript_messages = [
            message
            for session_id in session_ids
            for message in self._list_transcript_messages(project_id, project["path"], chat_id, session_id, chat.get("transcript_path"))
        ]
        local_messages = [
            message
            for message in self.messages.list_messages(project_id, chat_id)
            if str(message.get("role") or "").lower() != "assistant"
        ]
        return _merge_messages(transcript_messages, local_messages)

    def _list_transcript_messages(self, project_id: str, project_path: str, chat_id: str, session_id: str, transcript_path: Any) -> list[dict]:
        saved_path = str(transcript_path) if transcript_path else None
        resolved_path = self.transcripts.find_transcript_path(project_path, session_id, saved_path)
        if resolved_path is None:
            return []
        if saved_path != str(resolved_path):
            self.chats.update_chat_transcript_path(project_id, chat_id, str(resolved_path))
        return self.transcripts.list_messages(project_path, session_id, chat_id, str(resolved_path))


class AppServerUsageService:
    def __init__(self, app_server: AppServerClient) -> None:
        self.app_server = app_server

    async def read_capacity(self) -> dict:
        response = await self.app_server.request("account/rateLimits/read")
        rate_limits = response.get("rateLimits")
        if not isinstance(rate_limits, dict):
            raise AppError("usage_unavailable", "Codex usage capacity was not available.", 502)
        primary = _usage_window(rate_limits.get("primary"))
        secondary = _usage_window(rate_limits.get("secondary"))
        return {
            "fiveHour": _window_for_minutes([primary, secondary], 300),
            "weekly": _window_for_minutes([primary, secondary], 10080),
            "planType": rate_limits.get("planType") if isinstance(rate_limits.get("planType"), str) else None,
            "rateLimitReachedType": rate_limits.get("rateLimitReachedType") if isinstance(rate_limits.get("rateLimitReachedType"), str) else None,
            "resetCredits": _reset_credits(response.get("rateLimitResetCredits")),
            "fetchedAt": utc_now(),
        }


@dataclass
class AppServerActiveRun:
    thread_id: str
    turn_id: str
    chat_id: str
    status: str = "running"
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    reconnect_count: int = 0
    last_reconnect_at: str | None = None
    last_reconnect_message: str | None = None


class AppServerRunService:
    def __init__(self, projects: ProjectService, threads: AppServerThreadService, messages: MessageService, app_server: AppServerClient, max_concurrent_runs: int, settings: AppServerRuntimeSettings) -> None:
        self.projects = projects
        self.threads = threads
        self.chats = threads.chats
        self.messages = messages
        self.app_server = app_server
        self.max_concurrent_runs = max_concurrent_runs
        self.settings = settings
        self.runs: dict[str, AppServerActiveRun] = {}
        self.events = EventHub()

    async def start_message_run(self, project_id: str, chat_id: str, content: str, attachments: list[dict[str, Any]] | None = None) -> dict:
        active_count = sum(1 for run in self.runs.values() if run.status == "running")
        if active_count >= self.max_concurrent_runs:
            raise AppError("run_already_active", "Another run is already active.", 409)
        project = self.projects.get_project_row(project_id)
        chat = self.chats.get_chat_row(project_id, chat_id)
        if any(run.chat_id == chat_id and run.status == "running" for run in self.runs.values()):
            raise AppError("run_already_active_in_chat", "Another run is already active in this chat.", 409)
        if not bool(chat.get("can_continue", 1)):
            raise AppError("chat_read_only", str(chat.get("continue_disabled_reason") or "This imported Codex history cannot be continued by Codex Lite."), 409)
        input_items = _build_turn_input(content, attachments or [])
        run_id = new_id("run")
        user_message = self.messages.insert_message(chat_id, "user", _content_with_attachment_summary(content, attachments or []), run_id=run_id, kind="instruction")
        await self.app_server.ensure_started()
        notification_queue = self.app_server.subscribe_queue()
        try:
            response = None
            thread_id = ""
            last_thread_not_found: AppError | None = None
            for candidate_thread_id in _candidate_thread_ids(chat_id, chat.get("codex_session_id")):
                try:
                    response = await self._start_turn(candidate_thread_id, project["path"], input_items)
                    thread_id = candidate_thread_id
                    break
                except AppError as exc:
                    if not _is_thread_not_found(exc):
                        raise
                    last_thread_not_found = exc
            if response is None:
                if last_thread_not_found is None:
                    raise AppError("thread_not_found", "No thread id candidate was available.", 404)
                raise AppError(
                    "thread_not_found",
                    "Codex thread was not found by app-server. No replacement session was created.",
                    404,
                ) from last_thread_not_found
        except Exception:
            self.app_server.unsubscribe_queue(notification_queue)
            raise
        turn_id = _turn_id_from_response(response)
        if not turn_id:
            raise AppError("app_server_error", "turn/start did not return a turn id.", 502)
        self.runs[run_id] = AppServerActiveRun(thread_id, turn_id, chat_id, started_at=utc_now())
        await self.events.publish(run_id, "status", {"status": "running"})
        asyncio.create_task(self._watch_run(run_id, notification_queue))
        return {"messageId": user_message["id"], "runId": run_id}

    async def _start_turn(self, thread_id: str, project_path: str, input_items: list[dict[str, Any]]) -> dict:
        try:
            await _apply_codex_lite_thread_settings(self.app_server, thread_id, self.settings)
        except AppError as exc:
            if not _is_thread_not_found(exc):
                raise
            await self._ensure_thread_loaded(thread_id)
            await _apply_codex_lite_thread_settings(self.app_server, thread_id, self.settings)
        try:
            return await self.app_server.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "cwd": project_path,
                    "input": input_items,
                },
            )
        except AppError as exc:
            if not _is_thread_not_found(exc):
                raise
            await self._ensure_thread_loaded(thread_id)
            return await self.app_server.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "cwd": project_path,
                    "input": input_items,
                },
            )

    async def _ensure_thread_loaded(self, thread_id: str) -> None:
        response = await self.app_server.request("thread/read", {"threadId": thread_id})
        thread = response.get("thread") or {}
        status = thread.get("status") if isinstance(thread, dict) else {}
        if isinstance(status, dict) and status.get("type") == "notLoaded":
            await self.app_server.request("thread/resume", {"threadId": thread_id}, timeout=120)

    def get_run(self, run_id: str) -> dict:
        run = self.runs.get(run_id)
        if run is None:
            raise AppError("run_not_found", "Run was not found.", 404)
        return _run_out(run_id, run)

    def list_run_diagnostics(self) -> list[dict[str, Any]]:
        return [
            {
                "id": run_id,
                "chatId": run.chat_id,
                "threadId": run.thread_id,
                "turnId": run.turn_id,
                "status": run.status,
                "startedAt": run.started_at,
                "finishedAt": run.finished_at,
                "error": run.error,
                "reconnectCount": run.reconnect_count,
                "lastReconnectAt": run.last_reconnect_at,
                "lastReconnectMessage": run.last_reconnect_message,
            }
            for run_id, run in self.runs.items()
        ]

    async def cancel_run(self, run_id: str) -> dict:
        run = self.runs.get(run_id)
        if run is None:
            raise AppError("cancel_failed", "Run is not active.", 409)
        if run.status != "running":
            return _run_out(run_id, run)
        try:
            await self.app_server.notify("turn/interrupt", {"threadId": run.thread_id, "turnId": run.turn_id})
        except AppError as exc:
            await self.events.publish(run_id, "progress", {"method": "turn/interrupt", "summary": exc.message})
        run.status = "cancelled"
        run.finished_at = utc_now()
        await self.events.publish(run_id, "done", {"status": "cancelled", "exitCode": None})
        return _run_out(run_id, run)

    async def steer_run(self, run_id: str, content: str, attachments: list[dict[str, Any]] | None = None) -> dict:
        run = self.runs.get(run_id)
        if run is None:
            raise AppError("steer_failed", "Run is not active.", 409)
        if run.status != "running":
            raise AppError("steer_failed", "Run is not running.", 409)
        clean_content = content.strip()
        if not clean_content:
            raise AppError("validation_error", "Steer content must not be empty.", 400)
        input_items = _build_turn_input(clean_content, attachments or [])
        await self.events.publish(run_id, "progress", {"method": "turn/steer", "summary": "sending additional instructions"})
        try:
            response = await self.app_server.request(
                "turn/steer",
                {
                    "threadId": run.thread_id,
                    "expectedTurnId": run.turn_id,
                    "input": input_items,
                },
            )
        except AppError as exc:
            await self.events.publish(run_id, "progress", {"method": "turn/steer", "summary": exc.message})
            raise
        steered_turn_id = _turn_id_from_response(response)
        if steered_turn_id:
            run.turn_id = steered_turn_id
        self.messages.insert_message(run.chat_id, "user", _content_with_attachment_summary(clean_content, attachments or []), run_id=run_id, kind="instruction")
        await self.events.publish(run_id, "progress", {"method": "turn/steer", "summary": "additional instructions sent"})
        return _run_out(run_id, run)

    async def stream_events(self, run_id: str) -> AsyncIterator[str]:
        if run_id not in self.runs:
            raise AppError("run_not_found", "Run was not found.", 404)
        async for item in self.events.subscribe(run_id):
            yield f"event: {item['event']}\ndata: {json.dumps(item['data'], ensure_ascii=False)}\n\n"

    async def _watch_run(self, run_id: str, notification_queue: asyncio.Queue[AppServerNotification]) -> None:
        run = self.runs[run_id]
        assistant_parts: list[str] = []
        try:
            while True:
                notification = await notification_queue.get()
                if not _notification_matches(notification, run):
                    continue
                if run.status != "running":
                    return
                if notification.method == "item/agentMessage/delta":
                    delta = str(notification.params.get("delta") or "")
                    assistant_parts.append(delta)
                    await self.events.publish(run_id, "output", {"stream": "stdout", "text": delta})
                elif notification.method.startswith("item/agentMessage"):
                    await self.events.publish(
                        run_id,
                        "progress",
                        {
                            "method": notification.method,
                            "summary": _notification_summary(notification),
                        },
                    )
                elif notification.method.startswith("item/") or notification.method in {
                    "exec_command_begin",
                    "exec_command_output_delta",
                    "exec_command_end",
                    "mcp_tool_call_begin",
                    "mcp_tool_call_end",
                    "apply_patch_begin",
                    "apply_patch_updated",
                    "apply_patch_end",
                    "thread/settings/applied",
                }:
                    await self.events.publish(
                        run_id,
                        "progress",
                        {
                            "method": notification.method,
                            "summary": _notification_summary(notification),
                        },
                    )
                elif notification.method == "turn/completed":
                    turn = notification.params.get("turn") or {}
                    status = str(turn.get("status") or "succeeded")
                    run.status = "succeeded" if status == "completed" else status
                    run.finished_at = utc_now()
                    if assistant_parts:
                        self.messages.insert_message(run.chat_id, "assistant", "".join(assistant_parts), run_id=run_id, kind="conclusion")
                    await self.events.publish(run_id, "done", {"status": run.status, "exitCode": 0 if run.status == "succeeded" else None})
                    return
                elif notification.method == "error":
                    if _is_retryable_app_server_error(notification.params):
                        summary = _retryable_error_summary(notification.params)
                        run.reconnect_count += 1
                        run.last_reconnect_at = utc_now()
                        run.last_reconnect_message = summary
                        await self.events.publish(
                            run_id,
                            "progress",
                            {
                                "method": "app_server/reconnecting",
                                "summary": summary,
                            },
                        )
                        continue
                    run.status = "failed"
                    run.error = str(notification.params)
                    run.finished_at = utc_now()
                    await self.events.publish(run_id, "error", {"code": "app_server_error", "message": run.error})
                    return
        except Exception as exc:
            run.status = "failed"
            run.error = str(exc)
            run.finished_at = utc_now()
            await self.events.publish(run_id, "error", {"code": "app_server_run_failed", "message": str(exc)})
        finally:
            self.app_server.unsubscribe_queue(notification_queue)


def _notification_matches(notification: AppServerNotification, run: AppServerActiveRun) -> bool:
    params = notification.params
    if params.get("threadId") != run.thread_id:
        return False
    if notification.method == "turn/completed":
        turn = params.get("turn") or {}
        return turn.get("id") == run.turn_id
    return params.get("turnId") == run.turn_id


def _turn_id_from_response(response: dict[str, Any]) -> str | None:
    turn = response.get("turn")
    if isinstance(turn, dict):
        turn_id = turn.get("id")
        if isinstance(turn_id, str) and turn_id:
            return turn_id
    for key in ("turnId", "turn_id"):
        turn_id = response.get(key)
        if isinstance(turn_id, str) and turn_id:
            return turn_id
    return None


def _notification_summary(notification: AppServerNotification) -> str:
    params = notification.params
    method = notification.method
    file_action = _file_action(method, params)
    if file_action is not None:
        active_text, completed_text, is_active = file_action
        action = active_text if is_active else completed_text
        path = _file_path(params)
        if path:
            return f"ファイルを{action}: {_short_text(path)}"
        return f"ファイルを{action}"
    if method == "exec_command_begin":
        command = _command_text(params)
        return f"コマンドを開始しました: {_short_text(command)}" if command else "コマンドを開始しました"
    if method == "exec_command_output_delta":
        stream = _first_string(params, ("stream", "channel"))
        return f"コマンド出力: {stream}" if stream else "コマンド出力"
    if method == "exec_command_end":
        exit_code = params.get("exitCode") if "exitCode" in params else params.get("exit_code")
        if exit_code is not None:
            return f"コマンドが終了しました: exit {exit_code}"
        return "コマンドが終了しました"
    if method == "mcp_tool_call_begin":
        name = _tool_name(params)
        return f"ツールを開始しました: {name}" if name else "ツールを開始しました"
    if method == "mcp_tool_call_end":
        name = _tool_name(params)
        return f"ツールが終了しました: {name}" if name else "ツールが終了しました"
    if method.startswith("apply_patch"):
        return "ファイルを編集しています"
    for key in ("title", "status", "message", "name", "summary"):
        value = params.get(key)
        if isinstance(value, str) and value:
            return value
    item = params.get("item")
    if isinstance(item, dict):
        for key in ("title", "status", "name", "type"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
    return notification.method


def _is_retryable_app_server_error(params: dict[str, Any]) -> bool:
    if params.get("willRetry") is True:
        return True
    error = params.get("error")
    if isinstance(error, dict) and error.get("willRetry") is True:
        return True
    return False


def _retryable_error_summary(params: dict[str, Any]) -> str:
    message = ""
    error = params.get("error")
    if isinstance(error, dict):
        value = error.get("message")
        if isinstance(value, str):
            message = value
    if not message:
        value = params.get("message")
        if isinstance(value, str):
            message = value
    if message:
        return f"再接続待機中: {_short_text(message)}"
    return "再接続待機中"


def _first_string(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return _redact_sensitive(value)
        if isinstance(value, list) and value:
            return _redact_sensitive(" ".join(str(part) for part in value))
        if isinstance(value, dict):
            nested = _first_string(value, keys)
            if nested:
                return nested
    for container_key in ("item", "arguments", "args", "input", "params", "payload"):
        value = data.get(container_key)
        if isinstance(value, dict):
            nested = _first_string(value, keys)
            if nested:
                return nested
        if isinstance(value, str):
            nested = _json_string_value(value, keys)
            if nested:
                return _redact_sensitive(nested)
    return None


def _command_text(data: dict[str, Any]) -> str | None:
    for container in _command_containers(data):
        combined = _command_from_container(container)
        if combined:
            return _redact_sensitive(combined)
    return None


def _command_containers(data: dict[str, Any]) -> list[dict[str, Any]]:
    containers: list[dict[str, Any]] = [data]
    for key in ("command", "arguments", "args", "input", "params", "payload", "item"):
        value = data.get(key)
        if isinstance(value, dict):
            containers.append(value)
        elif isinstance(value, str):
            parsed = _json_object(value)
            if parsed is not None:
                containers.append(parsed)
    return containers


def _command_from_container(data: dict[str, Any]) -> str | None:
    command_value = data.get("command")
    if isinstance(command_value, str) and command_value.strip():
        return command_value
    if isinstance(command_value, list):
        return _join_command_parts(command_value)
    if isinstance(command_value, dict):
        nested = _command_from_container(command_value)
        if nested:
            return nested

    cmd = data.get("cmd")
    args = data.get("args")
    argv = data.get("argv")
    if isinstance(cmd, str) and cmd.strip():
        if isinstance(args, list) and args:
            return _join_command_parts([cmd, *args])
        if isinstance(argv, list) and argv:
            return _join_command_parts([cmd, *argv])
        return cmd
    if isinstance(args, list) and args:
        return _join_command_parts(args)
    if isinstance(argv, list) and argv:
        return _join_command_parts(argv)
    return None


def _join_command_parts(parts: list[Any]) -> str:
    return " ".join(shlex.quote(str(part)) for part in parts if str(part))


def _json_object(value: str) -> dict[str, Any] | None:
    text = value.strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _json_string_value(value: str, keys: tuple[str, ...]) -> str | None:
    text = value.strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return _first_string(parsed, keys)
    return None


def _file_action(method: str, params: dict[str, Any]) -> tuple[str, str, bool] | None:
    token = " ".join(part for part in [method, _tool_name(params) or "", _item_type(params) or ""]).lower()
    if any(value in token for value in ("exec_command", "command")):
        return None
    is_active = any(value in method for value in ("begin", "delta", "updated", "start"))
    if any(value in token for value in ("apply_patch", "write_file", "edit_file", "replace_file", "patch_file", "file/write", "file/edit", "filesystem.write", "filesystem.edit")):
        return ("編集しています", "編集しました", is_active)
    if any(value in token for value in ("read_file", "open_file", "file/read", "filesystem.read", "filesystem.open")):
        return ("読み取っています", "読み取りました", is_active)
    return None


def _file_path(params: dict[str, Any]) -> str | None:
    path = _first_string(params, ("path", "file", "filePath", "filepath", "target", "target_file"))
    if path:
        return path
    item = params.get("item")
    if isinstance(item, dict):
        return _first_string(item, ("path", "file", "filePath", "filepath", "target", "target_file"))
    return None


def _item_type(params: dict[str, Any]) -> str | None:
    item = params.get("item")
    if isinstance(item, dict):
        value = item.get("type")
        if isinstance(value, str) and value:
            return value
    return None


def _tool_name(params: dict[str, Any]) -> str | None:
    name = _first_string(params, ("name", "toolName", "tool_name"))
    if name:
        return _short_text(name)
    item = params.get("item")
    if isinstance(item, dict):
        return _first_string(item, ("name", "toolName", "tool_name", "title"))
    return None


def _redact_sensitive(value: str) -> str:
    authorization_redacted = re.sub(
        r"(?i)\bauthorization\b\s*[:=]\s*(bearer\s+)?[A-Za-z0-9._~+/=-]{6,}",
        "Authorization=<redacted>",
        value,
    )
    bearer_redacted = re.sub(r"(?i)(bearer|token)\s+[A-Za-z0-9._~+/=-]{16,}", r"\1 <redacted>", authorization_redacted)
    return re.sub(
        r'(?i)\b(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|cookie|password|secret)\b\s*[:=]\s*([^\s,;\'"]+)',
        r"\1=<redacted>",
        bearer_redacted,
    )


def _short_text(value: str, limit: int = 96) -> str:
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit - 1]}..."


def _is_thread_not_found(exc: AppError) -> bool:
    message = exc.message.lower()
    return "thread not found" in message or ("not found" in message and "thread" in message)


def _candidate_thread_ids(chat_id: str, codex_session_id: Any) -> list[str]:
    candidates = [chat_id]
    if codex_session_id:
        value = str(codex_session_id)
        if value not in candidates:
            candidates.append(value)
    return candidates


def _build_turn_input(content: str, attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    input_items: list[dict[str, Any]] = [{"type": "text", "text": content}]
    file_paths: list[Path] = []
    for attachment in attachments:
        path = _validated_attachment_path(attachment)
        name = str(attachment.get("name") or path.name)
        kind = str(attachment.get("kind") or "file")
        if kind == "image" or _looks_like_image(path):
            input_items.append({"type": "localImage", "path": str(path)})
        else:
            input_items.append({"type": "mention", "name": name, "path": str(path)})
            file_paths.append(path)
    if file_paths:
        input_items[0]["text"] = _content_with_direct_attachment_instruction(content, file_paths)
    return input_items


def _content_with_direct_attachment_instruction(content: str, file_paths: list[Path]) -> str:
    lines = [
        content.rstrip(),
        "",
        "添付ファイルは次の絶対パスにあります。記載されたファイルを直接確認し、同名ファイルをプロジェクト内や他の場所から探さないでください。",
    ]
    lines.extend(f"- {path}" for path in file_paths)
    return "\n".join(lines)


def _validated_attachment_path(attachment: dict[str, Any]) -> Path:
    value = attachment.get("path")
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise AppError("attachment_invalid", "Attachment path is invalid.", 400)
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise AppError("attachment_invalid", "Attachment path must be absolute.", 400)
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise AppError("attachment_not_found", "Attachment path could not be resolved.", 404) from exc
    if not resolved.is_file():
        raise AppError("attachment_not_found", "Attachment file was not found.", 404)
    return resolved


def _looks_like_image(path: Path) -> bool:
    return path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def _content_with_attachment_summary(content: str, attachments: list[dict[str, Any]]) -> str:
    if not attachments:
        return content
    lines = [content.rstrip(), "", "Attachments:"]
    for attachment in attachments:
        name = str(attachment.get("name") or Path(str(attachment.get("path") or "")).name or "attachment")
        path = str(attachment.get("path") or "")
        if _is_clipboard_attachment_name(name):
            lines.append(f"- {name}")
        else:
            lines.append(f"- {name}: {path}")
    return "\n".join(lines)


def _is_clipboard_attachment_name(name: str) -> bool:
    value = name.lower()
    return value.startswith("clipboard-") or value.startswith("codex-clipboard-")


async def _apply_codex_lite_thread_settings(app_server: AppServerClient, thread_id: str, settings: AppServerRuntimeSettings) -> None:
    params: dict[str, Any] = {
        "threadId": thread_id,
        "approvalPolicy": settings.approval_policy,
        "permissions": settings.permission_profile,
    }
    if settings.model:
        params["model"] = settings.model
    await app_server.request("thread/settings/update", params)


def _chat_out(project_id: str, thread: dict[str, Any]) -> dict:
    return {
        "id": thread["id"],
        "projectId": project_id,
        "title": thread.get("name") or thread.get("preview") or "New Chat",
        "codexSessionId": thread.get("sessionId"),
        "createdAt": _iso_from_epoch(thread.get("createdAt")),
        "updatedAt": _iso_from_epoch(thread.get("updatedAt") or thread.get("recencyAt") or thread.get("createdAt")),
    }


def _merge_messages(transcript_messages: list[dict], local_messages: list[dict]) -> list[dict]:
    seen_ids: set[str] = set()
    merged_with_source: list[tuple[dict, str]] = []
    for message, source in [*((message, "transcript") for message in transcript_messages), *((message, "local") for message in local_messages)]:
        message_id = str(message.get("id") or "")
        if message_id and message_id in seen_ids:
            continue
        if message_id:
            seen_ids.add(message_id)
        merged_with_source.append((message, source))

    sorted_messages = sorted(merged_with_source, key=lambda item: _message_sort_key(item[0]))
    deduped: list[tuple[dict, str]] = []
    for message, source in sorted_messages:
        if deduped and _is_same_adjacent_message(deduped[-1][0], message):
            _, previous_source = deduped[-1]
            if previous_source == "local" and source == "transcript":
                deduped[-1] = (message, source)
            continue
        deduped.append((message, source))
    return [message for message, _ in deduped]


def _is_same_adjacent_message(previous: dict, current: dict) -> bool:
    previous_role = str(previous.get("role") or "").strip().lower()
    current_role = str(current.get("role") or "").strip().lower()
    if not previous_role or previous_role != current_role:
        return False
    if _message_content_key(str(previous.get("content") or "")) != _message_content_key(str(current.get("content") or "")):
        return False
    return _message_time_slot(previous) == _message_time_slot(current)


def _message_content_key(content: str) -> str:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.strip()


def _message_time_slot(message: dict) -> datetime:
    return _message_timestamp(message).replace(microsecond=0)


def _message_sort_key(message: dict) -> tuple[datetime, str]:
    return (_message_timestamp(message), str(message.get("id") or ""))


def _message_timestamp(message: dict) -> datetime:
    value = str(message.get("createdAt") or "")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _run_out(run_id: str, run: AppServerActiveRun) -> dict:
    return {
        "id": run_id,
        "chatId": run.chat_id,
        "threadId": run.thread_id,
        "turnId": run.turn_id,
        "status": run.status,
        "pid": None,
        "exitCode": 0 if run.status == "succeeded" else None,
        "startedAt": run.started_at,
        "finishedAt": run.finished_at,
        "logPath": None,
        "error": run.error,
        "reconnectCount": run.reconnect_count,
        "lastReconnectAt": run.last_reconnect_at,
        "lastReconnectMessage": run.last_reconnect_message,
    }


def _iso_from_epoch(value: Any) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, str) and value:
        return value
    return utc_now()


def _usage_window(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    used_percent = _number(value.get("usedPercent"))
    window_minutes = _number(value.get("windowDurationMins"))
    resets_at = _iso_from_epoch(value.get("resetsAt")) if value.get("resetsAt") is not None else None
    if used_percent is None or window_minutes is None:
        return None
    used = max(0.0, min(100.0, used_percent))
    return {
        "usedPercent": used,
        "remainingPercent": max(0.0, 100.0 - used),
        "windowMinutes": int(window_minutes),
        "resetsAt": resets_at,
    }


def _window_for_minutes(windows: list[dict | None], minutes: int) -> dict | None:
    for window in windows:
        if window is not None and abs(int(window["windowMinutes"]) - minutes) <= 1:
            return window
    return None


def _reset_credits(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    available_count = value.get("availableCount")
    return {"availableCount": int(available_count)} if isinstance(available_count, int) else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None
