import assert from "node:assert/strict";
import test from "node:test";
import { decodeSharedKey, decryptJson, encryptJson, generateSharedKey } from "./crypto.js";

test("AES-GCM round trip binds payload to task context", async () => {
  const key = decodeSharedKey(generateSharedKey());
  const context = { taskId: "task-1", deviceId: "device-1", direction: "controller_to_target" as const, sequence: 0 };
  const encrypted = await encryptJson(key, context, { content: "hello" });
  assert.deepEqual(await decryptJson(key, context, encrypted), { content: "hello" });
  await assert.rejects(() => decryptJson(key, { ...context, taskId: "task-2" }, encrypted));
});
