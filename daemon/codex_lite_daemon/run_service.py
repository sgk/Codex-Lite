from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .db import Database
from .errors import AppError
from .models import run_out
from .runner.base import Runner
from .services import ChatService, MessageService, ProjectService
from .util.ids import new_id
from .util.time import utc_now


@dataclass
class ActiveRun:
    task: asyncio.Task
    cancel_requested: bool = False


class EventHub:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[dict]]] = {}
        self._history: dict[str, list[dict]] = {}
        self._lock = asyncio.Lock()

    async def publish(self, run_id: str, event: str, data: dict) -> None:
        payload = {"event": event, "data": data}
        async with self._lock:
            self._history.setdefault(run_id, []).append(payload)
            subscribers = list(self._subscribers.get(run_id, []))
        for queue in subscribers:
            await queue.put(payload)

    async def subscribe(self, run_id: str) -> AsyncIterator[dict]:
        queue: asyncio.Queue[dict] = asyncio.Queue()
        async with self._lock:
            history = list(self._history.get(run_id, []))
            self._subscribers.setdefault(run_id, []).append(queue)
        try:
            for item in history:
                yield item
                if item["event"] in {"done", "error"}:
                    return
            while True:
                item = await queue.get()
                yield item
                if item["event"] in {"done", "error"}:
                    break
        finally:
            async with self._lock:
                subscribers = self._subscribers.get(run_id, [])
                if queue in subscribers:
                    subscribers.remove(queue)


class RunService:
    def __init__(
        self,
        db: Database,
        config: Config,
        projects: ProjectService,
        chats: ChatService,
        messages: MessageService,
        runner: Runner,
    ) -> None:
        self.db = db
        self.config = config
        self.projects = projects
        self.chats = chats
        self.messages = messages
        self.runner = runner
        self.active_runs: dict[str, ActiveRun] = {}
        self.events = EventHub()
        self.config.run_log_dir.mkdir(parents=True, exist_ok=True)

    def recover_stale_runs(self) -> None:
        now = utc_now()
        rows = self.db.fetchall("SELECT id FROM runs WHERE status IN ('queued', 'running')")
        for row in rows:
            self.db.execute(
                "UPDATE runs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                (now, "daemon started while this run was not completed", row["id"]),
            )

    def get_run_row(self, run_id: str) -> dict:
        row = self.db.fetchone("SELECT * FROM runs WHERE id = ?", (run_id,))
        if row is None:
            raise AppError("run_not_found", "Run was not found.", 404)
        return row

    def get_run(self, run_id: str) -> dict:
        return run_out(self.get_run_row(run_id))

    def list_run_diagnostics(self) -> list[dict]:
        diagnostics = []
        for run_id, active in self.active_runs.items():
            row = self.db.fetchone("SELECT * FROM runs WHERE id = ?", (run_id,))
            item = run_out(row) if row is not None else {"id": run_id, "status": "unknown"}
            item["cancelRequested"] = active.cancel_requested
            diagnostics.append(item)
        return diagnostics

    def start_message_run(self, project_id: str, chat_id: str, content: str) -> dict:
        if len(self.active_runs) >= self.config.max_concurrent_runs:
            raise AppError("run_already_active", "Another run is already active.", 409)
        for run_id in self.active_runs:
            row = self.db.fetchone("SELECT chat_id FROM runs WHERE id = ?", (run_id,))
            if row is not None and row["chat_id"] == chat_id:
                raise AppError("run_already_active_in_chat", "Another run is already active in this chat.", 409)
        project = self.projects.get_project_row(project_id)
        chat = self.chats.get_chat_row(project_id, chat_id)
        settings = self.chats.get_chat_settings_row(project_id, chat_id)
        self.chats.record_model_reasoning_choice(
            settings.get("model") or self.config.model,
            settings.get("reasoning_effort") or "",
        )
        user_message = self.messages.insert_message(chat_id, "user", content, kind="instruction")
        run_id = new_id("run")
        log_path = self._log_path(run_id)
        self.db.execute(
            "INSERT INTO runs(id, chat_id, status, pid, exit_code, started_at, finished_at, log_path, error) VALUES (?, ?, 'queued', NULL, NULL, NULL, NULL, ?, NULL)",
            (run_id, chat_id, str(log_path)),
        )
        task = asyncio.create_task(self._run(run_id, project["path"], chat["id"], content, log_path))
        self.active_runs[run_id] = ActiveRun(task)
        return {"messageId": user_message["id"], "runId": run_id}

    async def cancel_run(self, run_id: str) -> dict:
        self.get_run_row(run_id)
        active = self.active_runs.get(run_id)
        if active is None:
            raise AppError("cancel_failed", "Run is not active.", 409)
        active.cancel_requested = True
        active.task.cancel()
        try:
            await active.task
        except asyncio.CancelledError:
            pass
        now = utc_now()
        self.db.execute("UPDATE runs SET status = 'cancelled', finished_at = ? WHERE id = ?", (now, run_id))
        await self.events.publish(run_id, "done", {"status": "cancelled", "exitCode": None})
        return self.get_run(run_id)

    async def stream_events(self, run_id: str) -> AsyncIterator[str]:
        self.get_run_row(run_id)
        async for item in self.events.subscribe(run_id):
            yield f"event: {item['event']}\ndata: {json.dumps(item['data'], ensure_ascii=False)}\n\n"

    async def _run(self, run_id: str, project_path: str, chat_id: str, content: str, log_path: Path) -> None:
        assistant_parts: list[str] = []
        now = utc_now()
        self.db.execute("UPDATE runs SET status = 'running', started_at = ? WHERE id = ?", (now, run_id))
        await self.events.publish(run_id, "status", {"status": "starting"})
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as log:
                async for event in self.runner.run(content, project_path):
                    line = {"ts": utc_now(), "stream": event.stream, "text": event.text}
                    log.write(json.dumps(line, ensure_ascii=False) + "\n")
                    log.flush()
                    if event.stream == "stdout":
                        assistant_parts.append(event.text)
                    await self.events.publish(run_id, "output", {"stream": event.stream, "text": event.text})
                log.write(json.dumps({"ts": utc_now(), "event": "exit", "exitCode": 0}) + "\n")
            self.messages.insert_message(chat_id, "assistant", "".join(assistant_parts), run_id=run_id, kind="conclusion")
            self.db.execute(
                "UPDATE runs SET status = 'succeeded', exit_code = 0, finished_at = ? WHERE id = ?",
                (utc_now(), run_id),
            )
            await self.events.publish(run_id, "done", {"status": "succeeded", "exitCode": 0})
        except asyncio.CancelledError:
            self.db.execute("UPDATE runs SET status = 'cancelled', finished_at = ? WHERE id = ?", (utc_now(), run_id))
            raise
        except AppError as exc:
            self.db.execute(
                "UPDATE runs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                (utc_now(), exc.message, run_id),
            )
            await self.events.publish(run_id, "error", {"code": exc.code, "message": exc.message})
        except Exception as exc:
            self.db.execute(
                "UPDATE runs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                (utc_now(), str(exc), run_id),
            )
            await self.events.publish(run_id, "error", {"code": "codex_run_failed", "message": str(exc)})
        finally:
            self.active_runs.pop(run_id, None)

    def _log_path(self, run_id: str) -> Path:
        now = utc_now()
        year, month, day = now[:4], now[5:7], now[8:10]
        return self.config.run_log_dir / year / month / day / f"{run_id}.log"
