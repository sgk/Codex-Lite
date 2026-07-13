from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .errors import AppError
from .process_env import codex_process_env
from .runner.codex_runner import CodexRunner


@dataclass
class AppServerNotification:
    method: str
    params: dict[str, Any]


class AppServerClient:
    REQUEST_TIMEOUT_SECONDS = 20
    STDIO_LIMIT_BYTES = 64 * 1024 * 1024

    def __init__(self, config: Config, codex_runner: CodexRunner) -> None:
        self.config = config
        self.codex_runner = codex_runner
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._subscribers: list[asyncio.Queue[AppServerNotification]] = []
        self._stderr_tail: list[str] = []
        self._last_env: dict[str, str] = {}
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def stderr_tail(self) -> list[str]:
        return list(self._stderr_tail[-40:])

    async def request(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        await self.ensure_started()
        try:
            return await asyncio.wait_for(self._request_started(method, params), timeout=timeout or self.REQUEST_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            raise AppError("app_server_timeout", f"Codex app-server request timed out: {method}", 504) from exc

    async def _request_started(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self._process is not None
        assert self._process.stdin is not None
        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        await self._write({"id": request_id, "method": method, "params": params or {}})
        return await future

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self.ensure_started()
        await self._write({"method": method, "params": params or {}})

    async def subscribe(self) -> AsyncIterator[AppServerNotification]:
        queue = self.subscribe_queue()
        try:
            while True:
                yield await queue.get()
        finally:
            self.unsubscribe_queue(queue)

    def subscribe_queue(self) -> asyncio.Queue[AppServerNotification]:
        queue: asyncio.Queue[AppServerNotification] = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe_queue(self, queue: asyncio.Queue[AppServerNotification]) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    async def ensure_started(self) -> None:
        if self.is_running:
            return
        async with self._start_lock:
            if self.is_running:
                return
            codex = await self.codex_runner.resolve()
            env = self._env()
            self._last_env = dict(env)
            self._process = await asyncio.create_subprocess_exec(
                codex,
                "app-server",
                "--listen",
                "stdio://",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=self.STDIO_LIMIT_BYTES,
            )
            self._reader_task = asyncio.create_task(self._read_stdout())
            self._stderr_task = asyncio.create_task(self._read_stderr())
            try:
                response = await asyncio.wait_for(
                    self._request_started(
                        "initialize",
                        {
                            "clientInfo": {
                                "name": "codex_lite",
                                "title": "Codex Lite",
                                "version": "0.1.0",
                            },
                            "capabilities": {"experimentalApi": True},
                        },
                    ),
                    timeout=self.REQUEST_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError as exc:
                raise AppError("app_server_timeout", "Codex app-server initialize timed out.", 504) from exc
            if "userAgent" not in response:
                raise AppError("app_server_initialize_failed", "Codex app-server did not initialize.", 503)
            await self.notify("initialized")

    async def close(self) -> None:
        process = self._process
        self._process = None
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()
        if process is not None and process.returncode is None:
            if process.stdin is not None:
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except Exception:
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()

    async def _write(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise AppError("app_server_not_running", "Codex app-server is not running.", 503)
        async with self._write_lock:
            process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            await process.stdin.drain()

    async def _read_stdout(self) -> None:
        process = self._process
        assert process is not None
        assert process.stdout is not None
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            if "id" in message:
                request_id = message["id"]
                future = self._pending.pop(request_id, None)
                if future is None or future.done():
                    continue
                if "error" in message:
                    future.set_exception(_app_server_error(message["error"]))
                else:
                    future.set_result(message.get("result") or {})
                continue
            method = message.get("method")
            if isinstance(method, str):
                notification = AppServerNotification(method, message.get("params") or {})
                for queue in list(self._subscribers):
                    await queue.put(notification)
        for future in self._pending.values():
            if not future.done():
                future.set_exception(AppError("app_server_closed", "Codex app-server exited.", 503))
        self._pending.clear()

    async def _read_stderr(self) -> None:
        process = self._process
        assert process is not None
        assert process.stderr is not None
        while True:
            line = await process.stderr.readline()
            if not line:
                break
            self._stderr_tail.append(line.decode("utf-8", errors="replace").rstrip())
            self._stderr_tail = self._stderr_tail[-80:]

    def _env(self) -> dict[str, str]:
        return codex_process_env(self.config)

    def environment_diagnostics(self) -> dict[str, Any]:
        env = self._last_env
        sock = env.get("SSH_AUTH_SOCK")
        return {
            "captured": bool(env),
            "sshAgentConfigured": bool(sock),
            "sshAgentSocketExists": bool(sock and Path(sock).exists()),
        }


def _app_server_error(error: dict[str, Any]) -> AppError:
    message = error.get("message") if isinstance(error, dict) else None
    return AppError("app_server_error", str(message or "Codex app-server request failed."), 502, {"error": error})
