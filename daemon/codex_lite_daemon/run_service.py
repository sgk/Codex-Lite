from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    MAX_PERSISTED_EVENTS_PER_RUN = 2000
    MAX_PERSISTED_DATA_BYTES = 256 * 1024
    PERSIST_BATCH_SECONDS = 0.25

    def __init__(self, db: Database | None = None) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[dict]]] = {}
        self._history: dict[str, list[dict]] = {}
        self._sequences: dict[str, int] = {}
        self._db = db
        self._pending_persistence: list[tuple[str, int, str, str, str]] = []
        self._flush_task: asyncio.Task | None = None
        self._last_persistence_error: str | None = None
        self._lock = asyncio.Lock()

    async def publish(self, run_id: str, event: str, data: dict) -> None:
        async with self._lock:
            sequence = self.current_sequence(run_id) + 1
            self._sequences[run_id] = sequence
            payload = {"event": event, "data": data, "sequence": sequence}
            history = self._history.setdefault(run_id, [])
            history.append(payload)
            if len(history) > self.MAX_PERSISTED_EVENTS_PER_RUN:
                del history[:-self.MAX_PERSISTED_EVENTS_PER_RUN]
            subscribers = list(self._subscribers.get(run_id, []))
            if self._db is not None:
                self._pending_persistence.append((run_id, sequence, event, self._persistable_data(data), utc_now()))
                if self._flush_task is None or self._flush_task.done():
                    self._flush_task = asyncio.create_task(self._flush_after_delay())
        for queue in subscribers:
            await queue.put(payload)
        if event in {"done", "error"}:
            await self.flush()

    def current_sequence(self, run_id: str) -> int:
        cached = self._sequences.get(run_id)
        if cached is not None:
            return cached
        if self._db is None:
            return 0
        row = self._db.fetchone("SELECT MAX(sequence) AS sequence FROM run_events WHERE run_id = ?", (run_id,))
        sequence = int(row["sequence"] or 0) if row else 0
        self._sequences[run_id] = sequence
        return sequence

    async def subscribe(self, run_id: str, after_sequence: int | None = None) -> AsyncIterator[dict]:
        queue: asyncio.Queue[dict] = asyncio.Queue()
        async with self._lock:
            persisted = self._persisted_history(run_id, after_sequence)
            memory = [
                item
                for item in self._history.get(run_id, [])
                if after_sequence is None or int(item.get("sequence") or 0) > after_sequence
            ]
            history_by_sequence = {int(item["sequence"]): item for item in persisted}
            history_by_sequence.update({int(item["sequence"]): item for item in memory})
            history = [history_by_sequence[key] for key in sorted(history_by_sequence)]
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

    async def _flush_after_delay(self) -> None:
        await asyncio.sleep(self.PERSIST_BATCH_SECONDS)
        await self.flush()

    async def flush(self) -> None:
        if self._db is None:
            return
        flush_task = self._flush_task
        self._flush_task = None
        if flush_task is not None and flush_task is not asyncio.current_task() and not flush_task.done():
            flush_task.cancel()
        async with self._lock:
            pending = self._pending_persistence
            self._pending_persistence = []
        if not pending:
            return
        try:
            run_ids = {item[0] for item in pending}
            existing_run_ids = {
                run_id
                for run_id in run_ids
                if self._db.fetchone("SELECT id FROM runs WHERE id = ?", (run_id,)) is not None
            }
            valid_pending = [item for item in pending if item[0] in existing_run_ids]
            if valid_pending:
                self._db.executemany(
                    "INSERT OR REPLACE INTO run_events(run_id, sequence, event, data_json, created_at) VALUES (?, ?, ?, ?, ?)",
                    valid_pending,
                )
            latest_sequence_by_run: dict[str, int] = {}
            for run_id, sequence, _, _, _ in valid_pending:
                latest_sequence_by_run[run_id] = max(sequence, latest_sequence_by_run.get(run_id, 0))
            for run_id, latest_sequence in latest_sequence_by_run.items():
                if latest_sequence <= self.MAX_PERSISTED_EVENTS_PER_RUN:
                    continue
                self._db.execute(
                    "DELETE FROM run_events WHERE run_id = ? AND sequence <= ?",
                    (run_id, latest_sequence - self.MAX_PERSISTED_EVENTS_PER_RUN),
                )
            self._last_persistence_error = None
        except Exception as exc:
            # Event persistence is diagnostic/replay support. It must never
            # terminate the underlying Codex run.
            self._last_persistence_error = str(exc)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "pendingCount": len(self._pending_persistence),
            "lastError": self._last_persistence_error,
            "maxEventsPerRun": self.MAX_PERSISTED_EVENTS_PER_RUN,
            "maxEventDataBytes": self.MAX_PERSISTED_DATA_BYTES,
        }

    def _persisted_history(self, run_id: str, after_sequence: int | None) -> list[dict]:
        if self._db is None:
            return []
        rows = self._db.fetchall(
            "SELECT sequence, event, data_json FROM run_events WHERE run_id = ? AND sequence > ? ORDER BY sequence",
            (run_id, after_sequence if after_sequence is not None else -1),
        )
        result: list[dict] = []
        for row in rows:
            try:
                data = json.loads(str(row["data_json"]))
            except json.JSONDecodeError:
                data = {"message": "保存されたイベントを読み取れませんでした。"}
            result.append({"event": row["event"], "data": data, "sequence": int(row["sequence"])})
        return result

    def replay_history(self, run_id: str, after_sequence: int | None = None) -> list[dict]:
        persisted = self._persisted_history(run_id, after_sequence)
        memory = [
            item
            for item in self._history.get(run_id, [])
            if after_sequence is None or int(item.get("sequence") or 0) > after_sequence
        ]
        history_by_sequence = {int(item["sequence"]): item for item in persisted}
        history_by_sequence.update({int(item["sequence"]): item for item in memory})
        return [history_by_sequence[key] for key in sorted(history_by_sequence)]

    def _persistable_data(self, data: dict) -> str:
        encoded = json.dumps(data, ensure_ascii=False)
        if len(encoded.encode("utf-8")) <= self.MAX_PERSISTED_DATA_BYTES:
            return encoded
        reduced = {
            "truncated": True,
            "method": data.get("method"),
            "summary": str(data.get("summary") or data.get("message") or "")[:4000],
        }
        return json.dumps(reduced, ensure_ascii=False)


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
        self.events = EventHub(db)
        self.config.run_log_dir.mkdir(parents=True, exist_ok=True)

    def recover_stale_runs(self) -> None:
        now = utc_now()
        rows = self.db.fetchall("SELECT id FROM runs WHERE status IN ('queued', 'running')")
        for row in rows:
            self.db.execute(
                "UPDATE runs SET status = 'failed', finished_at = ?, error = ?, watcher_state = 'stopped', terminal_reason = 'daemon_restarted', revision = revision + 1 WHERE id = ?",
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

    async def flush_events(self) -> None:
        await self.events.flush()

    def event_diagnostics(self) -> dict[str, Any]:
        return self.events.diagnostics()

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
