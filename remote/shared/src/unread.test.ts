import assert from "node:assert/strict";
import test from "node:test";
import { latestMessageVersion, messageReadState } from "./unread.js";

const work = { id: "work-1", role: "assistant", kind: "work", content: "進捗", createdAt: "2026-10-25T01:00:00Z" };

test("作業内容と結論の新規メッセージだけが未読を増やす", () => {
  const first = latestMessageVersion([work]);
  const initial = messageReadState(undefined, first, false);
  assert.equal(initial.unread, false);
  assert.deepEqual(messageReadState(initial, first, false), initial);
  assert.equal(messageReadState(initial, latestMessageVersion([{ ...work, createdAt: "2026-10-25T01:02:00Z" }]), false).unread, false);
  assert.equal(latestMessageVersion([work, { ...work, id: "status", role: "status", kind: "activity" }]), first);
  assert.equal(latestMessageVersion([work, { ...work, id: "waiting", kind: "waiting" }]), first);
  assert.equal(latestMessageVersion([work, { ...work, id: "empty", content: " " }]), first);
  assert.equal(latestMessageVersion([{ ...work, content: "本文の再整形" }]), first);
  const next = latestMessageVersion([work, { ...work, id: "answer", kind: "conclusion", createdAt: "2026-10-25T01:01:00Z" }]);
  assert.equal(messageReadState(initial, next, false).unread, true);
  assert.equal(messageReadState(initial, next, true).unread, false);
  assert.equal(messageReadState({ version: "", unread: false }, first, false).unread, true);
});

test("過去履歴の再取り込み・欠落・再配信で既読基準を巻き戻さない", () => {
  const version = latestMessageVersion([work]);
  const read = { version, unread: false };
  assert.deepEqual(messageReadState(read, "", false), read);
  const older = { ...work, id: "old", createdAt: "2026-10-24T01:00:00Z" };
  assert.deepEqual(messageReadState(read, latestMessageVersion([older]), false), read);
  assert.deepEqual(messageReadState(read, latestMessageVersion([work, older]), false), read);
  assert.equal(messageReadState({ version, unread: true }, version, true).unread, false);
});
