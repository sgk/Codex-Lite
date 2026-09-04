import { initializeApp } from "firebase/app";
import { mkdir, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { getAuth, GoogleAuthProvider, signInWithCredential } from "firebase/auth";
import {
  collection,
  deleteField,
  getDocs,
  doc,
  getFirestore,
  onSnapshot,
  query,
  runTransaction,
  serverTimestamp,
  setDoc,
  updateDoc,
  where,
  writeBatch,
  type DocumentData,
  type DocumentReference,
} from "firebase/firestore";
import type { RemoteOperation, RemoteTaskPayload } from "../../shared/src/types.js";
import { catalogRetryDecision, diffCatalog, prioritizeCatalogChanges, type CatalogProject } from "./catalog-sync.js";
import { loadAgentConfig } from "./config.js";
import { firebaseGoogleAccessToken } from "./credentials.js";
import { LocalDaemon, type SseEvent } from "./daemon.js";
import { appendProgressItem, isCountableProgressEvent, type RemoteProgressItem } from "./progress-sync.js";
import { historyChunks } from "./history-chunks.js";

interface QueuedTask {
  operation: RemoteOperation;
  payload?: RemoteTaskPayload;
  status: string;
}

const config = await loadAgentConfig();
const app = initializeApp(config.firebase);
const auth = getAuth(app);
const login = await signInWithCredential(auth, GoogleAuthProvider.credential(null, firebaseGoogleAccessToken()));
const uid = login.user.uid;
const db = getFirestore(app);
const daemon = new LocalDaemon(config);
let catalogSyncRunning = false;
let lastCatalogSnapshot: CatalogProject[] | undefined;
let lastCatalogRevision = -1;
let lastRevisionCheckErrorAt = 0;
let catalogWatchStopped = false;
let catalogSyncFailures = 0;
let heartbeatRunning = false;
const relayedRunIds = new Set<string>();
const desktopRunWatchers = new Map<string, Promise<void>>();
await daemon.connect();
const deviceId = config.deviceId;
const deviceRef = doc(db, "users", uid, "devices", deviceId);
try {
  await reconcileInterruptedTasks();
} catch (error) {
  reportBackgroundError(error);
}
try {
  await setDoc(deviceRef, {
    displayName: config.displayName,
    remoteEnabled: true,
    status: "online",
    clientVersion: "0.1.0",
    heartbeatSeconds: config.heartbeatSeconds,
    lastSeenAt: serverTimestamp(),
    updatedAt: serverTimestamp(),
  }, { merge: true });
} catch (error) {
  reportBackgroundError(error);
}
const processingTasks = new Set<string>();
const queuedTasks = query(collection(deviceRef, "tasks"), where("status", "==", "queued"));
const unsubscribe = onSnapshot(queuedTasks, (snapshot) => {
  for (const change of snapshot.docChanges()) {
    if (change.type !== "added" && change.type !== "modified") continue;
    if (processingTasks.has(change.doc.id)) continue;
    processingTasks.add(change.doc.id);
    void processTask(change.doc.ref, change.doc.data() as QueuedTask)
      .catch(reportBackgroundError)
      .finally(() => processingTasks.delete(change.doc.id));
  }
}, reportBackgroundError);
const heartbeat = setInterval(() => void heartbeatNow(), config.heartbeatSeconds * 1000);
try {
  await syncCatalog();
} catch (error) {
  catalogSyncFailures = 1;
  reportBackgroundError(error);
}
void watchCatalogRevision();

console.log(`Codex Lite Remote Agent: online (${config.displayName})`);
console.log("ペイロード暗号化: 初期版では無効（HTTPS + Firestore Rulesのみ）");

for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, () => void shutdown());
}

async function reconcileInterruptedTasks(): Promise<void> {
  const interrupted = await getDocs(query(
    collection(deviceRef, "tasks"),
    where("status", "in", ["claimed", "running", "waiting_for_approval"]),
  ));
  for (let offset = 0; offset < interrupted.docs.length; offset += 450) {
    const batch = writeBatch(db);
    for (const task of interrupted.docs.slice(offset, offset + 450)) {
      batch.update(task.ref, {
        status: "connection_lost",
        errorCode: "agent_restarted",
        error: { message: "Remote Agentの再起動により実行状態を終了しました。" },
        completedAt: serverTimestamp(),
        updatedAt: serverTimestamp(),
      });
    }
    await batch.commit();
  }
}

async function processTask(taskRef: DocumentReference<DocumentData>, initial: QueuedTask): Promise<void> {
  const claimed = await runTransaction(db, async (transaction) => {
    const snapshot = await transaction.get(taskRef);
    if (!snapshot.exists() || snapshot.data().status !== "queued") return false;
    transaction.update(taskRef, {
      status: "claimed",
      claimedByDeviceId: deviceId,
      claimedAt: serverTimestamp(),
      updatedAt: serverTimestamp(),
    });
    return true;
  });
  if (!claimed) return;

  const taskId = taskRef.id;
  let cleanup = async (): Promise<void> => {};
  try {
    const payload = await taskPayload(taskId, initial);
    const prepared = await prepareTaskPayload(taskId, payload);
    cleanup = prepared.cleanup;
    await updateDoc(taskRef, { status: "running", startedAt: serverTimestamp(), updatedAt: serverTimestamp() });
    const result = initial.operation === "get_runtime"
      ? await daemon.runtime(textField(prepared.payload, "projectId"), textField(prepared.payload, "chatId") || undefined)
      : initial.operation === "update_runtime"
        ? await daemon.updateRuntime(
          textField(prepared.payload, "projectId"),
          textField(prepared.payload, "chatId") || undefined,
          textField(prepared.payload, "model"),
          textField(prepared.payload, "reasoningEffort"),
          textField(prepared.payload, "permissionProfile"),
          textField(prepared.payload, "approvalPolicy"),
          textField(prepared.payload, "approvalsReviewer"),
        )
        : await daemon.execute(taskId, initial.operation, prepared.payload);
    const runId = textField(result, "runId") || textField(result, "id");
    const relaysCreatedRun = Boolean(runId && (initial.operation === "send_message" || initial.operation === "create_chat"));
    if (relaysCreatedRun) relayedRunIds.add(runId);
    const publicResult = initial.operation === "get_runtime" || initial.operation === "update_runtime" ? result : {
      ...(runId ? { runId } : {}),
      ...(isObject(result.chat) ? { chat: { id: textField(result.chat, "id"), title: textField(result.chat, "title") } } : {}),
    };
    try {
      await updateDoc(taskRef, {
        result: publicResult,
        ...(runId ? { runId } : {}),
        updatedAt: serverTimestamp(),
      });
    } catch (error) {
      if (relaysCreatedRun) relayedRunIds.delete(runId);
      throw error;
    }
    if (relaysCreatedRun) {
      const projectId = textField(prepared.payload, "projectId");
      const chatId = textField(prepared.payload, "chatId") || (isObject(result.chat) ? textField(result.chat, "id") : "");
      if (!projectId || !chatId) throw new Error("実行中の会話をクラウドへ関連付けられませんでした。");
      try { await relayRun(taskRef, taskId, projectId, chatId, runId); }
      finally { relayedRunIds.delete(runId); }
    } else {
      await updateDoc(taskRef, { status: "completed", completedAt: serverTimestamp(), updatedAt: serverTimestamp() });
    }
    await syncCatalog();
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    const errorFields: Record<string, unknown> = {
      status: "failed",
      errorCode: "remote_task_failed",
      completedAt: serverTimestamp(),
      updatedAt: serverTimestamp(),
    };
    errorFields.error = { message: detail.slice(0, 2000) };
    await updateDoc(taskRef, errorFields);
  } finally {
    try { await cleanup(); }
    catch (error) { reportBackgroundError(error); }
  }
}

async function prepareTaskPayload(taskId: string, payload: RemoteTaskPayload): Promise<{
  payload: Record<string, unknown>;
  cleanup: () => Promise<void>;
}> {
  if (!payload.attachments?.length) return { payload: payload as Record<string, unknown>, cleanup: async () => {} };
  if (payload.attachments.length > 4) throw new Error("添付ファイルは4件までです。");
  const directory = path.join(config.attachmentRoot, taskId);
  await mkdir(directory, { recursive: true });
  try {
    const attachments: Array<Record<string, string>> = [];
    const extensions: Record<string, string> = { "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp" };
    let totalSize = 0;
    for (const [index, attachment] of payload.attachments.entries()) {
      const kind = attachment.kind === "image" ? "image" : "file";
      const imageExtension = extensions[attachment.mimeType];
      if (kind === "image" && !imageExtension) throw new Error("対応していない画像形式です。");
      const suppliedExtension = path.extname(attachment.name).toLowerCase();
      const extension = imageExtension || (/^\.[a-z0-9]{1,16}$/.test(suppliedExtension) ? suppliedExtension : "");
      const bytes = Buffer.from(attachment.dataBase64, "base64");
      totalSize += bytes.length;
      if (!bytes.length || bytes.length > 600_000 || totalSize > 600_000) throw new Error("添付ファイルの合計は600 KiB以下にしてください。");
      const filePath = path.join(directory, `attachment-${index + 1}${extension}`);
      await writeFile(filePath, bytes, { mode: 0o600 });
      const cleanName = path.basename(attachment.name).replace(/[\u0000-\u001f\u007f]/g, "").slice(0, 200) || `attachment-${index + 1}${extension}`;
      attachments.push({ path: filePath, name: cleanName, kind });
    }
    const { attachments: _wireAttachments, ...rest } = payload;
    return {
      payload: { ...rest, attachments },
      cleanup: async () => { await rm(directory, { recursive: true, force: true }); },
    };
  } catch (error) {
    await rm(directory, { recursive: true, force: true });
    throw error;
  }
}

async function taskPayload(_taskId: string, task: QueuedTask): Promise<RemoteTaskPayload> {
  if (!task.payload || !isObject(task.payload)) throw new Error("タスク本文がありません。");
  return task.payload;
}

async function relayRun(taskRef: DocumentReference<DocumentData>, taskId: string, projectId: string, chatId: string, runId: string): Promise<void> {
  const chatRef = doc(deviceRef, "chats", chatId);
  const startedAt = new Date().toISOString();
  let terminalStatus = "connection_lost";
  let activityCount = 0;
  let reasoningActivityCount = 0;
  let workActivityCount = 0;
  let progressItems: RemoteProgressItem[] = [];
  let writtenActivityCount = 0;
  let lastActivityWriteAt = 0;
  let pendingOutput: SseEvent[] = [];
  let outputTimer: ReturnType<typeof setTimeout> | undefined;
  await setDoc(chatRef, {
    projectId,
    chatId,
    activeRun: { id: runId, status: "running", startedAt, activityCount: 0, reasoningActivityCount: 0, workActivityCount: 0, progressItems: [] },
  }, { merge: true });
  const flushOutput = async () => {
    if (outputTimer) clearTimeout(outputTimer);
    outputTimer = undefined;
    const events = pendingOutput;
    pendingOutput = [];
    if (!events.length) return;
    const last = events[events.length - 1];
    const firstData = isObject(events[0].data) ? events[0].data : {};
    const text = events.map((item) => isObject(item.data) ? textField(item.data, "text") : "").join("");
    await writeEvent(taskRef, taskId, {
      sequence: last.sequence,
      event: "output",
      data: { ...firstData, text, fromSequence: events[0].sequence, toSequence: last.sequence },
    });
  };
  const flushActivityCount = async (force = false) => {
    if (!force && activityCount === writtenActivityCount) return;
    const now = Date.now();
    if (!force && now - lastActivityWriteAt < 2_000) return;
    await updateDoc(chatRef, {
      activeRun: { id: runId, status: "running", startedAt, activityCount, reasoningActivityCount, workActivityCount, progressItems },
    });
    writtenActivityCount = activityCount;
    lastActivityWriteAt = now;
  };
  for await (const event of daemon.events(runId)) {
    if (canMergeOutput(pendingOutput, event)) {
      pendingOutput.push(event);
      if (!outputTimer) outputTimer = setTimeout(() => void flushOutput().catch(reportBackgroundError), 750);
      continue;
    }
    await flushOutput();
    if (event.event === "progress") {
      const method = isObject(event.data) ? textField(event.data, "method") : "";
      progressItems = appendProgressItem(progressItems, event);
      if (isCountableProgressEvent(event)) {
        activityCount += 1;
        if (method.startsWith("item/reasoning/")) reasoningActivityCount += 1;
        else workActivityCount += 1;
        await flushActivityCount();
      }
      continue;
    }
    if (event.event === "output") {
      pendingOutput.push(event);
      outputTimer = setTimeout(() => void flushOutput().catch(reportBackgroundError), 750);
      continue;
    }
    await writeEvent(taskRef, taskId, event);
    if (event.event === "approval") {
      await updateDoc(taskRef, { status: "waiting_for_approval", updatedAt: serverTimestamp() });
      await updateDoc(chatRef, {
        activeRun: { id: runId, status: "waiting_for_approval", startedAt, activityCount, reasoningActivityCount, workActivityCount, progressItems },
      });
    } else if (event.event === "done") {
      const status = isObject(event.data) ? textField(event.data, "status") : "";
      terminalStatus = status === "cancelled" ? "cancelled" : status === "failed" ? "failed" : "completed";
    } else if (event.event === "error") {
      terminalStatus = "failed";
    } else {
      await updateDoc(taskRef, { status: "running", updatedAt: serverTimestamp() });
    }
  }
  await flushOutput();
  await flushActivityCount(true);
  await Promise.all([
    updateDoc(taskRef, { status: terminalStatus, completedAt: serverTimestamp(), updatedAt: serverTimestamp() }),
    updateDoc(chatRef, {
      activeRun: deleteField(),
      lastRunStatus: terminalStatus,
      lastRunProgress: { id: runId, status: terminalStatus, startedAt, activityCount, reasoningActivityCount, workActivityCount, progressItems },
    }),
  ]);
}

function reconcileDesktopRunWatchers(projects: CatalogProject[]): void {
  for (const project of projects) {
    for (const chat of project.chats) {
      const runId = chat.activeRunId || "";
      if (!runId || relayedRunIds.has(runId) || desktopRunWatchers.has(runId)) continue;
      const watcher = relayDesktopRun(project.id, chat.id, runId, chat.activeRunEventSequence ?? 0)
        .catch(reportBackgroundError)
        .finally(() => desktopRunWatchers.delete(runId));
      desktopRunWatchers.set(runId, watcher);
    }
  }
}

async function relayDesktopRun(projectId: string, chatId: string, runId: string, afterSequence: number): Promise<void> {
  const chatRef = doc(deviceRef, "chats", chatId);
  const startedAt = new Date().toISOString();
  let activityCount = 0;
  let reasoningActivityCount = 0;
  let workActivityCount = 0;
  let progressItems: RemoteProgressItem[] = [];
  let writtenActivityCount = 0;
  let lastActivityWriteAt = 0;
  let terminalStatus = "connection_lost";
  await updateDoc(chatRef, {
    activeRun: { id: runId, status: "running", startedAt, activityCount: 0, reasoningActivityCount: 0, workActivityCount: 0, progressItems: [] },
  });
  const flush = async (force = false) => {
    if (!force && activityCount === writtenActivityCount) return;
    const now = Date.now();
    if (!force && now - lastActivityWriteAt < 2_000) return;
    await updateDoc(chatRef, {
      activeRun: { id: runId, status: "running", startedAt, activityCount, reasoningActivityCount, workActivityCount, progressItems },
    });
    writtenActivityCount = activityCount;
    lastActivityWriteAt = now;
  };
  for await (const event of daemon.events(runId, afterSequence)) {
    if (event.event === "progress") {
      const method = isObject(event.data) ? textField(event.data, "method") : "";
      progressItems = appendProgressItem(progressItems, event);
      if (isCountableProgressEvent(event)) {
        activityCount += 1;
        if (method.startsWith("item/reasoning/")) reasoningActivityCount += 1;
        else workActivityCount += 1;
        await flush();
      }
    } else if (event.event === "done") {
      terminalStatus = isObject(event.data) ? textField(event.data, "status") || "completed" : "completed";
    } else if (event.event === "error") {
      terminalStatus = "failed";
    }
  }
  await flush(true);
  await updateDoc(chatRef, {
    activeRun: deleteField(),
    lastRunStatus: terminalStatus,
    lastRunProgress: { id: runId, status: terminalStatus, startedAt, activityCount, reasoningActivityCount, workActivityCount, progressItems },
  });
}

function canMergeOutput(pending: SseEvent[], incoming: SseEvent): boolean {
  if (incoming.event !== "output" || !isObject(incoming.data) || !textField(incoming.data, "text")) return false;
  if (!pending.length) return true;
  const previous = pending[pending.length - 1];
  if (!isObject(previous.data)) return false;
  return textField(previous.data, "messageId") === textField(incoming.data, "messageId")
    && textField(previous.data, "phase") === textField(incoming.data, "phase")
    && textField(previous.data, "stream") === textField(incoming.data, "stream");
}

async function writeEvent(taskRef: DocumentReference<DocumentData>, taskId: string, event: SseEvent): Promise<void> {
  const sequence = Math.max(0, event.sequence);
  const eventRef = doc(collection(taskRef, "events"), String(sequence).padStart(12, "0"));
  const fields: Record<string, unknown> = {
    sequence,
    type: event.event,
    createdAt: serverTimestamp(),
  };
  fields.payload = event.data;
  await setDoc(eventRef, fields);
}

async function syncCatalog(): Promise<void> {
  if (catalogSyncRunning) return;
  catalogSyncRunning = true;
  let phase = "reading";
  reportSyncProgress({ state: "syncing", phase: "reading" });
  try {
    phase = "catalog";
    const catalog = await withTimeout(daemon.catalog(), 120_000, "ローカル会話一覧の取得がタイムアウトしました。");
    // Live run state and Web commands must not wait behind a potentially large
    // first-time history migration.
    reconcileDesktopRunWatchers(catalog.projects);
    const chatsTotal = catalog.projects.reduce((total, project) => total + project.chats.length, 0);
    const changes = diffCatalog(lastCatalogSnapshot, catalog.projects);
    const hasChanges = lastCatalogSnapshot === undefined
      || changes.changedProjects.length > 0
      || changes.changedChats.length > 0
      || changes.removedProjects.length > 0
      || changes.removedChats.length > 0;
    if (!hasChanges) {
      reportSyncProgress({
        state: "completed",
        phase: "writing",
        projectsTotal: catalog.projects.length,
        projectsCompleted: catalog.projects.length,
        chatsTotal,
        chatsCompleted: chatsTotal,
        recordsTotal: 0,
        recordsWritten: 0,
        recordsSkipped: changes.unchangedRecords,
      });
      lastCatalogRevision = catalog.revision;
      reconcileDesktopRunWatchers(catalog.projects);
      return;
    }
    const prioritizedChanges = prioritizeCatalogChanges(
      changes,
      catalog.projects,
      catalog.selectedProjectId,
      catalog.selectedChatId,
    );
    phase = "stale";
    const staleReferences = lastCatalogSnapshot === undefined
      ? await withTimeout(
        findStaleCatalogReferences(catalog),
        30_000,
        "クラウド上の既存会話確認がタイムアウトしました。",
      )
      : await withTimeout(
        staleReferencesFromChanges(changes),
        30_000,
        "解除したプロジェクトのクラウド会話確認がタイムアウトしました。",
      );
    let recordsTotal = prioritizedChanges.length + staleReferences.length;
    let recordsWritten = 0;
    for (let offset = 0; offset < staleReferences.length; offset += 450) {
      const batch = writeBatch(db);
      for (const reference of staleReferences.slice(offset, offset + 450)) batch.delete(reference);
      await batch.commit();
      recordsWritten += Math.min(450, staleReferences.length - offset);
    }

    const previousChats = new Map(
      (lastCatalogSnapshot ?? []).flatMap((project) =>
        project.chats.map((chat) => [`${project.id}\n${chat.id}`, chat] as const)),
    );
    const writes: Array<{ reference: DocumentReference<DocumentData>; data: Record<string, unknown> }> = [];
    phase = "history";
    for (const change of prioritizedChanges) {
      if (change.kind === "project") {
        const projectRef = doc(deviceRef, "projects", change.project.id);
        writes.push({ reference: projectRef, data: { displayName: change.project.name, syncOrder: change.syncOrder } });
        continue;
      }
      const previousChat = previousChats.get(`${change.projectId}\n${change.chat.id}`);
      const data: Record<string, unknown> = {
        projectId: change.projectId,
        chatId: change.chat.id,
        title: change.chat.title,
        status: change.chat.status,
        syncOrder: change.syncOrder,
        lastUpdatedAt: change.chat.updatedAt || null,
        historyRevision: change.chat.historyRevision || "",
        history: deleteField(),
        messages: deleteField(),
        ...(change.chat.activeRunId ? {} : { activeRun: deleteField() }),
      };
      if (!previousChat || previousChat.historyRevision !== change.chat.historyRevision) {
        const history = await withTimeout(
          daemon.chatHistory(change.projectId, change.chat.id),
          120_000,
          `会話履歴の取得がタイムアウトしました（${change.chat.id}）。`,
        );
        const historyWrites = await syncHistoryChunks(change.chat.id, history);
        recordsTotal += historyWrites;
        recordsWritten += historyWrites;
        data.historyItemCount = history.length;
        data.historySyncedAt = serverTimestamp();
      }
      writes.push({
        reference: doc(deviceRef, "chats", change.chat.id),
        data,
      });
    }
    phase = "writing";
    reportSyncProgress({
      state: "syncing",
      phase: "writing",
      projectsTotal: catalog.projects.length,
      projectsCompleted: catalog.projects.length,
      chatsTotal,
      chatsCompleted: chatsTotal,
      recordsTotal,
      recordsWritten,
      recordsSkipped: changes.unchangedRecords,
    });
    for (let offset = 0; offset < writes.length; offset += 450) {
      const batch = writeBatch(db);
      for (const write of writes.slice(offset, offset + 450)) batch.set(write.reference, write.data, { merge: true });
      await batch.commit();
      recordsWritten += Math.min(450, writes.length - offset);
      reportSyncProgress({
        state: "syncing",
        phase: "writing",
        projectsTotal: catalog.projects.length,
        projectsCompleted: catalog.projects.length,
        chatsTotal,
        chatsCompleted: chatsTotal,
        recordsTotal,
        recordsWritten,
        recordsSkipped: changes.unchangedRecords,
      });
    }
    reportSyncProgress({
      state: "completed",
      phase: "writing",
      projectsTotal: catalog.projects.length,
      projectsCompleted: catalog.projects.length,
      chatsTotal,
      chatsCompleted: chatsTotal,
      recordsTotal,
      recordsWritten,
      recordsSkipped: changes.unchangedRecords,
    });
    lastCatalogSnapshot = catalog.projects;
    lastCatalogRevision = catalog.revision;
    reconcileDesktopRunWatchers(catalog.projects);
  } catch (error) {
    reportSyncProgress({ state: "failed", phase, error: safeErrorMessage(error) });
    throw error;
  } finally {
    catalogSyncRunning = false;
  }
}

async function watchCatalogRevision(): Promise<void> {
  while (!catalogWatchStopped) {
    let observedRevision = lastCatalogRevision;
    try {
      observedRevision = await daemon.waitCatalogRevision(lastCatalogRevision);
      if (!catalogWatchStopped && observedRevision !== lastCatalogRevision) await syncCatalog();
      catalogSyncFailures = 0;
    } catch (error) {
      catalogSyncFailures += 1;
      if (Date.now() - lastRevisionCheckErrorAt >= 30_000) {
        lastRevisionCheckErrorAt = Date.now();
        reportBackgroundError(error);
      }
      const retry = catalogRetryDecision(catalogSyncFailures, observedRevision, lastCatalogRevision);
      if (retry.stopRevision !== undefined) {
        // Do not retry the same failed catalog revision forever. A later local
        // change produces a new revision and permits a fresh bounded attempt.
        lastCatalogRevision = retry.stopRevision;
        catalogSyncFailures = 0;
        continue;
      }
      await new Promise((resolve) => setTimeout(resolve, retry.backoffMs));
    }
  }
}

async function withTimeout<T>(operation: Promise<T>, timeoutMs: number, message: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      operation,
      new Promise<T>((_, reject) => {
        timer = setTimeout(() => reject(new Error(message)), timeoutMs);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

async function findStaleCatalogReferences(catalog: { projects: Array<{ id: string; chats: Array<{ id: string }> }> }): Promise<DocumentReference<DocumentData>[]> {
  const activeProjects = new Map(catalog.projects.map((project) => [project.id, new Set(project.chats.map((chat) => chat.id))]));
  const stale: DocumentReference<DocumentData>[] = [];
  const existingChats = await getDocs(collection(deviceRef, "chats"));
  for (const chat of existingChats.docs) {
    const projectId = textField(chat.data(), "projectId");
    const chatId = textField(chat.data(), "chatId") || chat.id;
    if (!activeProjects.get(projectId)?.has(chatId)) stale.push(chat.ref);
  }
  const existingProjects = await getDocs(collection(deviceRef, "projects"));
  for (const project of existingProjects.docs) {
    if (!activeProjects.has(project.id)) stale.push(project.ref);
  }
  const activeChatIds = new Set(catalog.projects.flatMap((project) => project.chats.map((chat) => chat.id)));
  const existingHistoryChunks = await getDocs(collection(deviceRef, "historyChunks"));
  for (const chunk of existingHistoryChunks.docs) {
    if (!activeChatIds.has(textField(chunk.data(), "chatId"))) stale.push(chunk.ref);
  }
  return stale;
}

async function staleReferencesFromChanges(changes: ReturnType<typeof diffCatalog>): Promise<DocumentReference<DocumentData>[]> {
  const stale = new Map<string, DocumentReference<DocumentData>>();
  for (const project of changes.removedProjects) {
    const projectRef = doc(deviceRef, "projects", project.projectId);
    for (const chatId of project.chatIds) {
      const chatRef = doc(deviceRef, "chats", chatId);
      stale.set(chatRef.path, chatRef);
    }
    stale.set(projectRef.path, projectRef);
  }
  for (const chat of changes.removedChats) {
    const chatRef = doc(deviceRef, "chats", chat.chatId);
    stale.set(chatRef.path, chatRef);
  }
  const removedChatIds = new Set([
    ...changes.removedProjects.flatMap((project) => project.chatIds),
    ...changes.removedChats.map((chat) => chat.chatId),
  ]);
  for (const chatId of removedChatIds) {
    const chunks = await getDocs(query(collection(deviceRef, "historyChunks"), where("chatId", "==", chatId)));
    for (const chunk of chunks.docs) stale.set(chunk.ref.path, chunk.ref);
  }
  return [...stale.values()];
}

async function syncHistoryChunks(chatId: string, history: import("./catalog-sync.js").CatalogHistoryItem[]): Promise<number> {
  const chunksRef = collection(deviceRef, "historyChunks");
  const existing = await getDocs(query(chunksRef, where("chatId", "==", chatId)));
  const existingById = new Map(existing.docs.map((snapshot) => [snapshot.id, snapshot]));
  const writes: Array<{ reference: DocumentReference<DocumentData>; data?: Record<string, unknown> }> = [];
  for (const chunk of historyChunks(chatId, history)) {
    const previous = existingById.get(chunk.id);
    existingById.delete(chunk.id);
    if (previous
      && textField(previous.data(), "hash") === chunk.hash
      && typeof previous.data().payload === "string") continue;
    writes.push({
      reference: doc(chunksRef, chunk.id),
      data: { chatId, index: chunk.index, hash: chunk.hash, payload: chunk.payload },
    });
  }
  for (const snapshot of existingById.values()) writes.push({ reference: snapshot.ref });
  // Keep each large history document in its own commit. Live activeRun
  // updates then wait behind at most one history document, not a multi-MiB
  // commit containing many documents.
  for (let offset = 0; offset < writes.length; offset += 1) {
    const batch = writeBatch(db);
    for (const write of writes.slice(offset, offset + 1)) {
      if (write.data) batch.set(write.reference, write.data);
      else batch.delete(write.reference);
    }
    await batch.commit();
  }
  return writes.length;
}

function reportSyncProgress(progress: Record<string, unknown>): void {
  console.log(`REMOTE_SYNC ${JSON.stringify(progress)}`);
}

async function heartbeatNow(): Promise<void> {
  if (heartbeatRunning) return;
  heartbeatRunning = true;
  try {
    await setDoc(deviceRef, { status: "online", lastSeenAt: serverTimestamp(), updatedAt: serverTimestamp() }, { merge: true });
  } catch (error) {
    reportBackgroundError(error);
  } finally {
    heartbeatRunning = false;
  }
}

async function shutdown(): Promise<void> {
  clearInterval(heartbeat);
  catalogWatchStopped = true;
  unsubscribe();
  try {
    await setDoc(deviceRef, { status: "offline", lastSeenAt: serverTimestamp(), updatedAt: serverTimestamp() }, { merge: true });
  } finally {
    process.exit(0);
  }
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function textField(value: unknown, name: string): string {
  return isObject(value) && typeof value[name] === "string" ? value[name] as string : "";
}

function reportBackgroundError(error: unknown): void {
  console.error(`REMOTE_BACKGROUND_ERROR ${error instanceof Error ? error.message : String(error)}`);
}

function safeErrorMessage(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  return message
    .replace(/Bearer\s+[^\s]+/gi, "Bearer [redacted]")
    .replace(/(token|apikey|api_key|authorization|cookie)[=:：]\s*[^\s,]+/gi, "$1=[redacted]")
    .slice(0, 240);
}
