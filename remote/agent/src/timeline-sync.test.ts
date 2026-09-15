import assert from "node:assert/strict";
import test from "node:test";
import type { RemoteProgressItem } from "./progress-sync.js";
import { changedProgressItems, MAX_TIMELINE_ITEMS_PER_DOCUMENT, timelineDocumentId, timelineItemBatches } from "./timeline-sync.js";

function item(firstSequence: number, sequence = firstSequence, text = `item-${firstSequence}`): RemoteProgressItem {
  return { firstSequence, sequence, kind: "work", method: "item/completed", text, createdAt: "2026-09-15T00:00:00Z" };
}

test("タイムラインには新規項目と内容が変わった項目だけを渡す", () => {
  assert.deepEqual(changedProgressItems(
    [item(1), item(2, 2, "before")],
    [item(1), item(2, 3, "after"), item(4)],
  ).map((value) => value.firstSequence), [2, 4]);
});

test("Firestore文書サイズを抑えるため進捗項目を固定件数で分割する", () => {
  const batches = timelineItemBatches(Array.from({ length: MAX_TIMELINE_ITEMS_PER_DOCUMENT + 1 }, (_, index) => item(index + 1)));
  assert.deepEqual(batches.map((batch) => batch.length), [MAX_TIMELINE_ITEMS_PER_DOCUMENT, 1]);
});

test("タイムライン文書IDはRunとシーケンス範囲から決定する", () => {
  assert.equal(timelineDocumentId("run/1", [item(8), item(3, 5)]), "cnVuLzE_000000000003_000000000008");
});
