import assert from "node:assert/strict";
import test from "node:test";
import { appendProgressItem, isCountableProgressEvent, type RemoteProgressItem } from "./progress-sync.js";

test("reasoning deltas are combined without creating a Firestore write per delta", () => {
  let items = appendProgressItem([], { sequence: 1, event: "progress", data: { method: "item/reasoning/textDelta", details: '{"delta":"考え"}' }, observedAt: "2026-09-01T10:00:00Z" });
  items = appendProgressItem(items, { sequence: 2, event: "progress", data: { method: "item/reasoning/textDelta", details: '{"delta":"中"}' } });
  assert.equal(items.length, 1);
  assert.equal(items[0].firstSequence, 1);
  assert.equal(items[0].sequence, 2);
  assert.equal(items[0].kind, "reasoning");
  assert.equal(items[0].createdAt, "2026-09-01T10:00:00Z");
  assert.equal(items[0].details, "考え中");
});

test("作業項目は生JSONではなく要約だけを表示用に残す", () => {
  const items = appendProgressItem([], {
    sequence: 3,
    event: "progress",
    data: {
      method: "item/started",
      summary: "コマンドを実行中: npm test",
      details: '{"item":{"id":"cmd-1","type":"commandExecution","command":"npm test"}}',
    },
  });
  assert.equal(items.length, 1);
  assert.equal(items[0].text, "コマンドを実行中: npm test");
  assert.equal(items[0].details, undefined);
});

test("agent message境界とreasoning項目境界は履歴項目にしない", () => {
  let items: RemoteProgressItem[] = [];
  items = appendProgressItem(items, { sequence: 1, event: "progress", data: { method: "item/started", summary: "agentMessage", details: '{"item":{"type":"agentMessage"}}' } });
  items = appendProgressItem(items, { sequence: 2, event: "progress", data: { method: "item/started", summary: "reasoning", details: '{"item":{"type":"reasoning"}}' } });
  assert.deepEqual(items, []);
});

test("only the latest bounded progress items are retained", () => {
  let items: RemoteProgressItem[] = [];
  for (let sequence = 1; sequence <= 30; sequence += 1) {
    items = appendProgressItem(items, { sequence, event: "progress", data: { method: `item/tool/${sequence}`, summary: `作業 ${sequence}` } });
  }
  assert.equal(items.length, 24);
  assert.equal(items[0].sequence, 7);
  assert.equal(items[23].sequence, 30);
});

test("本文deltaは表示内容へ結合するが進行件数には数えない", () => {
  assert.equal(isCountableProgressEvent({ sequence: 1, event: "progress", data: { method: "item/reasoning/summaryTextDelta" } }), false);
  assert.equal(isCountableProgressEvent({ sequence: 2, event: "progress", data: { method: "item/commandExecution/outputDelta" } }), false);
  assert.equal(isCountableProgressEvent({ sequence: 3, event: "progress", data: { method: "item/completed" } }), true);
});
