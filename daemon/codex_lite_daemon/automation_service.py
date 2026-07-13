from __future__ import annotations

import asyncio
import inspect
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .db import Database
from .errors import AppError
from .models import automation_out
from .services import ChatService, ProjectService
from .util.ids import new_id
from .util.time import utc_now


class AutomationRunner(Protocol):
    async def start_message_run(self, project_id: str, chat_id: str, content: str) -> dict:
        ...


class AutomationService:
    def __init__(self, db: Database, projects: ProjectService, chats: ChatService) -> None:
        self.db = db
        self.projects = projects
        self.chats = chats

    def list_automations(self, project_id: str, chat_id: str) -> list[dict]:
        self.chats.get_chat_row(project_id, chat_id)
        rows = self.db.fetchall(
            "SELECT * FROM automations WHERE project_id = ? AND chat_id = ? ORDER BY created_at ASC",
            (project_id, chat_id),
        )
        return [automation_out(row) for row in rows]

    def create_automation(self, project_id: str, chat_id: str, name: str, prompt: str, interval_minutes: int, enabled: bool = True) -> dict:
        self._ensure_chat_can_automate(project_id, chat_id, enabled)
        now = utc_now()
        clean_name = _clean_required(name, "Automation name must not be empty.")
        clean_prompt = _clean_prompt_required(prompt, "Automation prompt must not be empty.")
        interval = max(1, int(interval_minutes))
        automation_id = new_id("aut")
        self.db.execute(
            """
            INSERT INTO automations(id, project_id, chat_id, name, prompt, schedule_kind, interval_minutes, enabled, running, next_run_at, last_run_at, last_error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'interval_minutes', ?, ?, 0, ?, NULL, NULL, ?, ?)
            """,
            (automation_id, project_id, chat_id, clean_name, clean_prompt, interval, int(enabled), _next_run_at(interval, now) if enabled else None, now, now),
        )
        return self.get_automation(project_id, chat_id, automation_id)

    def get_automation(self, project_id: str, chat_id: str, automation_id: str) -> dict:
        self.chats.get_chat_row(project_id, chat_id)
        row = self.db.fetchone(
            "SELECT * FROM automations WHERE id = ? AND project_id = ? AND chat_id = ?",
            (automation_id, project_id, chat_id),
        )
        if row is None:
            raise AppError("automation_not_found", "Automation was not found.", 404)
        return automation_out(row)

    def update_automation(
        self,
        project_id: str,
        chat_id: str,
        automation_id: str,
        name: str | None = None,
        prompt: str | None = None,
        interval_minutes: int | None = None,
        enabled: bool | None = None,
    ) -> dict:
        current = self._get_automation_row(project_id, chat_id, automation_id)
        new_enabled = bool(current["enabled"]) if enabled is None else bool(enabled)
        self._ensure_chat_can_automate(project_id, chat_id, new_enabled)
        clean_name = current["name"] if name is None else _clean_required(name, "Automation name must not be empty.")
        clean_prompt = current["prompt"] if prompt is None else _clean_prompt_required(prompt, "Automation prompt must not be empty.")
        interval = int(current["interval_minutes"] if interval_minutes is None else max(1, int(interval_minutes)))
        now = utc_now()
        next_run_at = _next_run_at(interval, now) if new_enabled and (not bool(current["enabled"]) or interval != int(current["interval_minutes"])) else current.get("next_run_at")
        if not new_enabled:
            next_run_at = None
        self.db.execute(
            """
            UPDATE automations
            SET name = ?, prompt = ?, interval_minutes = ?, enabled = ?, next_run_at = ?, last_error = CASE WHEN ? THEN NULL ELSE last_error END, updated_at = ?
            WHERE id = ?
            """,
            (clean_name, clean_prompt, interval, int(new_enabled), next_run_at, int(new_enabled), now, automation_id),
        )
        return self.get_automation(project_id, chat_id, automation_id)

    def delete_automation(self, project_id: str, chat_id: str, automation_id: str) -> None:
        self._get_automation_row(project_id, chat_id, automation_id)
        self.db.execute("DELETE FROM automations WHERE id = ?", (automation_id,))

    async def run_now(self, project_id: str, chat_id: str, automation_id: str, runner: AutomationRunner) -> dict:
        automation = self._get_automation_row(project_id, chat_id, automation_id)
        if not bool(automation["enabled"]):
            raise AppError("automation_disabled", "Disabled automations cannot be run.", 409)
        if bool(automation["running"]):
            raise AppError("automation_already_running", "Automation is already running.", 409)
        self._ensure_chat_can_automate(project_id, chat_id, enabled=True)
        self.mark_running(automation_id)
        run = await _execute_automation(self, runner, automation)
        return {"automation": self.get_automation(project_id, chat_id, automation_id), "run": run}

    def recover_running(self) -> None:
        self.db.execute("UPDATE automations SET running = 0 WHERE running = 1")

    def due_automations(self, now: str) -> list[dict]:
        return self.db.fetchall(
            """
            SELECT * FROM automations
            WHERE enabled = 1 AND running = 0 AND next_run_at IS NOT NULL AND next_run_at <= ?
            ORDER BY next_run_at ASC
            LIMIT 5
            """,
            (now,),
        )

    def mark_running(self, automation_id: str) -> None:
        self.db.execute("UPDATE automations SET running = 1, updated_at = ? WHERE id = ?", (utc_now(), automation_id))

    def mark_finished(self, automation_id: str, interval_minutes: int, error: str | None = None) -> None:
        now = utc_now()
        self.db.execute(
            """
            UPDATE automations
            SET running = 0, last_run_at = ?, next_run_at = ?, last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, _next_run_at(interval_minutes, now), error, now, automation_id),
        )

    def disable(self, automation_id: str, error: str) -> None:
        now = utc_now()
        self.db.execute(
            "UPDATE automations SET enabled = 0, running = 0, next_run_at = NULL, last_error = ?, updated_at = ? WHERE id = ?",
            (error, now, automation_id),
        )

    def _get_automation_row(self, project_id: str, chat_id: str, automation_id: str) -> dict:
        self.chats.get_chat_row(project_id, chat_id)
        row = self.db.fetchone(
            "SELECT * FROM automations WHERE id = ? AND project_id = ? AND chat_id = ?",
            (automation_id, project_id, chat_id),
        )
        if row is None:
            raise AppError("automation_not_found", "Automation was not found.", 404)
        return row

    def _ensure_chat_can_automate(self, project_id: str, chat_id: str, enabled: bool) -> None:
        chat = self.chats.get_chat_row(project_id, chat_id)
        if not enabled:
            return
        if chat.get("archived_at"):
            raise AppError("automation_chat_archived", "Archived chats cannot run automations.", 409)
        if not bool(chat.get("can_continue", 1)):
            raise AppError("automation_chat_read_only", str(chat.get("continue_disabled_reason") or "This chat cannot be continued."), 409)


async def run_automation_scheduler(service: AutomationService, runner: AutomationRunner, stop_event: asyncio.Event) -> None:
    service.recover_running()
    while not stop_event.is_set():
        try:
            await _run_due_automations(service, runner)
        except Exception as exc:
            _log_scheduler_error("automation_scheduler_error", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=15)
        except TimeoutError:
            pass


async def _run_due_automations(service: AutomationService, runner: AutomationRunner) -> None:
    for automation in service.due_automations(utc_now()):
        automation_id = str(automation["id"])
        service.mark_running(automation_id)
        await _execute_automation(service, runner, automation)


async def _execute_automation(service: AutomationService, runner: AutomationRunner, automation: dict) -> dict | None:
    automation_id = str(automation["id"])
    try:
        chat = service.chats.get_chat_row(str(automation["project_id"]), str(automation["chat_id"]))
        if chat.get("archived_at"):
            service.disable(automation_id, "チャットがアーカイブ済みのため無効化しました。")
            return None
        if not bool(chat.get("can_continue", 1)):
            service.disable(automation_id, str(chat.get("continue_disabled_reason") or "チャットを継続できないため無効化しました。"))
            return None
        result = runner.start_message_run(str(automation["project_id"]), str(automation["chat_id"]), str(automation["prompt"]))
        if inspect.isawaitable(result):
            result = await result
        service.mark_finished(automation_id, int(automation["interval_minutes"]))
        return result if isinstance(result, dict) else None
    except Exception as exc:
        service.mark_finished(automation_id, int(automation["interval_minutes"]), _short_error(exc))
        _log_scheduler_error("automation_run_error", exc, automation_id)
        return None


def _clean_required(value: str, message: str) -> str:
    text = " ".join((value or "").split())
    if not text:
        raise AppError("validation_error", message, 400)
    return text


def _clean_prompt_required(value: str, message: str) -> str:
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise AppError("validation_error", message, 400)
    return text


def _next_run_at(interval_minutes: int, now: str | None = None) -> str:
    base = _parse_utc(now or utc_now())
    return (base + timedelta(minutes=max(1, interval_minutes))).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    return parsed.astimezone(timezone.utc)


def _short_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text[:500]


def _log_scheduler_error(event: str, exc: Exception, automation_id: str | None = None) -> None:
    payload = {
        "event": event,
        "automationId": automation_id,
        "error": _short_error(exc),
    }
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)
