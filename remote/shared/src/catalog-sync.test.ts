import test from "node:test";
import assert from "node:assert/strict";
import { catalogRecordCount, catalogRetryDecision, cloudCatalogSnapshot, diffCatalog, prioritizeCatalogChanges, type CatalogProject } from "../../agent/src/catalog-sync.js";

const first: CatalogProject[] = [
  {
    id: "project-1",
    name: "One",
    chats: [
      { id: "chat-1", title: "First", status: "idle", historyRevision: "r1" },
      { id: "chat-2", title: "Second", status: "idle", historyRevision: "r1" },
    ],
  },
  { id: "project-2", name: "Two", chats: [] },
];

test("差分同期は変更されたレコードだけを抽出する", () => {
  const current = structuredClone(first);
  current[0].chats[0].historyRevision = "r2";
  const changes = diffCatalog(first, current);

  assert.equal(changes.changedProjects.length, 0);
  assert.deepEqual(changes.changedChats.map((item) => item.chat.id), ["chat-1"]);
  assert.equal(changes.unchangedRecords, 3);
  assert.equal(catalogRecordCount(current), 4);
});

test("並び順の変更は位置が変わったレコードだけを抽出する", () => {
  const current = [
    first[1],
    { ...first[0], chats: [...first[0].chats].reverse() },
  ];
  const changes = diffCatalog(first, current);

  assert.deepEqual(changes.changedProjects.map((item) => item.project.id), ["project-2", "project-1"]);
  assert.deepEqual(changes.changedChats.map((item) => item.chat.id), ["chat-2", "chat-1"]);
  assert.equal(changes.unchangedRecords, 0);
});

test("削除されたプロジェクトとチャットを差分として扱う", () => {
  const current = [{ ...first[0], chats: [first[0].chats[0]] }];
  const changes = diffCatalog(first, current);

  assert.deepEqual(changes.removedChats, [{ projectId: "project-1", chatId: "chat-2" }]);
  assert.deepEqual(changes.removedProjects, [{ projectId: "project-2", chatIds: [] }]);
});

test("デスクトップRunの開始と進行シーケンスを差分として扱う", () => {
  const running = structuredClone(first);
  running[0].chats[0].status = "running";
  running[0].chats[0].activeRunId = "run-1";
  running[0].chats[0].activeRunEventSequence = 4;
  assert.deepEqual(diffCatalog(first, running).changedChats.map((item) => item.chat.id), ["chat-1"]);

  const progressed = structuredClone(running);
  progressed[0].chats[0].activeRunEventSequence = 9;
  assert.deepEqual(diffCatalog(running, progressed).changedChats.map((item) => item.chat.id), ["chat-1"]);
});

test("選択中は転送だけを優先し表示順の値を変更しない", () => {
  const changes = diffCatalog(undefined, first);
  const prioritized = prioritizeCatalogChanges(changes, first, "project-1", "chat-2");

  assert.deepEqual(
    prioritized.map((record) => record.kind === "project" ? record.project.id : record.chat.id),
    ["project-1", "chat-2", "chat-1", "project-2"],
  );
  assert.equal(prioritized[0].syncOrder, 0);
  assert.equal(prioritized[1].syncOrder, 1);
});

test("同じカタログ版の同期失敗は待機を増やして3回で止める", () => {
  assert.deepEqual(catalogRetryDecision(1, 10, 9), { backoffMs: 30_000 });
  assert.deepEqual(catalogRetryDecision(2, 10, 9), { backoffMs: 60_000 });
  assert.deepEqual(catalogRetryDecision(3, 10, 9), { stopRevision: 10, backoffMs: 0 });
  assert.deepEqual(catalogRetryDecision(3, 10, 10), { backoffMs: 120_000 });
});

test("起動時は軽量なクラウドカタログを前回同期状態として復元する", () => {
  assert.deepEqual(cloudCatalogSnapshot(
    [
      { id: "project-2", name: "Two", syncOrder: 1 },
      { id: "project-1", name: "One", syncOrder: 0 },
    ],
    [
      { id: "chat-2", projectId: "project-1", title: "Second", status: "idle", historyRevision: "r1", syncOrder: 1 },
      { id: "chat-1", projectId: "project-1", title: "First", status: "idle", historyRevision: "r1", syncOrder: 0 },
    ],
  ), first);
});
