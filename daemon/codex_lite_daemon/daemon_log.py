"""Bound stdout/stderr on disk, including non-logging-library writes."""
from __future__ import annotations

import os
import logging
import sys
import threading
import traceback
from copy import deepcopy
from pathlib import Path
from typing import Callable

MAX_LOG_BYTES = 10 * 1024 * 1024


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("daemon log write made no progress")
        view = view[written:]


def compact_existing_log(path: Path, limit: int = MAX_LOG_BYTES) -> bool:
    """Preserve the tail, then truncate the same inode (also safe for O_APPEND owners)."""
    if limit < 1:
        raise ValueError("log limit must be positive")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
        size = os.fstat(fd).st_size
        if size <= limit:
            return False
        tail = os.pread(fd, limit, size - limit)
        previous = path.with_name("daemon.previous.log")
        backup = os.open(previous, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.fchmod(backup, 0o600)
            _write_all(backup, tail)
            os.fsync(backup)
        finally:
            os.close(backup)
        os.ftruncate(fd, 0)
        return True
    finally:
        os.close(fd)


class BoundedLogFile:
    def __init__(self, path: Path, limit: int = MAX_LOG_BYTES):
        if limit < 1:
            raise ValueError("log limit must be positive")
        self.path = path
        self.previous = path.with_name("daemon.previous.log")
        self.limit = limit
        compact_existing_log(path, limit)
        if self.previous.exists():
            self.previous.chmod(0o600)
        # Old backups must not remain unlimited either.
        if self.previous.exists() and self.previous.stat().st_size > limit:
            with self.previous.open("rb") as source:
                source.seek(-limit, os.SEEK_END)
                tail = source.read(limit)
            fd = os.open(self.previous, os.O_WRONLY | os.O_TRUNC)
            try:
                os.fchmod(fd, 0o600)
                _write_all(fd, tail)
            finally:
                os.close(fd)
        self.fd = self._open()
        self.size = os.fstat(self.fd).st_size

    def _open(self) -> int:
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        return fd

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            if self.size == self.limit:
                os.close(self.fd)
                self.fd = -1
                os.replace(self.path, self.previous)
                self.fd = self._open()
                self.size = 0
            amount = min(len(view), self.limit - self.size)
            _write_all(self.fd, view[:amount])
            self.size += amount
            view = view[amount:]

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


class ErrorAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn access args: client, method, path, http_version, status_code.
        if not isinstance(record.args, tuple) or len(record.args) != 5:
            return True
        try:
            return int(record.args[4]) >= 400
        except (TypeError, ValueError):
            return True


def daemon_logging_config() -> dict:
    from uvicorn.config import LOGGING_CONFIG

    config = deepcopy(LOGGING_CONFIG)
    config["filters"] = {"error_access": {"()": ErrorAccessFilter}}
    config["handlers"]["access"]["filters"] = ["error_access"]
    return config


class RedirectedDaemonLog:
    """Drain both output FDs through one bounded writer, independent of desktop pipes."""

    def __init__(self, path: Path, on_error: Callable[[], None], limit: int = MAX_LOG_BYTES):
        self.sink = BoundedLogFile(path, limit)
        self.on_error = on_error
        self.error: BaseException | None = None
        self.reader, self.writer = os.pipe()
        self.thread = threading.Thread(target=self._drain, name="daemon-log", daemon=True)

    def __enter__(self):
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(self.writer, 1)
        os.dup2(self.writer, 2)
        os.close(self.writer)
        self.thread.start()
        return self

    def _drain(self) -> None:
        try:
            while data := os.read(self.reader, 65536):
                self.sink.write(data)
        except BaseException as error:
            self.error = error
            self.on_error()
        finally:
            os.close(self.reader)
            self.sink.close()

    def __exit__(self, exc_type, exc_value, exc_traceback):
        try:
            # The caller's original desktop pipes may no longer exist. Capture
            # an escaping exception before detaching the bounded log writer.
            if exc_type is not None:
                traceback.print_exception(exc_type, exc_value, exc_traceback, file=sys.stderr)
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            # Do not retain or restore a desktop's pipes after it has disconnected.
            null = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(null, 1)
                os.dup2(null, 2)
            finally:
                os.close(null)
            self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("daemon log writer did not stop")
        if self.error is not None:
            raise RuntimeError("daemon log writer failed") from self.error
