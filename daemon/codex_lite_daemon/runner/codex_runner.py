from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

from ..config import Config
from ..errors import AppError
from ..process_env import codex_process_env
from .base import Runner, RunnerEvent


class CodexRunner(Runner):
    def __init__(self, config: Config) -> None:
        self.config = config
        self.codex_path: str | None = None
        self.codex_version: str | None = None

    async def resolve(self) -> str:
        if self.codex_path:
            return self.codex_path
        candidates = self._candidate_paths()
        for candidate in candidates:
            version = await self._try_version(candidate)
            if version is not None:
                self.codex_path = candidate
                self.codex_version = version
                return self.codex_path
        raise AppError("codex_not_found", "Codex binary was not found.", 503)

    def resolved_path_sync(self) -> str | None:
        if self.codex_path:
            return self.codex_path
        candidates = self._candidate_paths()
        return candidates[0] if candidates else None

    def _candidate_paths(self) -> list[str]:
        candidates: list[str] = []
        if self.config.codex_path:
            candidates.append(self.config.codex_path)
        candidates.extend(str(path) for path in sorted((self.config.codex_home / "bin" / "wsl").glob("*/codex"), reverse=True))
        candidates.extend(self._codex_desktop_candidate_paths())
        home = Path.home()
        candidates.extend(str(path) for path in sorted((home / ".vscode-server" / "extensions").glob("*/bin/linux-x86_64/codex"), reverse=True))
        candidates.extend(str(path) for path in sorted((home / ".codex" / "bin").glob("*/codex"), reverse=True))
        path_codex = shutil.which("codex")
        if path_codex:
            candidates.append(path_codex)
        seen: set[str] = set()
        unique: list[str] = []
        for candidate in candidates:
            if candidate not in seen:
                seen.add(candidate)
                unique.append(candidate)
        return unique

    def _codex_desktop_candidate_paths(self) -> list[str]:
        windows_home = _windows_home_from_codex_home(self.config.codex_home)
        if windows_home is None:
            return []
        local_app_data = windows_home / "AppData" / "Local"
        roots = [
            local_app_data / "Programs",
            local_app_data / "Codex",
            local_app_data / "OpenAI",
        ]
        candidates: list[str] = []
        for root in roots:
            if not root.exists():
                continue
            candidates.extend(str(path) for path in _desktop_bundle_candidates(root))
        return candidates

    async def _try_version(self, candidate: str) -> str | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                candidate,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env(),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
        except Exception:
            return None
        if proc.returncode != 0:
            return None
        text = (stdout or stderr).decode("utf-8", errors="replace").strip()
        return text or "unknown"

    async def run(self, prompt: str, project_path: str) -> AsyncIterator[RunnerEvent]:
        codex = await self.resolve()
        proc = await asyncio.create_subprocess_exec(
            codex,
            "exec",
            "--color",
            "never",
            "--skip-git-repo-check",
            "-",
            cwd=project_path,
            env=self._env(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        queue: asyncio.Queue[RunnerEvent | int] = asyncio.Queue()

        async def read_stream(stream: asyncio.StreamReader, name: str) -> None:
            while True:
                chunk = await stream.readline()
                if not chunk:
                    break
                await queue.put(RunnerEvent(name, chunk.decode("utf-8", errors="replace")))

        readers = [asyncio.create_task(read_stream(proc.stdout, "stdout")), asyncio.create_task(read_stream(proc.stderr, "stderr"))]
        waiter = asyncio.create_task(proc.wait())
        pending_exit: int | None = None
        try:
            while True:
                if pending_exit is None and waiter.done():
                    pending_exit = waiter.result()
                if pending_exit is not None and all(task.done() for task in readers) and queue.empty():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.05)
                except asyncio.TimeoutError:
                    continue
                if isinstance(item, RunnerEvent):
                    yield item
            if pending_exit is None:
                pending_exit = await waiter
            if pending_exit != 0:
                raise AppError("codex_run_failed", f"Codex exited with status {pending_exit}.", 500, {"exitCode": pending_exit})
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.send_signal(2)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
            raise
        finally:
            for task in readers:
                if not task.done():
                    task.cancel()

    def _env(self) -> dict[str, str]:
        return codex_process_env(self.config)


def _windows_home_from_codex_home(codex_home: Path) -> Path | None:
    path = codex_home
    if path.name != ".codex":
        return None
    parent = path.parent
    parts = parent.parts
    if len(parts) >= 4 and parts[:3] == ("/", "mnt", "c") and parts[3] == "Users":
        return parent
    return None


def _desktop_bundle_candidates(root: Path) -> list[Path]:
    patterns = [
        "Codex*/resources/app/bin/linux-x86_64/codex",
        "Codex*/resources/app.asar.unpacked/bin/linux-x86_64/codex",
        "Codex*/resources/app*/bin/linux-x86_64/codex",
        "OpenAI*/resources/app/bin/linux-x86_64/codex",
        "OpenAI*/resources/app.asar.unpacked/bin/linux-x86_64/codex",
        "OpenAI*/resources/app*/bin/linux-x86_64/codex",
        "*/resources/app/bin/linux-x86_64/codex",
        "*/resources/app.asar.unpacked/bin/linux-x86_64/codex",
        "*/resources/app*/bin/linux-x86_64/codex",
        "resources/app/bin/linux-x86_64/codex",
        "resources/app.asar.unpacked/bin/linux-x86_64/codex",
        "resources/app*/bin/linux-x86_64/codex",
    ]
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(root.glob(pattern))
    return candidates
