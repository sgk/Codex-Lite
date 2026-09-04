export function titleFromFirstInstruction(content: string): string {
  const normalized = content.trim().split(/\s+/u).filter(Boolean).join(" ");
  if (!normalized) return "New Chat";
  return normalized.length > 80 ? `${normalized.slice(0, 77).trimEnd()}...` : normalized;
}

export function composerOperation(newChat: boolean, activeRunId: string): "create_chat" | "send_message" | "steer_run" {
  if (newChat) return "create_chat";
  return activeRunId ? "steer_run" : "send_message";
}

export function parseHistoryChunk(value: string): unknown[] {
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function historyUpdatePosition(selectionChanged: boolean, wasNearEnd: boolean, endFollowPending = false): "end" | "preserve" {
  return selectionChanged || wasNearEnd || endFollowPending ? "end" : "preserve";
}

export function sortConversationTimeline<T extends { createdAt?: string; sourceOrder: number }>(items: T[]): T[] {
  return [...items].sort((left, right) => {
    const leftTime = Date.parse(left.createdAt || "");
    const rightTime = Date.parse(right.createdAt || "");
    const normalizedLeft = Number.isFinite(leftTime) ? leftTime : Number.MAX_SAFE_INTEGER;
    const normalizedRight = Number.isFinite(rightTime) ? rightTime : Number.MAX_SAFE_INTEGER;
    return normalizedLeft - normalizedRight || left.sourceOrder - right.sourceOrder;
  });
}

export function createdChatSelectionAction(
  status: string,
  chatId: string,
  chatAvailable: boolean,
): "wait" | "select" | "clear" {
  if (["failed", "cancelled", "connection_lost"].includes(status)) return "clear";
  return chatId && chatAvailable ? "select" : "wait";
}

export function approvalSummary(reason: string, command: string): string {
  const details = [reason.trim(), command.trim() ? `対象コマンド: ${command.trim()}` : ""].filter(Boolean);
  return details.length ? details.join(" / ") : "この操作の承認が必要です。";
}

export function permissionModeForSettings(permissionProfile: string, approvalsReviewer: string): "ask-for-approval" | "approve-for-me" | "full-access" {
  if (permissionProfile === ":danger-full-access") return "full-access";
  return approvalsReviewer === "auto_review" ? "approve-for-me" : "ask-for-approval";
}

export function permissionSettingsForMode(mode: string): { permissionProfile: string; approvalPolicy: string; approvalsReviewer: string } {
  if (mode === "approve-for-me") return { permissionProfile: ":workspace", approvalPolicy: "on-request", approvalsReviewer: "auto_review" };
  if (mode === "full-access") return { permissionProfile: ":danger-full-access", approvalPolicy: "never", approvalsReviewer: "user" };
  return { permissionProfile: ":workspace", approvalPolicy: "on-request", approvalsReviewer: "user" };
}

export function remainingChargeLabel(credits: Record<string, unknown> | undefined): string {
  if (!credits) return "未取得";
  if (credits.unlimited === true) return "無制限";
  const balance = typeof credits.balance === "string" || typeof credits.balance === "number" ? String(credits.balance) : "";
  if (balance) return `残り ${balance}`;
  return credits.hasCredits === true ? "利用可能" : "なし";
}

export function deepSeekBalanceLabel(balance: Record<string, unknown> | undefined): string {
  if (!balance) return "未設定";
  if (balance.status !== "ok") return "取得不可";
  const infos = Array.isArray(balance.balanceInfos) ? balance.balanceInfos.filter(isRecord) : [];
  if (!infos.length) return balance.isAvailable === true ? "残高情報なし" : "利用不可";
  const labels = infos.map((info) => {
    const currency = typeof info.currency === "string" ? info.currency.trim() : "";
    const total = typeof info.totalBalance === "string" || typeof info.totalBalance === "number" ? String(info.totalBalance) : "";
    return [currency, total].filter(Boolean).join(" ");
  }).filter(Boolean);
  return labels.length ? labels.join(" / ") : "残高情報なし";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export interface ChatSelection {
  deviceId: string;
  projectId: string;
  chatId: string;
}

export function chatSelectionHash(selection: ChatSelection): string {
  return `#/devices/${encodeURIComponent(selection.deviceId)}/projects/${encodeURIComponent(selection.projectId)}/chats/${encodeURIComponent(selection.chatId)}`;
}

export function chatSelectionFromHash(hash: string): ChatSelection | undefined {
  const match = hash.match(/^#\/devices\/([^/]+)\/projects\/([^/]+)\/chats\/([^/]+)$/);
  if (!match) return undefined;
  try {
    const deviceId = decodeURIComponent(match[1]);
    const projectId = decodeURIComponent(match[2]);
    const chatId = decodeURIComponent(match[3]);
    return deviceId && projectId && chatId ? { deviceId, projectId, chatId } : undefined;
  } catch { return undefined; }
}

export function shouldSubmitComposer(key: string, shiftKey: boolean, isComposing: boolean, mobileLayout = false): boolean {
  return key === "Enter" && !shiftKey && !isComposing && !mobileLayout;
}

export function composerDraftContextKey(deviceId: string, projectId: string, chatId: string): string {
  return deviceId && projectId ? JSON.stringify([deviceId, projectId, chatId || "@new-chat"]) : "";
}

export function shouldKeepCompletedProgress(runId: string, persistedRunIds: string[]): boolean {
  return Boolean(runId) && !persistedRunIds.includes(runId);
}

export function isSupportedImageMimeType(type: string): boolean {
  return ["image/png", "image/jpeg", "image/gif", "image/webp"].includes(type.toLowerCase());
}

export function activityCountLabel(reasoningCount: number, workCount: number): string {
  const reasoning = Math.max(0, Math.trunc(reasoningCount));
  const work = Math.max(0, Math.trunc(workCount));
  const total = reasoning + work;
  if (!total) return "";
  const details = [reasoning ? `思考 ${reasoning}` : "", work ? `作業 ${work}` : ""].filter(Boolean).join("・");
  return `進行 ${total}件（${details}）`;
}

export function completedRunVersion(lastRunProgress: unknown): string {
  if (!lastRunProgress || typeof lastRunProgress !== "object" || Array.isArray(lastRunProgress)) return "";
  const id = (lastRunProgress as Record<string, unknown>).id;
  return typeof id === "string" ? id : "";
}

export function chatTreeIndicator(status: string, activeRunStatus: string, unread: boolean): "running" | "unread" | "none" {
  const activeStatuses = ["queued", "claimed", "running", "waiting_for_approval"];
  if (status === "running" || activeStatuses.includes(activeRunStatus)) return "running";
  return unread ? "unread" : "none";
}

export function projectTreeIndicator(children: Array<"running" | "unread" | "none">): "running" | "unread" | "none" {
  if (children.includes("running")) return "running";
  if (children.includes("unread")) return "unread";
  return "none";
}

export function sidebarSwipeAction(startX: number, currentX: number, verticalDistance: number, sidebarOpen: boolean): "open" | "close" | "none" {
  if (Math.abs(verticalDistance) > 48) return "none";
  const horizontalDistance = currentX - startX;
  if (!sidebarOpen && horizontalDistance >= 64) return "open";
  if (sidebarOpen && horizontalDistance <= -64) return "close";
  return "none";
}
