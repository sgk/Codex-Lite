"""Opt-in, real app-server smoke check; never attaches to the user's daemon.

Run with daemon/.venv/bin/python and PYTHONPATH=daemon. Only its newly created
test threads/process are operated on; no existing thread content is read.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from codex_lite_daemon.app_server import AppServerClient, SERVER_REQUEST_ID_KEY
from codex_lite_daemon.config import load_config
from codex_lite_daemon.errors import AppError
from codex_lite_daemon.runner.codex_runner import CodexRunner


def process_identity(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return (int(stat[1]), stat[19])  # Parent PID and start ticks, never argv.
    except (FileNotFoundError, ProcessLookupError):
        return None


def descendants(root):
    snapshot = {int(path.name): process_identity(int(path.name)) for path in Path("/proc").iterdir() if path.name.isdigit()}
    owned = {root}
    while True:
        added = {pid for pid, identity in snapshot.items() if identity and identity[0] in owned}
        if added <= owned:
            break
        owned.update(added)
    return {pid: snapshot[pid][1] for pid in owned if snapshot.get(pid)}


async def windows_nodes():
    process = await asyncio.create_subprocess_exec(
        "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", "-NoProfile", "-Command",
        "@(Get-CimInstance Win32_Process -Filter \"Name = 'node_repl.exe'\" | Select-Object ProcessId,CreationDate) | ConvertTo-Json -Compress",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    assert process.returncode == 0, "Windows process inspection failed"
    rows = json.loads(stdout.decode("utf-8-sig")) if stdout.strip() else []
    if isinstance(rows, dict):
        rows = [rows]
    return {(row["ProcessId"], row["CreationDate"]) for row in rows}


async def main(run_turns: bool, windows_node: str | None):
    config = load_config()
    client = AppServerClient(config, CodexRunner(config), "openai")
    threads = []
    baseline_windows = await windows_nodes() if windows_node else set()
    owned_windows = set()
    owned_linux = {}
    try:
        await client.prepare()
        print(json.dumps({"appServerPid": client._process.pid}), flush=True)
        with tempfile.TemporaryDirectory(prefix="codex-lite-lifecycle-") as directory:
            for _ in range(2):
                response = await client.request("thread/start", {
                    "cwd": directory, "approvalPolicy": "never", "sandbox": "workspace-write",
                    "developerInstructions": "This is an isolated Codex Lite lifecycle test. Do not inspect any files outside this test directory. Follow the short test instruction exactly.",
                    "config": {"features.code_mode": True, "js_repl_node_path": windows_node} if windows_node else {},
                })
                threads.append(response["thread"]["id"])
            first, second = threads
            async def turn(thread, text):
                client.acquire_run()
                queue = client.subscribe_queue(thread)
                try:
                    await client.request("turn/start", {"threadId": thread, "input": [{"type": "text", "text": text}]})
                    deadline = asyncio.get_running_loop().time() + 90
                    while True:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            raise TimeoutError("Test turn exceeded its total deadline")
                        notification = await asyncio.wait_for(queue.get(), remaining)
                        if SERVER_REQUEST_ID_KEY in notification.params:
                            raise RuntimeError(f"Test requires an unsupported client response: {notification.method}")
                        if notification.method == "item/completed":
                            item = notification.params.get("item", {})
                            print(json.dumps({"completedItemType": item.get("type")}), flush=True)
                        if notification.method == "turn/completed":
                            status = notification.params["turn"]["status"]
                            assert status == "completed", status
                            return
                        if notification.method == "app_server/exited":
                            raise RuntimeError("app-server exited during test turn")
                finally:
                    client.unsubscribe_queue(queue)
                    await client.release_run()

            if windows_node:
                for thread in threads:
                    queue = client.subscribe_queue(thread)
                    try:
                        result = await client.request("turn/start", {"threadId": thread, "input": [{"type": "text", "text": "Reply only OK. Do not inspect files."}]})
                        await asyncio.sleep(1)
                        owned_linux.update(descendants(client._process.pid))
                        owned_windows.update(await windows_nodes() - baseline_windows)
                        assert not await client._can_close_idle()
                        await client.request("turn/interrupt", {"threadId": thread, "turnId": result["turn"]["id"]})
                        async with asyncio.timeout(15):
                            while True:
                                event = await queue.get()
                                if event.method == "turn/completed":
                                    assert event.params["turn"]["status"] == "interrupted"
                                    break
                    finally:
                        client.unsubscribe_queue(queue)
                print(json.dumps({"windowsNodeInterrupted": True, "activeProtection": True}), flush=True)
            elif run_turns:
                await asyncio.gather(
                    turn(first, "Reply only OK. Do not use any tools."),
                    turn(second, "Reply only OK. Do not use tools."),
                )
                print(json.dumps({"parallelCompletion": True}), flush=True)
            # No model request is needed to check unsubscribe/grace.
            assert await client.release_thread_if_unused(first)
            loaded = await client.request("thread/loaded/list", {})
            assert first in loaded["data"], "unsubscribe must not be treated as immediate unload"
            if run_turns and not windows_node:
                await turn(first, "Reply only RESUMED. Do not use tools.")
            queue = client.subscribe_queue(first)
            assert not await client.release_thread_if_unused(first)
            client.unsubscribe_queue(queue)
            assert await client.release_thread_if_unused(first)
            assert await client.release_thread_if_unused(second)
            assert await client._can_close_idle()
            print(json.dumps({"unsubscribeGrace": True, "resume": run_turns, "subscriberProtection": True, "idleEligible": True}), flush=True)
            # A real local RPC error must not leak a pending Future or
            # prevent later cleanup. No invalid request is sent to the cloud.
            try:
                await client.request("turn/start", {"threadId": first})
            except AppError:
                pass
            else:
                raise AssertionError("Malformed local request unexpectedly succeeded")
            assert not client._pending
            print(json.dumps({"localErrorCleanup": True}), flush=True)
            process = client._process
            owned_linux.update(descendants(process.pid))
            if windows_node:
                assert len(owned_windows) >= 2, "Test did not start both Windows runtimes"
                assert owned_windows & await windows_nodes(), "Must verify shutdown without archiving/killing runtimes first"
            # Exercise the actual idle task, not an explicit force-close.
            client.IDLE_SHUTDOWN_SECONDS = .05
            client._cancel_idle_shutdown()
            client._schedule_idle_shutdown()
            idle_task = client._idle_shutdown_task
            await asyncio.wait_for(asyncio.shield(idle_task), 30)
            assert process.returncode is not None
            async with asyncio.timeout(10):
                while any((identity := process_identity(pid)) and identity[1] == start for pid, start in owned_linux.items()):
                    await asyncio.sleep(.1)
            if windows_node:
                assert not (await windows_nodes() & owned_windows)
            print(json.dumps({"linuxProcessesReaped": len(owned_linux), "windowsNodesReaped": len(owned_windows)}), flush=True)
            print(json.dumps({"appServerExited": True, "returncode": process.returncode}), flush=True)
            # Archive test metadata only AFTER verifying idle shutdown and
            # process collection. This starts a separate short-lived server.
            for thread in threads:
                await client.request("thread/archive", {"threadId": thread})
            threads.clear()
    finally:
        # Only threads created above belong to this check.
        try:
            for thread in threads:
                if client.is_running:
                    await client.request_if_running("thread/archive", {"threadId": thread})
        finally:
            await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--turns", action="store_true", help="Also run three small authenticated model turns")
    parser.add_argument("--windows-node", metavar="WSL_EXE_PATH", help="Exercise interruption with the specified Windows node_repl executable")
    args = parser.parse_args()
    asyncio.run(main(args.turns, args.windows_node))
