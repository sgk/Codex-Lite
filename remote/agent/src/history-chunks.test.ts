import assert from "node:assert/strict";
import test from "node:test";
import { historyChunks } from "./history-chunks.js";

const entry = (id: string, content: string) => ({ id, role: "status", content, createdAt: id, kind: "work" });

test("履歴をFirestore文書上限より十分小さい安定チャンクへ分割する", () => {
  const entries = Array.from({ length: 400 }, (_, index) => entry(String(index), "あ".repeat(2000)));
  const first = historyChunks("chat-1", entries);
  const second = historyChunks("chat-1", entries);
  assert.ok(first.length > 1);
  assert.deepEqual(first, second);
  assert.deepEqual(JSON.parse(first.map((chunk) => chunk.payload).join("")), entries);
  assert.ok(first.every((chunk) => Buffer.byteLength(chunk.payload, "utf8") < 500_000));
});

test("末尾追加では既存の確定チャンクを変更しない", () => {
  const entries = Array.from({ length: 170 }, (_, index) => entry(String(index), "work"));
  const before = historyChunks("chat-1", entries);
  const after = historyChunks("chat-1", [...entries, entry("new", "new")]);
  assert.equal(before[0].hash, after[0].hash);
  assert.notEqual(before.at(-1)?.hash, after.at(-1)?.hash);
});

test("単一履歴項目が文書上限を超えてもUTF-8を壊さず分割して復元する", () => {
  const entries = [entry("large", "あ".repeat(500_000))];
  const chunks = historyChunks("chat-large", entries);
  assert.ok(chunks.length > 1);
  assert.ok(chunks.every((chunk) => Buffer.byteLength(chunk.payload, "utf8") <= 400_000));
  assert.deepEqual(JSON.parse(chunks.map((chunk) => chunk.payload).join("")), entries);
});
