import json
import threading

from codex_lite_daemon.thread_diagnostics import capture_startup_threads, thread_snapshot


def test_snapshot_reports_locations_without_local_values():
    ready = threading.Event()
    release = threading.Event()

    def diagnostic_worker():
        secret_local = "private-local-value-not-for-diagnostics"
        ready.set()
        release.wait(5)
        return secret_local

    worker = threading.Thread(target=diagnostic_worker, name="diagnostic-test-worker")
    worker.start()
    try:
        assert ready.wait(1)
        snapshot = thread_snapshot()
        entry = next(item for item in snapshot if item["nativeId"] == worker.native_id)
        assert any(frame["function"] == "diagnostic_worker" for frame in entry["stack"])
        assert all(set(frame) == {"file", "function", "line"} for frame in entry["stack"])
        assert all("/" not in frame["file"] for frame in entry["stack"])
        assert "private-local-value-not-for-diagnostics" not in json.dumps(snapshot)
    finally:
        release.set()
        worker.join(1)


def test_startup_capture_is_bounded(tmp_path):
    path = tmp_path / "threads.json"
    worker = capture_startup_threads(path, intervals=(0, 0))
    worker.join(2)
    assert not worker.is_alive()
    samples = json.loads(path.read_text())
    assert len(samples) == 2
    assert any(t["name"] == "startup-thread-diagnostics" for t in samples[0])
