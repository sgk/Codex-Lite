import assert from "node:assert/strict";
import test from "node:test";
import { isCountableProgressEvent, shouldRelayAcceptedRun } from "./progress-sync.js";

test("終了境界の追加指示で新しいRunが始まればWebへ実行状態を中継する", () => {
  assert.equal(shouldRelayAcceptedRun("steer_run", "next", "finished"), true);
  assert.equal(shouldRelayAcceptedRun("steer_run", "active", "active"), false);
  assert.equal(shouldRelayAcceptedRun("send_message", "new", ""), true);
  assert.equal(shouldRelayAcceptedRun("create_chat", "new", ""), true);
  assert.equal(shouldRelayAcceptedRun("cancel_run", "old", "old"), false);
  assert.equal(shouldRelayAcceptedRun("steer_run", "", "old"), false);
});

test("deltaと思考は同期せず完了した作業だけを進行件数に数える", () => {
  assert.equal(isCountableProgressEvent({ sequence: 1, event: "progress", data: { method: "item/reasoning/summaryTextDelta" } }), false);
  assert.equal(isCountableProgressEvent({ sequence: 2, event: "progress", data: { method: "item/commandExecution/outputDelta" } }), false);
  assert.equal(isCountableProgressEvent({ sequence: 3, event: "progress", data: { method: "item/completed", details: '{"item":{"type":"reasoning"}}' } }), false);
  assert.equal(isCountableProgressEvent({ sequence: 4, event: "progress", data: { method: "item/completed", details: '{"item":{"type":"commandExecution"}}' } }), true);
  assert.equal(isCountableProgressEvent({ sequence: 5, event: "progress", data: { method: "item/started", details: '{"item":{"type":"commandExecution"}}' } }), false);
  assert.equal(isCountableProgressEvent({ sequence: 6, event: "progress", data: { method: "mcp_tool_call_end" } }), true);
});
