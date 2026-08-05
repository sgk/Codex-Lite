from __future__ import annotations

import asyncio
import json
import re
import shlex
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .app_server import SERVER_REQUEST_ID_KEY, AppServerClient, AppServerNotification
from .deepseek import DEEPSEEK_PROVIDER, model_provider_for_model, read_deepseek_balance
from .errors import AppError
from .provider_context import PROVIDER_CONTEXT_PREFIX
from .run_service import EventHub
from .services import ChatService, MessageService, ProjectService
from .transcript_import import TranscriptImportService
from .util.ids import new_id
from .util.time import utc_now


REASONING_PROGRESS_BATCH_SECONDS = 0.15
REASONING_PROGRESS_MAX_BATCH_CHARS = 4096
CANCEL_IDLE_WAIT_SECONDS = 15.0
CANCEL_IDLE_POLL_SECONDS = 0.1
RUN_STATE_RECONCILE_SECONDS = 5.0
PROVIDER_CONTEXT_MAX_CHARS = 32000
APPROVAL_REQUEST_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
}
APPROVAL_DECISIONS = {"accept", "acceptForSession", "decline", "cancel"}


@dataclass
class AppServerRuntimeSettings:
    permission_profile: str
    approval_policy: str
    model: str
    reasoning_effort: str = ""
    approvals_reviewer: str = "user"


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

    async def create_chat(self, project_id: str, title: str | None = None, settings: AppServerRuntimeSettings | None = None) -> dict:
        project = self.projects.get_project_row(project_id)
        runtime_settings = replace(settings or self.settings)
        app_server = _app_server_for_provider(self.app_server, model_provider_for_model(runtime_settings.model))
        start_params: dict[str, Any] = {"cwd": project["path"]}
        if runtime_settings.model:
            start_params["model"] = runtime_settings.model
            provider = model_provider_for_model(runtime_settings.model)
            if provider == DEEPSEEK_PROVIDER:
                start_params["modelProvider"] = provider
        response = await app_server.request("thread/start", start_params)
        thread = response["thread"]
        await _apply_codex_lite_thread_settings(app_server, thread["id"], runtime_settings)
        clean_title = (title or "New Chat").strip() or "New Chat"
        await app_server.request("thread/name/set", {"threadId": thread["id"], "name": clean_title})
        chat = _chat_out(project_id, thread) | {"title": clean_title}
        result = self.chats.upsert_chat_index(
            project_id,
            chat["id"],
            chat["title"],
            chat["codexSessionId"],
            chat["createdAt"],
            chat["updatedAt"],
        )
        self.chats.update_chat_settings(project_id, result["id"], _runtime_settings_values(runtime_settings))
        self.chats.upsert_provider_thread(
            project_id,
            result["id"],
            model_provider_for_model(runtime_settings.model),
            str(thread["id"]),
            history_initialized=False,
        )
        return result

    async def update_chat(self, project_id: str, chat_id: str, title: str | None) -> dict:
        chat = self.chats.get_chat_row(project_id, chat_id)
        clean_title = (title or "").strip()
        if not clean_title:
            raise AppError("validation_error", "Chat title must not be empty.")
        updated = self.chats.update_chat(project_id, chat_id, clean_title)
        provider_threads = self.chats.list_provider_threads(project_id, chat_id)
        if provider_threads:
            for provider_thread in provider_threads:
                app_server = _app_server_for_provider(self.app_server, str(provider_thread["provider"]))
                try:
                    await app_server.request("thread/name/set", {"threadId": str(provider_thread["thread_id"]), "name": clean_title})
                except AppError:
                    pass
        else:
            thread_id = str(chat.get("codex_session_id") or chat_id)
            runtime_settings = self.settings_for_chat(project_id, chat_id)
            app_server = _app_server_for_provider(self.app_server, model_provider_for_model(runtime_settings.model))
            try:
                await app_server.request("thread/name/set", {"threadId": thread_id, "name": clean_title})
            except AppError:
                pass
        return updated

    async def archive_chat(self, project_id: str, chat_id: str) -> dict:
        chat = self.chats.get_chat_row(project_id, chat_id)
        provider_threads = self.chats.list_provider_threads(project_id, chat_id)
        if provider_threads:
            archive_targets = [(str(item["provider"]), str(item["thread_id"])) for item in provider_threads]
        else:
            runtime_settings = self.settings_for_chat(project_id, chat_id)
            archive_targets = [(model_provider_for_model(runtime_settings.model), candidate) for candidate in _candidate_thread_ids(chat_id, chat.get("codex_session_id"))]
        for provider, candidate_thread_id in archive_targets:
            app_server = _app_server_for_provider(self.app_server, provider)
            try:
                await app_server.request("thread/archive", {"threadId": candidate_thread_id})
            except AppError:
                # Codex Lite hides the row locally when app-server cannot
                # archive it. A later metadata sync may restore it if needed.
                continue
        # Imported JSONL sessions may no longer be known to app-server. They
        # still need to disappear from Codex Lite's active list.
        return self.chats.archive_chat(project_id, chat_id)

    def settings_for_chat(self, project_id: str, chat_id: str) -> AppServerRuntimeSettings:
        stored = self.chats.get_chat_settings_row(project_id, chat_id)
        settings = replace(
            self.settings,
            permission_profile=stored.get("permission_profile") or self.settings.permission_profile,
            approval_policy=stored.get("approval_policy") or self.settings.approval_policy,
            approvals_reviewer=stored.get("approvals_reviewer") or self.settings.approvals_reviewer,
            model=stored.get("model") or self.settings.model,
            reasoning_effort=stored.get("reasoning_effort") or self.settings.reasoning_effort,
        )
        if any(value is None for value in stored.values()):
            self.chats.update_chat_settings(project_id, chat_id, _runtime_settings_values(settings))
        return settings

    async def delete_chat(self, project_id: str, chat_id: str) -> None:
        # Codex app surfaces archive as the normal way to remove a thread from
        # active lists. Keep this endpoint local-only instead of invoking
        # app-server's permanent delete operation.
        self.chats.get_chat_row(project_id, chat_id)
        self.chats.delete_chat(project_id, chat_id)

    async def list_messages(self, project_id: str, chat_id: str) -> list[dict]:
        project = self.projects.get_project_row(project_id)
        chat = self.chats.get_chat_row(project_id, chat_id)
        provider_threads = self.chats.list_provider_threads(project_id, chat_id)
        transcript_messages: list[dict] = []
        if provider_threads:
            for provider_thread in provider_threads:
                transcript_messages.extend(
                    self._list_transcript_messages(
                        project_id,
                        project["path"],
                        chat_id,
                        str(provider_thread["thread_id"]),
                        provider_thread.get("transcript_path"),
                        str(provider_thread["provider"]),
                    )
                )
        else:
            session_ids = [chat_id]
            codex_session_id = chat.get("codex_session_id")
            if codex_session_id and codex_session_id not in session_ids:
                session_ids.append(str(codex_session_id))
            transcript_messages = [
                message
                for session_id in session_ids
                for message in self._list_transcript_messages(project_id, project["path"], chat_id, session_id, chat.get("transcript_path"), "openai")
            ]
        all_local_messages = self.messages.list_messages(project_id, chat_id)
        # Once Lite has executed a turn, its local user/assistant rows are the
        # provider-neutral visible history.  Do not expose a DeepSeek
        # synthetic context prompt or duplicate the alternate provider's
        # assistant answer from JSONL. Imported/read-only chats have no local
        # run rows, so they continue to use the transcript as before.
        canonical_local = any(
            str(message.get("role") or "").lower() == "user" and message.get("runId")
            for message in all_local_messages
        )
        if canonical_local:
            transcript_messages = [
                message
                for message in transcript_messages
                if str(message.get("role") or "").lower() not in {"user", "assistant"}
            ]
            local_messages = all_local_messages
        else:
            local_messages = [
                message
                for message in all_local_messages
                if str(message.get("role") or "").lower() != "assistant"
            ]
        return _merge_messages(transcript_messages, local_messages)

    def _list_transcript_messages(self, project_id: str, project_path: str, chat_id: str, session_id: str, transcript_path: Any, provider: str = "openai") -> list[dict]:
        saved_path = str(transcript_path) if transcript_path else None
        include_internal = provider == DEEPSEEK_PROVIDER
        resolved_path = self.transcripts.find_transcript_path(project_path, session_id, saved_path, include_internal=include_internal)
        if resolved_path is None:
            return []
        if provider == "openai":
            if saved_path != str(resolved_path):
                self.chats.update_chat_transcript_path(project_id, chat_id, str(resolved_path))
        else:
            provider_thread = self.chats.get_provider_thread(project_id, chat_id, provider)
            if provider_thread is not None and saved_path != str(resolved_path):
                self.chats.update_provider_thread_transcript_path(project_id, chat_id, provider, str(resolved_path))
        return self.transcripts.list_messages(project_path, session_id, chat_id, str(resolved_path), include_internal=include_internal)

    async def ensure_provider_thread(self, project_id: str, chat_id: str, project_path: str, settings: AppServerRuntimeSettings) -> dict:
        provider = model_provider_for_model(settings.model)
        existing = self.chats.get_provider_thread(project_id, chat_id, provider)
        if existing is not None:
            return existing

        chat = self.chats.get_chat_row(project_id, chat_id)
        if provider == "openai":
            primary_thread_id = _primary_openai_thread_id(chat, self.transcripts.config.codex_home)
            if primary_thread_id is not None:
                return self.chats.upsert_provider_thread(
                    project_id,
                    chat_id,
                    provider,
                    primary_thread_id,
                    chat.get("transcript_path"),
                    history_initialized=True,
                )
        app_server = _app_server_for_provider(self.app_server, provider)
        # Existing OpenAI sessions are safe to resume in the primary home.
        # A legacy DeepSeek mapping is only used when no dedicated home is
        # configured (primarily compatibility for embedded/test callers).
        dedicated_deepseek = provider == DEEPSEEK_PROVIDER and getattr(getattr(app_server, "config", None), "deepseek_codex_home", None) is not None
        existing_provider_threads = self.chats.list_provider_threads(project_id, chat_id)
        legacy_thread_id = chat.get("codex_session_id") if not existing_provider_threads else None
        if provider == "openai" and legacy_thread_id and str(legacy_thread_id) != chat_id:
            # Preserve the historic Codex Lite candidate order for imported
            # rows: the Lite chat id was tried before the Codex metadata id.
            legacy_thread_id = chat_id
        if legacy_thread_id and (provider == "openai" or not dedicated_deepseek):
            return self.chats.upsert_provider_thread(
                project_id,
                chat_id,
                provider,
                str(legacy_thread_id),
                chat.get("transcript_path") if provider == "openai" else None,
                history_initialized=True,
            )

        start_params: dict[str, Any] = {"cwd": project_path}
        if settings.model:
            start_params["model"] = settings.model
            if provider == DEEPSEEK_PROVIDER:
                start_params["modelProvider"] = provider
        response = await app_server.request("thread/start", start_params)
        thread = response.get("thread") or {}
        thread_id = thread.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise AppError("app_server_error", "thread/start did not return a thread id.", 502)
        await _apply_codex_lite_thread_settings(app_server, thread_id, settings)
        try:
            await app_server.request("thread/name/set", {"threadId": thread_id, "name": chat.get("title") or "New Chat"})
        except AppError:
            pass
        return self.chats.upsert_provider_thread(project_id, chat_id, provider, thread_id, history_initialized=False)

    async def context_for_provider(self, project_id: str, chat_id: str, provider: str, exclude_content: str | None = None) -> str:
        target_thread = self.chats.get_provider_thread(project_id, chat_id, provider)
        if target_thread is None:
            # A provider used for the first time needs the existing visible
            # conversation. Internal reasoning and tool/status rows are
            # filtered below.
            messages = await self.list_messages(project_id, chat_id)
        else:
            # Existing provider threads already contain their own history.
            # Pass only the canonical user/final-answer messages added since
            # that provider was last active, avoiding nested copies of its
            # entire transcript on every provider switch.
            checkpoint = str(target_thread.get("updated_at") or "")
            messages = [
                message
                for message in self.messages.list_messages(project_id, chat_id)
                if str(message.get("createdAt") or "") > checkpoint
            ]
        excluded = _message_content_key(exclude_content or "")
        entries: list[str] = []
        for message in messages:
            role = str(message.get("role") or "").lower()
            if role not in {"user", "assistant"}:
                continue
            if role == "assistant" and str(message.get("kind") or "") != "conclusion":
                continue
            content = str(message.get("content") or "").strip()
            if (
                not content
                or content.startswith(PROVIDER_CONTEXT_PREFIX)
                or (excluded and role == "user" and _message_content_key(content) == excluded)
            ):
                continue
            label = "User" if role == "user" else "Assistant"
            entries.append(f"{label}: {content}")
        return _bounded_provider_context(entries)


class AppServerUsageService:
    def __init__(self, app_server: AppServerClient, deepseek_balance_reader=read_deepseek_balance) -> None:
        self.app_server = app_server
        self.deepseek_balance_reader = deepseek_balance_reader

    async def read_capacity(self, provider: str = "openai") -> dict:
        if provider == DEEPSEEK_PROVIDER:
            return {
                "provider": DEEPSEEK_PROVIDER,
                "fiveHour": None,
                "weekly": None,
                "planType": None,
                "rateLimitReachedType": None,
                "resetCredits": None,
                "codexCredits": None,
                "deepseekBalance": await self.deepseek_balance_reader(),
                "fetchedAt": utc_now(),
            }
        app_server = _app_server_for_provider(self.app_server, provider)
        response = await app_server.request("account/rateLimits/read")
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
            "codexCredits": _codex_credits(rate_limits.get("credits")),
            "deepseekBalance": None,
            "fetchedAt": utc_now(),
        }


@dataclass
class AppServerActiveRun:
    thread_id: str
    turn_id: str
    chat_id: str
    provider: str = "openai"
    status: str = "running"
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    reconnect_count: int = 0
    last_reconnect_at: str | None = None
    last_reconnect_message: str | None = None
    lease_released: bool = False
    replacing_idle_turn: bool = False
    pending_approvals: dict[str, dict[str, Any]] = field(default_factory=dict)


def _app_server_for_provider(app_server: Any, provider: str) -> Any:
    client_for_provider = getattr(app_server, "client_for_provider", None)
    if callable(client_for_provider):
        return client_for_provider(provider)
    return app_server


def _runtime_settings_values(settings: AppServerRuntimeSettings) -> dict[str, str]:
    return {
        "permission_profile": settings.permission_profile,
        "approval_policy": settings.approval_policy,
        "approvals_reviewer": settings.approvals_reviewer,
        "model": settings.model,
        "reasoning_effort": settings.reasoning_effort,
    }


def _latest_provider(provider_threads: list[dict]) -> str | None:
    if not provider_threads:
        return None
    latest = max(provider_threads, key=lambda item: str(item.get("updated_at") or ""))
    provider = latest.get("provider")
    return str(provider) if provider else None


def _primary_openai_thread_id(chat: dict, codex_home: Path) -> str | None:
    thread_id = chat.get("codex_session_id")
    transcript_path = chat.get("transcript_path")
    if not isinstance(thread_id, str) or not thread_id or not isinstance(transcript_path, str) or not transcript_path:
        return None
    try:
        transcript = Path(transcript_path).resolve()
        home = codex_home.resolve()
    except OSError:
        return None
    if transcript.suffix != ".jsonl" or not transcript.is_file():
        return None
    if any(transcript.is_relative_to(home / child) for child in ("sessions", "archived_sessions")):
        return thread_id
    return None


def _acquire_run_lease(app_server: Any) -> None:
    acquire_run = getattr(app_server, "acquire_run", None)
    if callable(acquire_run):
        acquire_run()


async def _release_run_lease(app_server: Any) -> None:
    release_run = getattr(app_server, "release_run", None)
    if callable(release_run):
        await release_run()


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
        run_id = new_id("run")
        run_settings = self.threads.settings_for_chat(project_id, chat_id)
        provider = model_provider_for_model(run_settings.model)
        app_server = _app_server_for_provider(self.app_server, provider)
        # A combination is considered recently used when a chat message is
        # actually sent, regardless of whether the user changed the dropdown
        # immediately beforehand.
        self.chats.record_model_reasoning_choice(run_settings.model, run_settings.reasoning_effort)
        prior_context = await self.threads.context_for_provider(project_id, chat_id, provider, exclude_content=content)
        provider_rows_before = self.chats.list_provider_threads(project_id, chat_id)
        last_provider = _latest_provider(provider_rows_before)
        user_message = self.messages.insert_message(chat_id, "user", _content_with_attachment_summary(content, attachments or []), run_id=run_id, kind="instruction")
        _acquire_run_lease(app_server)
        try:
            await app_server.ensure_started()
        except Exception:
            await _release_run_lease(app_server)
            raise
        notification_queue = app_server.subscribe_queue()
        try:
            provider_thread = await self.threads.ensure_provider_thread(project_id, chat_id, project["path"], run_settings)
            input_items = _build_turn_input(content, attachments or [])
            if prior_context and (not bool(provider_thread.get("history_initialized")) or (last_provider is not None and last_provider != provider)):
                input_items[0]["text"] = _provider_context_prompt(prior_context, str(input_items[0].get("text") or ""))
            candidate_thread_ids = [str(provider_thread["thread_id"])]
            legacy_id = chat.get("codex_session_id")
            if provider == "openai" and legacy_id and str(legacy_id) not in candidate_thread_ids:
                candidate_thread_ids.append(str(legacy_id))
            response = None
            thread_id = candidate_thread_ids[0]
            last_thread_not_found: AppError | None = None
            for candidate_thread_id in candidate_thread_ids:
                try:
                    response = await self._start_turn(app_server, candidate_thread_id, project["path"], input_items, run_settings)
                    thread_id = candidate_thread_id
                    break
                except AppError as exc:
                    if not _is_thread_not_found(exc):
                        raise
                    last_thread_not_found = exc
            if response is None:
                raise AppError(
                    "thread_not_found",
                    "Codex thread was not found by app-server. No replacement session was created.",
                    404,
                ) from last_thread_not_found
            if thread_id != str(provider_thread["thread_id"]):
                self.chats.upsert_provider_thread(project_id, chat_id, provider, thread_id, history_initialized=bool(provider_thread.get("history_initialized")))
            self.chats.upsert_provider_thread(project_id, chat_id, provider, thread_id, history_initialized=True)
        except Exception:
            app_server.unsubscribe_queue(notification_queue)
            await _release_run_lease(app_server)
            raise
        turn_id = _turn_id_from_response(response)
        if not turn_id:
            app_server.unsubscribe_queue(notification_queue)
            await _release_run_lease(app_server)
            raise AppError("app_server_error", "turn/start did not return a turn id.", 502)
        self.runs[run_id] = AppServerActiveRun(thread_id, turn_id, chat_id, provider=provider, started_at=utc_now())
        await self.events.publish(run_id, "status", {"status": "running"})
        asyncio.create_task(self._watch_run(run_id, notification_queue, app_server))
        return {"messageId": user_message["id"], "runId": run_id}

    async def _start_turn(self, app_server: Any, thread_id: str, project_path: str, input_items: list[dict[str, Any]], settings: AppServerRuntimeSettings) -> dict:
        try:
            await _apply_codex_lite_thread_settings(app_server, thread_id, settings)
        except AppError as exc:
            if not _is_thread_not_found(exc):
                raise
            await self._ensure_thread_loaded(app_server, thread_id, settings)
            await _apply_codex_lite_thread_settings(app_server, thread_id, settings)
        try:
            return await app_server.request(
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
            await self._ensure_thread_loaded(app_server, thread_id, settings)
            return await app_server.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "cwd": project_path,
                    "input": input_items,
                },
            )

    async def _ensure_thread_loaded(self, app_server: Any, thread_id: str, settings: AppServerRuntimeSettings) -> None:
        response = await app_server.request("thread/read", {"threadId": thread_id})
        thread = response.get("thread") or {}
        status = thread.get("status") if isinstance(thread, dict) else {}
        if isinstance(status, dict) and status.get("type") == "notLoaded":
            resume_params: dict[str, Any] = {"threadId": thread_id}
            if settings.model:
                resume_params["model"] = settings.model
                provider = model_provider_for_model(settings.model)
                if provider == DEEPSEEK_PROVIDER:
                    resume_params["modelProvider"] = provider
            await app_server.request("thread/resume", resume_params, timeout=120)

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
                "provider": run.provider,
                "status": run.status,
                "startedAt": run.started_at,
                "finishedAt": run.finished_at,
                "error": run.error,
                "reconnectCount": run.reconnect_count,
                "lastReconnectAt": run.last_reconnect_at,
                "lastReconnectMessage": run.last_reconnect_message,
                "pendingApprovals": [
                    {
                        "requestId": request_id,
                        "method": approval["method"],
                        "itemId": approval.get("itemId"),
                    }
                    for request_id, approval in run.pending_approvals.items()
                ],
            }
            for run_id, run in self.runs.items()
        ]

    async def cancel_run(self, run_id: str) -> dict:
        run = self.runs.get(run_id)
        if run is None:
            raise AppError("cancel_failed", "Run is not active.", 409)
        if run.status != "running":
            return _run_out(run_id, run)
        app_server = _app_server_for_provider(self.app_server, run.provider)
        try:
            await app_server.request("turn/interrupt", {"threadId": run.thread_id, "turnId": run.turn_id})
        except AppError as exc:
            await self.events.publish(run_id, "progress", {"method": "turn/interrupt", "summary": exc.message})
            if _is_no_active_turn(exc):
                await self._finish_cancelled_run(run_id, run)
                return _run_out(run_id, run)
            raise
        try:
            await self._wait_for_thread_idle(app_server, run.thread_id)
        except asyncio.TimeoutError as exc:
            raise AppError(
                "cancel_pending",
                "Codex is still stopping the active turn. Please wait before starting the next turn.",
                409,
            ) from exc
        if run.status == "running":
            await self._finish_cancelled_run(run_id, run)
        return _run_out(run_id, run)

    async def _finish_cancelled_run(self, run_id: str, run: AppServerActiveRun) -> None:
        run.status = "cancelled"
        run.finished_at = utc_now()
        self.chats.touch_provider_thread(run.chat_id, run.provider)
        await self._release_run_lease(run)
        await self.events.publish(run_id, "done", {"status": "cancelled", "exitCode": None})

    async def _wait_for_thread_idle(self, app_server: Any, thread_id: str) -> None:
        deadline = time.monotonic() + CANCEL_IDLE_WAIT_SECONDS
        while time.monotonic() < deadline:
            response = await app_server.request("thread/read", {"threadId": thread_id})
            thread = response.get("thread") or {}
            status = thread.get("status") if isinstance(thread, dict) else None
            status_type = status.get("type") if isinstance(status, dict) else None
            if status_type in {"idle", "notLoaded", "systemError"}:
                return
            await asyncio.sleep(CANCEL_IDLE_POLL_SECONDS)
        raise asyncio.TimeoutError

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
        app_server = _app_server_for_provider(self.app_server, run.provider)
        chat_row = self.chats.get_chat_row_by_id(run.chat_id)
        run_settings = self.threads.settings_for_chat(str(chat_row["project_id"]), run.chat_id)
        self.chats.record_model_reasoning_choice(run_settings.model, run_settings.reasoning_effort)
        await self.events.publish(run_id, "progress", {"method": "turn/steer", "summary": "sending additional instructions"})
        try:
            await app_server.request(
                "turn/steer",
                {
                    "threadId": run.thread_id,
                    "expectedTurnId": run.turn_id,
                    "input": input_items,
                },
            )
        except AppError as exc:
            no_active_turn = _is_no_active_turn(exc)
            if no_active_turn:
                # Keep a delayed completion notification for the previous turn
                # from terminating the run while its replacement is starting.
                run.replacing_idle_turn = True
            await self.events.publish(run_id, "progress", {"method": "turn/steer", "summary": exc.message})
            if not no_active_turn:
                raise
            project_id = str(chat_row["project_id"])
            project = self.projects.get_project_row(project_id)
            try:
                response = await self._start_turn(app_server, run.thread_id, str(project["path"]), input_items, run_settings)
                turn_id = _turn_id_from_response(response)
                if not turn_id:
                    raise AppError("app_server_error", "turn/start did not return a turn id.", 502)
                run.turn_id = turn_id
            except Exception as start_exc:
                run.status = "failed"
                run.error = str(start_exc)
                run.finished_at = utc_now()
                await self.events.publish(run_id, "error", {"code": "app_server_error", "message": run.error})
                raise
            finally:
                run.replacing_idle_turn = False
            await self.events.publish(
                run_id,
                "progress",
                {
                    "method": "turn/start",
                    "summary": "The previous turn had already completed; the instruction was started as the next turn.",
                },
            )
        self.messages.insert_message(run.chat_id, "user", _content_with_attachment_summary(clean_content, attachments or []), run_id=run_id, kind="instruction")
        await self.events.publish(run_id, "progress", {"method": "turn/steer", "summary": "additional instructions sent"})
        return _run_out(run_id, run)

    async def resolve_approval(self, run_id: str, request_id: str, decision: str) -> dict:
        run = self.runs.get(run_id)
        if run is None or run.status != "running":
            raise AppError("approval_failed", "Run is not active.", 409)
        approval = run.pending_approvals.get(request_id)
        if approval is None:
            raise AppError("approval_not_found", "Approval request was not found or was already resolved.", 404)
        if decision not in APPROVAL_DECISIONS:
            raise AppError("validation_error", "Approval decision is invalid.", 400)
        available = approval.get("availableDecisions")
        if isinstance(available, list) and available and decision not in available:
            raise AppError("approval_decision_unavailable", "That approval decision is not available for this request.", 400)
        app_server = _app_server_for_provider(self.app_server, run.provider)
        responder = getattr(app_server, "respond_server_request", None)
        if not callable(responder):
            raise AppError("approval_not_supported", "The app-server client cannot answer approval requests.", 500)
        await responder(approval["rawRequestId"], {"decision": decision})
        run.pending_approvals.pop(request_id, None)
        await self.events.publish(
            run_id,
            "progress",
            {
                "method": "approval/resolved",
                "summary": _approval_decision_summary(decision),
            },
        )
        return _run_out(run_id, run)

    async def stream_events(self, run_id: str) -> AsyncIterator[str]:
        if run_id not in self.runs:
            raise AppError("run_not_found", "Run was not found.", 404)
        async for item in self.events.subscribe(run_id):
            yield f"event: {item['event']}\ndata: {json.dumps(item['data'], ensure_ascii=False)}\n\n"

    async def _watch_run(self, run_id: str, notification_queue: asyncio.Queue[AppServerNotification], app_server: Any) -> None:
        run = self.runs[run_id]
        assistant_parts: list[str] = []
        final_answer_parts: list[str] = []
        agent_message_phases: dict[str, str] = {}
        pending_agent_message_parts: dict[str, list[str]] = {}
        reasoning_buffers: dict[tuple[str, str], list[str]] = {}
        reasoning_buffered_chars = 0
        last_reasoning_flush = time.monotonic()
        last_state_reconcile = time.monotonic()

        async def flush_reasoning_progress() -> None:
            nonlocal last_reasoning_flush, reasoning_buffered_chars
            if not reasoning_buffers:
                last_reasoning_flush = time.monotonic()
                return
            pending = list(reasoning_buffers.items())
            reasoning_buffers.clear()
            reasoning_buffered_chars = 0
            for (method, item_id), parts in pending:
                delta = "".join(parts)
                if not delta:
                    continue
                await self.events.publish(
                    run_id,
                    "progress",
                    {
                        "method": method,
                        "summary": method,
                        "details": json.dumps(
                            {"itemId": item_id, "delta": delta},
                            ensure_ascii=False,
                        ),
                    },
                )
            last_reasoning_flush = time.monotonic()

        async def publish_agent_output(item_id: str, delta: str, phase: str) -> None:
            if not delta:
                return
            if phase == "final_answer":
                final_answer_parts.append(delta)
            await self.events.publish(
                run_id,
                "output",
                {
                    "stream": "stdout",
                    "text": delta,
                    "messageId": item_id,
                    "phase": phase,
                },
            )

        async def flush_pending_agent_output(item_id: str, phase: str) -> None:
            parts = pending_agent_message_parts.pop(item_id, None)
            if parts:
                await publish_agent_output(item_id, "".join(parts), phase)

        async def flush_all_pending_agent_output() -> None:
            for item_id in list(pending_agent_message_parts):
                await flush_pending_agent_output(item_id, agent_message_phases.get(item_id, ""))

        try:
            while True:
                if run.status != "running":
                    return
                try:
                    notification = await asyncio.wait_for(
                        notification_queue.get(),
                        timeout=REASONING_PROGRESS_BATCH_SECONDS,
                    )
                except asyncio.TimeoutError:
                    await flush_reasoning_progress()
                    now = time.monotonic()
                    if now - last_state_reconcile >= RUN_STATE_RECONCILE_SECONDS:
                        last_state_reconcile = now
                        response = await app_server.request("thread/read", {"threadId": run.thread_id})
                        thread = response.get("thread") or {}
                        status = thread.get("status") if isinstance(thread, dict) else None
                        status_type = status.get("type") if isinstance(status, dict) else None
                        if status_type == "idle" and not run.replacing_idle_turn:
                            await flush_all_pending_agent_output()
                            run.status = "succeeded"
                            run.finished_at = utc_now()
                            self.chats.touch_provider_thread(run.chat_id, run.provider)
                            stored_parts = final_answer_parts or assistant_parts
                            if stored_parts:
                                self.messages.insert_message(run.chat_id, "assistant", "".join(stored_parts), run_id=run_id, kind="conclusion")
                            await self.events.publish(run_id, "done", {"status": run.status, "exitCode": 0})
                            return
                    continue
                if notification.method == "app_server/exited":
                    await flush_reasoning_progress()
                    await flush_all_pending_agent_output()
                    run.status = "failed"
                    run.error = str(notification.params.get("message") or "Codex app-server exited.")
                    run.finished_at = utc_now()
                    self.chats.touch_provider_thread(run.chat_id, run.provider)
                    await self.events.publish(run_id, "error", {"code": "app_server_closed", "message": run.error})
                    return
                if notification.method == "serverRequest/resolved" and notification.params.get("threadId") == run.thread_id:
                    resolved_id = str(notification.params.get("requestId") or "")
                    if resolved_id:
                        run.pending_approvals.pop(resolved_id, None)
                    await self.events.publish(
                        run_id,
                        "progress",
                        {
                            "method": notification.method,
                            "summary": "承認要求が解決されました",
                        },
                    )
                    continue
                if not _notification_matches(notification, run):
                    continue
                if run.status != "running":
                    return
                if notification.method == "turn/completed" and run.replacing_idle_turn:
                    continue
                if SERVER_REQUEST_ID_KEY in notification.params:
                    raw_request_id = notification.params[SERVER_REQUEST_ID_KEY]
                    request_id = str(raw_request_id)
                    if notification.method not in APPROVAL_REQUEST_METHODS:
                        rejecter = getattr(app_server, "reject_server_request", None)
                        message = f"Unsupported app-server request: {notification.method}"
                        if callable(rejecter):
                            await rejecter(raw_request_id, message)
                        run.status = "failed"
                        run.error = message
                        run.finished_at = utc_now()
                        self.chats.touch_provider_thread(run.chat_id, run.provider)
                        await self.events.publish(run_id, "error", {"code": "unsupported_server_request", "message": message})
                        return
                    approval = {
                        "rawRequestId": raw_request_id,
                        "method": notification.method,
                        "itemId": notification.params.get("itemId"),
                        "availableDecisions": notification.params.get("availableDecisions"),
                    }
                    run.pending_approvals[request_id] = approval
                    await self.events.publish(
                        run_id,
                        "approval",
                        {
                            "requestId": request_id,
                            "method": notification.method,
                            "itemId": notification.params.get("itemId"),
                            "reason": notification.params.get("reason"),
                            "command": _approval_command(notification.params.get("command")),
                            "cwd": notification.params.get("cwd"),
                            "availableDecisions": notification.params.get("availableDecisions"),
                            "networkApprovalContext": notification.params.get("networkApprovalContext"),
                        },
                    )
                    continue
                if _is_reasoning_delta_notification(notification):
                    delta = _reasoning_delta(notification)
                    if delta:
                        key = (notification.method, _reasoning_item_id(notification))
                        reasoning_buffers.setdefault(key, []).append(delta)
                        reasoning_buffered_chars += len(delta)
                    if (
                        time.monotonic() - last_reasoning_flush >= REASONING_PROGRESS_BATCH_SECONDS
                        or reasoning_buffered_chars >= REASONING_PROGRESS_MAX_BATCH_CHARS
                    ):
                        await flush_reasoning_progress()
                    continue
                await flush_reasoning_progress()
                raw_item = _raw_agent_message_item(notification)
                if raw_item is not None:
                    item_id = _raw_agent_message_item_id(raw_item)
                    phase = _raw_agent_message_phase(raw_item)
                    if item_id and phase:
                        agent_message_phases[item_id] = phase
                        await flush_pending_agent_output(item_id, phase)
                    continue
                agent_item = _agent_message_item(notification)
                if agent_item is not None:
                    item_id = _agent_message_item_id(agent_item)
                    phase = _agent_message_phase(agent_item)
                    if item_id and phase:
                        agent_message_phases[item_id] = phase
                        await flush_pending_agent_output(item_id, phase)
                    if phase:
                        boundary = "completed" if notification.method == "item/completed" else "started"
                        await self.events.publish(
                            run_id,
                            "progress",
                            {
                                "method": f"item/agentMessage/{boundary}",
                                "summary": "agentMessage",
                                "messageId": item_id,
                                "phase": phase,
                            },
                        )
                        continue
                if notification.method == "item/agentMessage/delta":
                    delta = str(notification.params.get("delta") or "")
                    item_id = _agent_message_delta_item_id(notification)
                    phase = agent_message_phases.get(item_id, "")
                    assistant_parts.append(delta)
                    if phase:
                        await publish_agent_output(item_id, delta, phase)
                    else:
                        pending_agent_message_parts.setdefault(item_id, []).append(delta)
                elif notification.method.startswith("item/agentMessage"):
                    await self.events.publish(
                        run_id,
                        "progress",
                        {
                            "method": notification.method,
                            "summary": _notification_summary(notification),
                            "details": _notification_details(notification),
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
                            "details": _notification_details(notification),
                        },
                    )
                elif notification.method == "turn/completed":
                    for item_id, phase in _turn_agent_message_phases(notification).items():
                        agent_message_phases[item_id] = phase
                        await flush_pending_agent_output(item_id, phase)
                    await flush_all_pending_agent_output()
                    turn = notification.params.get("turn") or {}
                    status = str(turn.get("status") or "succeeded")
                    run.status = "succeeded" if status == "completed" else status
                    run.finished_at = utc_now()
                    self.chats.touch_provider_thread(run.chat_id, run.provider)
                    stored_parts = final_answer_parts or assistant_parts
                    if stored_parts:
                        self.messages.insert_message(run.chat_id, "assistant", "".join(stored_parts), run_id=run_id, kind="conclusion")
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
                    await flush_all_pending_agent_output()
                    run.error = str(notification.params)
                    run.finished_at = utc_now()
                    self.chats.touch_provider_thread(run.chat_id, run.provider)
                    await self.events.publish(run_id, "error", {"code": "app_server_error", "message": run.error})
                    return
        except Exception as exc:
            await flush_reasoning_progress()
            await flush_all_pending_agent_output()
            run.status = "failed"
            run.error = str(exc)
            run.finished_at = utc_now()
            self.chats.touch_provider_thread(run.chat_id, run.provider)
            await self.events.publish(run_id, "error", {"code": "app_server_run_failed", "message": str(exc)})
        finally:
            if run.status != "running":
                run.pending_approvals.clear()
            app_server.unsubscribe_queue(notification_queue)
            await self._release_run_lease(run)

    async def _release_run_lease(self, run: AppServerActiveRun) -> None:
        if run.lease_released:
            return
        run.lease_released = True
        app_server = _app_server_for_provider(self.app_server, run.provider)
        await _release_run_lease(app_server)


def _is_reasoning_delta_notification(notification: AppServerNotification) -> bool:
    method = notification.method
    return method.startswith("item/reasoning/") and "delta" in method.lower()


def _reasoning_delta(notification: AppServerNotification) -> str:
    value = notification.params.get("delta")
    return value if isinstance(value, str) else ""


def _reasoning_item_id(notification: AppServerNotification) -> str:
    for key in ("itemId", "item_id"):
        value = notification.params.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _agent_message_item(notification: AppServerNotification) -> dict[str, Any] | None:
    if notification.method not in {"item/started", "item/completed"}:
        return None
    item = notification.params.get("item")
    if not isinstance(item, dict) or item.get("type") != "agentMessage":
        return None
    return item


def _agent_message_item_id(item: dict[str, Any]) -> str:
    value = item.get("id")
    return value if isinstance(value, str) else ""


def _agent_message_phase(item: dict[str, Any]) -> str:
    value = item.get("phase")
    return value if value in {"commentary", "final_answer"} else ""


def _agent_message_delta_item_id(notification: AppServerNotification) -> str:
    value = notification.params.get("itemId")
    return value if isinstance(value, str) else ""


def _raw_agent_message_item(notification: AppServerNotification) -> dict[str, Any] | None:
    if notification.method != "rawResponseItem/completed":
        return None
    item = notification.params.get("item")
    if not isinstance(item, dict) or item.get("type") != "message" or item.get("role") != "assistant":
        return None
    return item


def _raw_agent_message_item_id(item: dict[str, Any]) -> str:
    value = item.get("id")
    return value if isinstance(value, str) else ""


def _raw_agent_message_phase(item: dict[str, Any]) -> str:
    value = item.get("phase")
    return value if value in {"commentary", "final_answer"} else ""


def _turn_agent_message_phases(notification: AppServerNotification) -> dict[str, str]:
    if notification.method != "turn/completed":
        return {}
    turn = notification.params.get("turn")
    items = turn.get("items") if isinstance(turn, dict) else None
    if not isinstance(items, list):
        return {}
    phases: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "agentMessage":
            continue
        item_id = _agent_message_item_id(item)
        phase = _agent_message_phase(item)
        if item_id and phase:
            phases[item_id] = phase
    return phases


def _notification_matches(notification: AppServerNotification, run: AppServerActiveRun) -> bool:
    params = notification.params
    if params.get("threadId") != run.thread_id:
        return False
    if notification.method == "turn/completed":
        turn = params.get("turn") or {}
        return turn.get("id") == run.turn_id
    return params.get("turnId") == run.turn_id


def _approval_command(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(str(part) for part in value)
    return ""


def _approval_decision_summary(decision: str) -> str:
    return {
        "accept": "今回の操作を承認しました",
        "acceptForSession": "このセッションで同種の操作を承認しました",
        "decline": "操作を拒否しました",
        "cancel": "操作をキャンセルしました",
    }.get(decision, decision)


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


def _notification_details(notification: AppServerNotification) -> str:
    params = notification.params
    method = notification.method
    if method == "exec_command_begin":
        command = _command_text(params)
        workdir = _first_string(params, ("workdir", "cwd"))
        lines = []
        if workdir:
            lines.append(f"$ cd {workdir}")
        if command:
            lines.append(f"$ {command}")
        return "\n".join(lines)
    if method == "exec_command_output_delta":
        return _first_string(params, ("delta", "text", "output")) or ""
    if method == "exec_command_end":
        output = _first_string(params, ("output", "text", "stdout", "stderr")) or ""
        exit_code = params.get("exitCode") if "exitCode" in params else params.get("exit_code")
        prefix = f"exit: {exit_code}" if exit_code is not None else ""
        return "\n".join(part for part in (prefix, output) if part)
    visible = {
        key: _redact_detail_value(value, key)
        for key, value in params.items()
        if key not in {"threadId", "turnId", "conversationId", "encrypted_content", "encryptedContent"}
    }
    if not visible:
        return ""
    try:
        return json.dumps(visible, ensure_ascii=False, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return _redact_sensitive(str(visible))


def _redact_detail_value(value: Any, key: str = "") -> Any:
    normalized_key = re.sub(r"[^a-z]", "", key.lower())
    if any(token in normalized_key for token in ("authorization", "apikey", "accesstoken", "refreshtoken", "idtoken", "cookie", "password", "secret", "encryptedcontent")):
        return "<redacted>"
    if isinstance(value, str):
        return _redact_sensitive(value)
    if isinstance(value, dict):
        return {child_key: _redact_detail_value(child_value, str(child_key)) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [_redact_detail_value(item) for item in value]
    return value


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


def _is_no_active_turn(exc: AppError) -> bool:
    return "no active turn" in exc.message.lower()


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


def _provider_context_prompt(prior_context: str, current_request: str) -> str:
    return (
        PROVIDER_CONTEXT_PREFIX
        + "提供元固有の推論状態は引き継がれていないため、必要な事実だけを会話の文脈として利用してください。\n\n"
        "--- 過去の会話 ---\n"
        f"{prior_context}\n"
        "--- 過去の会話ここまで ---\n\n"
        "--- 今回の依頼 ---\n"
        f"{current_request}"
    )


def _bounded_provider_context(entries: list[str]) -> str:
    selected: list[str] = []
    used = 0
    separator_chars = 2
    for entry in reversed(entries):
        extra = len(entry) + (separator_chars if selected else 0)
        if used + extra <= PROVIDER_CONTEXT_MAX_CHARS:
            selected.append(entry)
            used += extra
            continue
        if not selected:
            marker = "[前半を省略]\n"
            selected.append(marker + entry[-(PROVIDER_CONTEXT_MAX_CHARS - len(marker)):])
        break
    selected.reverse()
    return "\n\n".join(selected)


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
        "approvalsReviewer": settings.approvals_reviewer,
        "permissions": settings.permission_profile,
    }
    if settings.model:
        params["model"] = settings.model
    if settings.reasoning_effort:
        params["effort"] = settings.reasoning_effort
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


def _message_sort_key(message: dict) -> datetime:
    # Python's sort is stable, so messages with the same second retain their
    # transcript/local insertion order instead of being shuffled by random IDs.
    return _message_timestamp(message)


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
        "provider": run.provider,
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


def _codex_credits(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    balance = value.get("balance")
    if isinstance(balance, bool) or not isinstance(balance, (str, int, float)):
        balance = None
    else:
        balance = str(balance)
    has_credits = value.get("hasCredits") if isinstance(value.get("hasCredits"), bool) else False
    unlimited = value.get("unlimited") if isinstance(value.get("unlimited"), bool) else False
    if balance is None and not has_credits and not unlimited:
        return None
    return {"hasCredits": has_credits, "unlimited": unlimited, "balance": balance}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None
