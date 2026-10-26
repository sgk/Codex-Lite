"""On-demand stack locations only: never inspect locals or source text."""
from __future__ import annotations

import sys
import threading
import json
from pathlib import Path


def thread_snapshot() -> list[dict]:
    frames = sys._current_frames()
    result = []
    try:
        for thread in threading.enumerate():
            frame = frames.get(thread.ident)
            stack = []
            while frame is not None and len(stack) < 24:
                stack.append({"file": Path(frame.f_code.co_filename).name,
                              "function": frame.f_code.co_name, "line": frame.f_lineno})
                frame = frame.f_back
            result.append({"name": thread.name, "nativeId": thread.native_id, "stack": stack})
        return result
    finally:
        # Frame objects must not keep requests and their locals alive.
        frames.clear()


def capture_startup_threads(path: Path, intervals: tuple[float, ...] = (10, 20, 30)) -> threading.Thread:
    """Bounded startup capture also works when the HTTP event loop is blocked."""
    def capture():
        samples = []
        for interval in intervals:
            threading.Event().wait(interval)
            samples.append(thread_snapshot())
            path.write_text(json.dumps(samples, ensure_ascii=False), encoding="utf-8")

    worker = threading.Thread(target=capture, name="startup-thread-diagnostics", daemon=True)
    worker.start()
    return worker
