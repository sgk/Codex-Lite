from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from codex_lite_daemon.daemon_log import BoundedLogFile, ErrorAccessFilter, compact_existing_log


def test_rotation_is_bounded_for_many_writes(tmp_path):
    path = tmp_path / "daemon.log"
    data = bytes(range(256)) * 40
    sink = BoundedLogFile(path, limit=256)
    try:
        for offset in range(0, len(data), 37):
            sink.write(data[offset:offset + 37])
            assert path.stat().st_size <= 256
            assert not sink.previous.exists() or sink.previous.stat().st_size <= 256
    finally:
        sink.close()
    assert sink.previous.read_bytes() + path.read_bytes() == data[-512:]
    assert path.stat().st_mode & 0o777 == 0o600
    assert sink.previous.stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["daemon.log", "daemon.previous.log"]


def test_single_oversized_record_cannot_exceed_limit(tmp_path):
    path = tmp_path / "daemon.log"
    sink = BoundedLogFile(path, limit=128)
    try:
        sink.write(b"x" * 10000)
    finally:
        sink.close()
    assert path.stat().st_size <= 128
    assert sink.previous.stat().st_size == 128


def test_live_legacy_file_is_truncated_without_replacing_its_inode(tmp_path):
    path = tmp_path / "daemon.log"
    path.write_bytes(b"old" * 1000 + b"diagnostic-tail")
    original = path.stat().st_ino
    with path.open("ab", buffering=0) as active_writer:
        expected = path.read_bytes()[-128:]
        assert compact_existing_log(path, 128)
        assert path.stat().st_ino == original
        assert path.stat().st_size == 0
        assert path.with_name("daemon.previous.log").read_bytes() == expected
        active_writer.write(b"new event\n")
    assert path.read_bytes() == b"new event\n"
    assert not compact_existing_log(path, 128)


def test_sparse_huge_file_reads_only_tail_and_caps_old_backup(tmp_path):
    path = tmp_path / "daemon.log"
    with path.open("wb") as file:
        file.seek(100 * 1024**3)
        file.write(b"retained-tail")
    sink = BoundedLogFile(path, 128)
    sink.close()
    assert path.stat().st_size == 0
    assert path.with_name("daemon.previous.log").read_bytes().endswith(b"retained-tail")
    path.with_name("daemon.previous.log").write_bytes(b"a" * 9999)
    path.with_name("daemon.previous.log").chmod(0o644)
    sink = BoundedLogFile(path, 128)
    sink.close()
    assert path.with_name("daemon.previous.log").stat().st_size == 128
    assert path.with_name("daemon.previous.log").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("status,retained", [(200, False), (204, False), (302, False), (400, True), (401, True), (404, True), (429, True), (500, True)])
def test_access_errors_are_retained(status, retained):
    record = logging.LogRecord("uvicorn.access", logging.INFO, "test", 0, "%s %s %s %s %s", ("local", "GET", "/health", "1.1", status), None)
    assert ErrorAccessFilter().filter(record) is retained


def test_unknown_access_record_is_not_silently_dropped():
    record = logging.LogRecord("uvicorn.access", logging.ERROR, "test", 0, "diagnostic", (), None)
    assert ErrorAccessFilter().filter(record)


def test_raw_stdout_stderr_are_rotated_and_drained_on_exit(tmp_path):
    path = tmp_path / "daemon.log"
    code = """
import os,sys
from pathlib import Path
from codex_lite_daemon.daemon_log import RedirectedDaemonLog
with RedirectedDaemonLog(Path(sys.argv[1]), lambda: None, limit=512):
    for i in range(1000):
        os.write(1, b'progress\\n')
        os.write(2, b'error\\n')
    print('FINAL-STDOUT', flush=True)
    print('FINAL-STDERR', file=sys.stderr, flush=True)
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])}
    result = subprocess.run([sys.executable, "-c", code, str(path)], env=env, capture_output=True, timeout=10)
    assert result.returncode == 0
    assert result.stdout == result.stderr == b""
    assert path.stat().st_size <= 512
    assert path.with_name("daemon.previous.log").stat().st_size <= 512
    retained = path.with_name("daemon.previous.log").read_bytes() + path.read_bytes()
    assert b"FINAL-STDOUT" in retained and b"FINAL-STDERR" in retained


def test_unhandled_exception_is_saved_before_detaching_output(tmp_path):
    path = tmp_path / "daemon.log"
    code = """
import sys
from pathlib import Path
from codex_lite_daemon.daemon_log import RedirectedDaemonLog
with RedirectedDaemonLog(Path(sys.argv[1]), lambda: None, limit=4096):
    raise RuntimeError('preserve-this-error')
"""
    result = subprocess.run([sys.executable, "-c", code, str(path)],
                            env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])},
                            capture_output=True, timeout=10)
    assert result.returncode != 0
    assert b"Traceback" in path.read_bytes()
    assert b"RuntimeError: preserve-this-error" in path.read_bytes()
