import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { loadOrCreateDeviceIdentity, parseFirebaseConnection } from "./config.js";

test("同期先は明示設定だけを使用し、不在・不正なら停止する", () => {
  for (const raw of [undefined, "", " ", "{}", "null", "[]", '{"projectId":1}', "private-invalid-json"]) {
    assert.throws(() => parseFirebaseConnection(raw));
  }
  const firebase = { projectId: "external-project", apiKey: "test-key", appId: "test-app", authDomain: "external-project.firebaseapp.com" };
  assert.deepEqual(parseFirebaseConnection(JSON.stringify(firebase)), firebase);
  assert.deepEqual(parseFirebaseConnection(JSON.stringify({ ...firebase, authDomain: null })),
    { projectId: firebase.projectId, apiKey: firebase.apiKey, appId: firebase.appId });
  for (const key of ["projectId", "apiKey", "appId"]) {
    assert.throws(() => parseFirebaseConnection(JSON.stringify({ ...firebase, [key]: " " })));
  }
  assert.throws(() => parseFirebaseConnection(JSON.stringify({ ...firebase, authDomain: 42 })));
  assert.throws(() => parseFirebaseConnection("private-invalid-json"), error =>
    error instanceof Error && !error.message.includes("private-invalid-json"));
});

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
