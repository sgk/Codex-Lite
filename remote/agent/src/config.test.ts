import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { loadOrCreateDeviceIdentity } from "./config.js";

test("端末IDは初回だけ生成し同じWSL状態ファイルを再利用する", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "codex-lite-remote-device-"));
  try {
    const identityPath = path.join(directory, "state", "remote-device.json");
    const first = await loadOrCreateDeviceIdentity(identityPath);
    const second = await loadOrCreateDeviceIdentity(identityPath);
    assert.match(first.deviceId, /^pc-[a-f0-9]{32}$/);
    assert.ok(first.displayName);
    assert.deepEqual(second, first);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("壊れた端末IDファイルを別経路で置き換えない", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "codex-lite-remote-device-invalid-"));
  try {
    const identityPath = path.join(directory, "remote-device.json");
    await writeFile(identityPath, JSON.stringify({ deviceId: "", displayName: "" }));
    await assert.rejects(loadOrCreateDeviceIdentity(identityPath), /deviceIdが不正/);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
