from __future__ import annotations

import io
import json
import os
import select
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from urllib.request import urlopen

import pytest

from codex_lite_daemon.daemon_instance import DaemonInstance
from test_api import linux_tmp_path, make_test_config


def test_single_owner_lock_is_released_and_not_unlinked(linux_tmp_path):
    cfg = make_test_config(linux_tmp_path)
    first, second = DaemonInstance(cfg), DaemonInstance(cfg)
    try:
        assert first.acquire()
        assert not second.acquire()
        first.close()
        assert second.acquire()
    finally:
        first.close()
        second.close()


def test_live_legacy_daemon_is_not_overwritten(linux_tmp_path):
    cfg = make_test_config(linux_tmp_path)
    cfg.app_data_dir.mkdir()
    endpoint = cfg.app_data_dir / "daemon-endpoint.json"
    text = json.dumps({"host": "127.0.0.1", "port": 12345, "pid": os.getpid()})
    endpoint.write_text(text)
    instance = DaemonInstance(cfg)
    try:
        with pytest.raises(RuntimeError, match="旧方式のデーモン"):
            instance.acquire()
        assert endpoint.read_text() == text
    finally:
        instance.close()


def test_sharing_rejects_different_home_without_releasing_owner(linux_tmp_path):
    cfg = make_test_config(linux_tmp_path)
    owner = DaemonInstance(cfg)
    stopped = threading.Event()
    try:
        assert owner.acquire()
        owner.serve_clients({"event": "ready", "host": "127.0.0.1", "port": 12345}, stopped.set)
        peer = DaemonInstance(replace(cfg, codex_home=linux_tmp_path / "different-home"))
        assert not peer.acquire()
        with pytest.raises(RuntimeError, match="異なります"):
            peer.attach(io.StringIO(""), io.StringIO())
        peer.close()
        assert not stopped.is_set()
        owner.release_client()
        assert stopped.wait(1)
    finally:
        owner.close()


def test_owner_startup_timeout_never_creates_a_second_owner(linux_tmp_path):
    cfg = make_test_config(linux_tmp_path)
    owner, peer = DaemonInstance(cfg), DaemonInstance(cfg)
    try:
        assert owner.acquire()
        assert not peer.acquire()
        peer.STARTUP_TIMEOUT = 0.01
        with pytest.raises(RuntimeError, match="二重起動は行いません"):
            peer.attach(io.StringIO(""), io.StringIO())
        assert not peer.acquire()
    finally:
        owner.close()
        peer.close()


def ready(process):
    assert process.stdout is not None
    assert select.select([process.stdout], [], [], 15)[0], "daemon ready timeout"
    line = process.stdout.readline()
    assert line, "daemon exited before ready"
    return json.loads(line)


@pytest.mark.parametrize("close_owner_first,simultaneous", [(True, False), (False, False), (True, True), (False, True)])
def test_two_desktops_share_one_server_and_last_disconnect_stops_it(linux_tmp_path, close_owner_first, simultaneous):
    """Real entrypoint, HTTP server, DB and EOF lifecycle; no Codex/API processes."""
    cfg = make_test_config(linux_tmp_path)
    env = {**os.environ,
           "CODEX_LITE_APP_DATA_DIR": str(cfg.app_data_dir),
           "CODEX_LITE_DATABASE": str(cfg.database_path),
           "CODEX_LITE_RUN_LOG_DIR": str(cfg.run_log_dir),
           "CODEX_LITE_CODEX_HOME": str(cfg.codex_home),
           "CODEX_LITE_CODEX_SQLITE_HOME": str(cfg.codex_sqlite_home),
           "CODEX_LITE_CONFIG": str(linux_tmp_path / "absent.toml"),
           "CODEX_LITE_RUNNER": "fake", "CODEX_LITE_PORT": "0"}
    processes = []
    # Own subprocesses only; neither the real daemon nor the real Codex home is used.
    with (linux_tmp_path / "daemon.stderr").open("w+") as errors:
        def start():
            process = subprocess.Popen([sys.executable, "-m", "codex_lite_daemon.main"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                                       text=True, env=env, cwd=Path(__file__).parents[1])
            processes.append(process)
            return process

        try:
            owner = start()
            early_peer = start() if simultaneous else None
            endpoint = ready(owner)
            if early_peer is not None:
                peer_endpoint = ready(early_peer)
                assert peer_endpoint == endpoint
                if endpoint["pid"] == early_peer.pid:
                    owner, early_peer = early_peer, owner
            # Force startup recovery to be observable if the second client creates_app.
            with sqlite3.connect(cfg.database_path) as db:
                db.execute("INSERT INTO projects(id,name,path,created_at,updated_at) VALUES ('p','test',?,'2026-10-19','2026-10-19')", (str(linux_tmp_path),))
                db.execute("INSERT INTO chats(id,project_id,title,created_at,updated_at) VALUES ('c','p','test','2026-10-19','2026-10-19')")
                db.execute("INSERT INTO runs(id,chat_id,status) VALUES ('r','c','running')")
            original_endpoint = (cfg.app_data_dir / "daemon-endpoint.json").read_bytes()
            peer = early_peer if simultaneous else start()
            if not simultaneous:
                assert ready(peer) == endpoint
            assert peer.pid != owner.pid == endpoint["pid"]
            assert (cfg.app_data_dir / "daemon-endpoint.json").read_bytes() == original_endpoint
            with sqlite3.connect(cfg.database_path) as db:
                assert db.execute("SELECT status FROM runs WHERE id='r'").fetchone()[0] == "running"
            first = owner if close_owner_first else peer
            last = peer if close_owner_first else owner
            first.stdin.close()
            if first is owner:
                # The original desktop's output reader also disappears on exit.
                owner.stdout.close()
            if first is peer:
                first.wait(timeout=5)
            # EOF of one desktop must not stop another desktop's shared server.
            time.sleep(0.1)
            assert owner.poll() is None and last.poll() is None
            with urlopen(f"http://127.0.0.1:{endpoint['port']}/projects", timeout=5) as response:
                assert response.status == 200
            last.stdin.close()
            for process in processes:
                assert process.wait(timeout=15) == 0
            assert not (cfg.app_data_dir / "daemon-endpoint.json").exists()
            assert not (cfg.app_data_dir / "daemon-control.sock").exists()
            log_path = cfg.app_data_dir / "daemon.log"
            assert log_path.stat().st_mode & 0o777 == 0o600
            assert "Finished server process" in log_path.read_text()
            # A subsequent launch can acquire the same owner lock normally.
            next_owner = DaemonInstance(cfg)
            try:
                assert next_owner.acquire()
            finally:
                next_owner.close()
        finally:
            for process in processes:
                if process.poll() is None:
                    if process.stdin and not process.stdin.closed:
                        process.stdin.close()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        process.wait(timeout=5)
                if process.stdout:
                    process.stdout.close()
