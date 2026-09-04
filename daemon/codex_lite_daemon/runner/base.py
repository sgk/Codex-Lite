from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass(frozen=True)
class RunnerEvent:
    stream: str
    text: str


class Runner:
    async def run(self, prompt: str, project_path: str) -> AsyncIterator[RunnerEvent]:
        raise NotImplementedError
