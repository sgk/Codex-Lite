import assert from "node:assert/strict";
import test from "node:test";
import { activityCountLabel, approvalSummary, chatSelectionFromHash, chatSelectionHash, chatTreeIndicator, completedRunVersion, composerDraftContextKey, composerOperation, createdChatSelectionAction, deepSeekBalanceLabel, historyChunksAreComplete, historyUpdatePosition, isSupportedImageMimeType, mergeProgressWindow, parseHistoryChunk, permissionModeForSettings, permissionSettingsForMode, projectTreeIndicator, remainingChargeLabel, sidebarSwipeAction, shouldKeepCompletedProgress, shouldSubmitComposer, sortConversationTimeline, titleFromFirstInstruction } from "./conversation-behavior.js";

test("新規チャットのタイトルはデスクトップと同じく最初の指示から作る", () => {
  assert.equal(titleFromFirstInstruction("  最初の\n\n指示です  "), "最初の 指示です");
  const long = "あ".repeat(81);
  assert.equal(titleFromFirstInstruction(long), `${"あ".repeat(77)}...`);
});

test("実行中チャットへの送信は同じRunへの追加指示にする", () => {
  assert.equal(composerOperation(true, "run-1"), "create_chat");
  assert.equal(composerOperation(false, ""), "send_message");
  assert.equal(composerOperation(false, "run-1"), "steer_run");
});

test("履歴チャンクJSONは配列だけを復元する", () => {
  assert.deepEqual(parseHistoryChunk('[{"id":"one"}]'), [{ id: "one" }]);
  assert.deepEqual(parseHistoryChunk('{"id":"one"}'), []);
  assert.deepEqual(parseHistoryChunk('broken'), []);
});

test("チャット切替時と末尾閲覧中だけ履歴末尾へ追従する", () => {
  assert.equal(historyUpdatePosition(true, false), "end");
  assert.equal(historyUpdatePosition(false, true), "end");
  assert.equal(historyUpdatePosition(false, false, true), "end");
  assert.equal(historyUpdatePosition(false, false), "preserve");
});

test("永続履歴とライブ作業を時刻順に統合する", () => {
  const sorted = sortConversationTimeline([
    { id: "conclusion", createdAt: "2026-09-01T20:20:16Z", sourceOrder: 0 },
    { id: "work", createdAt: "2026-09-01T19:19:53Z", sourceOrder: 1 },
    { id: "unknown", sourceOrder: 2 },
  ]);
  assert.deepEqual(sorted.map((item) => item.id), ["work", "conclusion", "unknown"]);
});

test("今回作成したチャットは一覧へ到着した時点で選択する", () => {
  assert.equal(createdChatSelectionAction("running", "chat-1", false), "wait");
  assert.equal(createdChatSelectionAction("running", "chat-1", true), "select");
  assert.equal(createdChatSelectionAction("failed", "", false), "clear");
});

test("承認理由と対象コマンドを表示する", () => {
  assert.equal(approvalSummary("ファイルを変更します", "git status"), "ファイルを変更します / 対象コマンド: git status");
  assert.equal(approvalSummary("", ""), "この操作の承認が必要です。");
});

test("デスクトップと同じ承認レベルをRuntime設定へ変換する", () => {
  assert.deepEqual(permissionSettingsForMode("ask-for-approval"), { permissionProfile: ":workspace", approvalPolicy: "on-request", approvalsReviewer: "user" });
  assert.deepEqual(permissionSettingsForMode("approve-for-me"), { permissionProfile: ":workspace", approvalPolicy: "on-request", approvalsReviewer: "auto_review" });
  assert.deepEqual(permissionSettingsForMode("full-access"), { permissionProfile: ":danger-full-access", approvalPolicy: "never", approvalsReviewer: "user" });
  assert.equal(permissionModeForSettings(":workspace", "auto_review"), "approve-for-me");
  assert.equal(permissionModeForSettings(":danger-full-access", "user"), "full-access");
});

test("残りチャージ額は数値でも表示する", () => {
  assert.equal(remainingChargeLabel({ balance: 12.5, hasCredits: true }), "残り 12.5");
  assert.equal(remainingChargeLabel({ unlimited: true }), "無制限");
});

test("DeepSeek残高は通貨ごとの合計を表示する", () => {
  assert.equal(deepSeekBalanceLabel({ status: "ok", isAvailable: true, balanceInfos: [{ currency: "USD", totalBalance: "12.50" }, { currency: "CNY", totalBalance: "8.00" }] }), "USD 12.50 / CNY 8.00");
  assert.equal(deepSeekBalanceLabel({ status: "unavailable", isAvailable: false, balanceInfos: [] }), "取得不可");
  assert.equal(deepSeekBalanceLabel(undefined), "未設定");
});

test("チャット選択をハッシュURLへ往復変換する", () => {
  const selection = { deviceId: "自宅 PC", projectId: "project/a", chatId: "chat#1" };
  const hash = chatSelectionHash(selection);
  assert.equal(hash, "#/devices/%E8%87%AA%E5%AE%85%20PC/projects/project%2Fa/chats/chat%231");
  assert.deepEqual(chatSelectionFromHash(hash), selection);
  assert.equal(chatSelectionFromHash("#/invalid"), undefined);
});

test("Enterで送信しShift+EnterとIME変換中は送信しない", () => {
  assert.equal(shouldSubmitComposer("Enter", false, false), true);
  assert.equal(shouldSubmitComposer("Enter", true, false), false);
  assert.equal(shouldSubmitComposer("Enter", false, true), false);
  assert.equal(shouldSubmitComposer("Enter", false, false, true), false);
});

test("画像添付で扱えるMIMEタイプだけを受け入れる", () => {
  assert.equal(isSupportedImageMimeType("image/png"), true);
  assert.equal(isSupportedImageMimeType("IMAGE/JPEG"), true);
  assert.equal(isSupportedImageMimeType("image/svg+xml"), false);
  assert.equal(isSupportedImageMimeType("text/plain"), false);
});

test("新規チャットと既存チャットの下書きを別の文脈として識別する", () => {
  assert.equal(composerDraftContextKey("pc", "project", ""), '["pc","project","@new-chat"]');
  assert.equal(composerDraftContextKey("pc", "project", "chat"), '["pc","project","chat"]');
  assert.equal(composerDraftContextKey("", "project", "chat"), "");
});

test("完了Runの進捗は永続履歴へ到着するまで保持する", () => {
  assert.equal(shouldKeepCompletedProgress("run-2", ["run-1"]), true);
  assert.equal(shouldKeepCompletedProgress("run-2", ["run-1", "run-2"]), false);
  assert.equal(shouldKeepCompletedProgress("", []), false);
});

test("履歴チャンクは全ハッシュが揃った時だけ完成とみなす", () => {
  assert.equal(historyChunksAreComplete(["a", "b"], [{ index: 0, hash: "a" }, { index: 1, hash: "b" }]), true);
  assert.equal(historyChunksAreComplete(["a", "b"], [{ index: 0, hash: "new" }, { index: 1, hash: "b" }]), false);
  assert.equal(historyChunksAreComplete(["a", "b"], [{ index: 0, hash: "a" }]), false);
});

test("進捗の短い受信窓を既に表示した項目へ累積する", () => {
  const retained = [{ firstSequence: 1, kind: "reasoning", text: "方針" }, { firstSequence: 2, kind: "work", text: "調査" }];
  const incoming = [{ firstSequence: 2, kind: "work", text: "調査完了" }, { firstSequence: 3, kind: "work", text: "修正" }];
  assert.deepEqual(mergeProgressWindow(retained, incoming).map((item) => item.text), ["方針", "調査完了", "修正"]);
});

test("思考本文を表示せず進行件数だけを表示する", () => {
  assert.equal(activityCountLabel(3, 5), "進行 8件（思考 3・作業 5）");
  assert.equal(activityCountLabel(2, 0), "進行 2件（思考 2）");
  assert.equal(activityCountLabel(0, 0), "");
});

test("チャットツリーは実行中を未読より優先して表示する", () => {
  assert.equal(chatTreeIndicator("running", "", true), "running");
  assert.equal(chatTreeIndicator("idle", "waiting_for_approval", true), "running");
  assert.equal(chatTreeIndicator("idle", "", true), "unread");
  assert.equal(chatTreeIndicator("idle", "", false), "none");
  assert.equal(projectTreeIndicator(["unread", "running"]), "running");
  assert.equal(projectTreeIndicator(["none", "unread"]), "unread");
  assert.equal(projectTreeIndicator(["none"]), "none");
});

test("未読判定は履歴改訂ではなく最後に完了したRunを使う", () => {
  assert.equal(completedRunVersion({ id: "run-2", activityCount: 8 }), "run-2");
  assert.equal(completedRunVersion({ activityCount: 8 }), "");
  assert.equal(completedRunVersion(undefined), "");
});

test("画面内の横スワイプでサイドバーを開閉する", () => {
  assert.equal(sidebarSwipeAction(180, 260, 20, false), "open");
  assert.equal(sidebarSwipeAction(260, 180, 20, true), "close");
  assert.equal(sidebarSwipeAction(180, 228, 0, false), "none");
  assert.equal(sidebarSwipeAction(180, 260, 60, false), "none");
  assert.equal(sidebarSwipeAction(180, 260, 0, true), "none");
});
