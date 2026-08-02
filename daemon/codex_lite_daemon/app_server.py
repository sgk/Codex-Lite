from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .deepseek import DEEPSEEK_PROVIDER, ensure_model_catalog
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
    IDLE_SHUTDOWN_SECONDS = 60

    def __init__(self, config: Config, codex_runner: CodexRunner, model_provider: str | None = None) -> None:
        self.config = config
        self.codex_runner = codex_runner
        self.provider = _normalize_provider(model_provider) if model_provider is not None else None
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._subscribers: list[asyncio.Queue[AppServerNotification]] = []
        self._stderr_tail: list[str] = []
        self._last_env: dict[str, str] = {}
        self._desired_model_provider = self.provider or "openai"
        self._active_model_provider: str | None = None
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._active_run_count = 0
        self._idle_shutdown_task: asyncio.Task | None = None

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def stderr_tail(self) -> list[str]:
        return list(self._stderr_tail[-40:])

    async def request(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        self._cancel_idle_shutdown()
        await self.ensure_started()
        try:
            return await asyncio.wait_for(self._request_started(method, params), timeout=timeout or self.REQUEST_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            raise AppError("app_server_timeout", f"Codex app-server request timed out: {method}", 504) from exc
        finally:
            self._schedule_idle_shutdown()

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
        self._cancel_idle_shutdown()
        await self.ensure_started()
        try:
            await self._write({"method": method, "params": params or {}})
        finally:
            self._schedule_idle_shutdown()

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
        if self.is_running and self._active_model_provider == self._desired_model_provider:
            return
        async with self._start_lock:
            if self.is_running and self._active_model_provider == self._desired_model_provider:
                return
            if self.is_running:
                await self.close()
            codex = await self.codex_runner.resolve()
            env = self._env()
            self._last_env = dict(env)
            self._process = await asyncio.create_subprocess_exec(
                *self._command_args(codex, self._desired_model_provider),
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
            self._active_model_provider = self._desired_model_provider
            await self.notify("initialized")

    def set_model_provider(self, provider: str) -> None:
        if self.provider is not None:
            return
        self._desired_model_provider = provider if provider == DEEPSEEK_PROVIDER else "openai"

    def acquire_run(self) -> None:
        self._active_run_count += 1
        self._cancel_idle_shutdown()

    async def release_run(self) -> None:
        self._active_run_count = max(0, self._active_run_count - 1)
        self._schedule_idle_shutdown()

    async def close(self) -> None:
        idle_task = self._idle_shutdown_task
        self._idle_shutdown_task = None
        if idle_task is not None and idle_task is not asyncio.current_task():
            idle_task.cancel()
        process = self._process
        self._process = None
        self._active_model_provider = None
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

    def _cancel_idle_shutdown(self) -> None:
        task = self._idle_shutdown_task
        if task is None:
            return
        self._idle_shutdown_task = None
        task.cancel()

    def _schedule_idle_shutdown(self) -> None:
        if not self.is_running or self._active_run_count > 0 or self._idle_shutdown_task is not None:
            return
        self._idle_shutdown_task = asyncio.create_task(self._close_after_idle())

    async def _close_after_idle(self) -> None:
        try:
            await asyncio.sleep(self.IDLE_SHUTDOWN_SECONDS)
            if self._active_run_count == 0:
                await self.close()
        except asyncio.CancelledError:
            return

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
        if self.provider == DEEPSEEK_PROVIDER:
            return codex_process_env(
                self.config,
                self.config.deepseek_codex_home,
                self.config.deepseek_codex_sqlite_home,
            )
        return codex_process_env(self.config)

    def _command_args(self, codex: str, model_provider: str | None = None) -> list[str]:
        args = [codex]
        if self.config.auto_compact_token_limit > 0:
            args.extend(
                [
                    "-c",
                    f"model_auto_compact_token_limit={self.config.auto_compact_token_limit}",
                    "-c",
                    f'model_auto_compact_token_limit_scope="{self.config.auto_compact_token_limit_scope}"',
                ]
            )
        provider = model_provider or self.provider or self._desired_model_provider
        if provider == DEEPSEEK_PROVIDER:
            catalog_path = ensure_model_catalog(self.config.app_data_dir)
            args.extend(
                [
                    "-c",
                    'model="deepseek-v4-flash"',
                    "-c",
                    'model_provider="deepseek"',
                    "-c",
                    f"model_catalog_json={json.dumps(str(catalog_path), ensure_ascii=False)}",
                    "-c",
                    'model_providers.deepseek.name="DeepSeek"',
                    "-c",
                    'model_providers.deepseek.base_url="https://api.deepseek.com/"',
                    "-c",
                    'model_providers.deepseek.env_key="DEEPSEEK_API_KEY"',
                    "-c",
                    'model_providers.deepseek.wire_api="responses"',
                ]
            )
        args.extend(["app-server", "--listen", "stdio://"])
        return args

    def environment_diagnostics(self) -> dict[str, Any]:
        env = self._last_env
        sock = env.get("SSH_AUTH_SOCK")
        return {
            "captured": bool(env),
            "sshAgentConfigured": bool(sock),
            "sshAgentSocketExists": bool(sock and Path(sock).exists()),
            "provider": self.provider or self._active_model_provider or self._desired_model_provider,
            "activeRunCount": self._active_run_count,
        }


class AppServerClientPool:
    def __init__(self, config: Config, codex_runner: CodexRunner) -> None:
        self._clients = {
            "openai": AppServerClient(config, codex_runner, "openai"),
            DEEPSEEK_PROVIDER: AppServerClient(config, codex_runner, DEEPSEEK_PROVIDER),
        }
        # The OpenAI client is the default lifecycle target. DeepSeek joins
        # the shutdown set once it is actually selected.
        self._used_providers = {"openai"}

    def client_for_provider(self, provider: str) -> AppServerClient:
        normalized = _normalize_provider(provider)
        self._used_providers.add(normalized)
        return self._clients[normalized]

    def diagnostics(self) -> dict[str, dict[str, Any]]:
        return {provider: client.environment_diagnostics() | {"running": client.is_running} for provider, client in self._clients.items()}

    async def close(self) -> None:
        await asyncio.gather(*(self._clients[provider].close() for provider in self._used_providers))


def _normalize_provider(provider: str | None) -> str:
    return provider if provider == DEEPSEEK_PROVIDER else "openai"


def _app_server_error(error: dict[str, Any]) -> AppError:
    message = error.get("message") if isinstance(error, dict) else None
    return AppError("app_server_error", str(message or "Codex app-server request failed."), 502, {"error": error})
