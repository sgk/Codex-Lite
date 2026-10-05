import assert from "node:assert/strict";
import test from "node:test";
import { ConnectionHealth } from "./connection-health.js";

test("既存のheartbeatとサーバー由来の受信で通信正常を通知できる", () => {
  const health = new ConnectionHealth();
  assert.deepEqual(health.confirmed("heartbeat"), { state: "transport_ready", phase: "heartbeat" });
  assert.deepEqual(health.confirmed("listen"), { state: "transport_ready", phase: "listen" });
});

test("通信成功だけでカタログ同期中や同期失敗を解消しない", () => {
  const health = new ConnectionHealth();
  health.catalogStarted();
  assert.equal(health.confirmed("heartbeat"), undefined);
  health.catalogFailed();
  assert.equal(health.confirmed("listen"), undefined);
  assert.equal(health.confirmed("heartbeat"), undefined);
  health.catalogCompleted();
  assert.ok(health.confirmed("heartbeat"));
});

test("受信購読の失敗をheartbeatやカタログ同期成功で隠さない", () => {
  const health = new ConnectionHealth();
  health.listenerError();
  health.catalogCompleted();
  assert.equal(health.confirmed("heartbeat"), undefined);
  assert.ok(health.confirmed("listen"));
});
