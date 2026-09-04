from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, AsyncIterator

from .errors import AppError
from .util.time import utc_now


REMOTE_OPERATIONS = {"send_message", "create_chat", "steer_run", "cancel_run", "resolve_approval"}
_POSIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])/(?:home|mnt|tmp|var|opt|workspace)(?:/[^\s`\"'<>]*)*")
_WINDOWS_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^\s`\"'<>]*")
_AUTHORIZATION_RE = re.compile(
    r"(?i)\bauthorization\b\s*[:=]\s*(bearer\s+)?[A-Za-z0-9._~+/=-]{6,}"
)
_BEARER_RE = re.compile(r"(?i)(bearer|token)\s+[A-Za-z0-9._~+/=-]{16,}")
_NAMED_SECRET_RE = re.compile(
    r'''(?i)\b(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|cookie|password|secret)\b\s*[:=]\s*"?([^\s,;'"}]+)'''
)
_INLINE_IMAGE_MARKDOWN_RE = re.compile(
    r"!\[[^\]\r\n]*\]\(data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]+\)",
    re.IGNORECASE,
)
_INLINE_IMAGE_DATA_RE = re.compile(
    r"data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]+",
    re.IGNORECASE,
)


class RemoteGateway:
    """Small, explicit boundary between a local Remote Agent and the daemon."""

    def __init__(self, db, projects, chats, app_threads, runs, app_runs, app_settings, transcript_import, use_app_server: bool) -> None:
        self.db = db
        self.projects = projects
        self.chats = chats
        self.app_threads = app_threads
        self.runs = runs
        self.app_runs = app_runs
        self.app_settings = app_settings
        self.transcript_import = transcript_import
        self.use_app_server = use_app_server
        self._selected_project_id: str | None = None
        self._selected_chat_id: str | None = None
        self._project_order: list[str] = []
        self._chat_order: dict[str, list[str]] = {}
        self._catalog_revision = 0
        self._catalog_condition = asyncio.Condition()

    async def catalog(self, include_history: bool = True) -> dict[str, Any]:
        # The desktop UI owns registration and projection order. Refresh only
        # registered projects; importing every unregistered transcript here
        # would resurrect projects the user deliberately removed.
        self.transcript_import.index_registered_projects()
        active_runs = self.app_runs.list_active_runs() if self.use_app_server else self.runs.list_run_diagnostics()
        active_by_chat = {
            str(run.get("chatId") or ""): run
            for run in active_runs
            if str(run.get("status") or "") in {"queued", "running"}
        }
        result: list[dict[str, Any]] = []
        projects = self._ordered_projects(self.projects.list_projects())
        for project in projects:
            project_id = str(project["id"])
            project_chats = self.chats.list_chats(project_id)
            project_chats = self._ordered_chats(project_id, project_chats)
            catalog_chats: list[dict[str, Any]] = []
            for chat in project_chats:
                catalog_chat = self._catalog_chat_metadata(chat, active_by_chat)
                if include_history:
                    catalog_chat["history"] = await self._chat_history(project, chat)
                catalog_chats.append(catalog_chat)
            result.append(
                {
                    "id": project_id,
                    "name": str(project.get("name") or ""),
                    "chats": catalog_chats,
                }
            )
        return {
            "projects": result,
            "revision": self._catalog_revision,
            "selectedProjectId": self._selected_project_id,
            "selectedChatId": self._selected_chat_id,
        }

    async def set_sync_priority(self, selected_project_id: str | None, selected_chat_id: str | None, project_order: list[str], chat_order: dict[str, list[str]] | None = None) -> dict[str, Any]:
        next_selected_project_id = _optional_id(selected_project_id, "selectedProjectId")
        next_selected_chat_id = _optional_id(selected_chat_id, "selectedChatId")
        next_project_order = _id_list(project_order, "projectOrder")
        next_chat_order = _id_map(chat_order or {}, "chatOrder")
        order_changed = (
            self._project_order != next_project_order
            or self._chat_order != next_chat_order
        )
        self._selected_project_id = next_selected_project_id
        self._selected_chat_id = next_selected_chat_id
        self._project_order = next_project_order
        self._chat_order = next_chat_order
        if order_changed:
            self.mark_catalog_changed()
        return {"ok": True, "revision": self._catalog_revision}

    def mark_catalog_changed(self, _chat_id: str | None = None) -> None:
        self._catalog_revision += 1
        try:
            asyncio.get_running_loop().create_task(self._notify_catalog_changed())
        except RuntimeError:
            pass

    async def _notify_catalog_changed(self) -> None:
        async with self._catalog_condition:
            self._catalog_condition.notify_all()

    async def wait_catalog_revision(self, after_revision: int, timeout_seconds: float = 30) -> int:
        async with self._catalog_condition:
            if self._catalog_revision == after_revision:
                try:
                    await asyncio.wait_for(
                        self._catalog_condition.wait_for(lambda: self._catalog_revision != after_revision),
                        timeout=timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
            return self._catalog_revision

    def catalog_revision(self) -> int:
        return self._catalog_revision

    def _ordered_projects(self, projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
        order = {project_id: index for index, project_id in enumerate(self._project_order)}
        return sorted(
            projects,
            key=lambda project: order.get(str(project.get("id") or ""), len(order)),
        )

    def _ordered_chats(self, project_id: str, chats: list[dict[str, Any]]) -> list[dict[str, Any]]:
        order = {chat_id: index for index, chat_id in enumerate(self._chat_order.get(project_id, []))}
        return sorted(
            chats,
            key=lambda chat: order.get(str(chat.get("id") or ""), len(order)),
        )

    async def catalog_chat_history(self, project_id: str, chat_id: str) -> dict[str, Any]:
        project = self.projects.get_project(project_id)
        chat = self.chats.get_chat(project_id, chat_id)
        return {"history": await self._chat_history(project, chat)}

    def _catalog_chat_metadata(self, chat: dict[str, Any], active_by_chat: dict[str, dict[str, Any]]) -> dict[str, Any]:
        chat_id = str(chat["id"])
        active_run = active_by_chat.get(chat_id)
        result = {
            "id": chat_id,
            "title": str(chat.get("title") or "New Chat"),
            "status": str(active_run.get("status") or "running") if active_run else "idle",
            "updatedAt": chat.get("updatedAt"),
            "historyRevision": self._history_revision(chat_id, chat.get("updatedAt")),
        }
        if active_run:
            result["activeRunId"] = str(active_run.get("id") or "")
            result["activeRunEventSequence"] = int(active_run.get("eventSequence") or 0)
        return result

    def _history_revision(self, chat_id: str, updated_at: Any) -> str:
        event_row = self.db.fetchone(
            """
            SELECT COUNT(*) AS event_count, MAX(re.created_at) AS latest_event_at
            FROM run_events re
            JOIN runs r ON r.id = re.run_id
            WHERE r.chat_id = ? AND re.event = 'progress'
            """,
            (chat_id,),
        )
        run_row = self.db.fetchone(
            """
            SELECT COUNT(*) AS terminal_count, MAX(finished_at) AS latest_finished_at,
                   MAX(revision) AS latest_run_revision
            FROM runs
            WHERE chat_id = ? AND status IN ('succeeded', 'failed', 'cancelled')
            """,
            (chat_id,),
        )
        return "|".join([
            str(updated_at or ""),
            str((event_row or {}).get("event_count") or 0),
            str((event_row or {}).get("latest_event_at") or ""),
            str((run_row or {}).get("terminal_count") or 0),
            str((run_row or {}).get("latest_finished_at") or ""),
            str((run_row or {}).get("latest_run_revision") or 0),
        ])

    async def _chat_history(self, project: dict[str, Any], chat: dict[str, Any]) -> list[dict[str, Any]]:
        project_id = str(project["id"])
        chat_id = str(chat["id"])
        try:
            messages = await self.app_threads.list_messages(project_id, chat_id)
        except (AppError, OSError):
            messages = []
        return _remote_history(messages, str(project.get("path") or ""))

    async def execute(self, task_id: str, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        clean_task_id = _required_text(task_id, "taskId", 1, 200)
        if operation not in REMOTE_OPERATIONS:
            raise AppError("remote_operation_not_allowed", "Remote operation is not allowed.", 400)
        existing = self.db.fetchone("SELECT * FROM remote_tasks WHERE task_id = ?", (clean_task_id,))
        if existing is not None:
            if existing["status"] == "completed" and existing.get("result_json"):
                return json.loads(str(existing["result_json"]))
            raise AppError("remote_task_already_started", "Remote task was already started and will not be repeated.", 409)

        now = utc_now()
        self.db.execute(
            "INSERT INTO remote_tasks(task_id, operation, status, created_at, updated_at) VALUES (?, ?, 'running', ?, ?)",
            (clean_task_id, operation, now, now),
        )
        try:
            result = await self._execute_once(operation, payload)
        except AppError as exc:
            self.db.execute(
                "UPDATE remote_tasks SET status = 'failed', error_code = ?, updated_at = ? WHERE task_id = ?",
                (exc.code, utc_now(), clean_task_id),
            )
            raise
        except Exception:
            self.db.execute(
                "UPDATE remote_tasks SET status = 'failed', error_code = 'remote_internal_error', updated_at = ? WHERE task_id = ?",
                (utc_now(), clean_task_id),
            )
            raise

        run_id = str(result.get("runId") or result.get("id") or "") or None
        self.db.execute(
            "UPDATE remote_tasks SET status = 'completed', run_id = ?, result_json = ?, updated_at = ? WHERE task_id = ?",
            (run_id, json.dumps(result, ensure_ascii=False), utc_now(), clean_task_id),
        )
        # Chat creation has no MessageService or Run lifecycle event. Run state
        # itself is announced only by EventHub's lifecycle listener.
        if operation == "create_chat":
            self.mark_catalog_changed()
        return result

    async def _execute_once(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation == "send_message":
            project_id = _required_text(payload.get("projectId"), "projectId")
            chat_id = _required_text(payload.get("chatId"), "chatId")
            content = _required_text(payload.get("content"), "content", 1, 100_000)
            attachments = _remote_attachments(payload)
            if attachments and not self.use_app_server:
                raise AppError("remote_attachment_not_supported", "Remote attachments require app-server mode.", 409)
            return await self.app_runs.start_message_run(project_id, chat_id, content, attachments) if self.use_app_server else self.runs.start_message_run(project_id, chat_id, content)

        if operation == "create_chat":
            project_id = _required_text(payload.get("projectId"), "projectId")
            title = _optional_text(payload.get("title"), 200) or "New Chat"
            if self.use_app_server:
                chat = await self.app_threads.create_chat(project_id, title, self.app_settings)
            else:
                chat = self.chats.create_chat(
                    project_id,
                    title,
                    {
                        "permission_profile": self.app_settings.permission_profile,
                        "approval_policy": self.app_settings.approval_policy,
                        "approvals_reviewer": self.app_settings.approvals_reviewer,
                        "model": self.app_settings.model,
                        "reasoning_effort": self.app_settings.reasoning_effort,
                    },
                )
            content = _optional_text(payload.get("content"), 100_000)
            if not content:
                return {"chat": _remote_chat(chat)}
            attachments = _remote_attachments(payload)
            if attachments and not self.use_app_server:
                raise AppError("remote_attachment_not_supported", "Remote attachments require app-server mode.", 409)
            run = await self.app_runs.start_message_run(project_id, str(chat["id"]), content, attachments) if self.use_app_server else self.runs.start_message_run(project_id, str(chat["id"]), content)
            return {**run, "chat": _remote_chat(chat)}

        run_id = _required_text(payload.get("runId"), "runId")
        if operation == "steer_run":
            if not self.use_app_server:
                raise AppError("remote_steer_not_supported", "Remote steering requires app-server mode.", 409)
            return await self.app_runs.steer_run(run_id, _required_text(payload.get("content"), "content", 1, 100_000), _remote_attachments(payload))
        if operation == "cancel_run":
            return await self.app_runs.cancel_run(run_id) if self.use_app_server else await self.runs.cancel_run(run_id)
        if operation == "resolve_approval":
            if not self.use_app_server:
                raise AppError("remote_approval_not_supported", "Remote approval requires app-server mode.", 409)
            decision = _required_text(payload.get("decision"), "decision")
            if decision not in {"accept", "acceptForSession", "decline", "cancel"}:
                raise AppError("remote_invalid_decision", "Remote approval decision is invalid.", 400)
            return await self.app_runs.resolve_approval(run_id, _required_text(payload.get("requestId"), "requestId"), decision)
        raise AppError("remote_operation_not_allowed", "Remote operation is not allowed.", 400)

    def stream_events(self, run_id: str, after_sequence: int | None = None) -> AsyncIterator[bytes]:
        clean_run_id = _required_text(run_id, "runId")
        return self.app_runs.stream_events(clean_run_id, after_sequence=after_sequence) if self.use_app_server else self.runs.stream_events(clean_run_id)


def _required_text(value: Any, field: str, minimum: int = 1, maximum: int = 500) -> str:
    if not isinstance(value, str):
        raise AppError("remote_validation_error", f"Remote field must be text: {field}", 400)
    clean = value.strip()
    if len(clean) < minimum or len(clean) > maximum:
        raise AppError("remote_validation_error", f"Remote field length is invalid: {field}", 400)
    return clean


def _optional_text(value: Any, maximum: int) -> str | None:
    if value is None:
        return None
    return _required_text(value, "optional", 1, maximum)


def _remote_chat(chat: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(chat["id"]), "title": str(chat.get("title") or "New Chat")}


def _remote_attachments(payload: dict[str, Any]) -> list[dict[str, str]]:
    value = payload.get("attachments")
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 4:
        raise AppError("remote_validation_error", "Remote attachments are invalid.", 400)
    root = (Path.home() / ".local" / "share" / "codex-lite" / "remote-attachments").resolve()
    result: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise AppError("remote_validation_error", f"Remote attachment is invalid: {index}", 400)
        target = Path(_required_text(item.get("path"), f"attachments.{index}.path", 1, 1000)).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise AppError("remote_validation_error", "Remote attachment path is invalid.", 400)
        kind = _required_text(item.get("kind"), f"attachments.{index}.kind", 1, 20)
        if kind not in {"image", "file"}:
            raise AppError("remote_validation_error", "Remote attachment kind is invalid.", 400)
        result.append({
            "path": str(target),
            "name": _required_text(item.get("name"), f"attachments.{index}.name", 1, 200),
            "kind": kind,
        })
    return result


def _optional_id(value: Any, field: str) -> str | None:
    if value is None or value == "":
        return None
    return _required_text(value, field, 1, 200)


def _id_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AppError("remote_validation_error", f"Remote field must be a string array: {field}", 400)
    if len(value) > 500:
        raise AppError("remote_validation_error", f"Remote field is too large: {field}", 400)
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        clean = item.strip()
        if not clean or len(clean) > 200 or clean in seen:
            continue
        seen.add(clean)
        result.append(clean)
    return result


def _id_map(value: Any, field: str) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        raise AppError("remote_validation_error", f"Remote field must be a string-array map: {field}", 400)
    if len(value) > 500:
        raise AppError("remote_validation_error", f"Remote field is too large: {field}", 400)
    return {key.strip(): _id_list(ids, f"{field}.{key}") for key, ids in value.items() if isinstance(key, str) and key.strip()}


def _remote_history(messages: list[dict[str, Any]], project_path: str) -> list[dict[str, Any]]:
    """Build the complete visible history projection; never send raw JSONL fields."""
    candidates: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "").lower()
        if role not in {"user", "assistant", "status"}:
            continue
        kind = str(message.get("kind") or "")
        if role == "assistant" and kind not in {"work", "conclusion"}:
            continue
        content = _without_inline_images(
            _redact_remote_text(str(message.get("content") or ""), project_path)
        ).strip()
        activity_details = _without_inline_images(
            _redact_remote_text(str(message.get("activityDetails") or ""), project_path)
        ).strip()
        if not content and not activity_details:
            continue
        candidate = {
            "id": str(message.get("id") or ""),
            "role": role,
            "content": content,
            "createdAt": str(message.get("createdAt") or ""),
            "kind": kind or ("instruction" if role == "user" else "conclusion"),
        }
        if role == "status":
            candidate["runId"] = str(message.get("runId") or "")
            candidate["activityKind"] = "reasoning" if message.get("activityKind") == "reasoning" else "work"
            candidate["activityDetails"] = activity_details
        candidates.append(candidate)
    return candidates


def _redact_remote_text(value: str, project_path: str) -> str:
    result = value
    if project_path:
        result = result.replace(project_path, "<local-project>")
    result = _AUTHORIZATION_RE.sub("Authorization=<redacted>", result)
    result = _BEARER_RE.sub(r"\1 <redacted>", result)
    result = _NAMED_SECRET_RE.sub(r"\1=<redacted>", result)
    result = _POSIX_PATH_RE.sub("<local-path>", result)
    return _WINDOWS_PATH_RE.sub("<local-path>", result)


def _without_inline_images(value: str) -> str:
    """Keep locally rendered image data outside the remote history projection."""
    result = _INLINE_IMAGE_MARKDOWN_RE.sub("", value)
    return _INLINE_IMAGE_DATA_RE.sub("", result)
