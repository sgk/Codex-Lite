from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from .base import Runner, RunnerEvent


class FakeRunner(Runner):
    async def run(self, prompt: str, project_path: str) -> AsyncIterator[RunnerEvent]:
        yield RunnerEvent("stdout", f"Fake Codex run in {project_path}\n")
        await asyncio.sleep(0.01)
        yield RunnerEvent("stdout", f"Prompt: {prompt}\n")
        await asyncio.sleep(0.01)
        yield RunnerEvent("stderr", "fake runner diagnostic\n")
        await asyncio.sleep(0.01)
        yield RunnerEvent("stdout", "done\n")
