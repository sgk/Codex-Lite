import assert from "node:assert/strict";
import test from "node:test";
import { isCountableProgressEvent } from "./progress-sync.js";

test("deltaと思考は同期せず完了した作業だけを進行件数に数える", () => {
  assert.equal(isCountableProgressEvent({ sequence: 1, event: "progress", data: { method: "item/reasoning/summaryTextDelta" } }), false);
  assert.equal(isCountableProgressEvent({ sequence: 2, event: "progress", data: { method: "item/commandExecution/outputDelta" } }), false);
  assert.equal(isCountableProgressEvent({ sequence: 3, event: "progress", data: { method: "item/completed", details: '{"item":{"type":"reasoning"}}' } }), false);
  assert.equal(isCountableProgressEvent({ sequence: 4, event: "progress", data: { method: "item/completed", details: '{"item":{"type":"commandExecution"}}' } }), true);
  assert.equal(isCountableProgressEvent({ sequence: 5, event: "progress", data: { method: "item/started", details: '{"item":{"type":"commandExecution"}}' } }), false);
  assert.equal(isCountableProgressEvent({ sequence: 6, event: "progress", data: { method: "mcp_tool_call_end" } }), true);
});
