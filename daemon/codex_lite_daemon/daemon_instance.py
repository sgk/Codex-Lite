"""One database owner, with stdin-lifetime leases for desktop clients."""
from __future__ import annotations

import errno
import fcntl
import json
import os
import socket
import stat
import threading
import time
from typing import Callable, TextIO

from .config import Config


class DaemonInstance:
    STARTUP_TIMEOUT = 180.0

    def __init__(self, config: Config) -> None:
        self.config = config
        self.path = config.app_data_dir / "daemon-control.sock"
        self.endpoint_path = config.app_data_dir / "daemon-endpoint.json"
        self._lock = None
        self._listener: socket.socket | None = None
        self._guard = threading.Lock()
        self._clients = 1  # The owner's stdin is the first client lease.
        self._closing = False
        self._stop: Callable[[], None] = lambda: None
        self._ready: dict = {}
        self._owns_socket = False

    def acquire(self) -> bool:
        # Lock beside the DB, not the executable: every deployment must share it.
        self.config.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.app_data_dir.mkdir(parents=True, exist_ok=True)
        database_path = self.config.database_path.resolve()
        self._lock = (database_path.parent / (database_path.name + ".daemon.lock")).open("a+b")
        os.chmod(self._lock.name, 0o600)
        try:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            self._lock = None
            return False
        # Pre-lock versions must be stopped explicitly; never overwrite their endpoint.
        if self.endpoint_path.exists():
            endpoint = json.loads(self.endpoint_path.read_text(encoding="utf-8"))
            pid = endpoint.get("pid")
            if not isinstance(pid, int) or pid <= 0:
                raise RuntimeError("既存デーモンの接続先に有効なPIDがありません。起動を停止しました。")
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise RuntimeError(f"旧方式のデーモン PID {pid} が残っています。既存のCodex Liteをすべて正常終了してから起動してください。")
        return True

    def _identity(self) -> dict:
        return {
            "databasePath": str(self.config.database_path.resolve()),
            "codexHome": str(self.config.codex_home.resolve()),
            "runnerMode": self.config.runner_mode,
        }

    def serve_clients(self, ready: dict, stop: Callable[[], None]) -> None:
        self._ready = ready
        self._stop = stop
        if self.path.exists():
            if not stat.S_ISSOCK(self.path.lstat().st_mode):
                raise RuntimeError(f"デーモン制御ソケットの場所に別のファイルがあります: {self.path}")
            self.path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.path))
        os.chmod(self.path, 0o600)
        listener.listen(16)
        listener.settimeout(0.2)
        self._listener = listener
        self._owns_socket = True
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        listener = self._listener
        assert listener is not None
        while True:
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                with self._guard:
                    if self._closing:
                        return
                continue
            except OSError:
                return
            threading.Thread(target=self._client, args=(connection,), daemon=True).start()

    def _client(self, connection: socket.socket) -> None:
        attached = False
        try:
            connection.settimeout(10)
            with connection, connection.makefile("rwb") as stream:
                request = json.loads(stream.readline(8192))
                with self._guard:
                    if self._closing:
                        response = {"error": "既存デーモンは終了処理中です。終了後に起動してください。"}
                    elif request != self._identity():
                        response = {"error": "既存デーモンとDB・Codex HOME・実行モードが異なります。共有接続を停止しました。"}
                    else:
                        self._clients += 1
                        attached = True
                        response = self._ready
                stream.write((json.dumps(response) + "\n").encode())
                stream.flush()
                connection.settimeout(None)
                if attached:
                    while stream.read(1):
                        pass
        except (OSError, ValueError):
            # A disconnected/unregistered client never owns the daemon.
            pass
        finally:
            connection.close()
            if attached:
                self.release_client()

    def release_client(self) -> None:
        with self._guard:
            self._clients -= 1
            if self._clients == 0:
                self._closing = True
                self._stop()

    def attach(self, stdin: TextIO, stdout: TextIO) -> None:
        # An occupied owner lock selects this protocol exclusively. No new daemon
        # is created on timeout, bad metadata, mismatched config, or connection failure.
        deadline = time.monotonic() + self.STARTUP_TIMEOUT
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(10)
            while True:
                try:
                    connection.connect(str(self.path))
                    break
                except OSError as exc:
                    if exc.errno not in {errno.ENOENT, errno.ECONNREFUSED}:
                        raise
                    if time.monotonic() >= deadline:
                        raise RuntimeError("既存デーモンの制御ソケットが準備されませんでした。二重起動は行いません。") from exc
                    time.sleep(0.05)
            with connection.makefile("rwb") as stream:
                stream.write((json.dumps(self._identity()) + "\n").encode())
                stream.flush()
                ready = json.loads(stream.readline(8192))
                if "error" in ready:
                    raise RuntimeError(ready["error"])
                if ready.get("event") != "ready" or ready.get("host") != "127.0.0.1" or not isinstance(ready.get("port"), int):
                    raise RuntimeError("既存デーモンの接続情報が不正です。")
                print(json.dumps(ready), file=stdout, flush=True)
                # Keep only a lease, not a second HTTP server/app-server/scheduler.
                for _ in stdin:
                    pass

    def close(self) -> None:
        with self._guard:
            self._closing = True
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        if self._owns_socket:
            self.path.unlink(missing_ok=True)
            self._owns_socket = False
        if self._lock is not None:
            self._lock.close()
            self._lock = None
