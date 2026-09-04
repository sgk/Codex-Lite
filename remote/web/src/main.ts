import "./style.css";
import { initializeApp } from "firebase/app";
import { getAuth, GoogleAuthProvider, onAuthStateChanged, signInWithPopup, signOut, type User } from "firebase/auth";
import {
  collection,
  doc,
  getFirestore,
  onSnapshot,
  query,
  serverTimestamp,
  setDoc,
  where,
  type DocumentData,
  type QueryDocumentSnapshot,
  type Unsubscribe,
} from "firebase/firestore";
import type { FirebasePublicConfig, RemoteAttachment, RemoteOperation, RemoteTaskPayload } from "../../shared/src/types.js";
import { activityCountLabel, approvalSummary, chatSelectionFromHash, chatSelectionHash, chatTreeIndicator, completedRunVersion, composerDraftContextKey, composerOperation, createdChatSelectionAction, deepSeekBalanceLabel, historyUpdatePosition, isSupportedImageMimeType, parseHistoryChunk, permissionModeForSettings, permissionSettingsForMode, projectTreeIndicator, remainingChargeLabel, sidebarSwipeAction, shouldKeepCompletedProgress, shouldSubmitComposer, sortConversationTimeline, titleFromFirstInstruction, type ChatSelection } from "./conversation-behavior.js";
import { renderMarkdown } from "./markdown.js";

if ("serviceWorker" in navigator) {
  void navigator.serviceWorker.register("/sw.js", { updateViaCache: "none" }).then((registration) => {
    const checkForUpdate = (): void => { if (navigator.onLine) void registration.update(); };
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") checkForUpdate();
    });
    window.addEventListener("online", checkForUpdate);
    checkForUpdate();
  });
}

const firebaseConfig = await loadFirebaseConfig();
const firebaseApp = initializeApp(firebaseConfig);
const auth = getAuth(firebaseApp);
const db = getFirestore(firebaseApp);
const elements = {
  login: byId<HTMLElement>("login"), app: byId<HTMLElement>("app"), logout: byId<HTMLButtonElement>("logout"),
  googleLogin: byId<HTMLButtonElement>("google-login"),
  device: byId<HTMLSelectElement>("device"), project: byId<HTMLSelectElement>("project"), chat: byId<HTMLSelectElement>("chat"),
  newChat: byId<HTMLInputElement>("new-chat"),
  content: byId<HTMLTextAreaElement>("content"), send: byId<HTMLButtonElement>("send"),
  approvalLevel: byId<HTMLSelectElement>("approval-level"), model: byId<HTMLSelectElement>("model"), reasoning: byId<HTMLSelectElement>("reasoning"), attachFile: byId<HTMLButtonElement>("attach-file"), fileInput: byId<HTMLInputElement>("file-input"), attachments: byId<HTMLElement>("attachments"), composer: byId<HTMLElement>("composer"),
  conversationBody: byId<HTMLElement>("conversation-body"), history: byId<HTMLElement>("history"), error: byId<HTMLElement>("error"), runProgress: byId<HTMLElement>("run-progress"), runProgressText: byId<HTMLElement>("run-progress-text"), cancelRun: byId<HTMLButtonElement>("cancel-run"), approvalActions: byId<HTMLElement>("approval-actions"), approvalSummary: byId<HTMLElement>("approval-summary"),
  usagePanel: byId<HTMLElement>("usage-panel"), usageRefresh: byId<HTMLButtonElement>("usage-refresh"), usageProvider: byId<HTMLElement>("usage-provider"), usageFiveText: byId<HTMLElement>("usage-five-text"), usageWeekText: byId<HTMLElement>("usage-week-text"), usageCredits: byId<HTMLElement>("usage-credits"), usageFiveBar: byId<HTMLElement>("usage-five-bar"), usageWeekBar: byId<HTMLElement>("usage-week-bar"), usageFiveStart: byId<HTMLElement>("usage-five-start"), usageFiveEnd: byId<HTMLElement>("usage-five-end"), usageWeekStart: byId<HTMLElement>("usage-week-start"), usageWeekEnd: byId<HTMLElement>("usage-week-end"),
  sidebar: document.querySelector<HTMLElement>(".sidebar")!, treeMenu: byId<HTMLButtonElement>("tree-menu"), sidebarBackdrop: byId<HTMLButtonElement>("sidebar-backdrop"),
  projectTree: byId<HTMLElement>("project-tree"), treeEmpty: byId<HTMLElement>("tree-empty"), breadcrumb: byId<HTMLElement>("breadcrumb"), conversationTitle: byId<HTMLElement>("conversation-title"), conversationStatus: byId<HTMLElement>("conversation-status"),
  approve: byId<HTMLButtonElement>("approve"), decline: byId<HTMLButtonElement>("decline"),
};

let user: User | null = null;
let deviceUnsubscribe: Unsubscribe | undefined;
let taskUnsubscribe: Unsubscribe | undefined;
let eventUnsubscribe: Unsubscribe | undefined;
let historyUnsubscribe: Unsubscribe | undefined;
let tasks: Array<QueryDocumentSnapshot<DocumentData>> = [];
let devices: Array<QueryDocumentSnapshot<DocumentData>> = [];
const projectUnsubscribes = new Map<string, Unsubscribe>();
const chatUnsubscribes = new Map<string, Unsubscribe>();
let projectsByDevice = new Map<string, Array<QueryDocumentSnapshot<DocumentData>>>();
let chatsByDeviceProject = new Map<string, Array<QueryDocumentSnapshot<DocumentData>>>();
const expandedDeviceIds = new Set<string>();
const expandedProjectIds = new Set<string>();
let treeRenderFrame: number | undefined;
let subscribedDeviceId = "";
let renderedDeviceSignature = "";
let renderedTreeSignature = "";
let renderedConversationSignature = "";
let renderedConversationSelection = "";
let selectedHistoryKey = "";
let selectedHistorySignature = "";
let selectedHistoryEntries: HistoryEntry[] = [];
let historyScrollVersion = 0;
let activeTaskId = "";
let activeTaskEvents: Array<QueryDocumentSnapshot<DocumentData>> = [];
let activeApproval: { runId: string; requestId: string; summary: string } | undefined;
let pendingCreatedChat: { taskId: string; deviceId: string; projectId: string; optimisticId: string } | undefined;
let optimisticInstructions: Array<{ id: string; taskId?: string; deviceId: string; projectId: string; chatId?: string; content: string; createdAt: string; baselineCount: number; state: "sending" | "failed" }> = [];
const expandedProgressIds = new Set<string>();
let pendingAttachments: Array<RemoteAttachment & { previewUrl?: string }> = [];
let composerFileDragDepth = 0;
let runtimeKey = "";
let runtimeApplying = false;
let reasoningByModel: Record<string, string[]> = {};
let sidebarSwipe: { touchId: number; startX: number; startY: number; sidebarOpen: boolean } | undefined;
interface WorkspaceSelection { deviceId: string; projectId: string; chatId: string }
let restoredSelection: WorkspaceSelection | undefined;
const chatReadStates = new Map<string, { version: string; unread: boolean }>();
let composerDraftContext = "";
let composerDrafts = new Map<string, { content: string; updatedAt: number }>();
let historyFollowingEnd = true;
let historyFollowFrame: number | undefined;

function updateVisualViewportHeight(): void {
  const height = window.visualViewport?.height ?? window.innerHeight;
  document.documentElement.style.setProperty("--visual-viewport-height", `${Math.round(height)}px`);
}

updateVisualViewportHeight();
window.visualViewport?.addEventListener("resize", updateVisualViewportHeight);

elements.googleLogin.addEventListener("click", () => void loginWithGoogle());
elements.logout.addEventListener("click", () => void signOut(auth));
elements.device.addEventListener("change", subscribeDeviceContents);
elements.send.addEventListener("click", () => void sendInstruction());
elements.content.addEventListener("keydown", (event) => {
  if (!shouldSubmitComposer(event.key, event.shiftKey, event.isComposing || event.keyCode === 229, mobileLayout())) return;
  event.preventDefault();
  void sendInstruction();
});
elements.content.addEventListener("input", saveCurrentComposerDraft);
elements.content.addEventListener("paste", (event) => {
  const files = clipboardImageFiles(event.clipboardData);
  if (!files.length) return;
  event.preventDefault();
  void addFiles(files);
});
const stopHistoryEndFollowing = (): void => {
  historyFollowingEnd = false;
  historyScrollVersion += 1;
};
elements.conversationBody.addEventListener("wheel", stopHistoryEndFollowing, { passive: true });
elements.conversationBody.addEventListener("touchstart", stopHistoryEndFollowing, { passive: true });
elements.conversationBody.addEventListener("pointerdown", stopHistoryEndFollowing, { passive: true });
elements.conversationBody.addEventListener("scroll", () => {
  if (isConversationHistoryNearEnd()) historyFollowingEnd = true;
}, { passive: true });
document.addEventListener("keydown", (event) => {
  if (!["ArrowUp", "PageUp", "PageDown", "Home", "End"].includes(event.key)) return;
  if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement || event.target instanceof HTMLSelectElement) return;
  stopHistoryEndFollowing();
});
new ResizeObserver(() => scheduleHistoryEndFollow()).observe(elements.history);
elements.cancelRun.addEventListener("click", () => void cancelActiveRun());
elements.usageRefresh.addEventListener("click", () => void refreshRuntime(true));
elements.model.addEventListener("change", () => void runtimeSettingChanged());
elements.reasoning.addEventListener("change", () => void runtimeSettingChanged());
elements.approvalLevel.addEventListener("change", () => void runtimeSettingChanged());
elements.attachFile.addEventListener("click", () => elements.fileInput.click());
elements.fileInput.addEventListener("change", () => void addSelectedFiles());
elements.composer.addEventListener("dragenter", (event) => {
  if (!hasDraggedFiles(event.dataTransfer)) return;
  event.preventDefault();
  composerFileDragDepth += 1;
  elements.composer.classList.add("file-drag-active");
});
elements.composer.addEventListener("dragover", (event) => {
  if (!hasDraggedFiles(event.dataTransfer)) return;
  event.preventDefault();
  if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
});
elements.composer.addEventListener("dragleave", () => {
  composerFileDragDepth = Math.max(0, composerFileDragDepth - 1);
  if (!composerFileDragDepth) elements.composer.classList.remove("file-drag-active");
});
elements.composer.addEventListener("drop", (event) => {
  if (!hasDraggedFiles(event.dataTransfer)) return;
  event.preventDefault();
  composerFileDragDepth = 0;
  elements.composer.classList.remove("file-drag-active");
  void addFiles([...(event.dataTransfer?.files ?? [])]);
});
elements.approve.addEventListener("click", () => void resolveApproval("accept"));
elements.decline.addEventListener("click", () => void resolveApproval("decline"));
elements.treeMenu.addEventListener("click", () => setMobileSidebarOpen(!elements.sidebar.classList.contains("mobile-open")));
elements.sidebarBackdrop.addEventListener("click", () => setMobileSidebarOpen(false));
document.addEventListener("touchstart", (event) => {
  if (!mobileLayout() || event.touches.length !== 1) return;
  const touch = event.touches[0];
  sidebarSwipe = {
    touchId: touch.identifier,
    startX: touch.clientX,
    startY: touch.clientY,
    sidebarOpen: elements.sidebar.classList.contains("mobile-open"),
  };
}, { capture: true, passive: true });
document.addEventListener("touchmove", (event) => {
  if (!sidebarSwipe) return;
  const touch = [...event.touches].find((item) => item.identifier === sidebarSwipe?.touchId);
  if (!touch) return;
  const horizontalDistance = touch.clientX - sidebarSwipe.startX;
  const verticalDistance = touch.clientY - sidebarSwipe.startY;
  const action = sidebarSwipeAction(sidebarSwipe.startX, touch.clientX, verticalDistance, sidebarSwipe.sidebarOpen);
  if (action !== "none") {
    event.preventDefault();
    sidebarSwipe = undefined;
    setMobileSidebarOpen(action === "open");
  } else if (Math.abs(verticalDistance) > 48
    || (!sidebarSwipe.sidebarOpen && horizontalDistance < -10)
    || (sidebarSwipe.sidebarOpen && horizontalDistance > 10)) {
    sidebarSwipe = undefined;
  }
}, { capture: true, passive: false });
const endSidebarSwipe = (): void => { sidebarSwipe = undefined; };
document.addEventListener("touchend", endSidebarSwipe, { capture: true, passive: true });
document.addEventListener("touchcancel", endSidebarSwipe, { capture: true, passive: true });
document.addEventListener("keydown", (event) => { if (event.key === "Escape") setMobileSidebarOpen(false); });
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible" || !elements.device.value || !elements.chat.value) return;
  markChatRead(elements.device.value, elements.chat.value);
  scheduleTreeRender();
});
window.addEventListener("resize", () => {
  updateVisualViewportHeight();
  if (!mobileLayout()) setMobileSidebarOpen(false);
});
window.addEventListener("hashchange", () => {
  const selection = chatSelectionFromHash(location.hash);
  if (selection) beginRestoreSelection(selection);
});
setInterval(renderDevices, 15_000);

onAuthStateChanged(auth, (current) => {
  user = current;
  loadComposerDrafts(current?.uid);
  loadChatReadStates(current?.uid);
  restoredSelection = current ? chatSelectionFromHash(location.hash) ?? loadStoredSelection(current.uid) : undefined;
  elements.login.hidden = Boolean(current);
  elements.app.hidden = !current;
  elements.logout.hidden = !current;
  clearError();
  stopSubscriptions();
  if (current) subscribeDevices();
});

async function loginWithGoogle(): Promise<void> {
  clearError();
  elements.googleLogin.disabled = true;
  try {
    await signInWithPopup(auth, new GoogleAuthProvider());
  } catch (error) {
    showError(error);
  } finally {
    elements.googleLogin.disabled = false;
  }
}

function subscribeDevices(): void {
  if (!user) return;
  deviceUnsubscribe = onSnapshot(collection(db, "users", user.uid, "devices"), (snapshot) => {
    devices = snapshot.docs;
    const previousDeviceId = elements.device.value;
    renderDevices();
    reconcileDeviceSubscriptions();
    if (elements.device.value !== subscribedDeviceId || elements.device.value !== previousDeviceId) {
      subscribeDeviceContents();
    }
  }, showError);
}

function renderDevices(): void {
  const selected = elements.device.value || restoredSelection?.deviceId || "";
  replaceOptions(elements.device, devices.map((item) => ({ value: item.id, label: deviceLabel(item) })), selected, true);
  const current = devices.find((item) => item.id === elements.device.value);
  if (current && (selected === "" || selected !== elements.device.value)) expandedDeviceIds.add(current.id);
  const signature = devices.map((item) => `${item.id}:${deviceLabel(item)}`).join("|");
  if (signature !== renderedDeviceSignature) {
    renderedDeviceSignature = signature;
    renderTree();
    renderConversationHeader();
  }
}

function deviceLabel(item: QueryDocumentSnapshot<DocumentData>): string {
  return `${text(item.data().displayName) || item.id} — ${deviceOnline(item) ? "online" : "offline"}`;
}

function deviceOnline(item: QueryDocumentSnapshot<DocumentData>): boolean {
  const data = item.data();
  const lastSeen = timestamp(data.lastSeenAt);
  const heartbeat = typeof data.heartbeatSeconds === "number" ? data.heartbeatSeconds : 30;
  return text(data.status) === "online" && Date.now() - lastSeen <= Math.max(60, heartbeat * 3) * 1000;
}

function subscribeDeviceContents(): void {
  taskUnsubscribe?.(); eventUnsubscribe?.();
  renderedTreeSignature = "";
  renderedConversationSignature = "";
  activeTaskId = "";
  activeTaskEvents = [];
  activeApproval = undefined;
  pendingCreatedChat = undefined;
  subscribedDeviceId = elements.device.value;
  elements.project.value = "";
  elements.chat.value = "";
  elements.newChat.checked = true;
  elements.history.innerHTML = '<p class="muted">左のツリーからチャットを選択してください。</p>';
  renderTree();
  renderConversationHeader();
  if (!user || !elements.device.value) return;
  reconcileDeviceSubscriptions();
  const deviceRef = doc(db, "users", user.uid, "devices", elements.device.value);
  taskUnsubscribe = onSnapshot(collection(deviceRef, "tasks"), (snapshot) => {
    tasks = [...snapshot.docs].sort((a, b) => timestamp(b.data().createdAt) - timestamp(a.data().createdAt)).slice(0, 30);
    reconcileOptimisticTaskStatus();
    reconcileCreatedChatSelection();
    renderedConversationSignature = "";
    renderConversationHeader();
  }, showError);
  subscribeDeviceProjects(elements.device.value, deviceRef);
  syncSelectedDeviceControls();
}

function reconcileDeviceSubscriptions(): void {
  if (!user) return;
  const deviceIds = new Set(devices.map((device) => device.id));
  for (const [deviceId, unsubscribe] of projectUnsubscribes) {
    if (deviceIds.has(deviceId)) continue;
    unsubscribe();
    projectUnsubscribes.delete(deviceId);
    chatUnsubscribes.get(deviceId)?.();
    chatUnsubscribes.delete(deviceId);
    for (const key of [...chatsByDeviceProject.keys()]) if (key.startsWith(`${deviceId}/`)) chatsByDeviceProject.delete(key);
    projectsByDevice.delete(deviceId);
  }
  for (const device of devices) {
    if (!projectUnsubscribes.has(device.id)) {
      subscribeDeviceProjects(device.id, doc(db, "users", user.uid, "devices", device.id));
    }
    if (!chatUnsubscribes.has(device.id)) subscribeDeviceChats(device.id, doc(db, "users", user.uid, "devices", device.id));
  }
}

function subscribeDeviceProjects(deviceId: string, deviceRef: ReturnType<typeof doc>): void {
  if (projectUnsubscribes.has(deviceId)) return;
  const unsubscribe = onSnapshot(collection(deviceRef, "projects"), (snapshot) => {
    const previousProjectId = elements.project.value || (restoredSelection?.deviceId === deviceId ? restoredSelection.projectId : "");
    const nextProjects = sortBySyncOrder(snapshot.docs.filter((item) => !isArchived(item.data())));
    projectsByDevice.set(deviceId, nextProjects);
    if (deviceId === elements.device.value) {
      replaceOptions(elements.project, nextProjects.map((item) => ({ value: item.id, label: text(item.data().displayName) || item.id })), previousProjectId, true);
    }
    if (deviceId === elements.device.value && elements.project.value !== previousProjectId) {
      elements.chat.value = "";
      elements.newChat.checked = Boolean(elements.project.value);
    }
    if (deviceId === elements.device.value && elements.project.value && elements.project.value !== previousProjectId) {
      expandedProjectIds.add(treeProjectKey(deviceId, elements.project.value));
    }
    renderTree();
    renderConversationHeader();
    tryRestoreSelection();
    if (deviceId === elements.device.value && elements.project.value && elements.project.value !== previousProjectId) {
      void refreshRuntime();
    }
  }, showError);
  projectUnsubscribes.set(deviceId, unsubscribe);
}

function subscribeDeviceChats(deviceId: string, deviceRef: ReturnType<typeof doc>): void {
  if (chatUnsubscribes.has(deviceId)) return;
  const unsubscribe = onSnapshot(collection(deviceRef, "chats"), (snapshot) => {
    updateChatReadStates(deviceId, snapshot.docs.filter((item) => !isArchived(item.data())));
    for (const key of [...chatsByDeviceProject.keys()]) if (key.startsWith(`${deviceId}/`)) chatsByDeviceProject.delete(key);
    const grouped = new Map<string, Array<QueryDocumentSnapshot<DocumentData>>>();
    for (const chat of snapshot.docs.filter((item) => !isArchived(item.data()))) {
      const projectId = text(chat.data().projectId);
      if (!projectId) continue;
      const chats = grouped.get(projectId) ?? [];
      chats.push(chat);
      grouped.set(projectId, chats);
    }
    for (const [projectId, chats] of grouped) chatsByDeviceProject.set(treeProjectKey(deviceId, projectId), sortBySyncOrder(chats));
    reconcileOptimisticHistory(deviceId);
    reconcileCreatedChatSelection();
    if (deviceId === elements.device.value) {
      syncHiddenChatOptions(elements.project.value);
      const selected = selectedChats(elements.project.value);
      if (elements.chat.value && !selected.some((item) => item.id === elements.chat.value)) {
        elements.chat.value = "";
        elements.newChat.checked = Boolean(elements.project.value);
      }
      renderConversationHeader();
      renderRunProgress();
    }
    tryRestoreSelection();
    scheduleTreeRender();
  }, showError);
  chatUnsubscribes.set(deviceId, unsubscribe);
}

function renderTree(): void {
  const signature = treeSignature();
  if (signature === renderedTreeSignature) return;
  renderedTreeSignature = signature;
  elements.projectTree.replaceChildren();
  elements.treeEmpty.hidden = devices.length > 0;
  for (const device of devices) {
    const deviceGroup = div("tree-node device-node");
    const deviceRow = div("tree-row");
    const deviceExpanded = expandedDeviceIds.has(device.id);
    const deviceToggle = treeToggle(deviceExpanded, deviceExpanded ? "PCを閉じる" : "PCを開く");
    deviceToggle.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleExpanded(expandedDeviceIds, device.id);
    });
    const deviceButton = treeButton("▣", text(device.data().displayName) || device.id, "pc", device.id === elements.device.value);
    const online = deviceOnline(device);
    const deviceStatus = document.createElement("span");
    deviceStatus.className = `status-dot tree-device-status${online ? " online" : ""}`;
    deviceStatus.title = online ? "オンライン" : "オフライン";
    deviceStatus.setAttribute("aria-label", deviceStatus.title);
    deviceButton.append(deviceStatus);
    deviceButton.addEventListener("click", () => selectDevice(device.id));
    deviceRow.append(deviceToggle, deviceButton);
    deviceGroup.append(deviceRow);
    if (deviceExpanded) {
      const deviceChildren = div("tree-children");
      for (const project of projectsByDevice.get(device.id) ?? []) {
        const projectGroup = div("tree-node project-node");
        const projectRow = div("tree-row");
        const projectKey = treeProjectKey(device.id, project.id);
        const projectExpanded = expandedProjectIds.has(projectKey);
        const projectChats = chatsByDeviceProject.get(projectKey) ?? [];
        const projectToggle = projectChats.length > 0
          ? treeToggle(projectExpanded, projectExpanded ? "プロジェクトを閉じる" : "プロジェクトを開く")
          : treeSpacer();
        if (projectChats.length > 0) {
          projectToggle.addEventListener("click", (event) => {
            event.stopPropagation();
            toggleExpanded(expandedProjectIds, projectKey);
          });
        }
        const projectButton = treeButton("▰", text(project.data().displayName) || project.id, "project", device.id === elements.device.value && project.id === elements.project.value);
        if (!projectExpanded) {
          const childIndicators = projectChats.map((chat) => chatIndicatorForDocument(device.id, chat));
          appendTreeIndicator(projectButton, projectTreeIndicator(childIndicators), true);
        }
        projectButton.addEventListener("click", () => selectProject(device.id, project.id));
        projectRow.append(projectToggle, projectButton);
        projectGroup.append(projectRow);
        if (projectExpanded) {
          const projectChildren = div("tree-children");
          for (const chat of projectChats) {
            const chatRow = div("tree-row chat-row");
            const chatButton = treeButton("●", text(chat.data().title) || "New Chat", "chat", device.id === elements.device.value && project.id === elements.project.value && chat.id === elements.chat.value);
            appendTreeIndicator(chatButton, chatIndicatorForDocument(device.id, chat), false);
            chatButton.addEventListener("click", () => selectChat(device.id, project.id, chat.id));
            chatRow.append(treeSpacer(), chatButton);
            projectChildren.append(chatRow);
          }
          projectGroup.append(projectChildren);
        }
        deviceChildren.append(projectGroup);
      }
      deviceGroup.append(deviceChildren);
    }
    elements.projectTree.append(deviceGroup);
  }
}

function treeSignature(): string {
  const devicesSignature = devices.map((device) => `${device.id}:${deviceLabel(device)}`).join("|");
  const projectsSignature = devices.map((device) => (projectsByDevice.get(device.id) ?? []).map((project) => {
    const key = treeProjectKey(device.id, project.id);
    const chats = chatsByDeviceProject.get(key) ?? [];
    return `${key}:${text(project.data().displayName)}:${expandedProjectIds.has(key)}:${chats.map((chat) => {
      const activeRunStatus = isObject(chat.data().activeRun) ? text(chat.data().activeRun.status) : "";
      const unread = chatReadStates.get(chatReadKey(device.id, chat.id))?.unread === true;
      return `${chat.id}:${text(chat.data().title)}:${chatTreeIndicator(text(chat.data().status), activeRunStatus, unread)}`;
    }).join(",")}`;
  }).join("|")).join("|");
  return `${elements.device.value}:${elements.project.value}:${elements.chat.value}:${[...expandedDeviceIds].join(",")}:${devicesSignature}:${projectsSignature}`;
}

function treeButton(icon: string, label: string, kind: "pc" | "project" | "chat", selected: boolean): HTMLButtonElement {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `tree-label ${kind}${selected ? " selected" : ""}`;
  const iconNode = document.createElement("span"); iconNode.className = "tree-icon"; iconNode.textContent = icon;
  const textNode = document.createElement("span"); textNode.className = "tree-text"; textNode.textContent = label; textNode.title = label;
  button.append(iconNode, textNode);
  return button;
}

function chatIndicatorForDocument(deviceId: string, chat: QueryDocumentSnapshot<DocumentData>): "running" | "unread" | "none" {
  const activeRunStatus = isObject(chat.data().activeRun) ? text(chat.data().activeRun.status) : "";
  return chatTreeIndicator(text(chat.data().status), activeRunStatus, chatReadStates.get(chatReadKey(deviceId, chat.id))?.unread === true);
}

function appendTreeIndicator(button: HTMLButtonElement, indicator: "running" | "unread" | "none", project: boolean): void {
  if (indicator === "none") return;
  const indicatorNode = document.createElement("span");
  indicatorNode.className = `tree-chat-indicator ${indicator}`;
  indicatorNode.title = indicator === "running"
    ? project ? "配下のチャットが思考中" : "思考中"
    : project ? "配下のチャットに未読あり" : "未読あり";
  indicatorNode.setAttribute("aria-label", indicatorNode.title);
  button.append(indicatorNode);
}

function treeToggle(expanded: boolean, label: string): HTMLButtonElement {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "tree-toggle";
  button.textContent = expanded ? "▾" : "▸";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.setAttribute("aria-expanded", String(expanded));
  return button;
}

function treeSpacer(): HTMLSpanElement {
  const spacer = document.createElement("span");
  spacer.className = "tree-toggle-spacer";
  return spacer;
}

function toggleExpanded(collection: Set<string>, id: string): void {
  if (collection.has(id)) collection.delete(id);
  else collection.add(id);
  renderTree();
}

function scheduleTreeRender(): void {
  if (treeRenderFrame !== undefined) return;
  treeRenderFrame = window.requestAnimationFrame(() => {
    treeRenderFrame = undefined;
    renderTree();
  });
}

function selectDevice(deviceId: string): void {
  if (elements.device.value === deviceId && subscribedDeviceId === deviceId) {
    expandedDeviceIds.add(deviceId);
    syncSelectedDeviceControls();
    renderTree();
    return;
  }
  elements.device.value = deviceId;
  expandedDeviceIds.add(deviceId);
  renderDevices();
  subscribeDeviceContents();
}

function selectProject(deviceId: string, projectId: string, clearHash = true): void {
  if (elements.device.value !== deviceId) selectDevice(deviceId);
  elements.project.value = projectId;
  elements.chat.value = "";
  elements.newChat.checked = true;
  saveStoredSelection({ deviceId, projectId, chatId: "" });
  syncHiddenChatOptions(projectId);
  expandedProjectIds.add(treeProjectKey(deviceId, projectId));
  renderTree();
  renderConversationHeader();
  void refreshRuntime();
  setMobileSidebarOpen(false);
  if (clearHash && location.hash) history.pushState(null, "", `${location.pathname}${location.search}`);
}

function selectChat(deviceId: string, projectId: string, chatId: string): void {
  if (elements.device.value !== deviceId || elements.project.value !== projectId) selectProject(deviceId, projectId, false);
  elements.chat.value = chatId;
  elements.newChat.checked = false;
  markChatRead(deviceId, chatId);
  const selection = { deviceId, projectId, chatId };
  saveStoredSelection(selection);
  setChatSelectionHash(selection, true);
  renderTree();
  renderConversationHeader();
  void refreshRuntime();
  setMobileSidebarOpen(false);
}

function beginRestoreSelection(selection: WorkspaceSelection): void {
  restoredSelection = selection;
  if (elements.device.value !== selection.deviceId && devices.some((device) => device.id === selection.deviceId)) {
    elements.device.value = selection.deviceId;
    expandedDeviceIds.add(selection.deviceId);
    subscribeDeviceContents();
    return;
  }
  tryRestoreSelection();
}

function tryRestoreSelection(): void {
  const target = restoredSelection;
  if (!target || elements.device.value !== target.deviceId) return;
  if (!(projectsByDevice.get(target.deviceId) ?? []).some((project) => project.id === target.projectId)) return;
  const chats = chatsByDeviceProject.get(treeProjectKey(target.deviceId, target.projectId)) ?? [];
  if (target.chatId && !chats.some((chat) => chat.id === target.chatId)) return;
  restoredSelection = undefined;
  elements.project.value = target.projectId;
  syncHiddenChatOptions(target.projectId);
  elements.chat.value = target.chatId;
  elements.newChat.checked = !target.chatId;
  if (target.chatId) markChatRead(target.deviceId, target.chatId);
  saveStoredSelection(target);
  if (target.chatId) setChatSelectionHash(target, false);
  expandedDeviceIds.add(target.deviceId);
  expandedProjectIds.add(treeProjectKey(target.deviceId, target.projectId));
  renderedConversationSignature = "";
  renderTree();
  renderConversationHeader();
  void refreshRuntime();
}

function setChatSelectionHash(selection: ChatSelection, push: boolean): void {
  const hash = chatSelectionHash(selection);
  if (location.hash === hash) return;
  const url = `${location.pathname}${location.search}${hash}`;
  if (push) history.pushState(null, "", url);
  else history.replaceState(null, "", url);
}

function selectionStorageKey(uid: string): string {
  return `codex-lite-remote:last-chat:${uid}`;
}

function loadStoredSelection(uid: string): WorkspaceSelection | undefined {
  try {
    const value = JSON.parse(localStorage.getItem(selectionStorageKey(uid)) || "null") as unknown;
    if (!isObject(value)) return undefined;
    const deviceId = text(value.deviceId); const projectId = text(value.projectId); const chatId = text(value.chatId);
    return deviceId && projectId ? { deviceId, projectId, chatId } : undefined;
  } catch { return undefined; }
}

function saveStoredSelection(selection: WorkspaceSelection): void {
  if (!user) return;
  localStorage.setItem(selectionStorageKey(user.uid), JSON.stringify(selection));
}

function composerDraftStorageKey(uid: string): string {
  return `codex-lite-remote-drafts:${uid}`;
}

function loadComposerDrafts(uid?: string): void {
  composerDrafts = new Map();
  if (!uid) return;
  try {
    const stored = JSON.parse(localStorage.getItem(composerDraftStorageKey(uid)) || "{}");
    if (!isObject(stored)) return;
    for (const [key, value] of Object.entries(stored)) {
      if (!isObject(value) || typeof value.content !== "string" || typeof value.updatedAt !== "number") continue;
      composerDrafts.set(key, { content: value.content, updatedAt: value.updatedAt });
    }
  } catch {
    localStorage.removeItem(composerDraftStorageKey(uid));
  }
}

function syncComposerDraftContext(): void {
  const next = composerDraftContextKey(elements.device.value, elements.project.value, elements.chat.value);
  if (next === composerDraftContext) return;
  saveComposerDraft(composerDraftContext, elements.content.value);
  composerDraftContext = next;
  elements.content.value = next ? composerDrafts.get(next)?.content || "" : "";
}

function saveCurrentComposerDraft(): void {
  syncComposerDraftContext();
  saveComposerDraft(composerDraftContext, elements.content.value);
}

function saveComposerDraft(context: string, content: string): void {
  if (!user || !context) return;
  if (content) composerDrafts.set(context, { content, updatedAt: Date.now() });
  else composerDrafts.delete(context);
  const retained = [...composerDrafts.entries()]
    .sort((left, right) => right[1].updatedAt - left[1].updatedAt)
    .slice(0, 50);
  composerDrafts = new Map(retained);
  try {
    localStorage.setItem(composerDraftStorageKey(user.uid), JSON.stringify(Object.fromEntries(retained)));
  } catch (error) {
    showError(error instanceof Error ? `下書きを保存できませんでした。${error.message}` : "下書きを保存できませんでした。");
  }
}

function chatReadStorageKey(uid: string): string {
  return `codex-lite-remote-chat-read-v2:${uid}`;
}

function chatReadKey(deviceId: string, chatId: string): string {
  return `${deviceId}\n${chatId}`;
}

function loadChatReadStates(uid?: string): void {
  chatReadStates.clear();
  if (!uid) return;
  try {
    const stored = JSON.parse(localStorage.getItem(chatReadStorageKey(uid)) || "{}");
    if (!isObject(stored)) return;
    for (const [key, value] of Object.entries(stored)) {
      if (!isObject(value) || typeof value.version !== "string") continue;
      chatReadStates.set(key, { version: value.version, unread: value.unread === true });
    }
  } catch {
    localStorage.removeItem(chatReadStorageKey(uid));
  }
}

function saveChatReadStates(): void {
  if (!user) return;
  localStorage.setItem(chatReadStorageKey(user.uid), JSON.stringify(Object.fromEntries(chatReadStates)));
}

function updateChatReadStates(deviceId: string, chats: Array<QueryDocumentSnapshot<DocumentData>>): void {
  const currentKeys = new Set(chats.map((chat) => chatReadKey(deviceId, chat.id)));
  let changed = false;
  for (const key of [...chatReadStates.keys()]) {
    if (!key.startsWith(`${deviceId}\n`) || currentKeys.has(key)) continue;
    chatReadStates.delete(key);
    changed = true;
  }
  for (const chat of chats) {
    const key = chatReadKey(deviceId, chat.id);
    const version = completedRunVersion(chat.data().lastRunProgress);
    const previous = chatReadStates.get(key);
    const visible = document.visibilityState === "visible" && deviceId === elements.device.value && chat.id === elements.chat.value;
    if (!previous) {
      chatReadStates.set(key, { version, unread: false });
      changed = true;
    } else if (previous.version !== version || (visible && previous.unread)) {
      chatReadStates.set(key, { version, unread: visible ? false : previous.unread || previous.version !== version });
      changed = true;
    }
  }
  if (changed) saveChatReadStates();
}

function markChatRead(deviceId: string, chatId: string): void {
  const key = chatReadKey(deviceId, chatId);
  const previous = chatReadStates.get(key);
  if (!previous?.unread) return;
  chatReadStates.set(key, { ...previous, unread: false });
  saveChatReadStates();
  renderedTreeSignature = "";
}

function syncSelectedHistorySubscription(): void {
  const desiredKey = user && elements.device.value && elements.chat.value
    ? `${elements.device.value}\n${elements.chat.value}`
    : "";
  if (desiredKey === selectedHistoryKey) return;
  historyUnsubscribe?.();
  historyUnsubscribe = undefined;
  selectedHistoryKey = desiredKey;
  selectedHistorySignature = "";
  selectedHistoryEntries = [];
  if (!user || !desiredKey) return;
  const subscriptionKey = desiredKey;
  const chunks = query(
    collection(db, "users", user.uid, "devices", elements.device.value, "historyChunks"),
    where("chatId", "==", elements.chat.value),
  );
  historyUnsubscribe = onSnapshot(chunks, (snapshot) => {
    if (selectedHistoryKey !== subscriptionKey) return;
    const ordered = [...snapshot.docs].sort((left, right) => number(left.data().index) - number(right.data().index));
    selectedHistoryEntries = parseHistoryChunk(ordered.map((chunk) => text(chunk.data().payload)).join(""))
      .filter(isHistoryEntry);
    selectedHistorySignature = ordered.map((chunk) => `${chunk.id}:${text(chunk.data().hash)}`).join("|");
    reconcileOptimisticHistory(elements.device.value);
    renderedConversationSignature = "";
    renderConversationHeader();
  }, showError);
}

function mobileLayout(): boolean {
  return window.matchMedia("(max-width: 760px)").matches;
}

function setMobileSidebarOpen(open: boolean): void {
  const next = mobileLayout() && open;
  elements.sidebar.classList.toggle("mobile-open", next);
  elements.sidebarBackdrop.hidden = !next;
  elements.treeMenu.setAttribute("aria-expanded", String(next));
  elements.treeMenu.setAttribute("aria-label", next ? "プロジェクト一覧を閉じる" : "プロジェクト一覧を開く");
}

function renderConversationHeader(): void {
  syncComposerDraftContext();
  syncSelectedHistorySubscription();
  const device = devices.find((item) => item.id === elements.device.value);
  const project = selectedProjects().find((item) => item.id === elements.project.value);
  const chat = selectedChats(elements.project.value).find((item) => item.id === elements.chat.value);
  const deviceName = device ? text(device.data().displayName) || device.id : "PC";
  const projectName = project ? text(project.data().displayName) || project.id : "プロジェクト";
  const chatName = chat ? text(chat.data().title) || "New Chat" : projectName;
  elements.breadcrumb.textContent = project ? `${deviceName} / ${projectName}` : "プロジェクトを選択してください";
  elements.conversationTitle.textContent = chat ? chatName : project ? "新しいチャット" : "会話";
  elements.conversationStatus.textContent = chat ? (text(chat.data().status) || "待機中") : "待機中";
  elements.send.textContent = chat ? "送信" : "新規チャットを作成";
  elements.send.disabled = !project;
  elements.content.disabled = !project;
  elements.model.disabled = !project;
  elements.reasoning.disabled = !project;
  elements.approvalLevel.disabled = !project;
  elements.attachFile.disabled = !project;
  elements.usagePanel.hidden = !project || Boolean(chat);
  const optimistic = selectedOptimisticInstructions();
  const liveProgress = selectedLiveProgressItems();
  const conversationSelection = `${elements.device.value}:${elements.project.value}:${elements.chat.value}`;
  const conversationSignature = `${elements.device.value}:${elements.project.value}:${elements.chat.value}:${selectedHistorySignature}:${chat ? `${text(chat.data().historyRevision)}:${number(chat.data().historyItemCount)}` : ""}:${optimistic.map((item) => `${item.id}:${item.state}:${item.chatId || ""}`).join(",")}:${progressSignature(liveProgress)}`;
  if (conversationSignature !== renderedConversationSignature) {
    const selectionChanged = conversationSelection !== renderedConversationSelection;
    if (selectionChanged) expandedProgressIds.clear();
    const previousOffset = elements.conversationBody.scrollTop;
    const position = historyUpdatePosition(selectionChanged, isConversationHistoryNearEnd(), historyFollowingEnd);
    renderedConversationSelection = conversationSelection;
    renderedConversationSignature = conversationSignature;
    renderConversationHistory();
    scheduleConversationHistoryPosition(chat ? position : "start", previousOffset);
  }
  renderRunProgress();
}

function renderConversationHistory(): void {
  elements.history.replaceChildren();
  const chat = selectedChats(elements.project.value).find((item) => item.id === elements.chat.value);
  const optimistic = selectedOptimisticInstructions();
  const liveProgress = selectedLiveProgressItems();
  if (!chat) {
    if (!optimistic.length && !liveProgress.length) elements.history.append(paragraph(
      elements.project.value ? "メッセージを入力して、新しいチャットを開始できます。" : "左のツリーからチャットを選択してください。",
      "empty muted",
    ));
    appendConversationTimeline([], optimistic, liveProgress);
    return;
  }
  const entries = selectedHistoryEntries;
  if (!entries.length && !optimistic.length && !liveProgress.length) {
    elements.history.append(paragraph("このチャットの履歴はまだ同期されていません。", "empty muted"));
    return;
  }
  appendConversationTimeline(entries, optimistic, liveProgress);
}

function appendConversationTimeline(
  entries: HistoryEntry[],
  optimistic: (typeof optimisticInstructions),
  liveProgress: Record<string, unknown>[],
): void {
  const timeline = sortConversationTimeline([
    ...entries.map((value, sourceOrder) => ({ type: "history" as const, value, createdAt: value.createdAt, sourceOrder })),
    ...optimistic.map((value, index) => ({ type: "optimistic" as const, value, createdAt: value.createdAt, sourceOrder: entries.length + index })),
    ...liveProgress.map((value, index) => ({ type: "progress" as const, value, createdAt: text(value.createdAt), sourceOrder: entries.length + optimistic.length + index })),
  ]);
  for (const item of timeline) {
    if (item.type === "optimistic") {
      appendOptimisticInstruction(item.value);
      continue;
    }
    if (item.type === "progress") {
      appendLiveProgressItem(item.value);
      continue;
    }
    const entry = item.value;
    if (entry.role === "status") {
      appendStatusHistoryItem(
        entry.activityKind === "reasoning" ? "reasoning" : "work",
        entry.content,
        text(entry.activityDetails),
        entry.createdAt,
        `history:${text(entry.id)}`,
      );
      continue;
    }
    const kind = historyKind(entry);
    const message = div(`message ${entry.role} ${kind}`);
    const meta = document.createElement("div");
    meta.className = "message-meta";
    meta.textContent = entry.role === "user" ? "あなた" : `Codex · ${historyKindLabel(kind)}`;
    if (entry.createdAt) meta.textContent += ` · ${formatDate(entry.createdAt)}`;
    const bubble = document.createElement("div");
    bubble.className = "message-bubble";
    renderMarkdown(bubble, entry.content);
    message.append(meta, bubble);
    elements.history.append(message);
  }
}

function appendLiveProgressItem(item: Record<string, unknown>): void {
  appendStatusHistoryItem(
    text(item.kind) === "reasoning" ? "reasoning" : "work",
    text(item.text),
    text(item.details),
    text(item.createdAt),
    text(item.displayKey),
  );
}

function appendStatusHistoryItem(kind: "reasoning" | "work", summary: string, details: string, createdAt = "", itemKey = ""): void {
  const content = details.trim() || summary.trim();
  if (!content) return;
  const message = div(`message status ${kind}`);
  const disclosure = document.createElement("details");
  disclosure.className = "status-disclosure";
  disclosure.open = Boolean(itemKey && expandedProgressIds.has(itemKey));
  if (itemKey) disclosure.addEventListener("toggle", () => {
    if (disclosure.open) expandedProgressIds.add(itemKey);
    else expandedProgressIds.delete(itemKey);
  });
  const toggle = document.createElement("summary");
  toggle.className = "message-meta status-summary";
  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  const cleanSummary = summary.trim();
  const compactSummary = (!/^[A-Za-z][A-Za-z0-9_./-]*$/.test(cleanSummary) ? cleanSummary : "") || content.split("\n", 1)[0];
  const shortSummary = compactSummary.length > 140 ? `${compactSummary.slice(0, 137)}...` : compactSummary;
  const summaryRow = document.createElement("span");
  summaryRow.className = "status-summary-row";
  const summaryLabel = document.createElement("span");
  summaryLabel.className = "status-summary-label";
  summaryLabel.textContent = `Codex · ${kind === "reasoning" ? "思考" : "作業"}${shortSummary ? ` — ${shortSummary}` : ""}`;
  const summaryTime = document.createElement("time");
  summaryTime.className = "status-summary-time";
  summaryTime.dateTime = createdAt;
  summaryTime.textContent = createdAt ? formatDate(createdAt) : "時刻未取得";
  summaryRow.append(summaryLabel, summaryTime);
  toggle.append(summaryRow);
  if (details.trim() && cleanSummary && cleanSummary !== details.trim() && !/^[A-Za-z][A-Za-z0-9_./-]*$/.test(cleanSummary)) {
    const heading = document.createElement("strong");
    heading.textContent = cleanSummary;
    bubble.append(heading);
  }
  const body = document.createElement("div");
  body.textContent = content;
  bubble.append(body);
  disclosure.append(toggle, bubble);
  message.append(disclosure);
  elements.history.append(message);
}

function appendOptimisticInstruction(item: (typeof optimisticInstructions)[number]): void {
  const message = div(`message user instruction optimistic ${item.state}`);
  const meta = document.createElement("div");
  meta.className = "message-meta";
  meta.textContent = `あなた · ${item.state === "failed" ? "送信失敗" : "送信中"}`;
  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  renderMarkdown(bubble, item.content);
  message.append(meta, bubble);
  elements.history.append(message);
}

function isConversationHistoryNearEnd(): boolean {
  const { scrollTop, clientHeight, scrollHeight } = elements.conversationBody;
  return scrollHeight <= clientHeight || scrollTop + clientHeight >= scrollHeight - 1;
}

function scheduleConversationHistoryPosition(position: "start" | "end" | "preserve", previousOffset: number): void {
  const version = ++historyScrollVersion;
  historyFollowingEnd = position === "end";
  const apply = (): void => {
    if (version !== historyScrollVersion) return;
    const body = elements.conversationBody;
    if (position === "start") body.scrollTop = 0;
    else if (position === "end") body.scrollTop = body.scrollHeight;
    else body.scrollTop = Math.min(previousOffset, Math.max(0, body.scrollHeight - body.clientHeight));
  };
  window.requestAnimationFrame(() => {
    apply();
    window.requestAnimationFrame(apply);
  });
}

function scheduleHistoryEndFollow(): void {
  if (!historyFollowingEnd || historyFollowFrame !== undefined) return;
  historyFollowFrame = window.requestAnimationFrame(() => {
    historyFollowFrame = undefined;
    if (historyFollowingEnd) elements.conversationBody.scrollTop = elements.conversationBody.scrollHeight;
  });
}

async function sendInstruction(): Promise<void> {
  if (!user || !elements.device.value || !elements.project.value) return;
  let content = elements.content.value.trim();
  if (!content && pendingAttachments.length) content = "添付ファイルを確認してください。";
  if (!content) return showError("指示を入力してください。");
  if (!elements.newChat.checked && !elements.chat.value) return showError("チャットを選択してください。");
  const activeRunId = elements.newChat.checked ? "" : selectedActiveRunId();
  const operation: RemoteOperation = composerOperation(elements.newChat.checked, activeRunId);
  const deviceId = elements.device.value;
  const projectId = elements.project.value;
  const chatId = operation === "create_chat" ? undefined : elements.chat.value;
  const wireAttachments = pendingAttachments.map(({ previewUrl: _previewUrl, ...attachment }) => attachment);
  const optimistic: (typeof optimisticInstructions)[number] = {
    id: crypto.randomUUID(),
    deviceId,
    projectId,
    ...(chatId ? { chatId } : {}),
    content,
    createdAt: new Date().toISOString(),
    baselineCount: chatId ? syncedInstructionCount(deviceId, projectId, chatId, content) : 0,
    state: "sending" as const,
  };
  optimisticInstructions.push(optimistic);
  elements.content.value = "";
  saveCurrentComposerDraft();
  clearPendingAttachments();
  renderedConversationSignature = "";
  renderConversationHeader();
  elements.send.disabled = true;
  clearError();
  try {
    const payload: RemoteTaskPayload = {
      projectId,
      ...(operation === "create_chat" ? { title: titleFromFirstInstruction(content) } : { chatId }),
      ...(operation === "steer_run" ? { runId: activeRunId } : {}),
      content,
      ...(wireAttachments.length ? { attachments: wireAttachments } : {}),
    };
    const taskId = await createTask(operation, payload);
    optimistic.taskId = taskId;
    if (operation === "create_chat") {
      pendingCreatedChat = {
        taskId,
        deviceId,
        projectId,
        optimisticId: optimistic.id,
      };
      reconcileCreatedChatSelection();
    }
  } catch (error) {
    optimistic.state = "failed";
    renderedConversationSignature = "";
    renderConversationHeader();
    showError(error);
  }
  finally { elements.send.disabled = false; }
}

async function createTaskAndWait(operation: RemoteOperation, payload: RemoteTaskPayload, timeoutMs = 25_000): Promise<Record<string, unknown>> {
  if (!user || !elements.device.value) throw new Error("接続先PCを選択してください。");
  const taskRef = doc(collection(db, "users", user.uid, "devices", elements.device.value, "tasks"));
  return await new Promise<Record<string, unknown>>((resolve, reject) => {
    let unsubscribe: Unsubscribe | undefined;
    const timer = window.setTimeout(() => {
      unsubscribe?.();
      reject(new Error("Remote設定の取得がタイムアウトしました。"));
    }, timeoutMs);
    unsubscribe = onSnapshot(taskRef, (snapshot) => {
      if (!snapshot.exists()) return;
      const data = snapshot.data();
      const status = text(data.status);
      if (!["completed", "failed", "cancelled", "connection_lost"].includes(status)) return;
      window.clearTimeout(timer);
      unsubscribe?.();
      if (status === "completed" && isObject(data.result)) resolve(data.result);
      else reject(new Error(isObject(data.error) ? text(data.error.message) || "Remote操作に失敗しました。" : "Remote操作に失敗しました。"));
    }, (error) => {
      window.clearTimeout(timer);
      unsubscribe?.();
      reject(error);
    });
    const body: Record<string, unknown> = { operation, payload, status: "queued", createdAt: serverTimestamp(), updatedAt: serverTimestamp() };
    void setDoc(taskRef, body).catch((error) => {
      window.clearTimeout(timer);
      unsubscribe?.();
      reject(error);
    });
  });
}

async function addSelectedFiles(): Promise<void> {
  const files = [...(elements.fileInput.files ?? [])];
  elements.fileInput.value = "";
  await addFiles(files);
}

function clipboardImageFiles(data: DataTransfer | null): File[] {
  if (!data) return [];
  const files = [...data.items]
    .filter((item) => item.kind === "file" && isSupportedImageMimeType(item.type))
    .map((item) => item.getAsFile())
    .filter((file): file is File => Boolean(file));
  return files.length ? files : [...data.files].filter((file) => isSupportedImageMimeType(file.type));
}

function hasDraggedFiles(data: DataTransfer | null): boolean {
  return Boolean(data && [...data.types].includes("Files"));
}

async function addFiles(files: File[]): Promise<void> {
  try {
    for (const file of files) {
      if (pendingAttachments.length >= 4) throw new Error("添付ファイルは4件までです。");
      if (!file.size) throw new Error("空のファイルは添付できません。");
      const totalSize = pendingAttachments.reduce((sum, item) => sum + Math.ceil(item.dataBase64.length * 0.75), 0) + file.size;
      if (totalSize > 600_000) throw new Error("添付ファイルの合計は600 KiB以下にしてください。");
      const dataUrl = await readFileDataUrl(file);
      const image = isSupportedImageMimeType(file.type);
      pendingAttachments.push({
        name: file.name || `attachment-${Date.now()}`,
        mimeType: file.type || "application/octet-stream",
        kind: image ? "image" : "file",
        dataBase64: dataUrl.slice(dataUrl.indexOf(",") + 1),
        ...(image ? { previewUrl: dataUrl } : {}),
      });
    }
    renderPendingAttachments();
  } catch (error) { showError(error); }
}

function readFileDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error("画像を読み込めませんでした。"));
    reader.readAsDataURL(file);
  });
}

function renderPendingAttachments(): void {
  elements.attachments.replaceChildren(...pendingAttachments.map((item, index) => {
    const wrapper = div("attachment");
    if (item.previewUrl) {
      const image = document.createElement("img"); image.src = item.previewUrl; image.alt = item.name;
      wrapper.append(image);
    } else {
      const file = document.createElement("span"); file.className = "attachment-file"; file.textContent = item.name; file.title = item.name;
      wrapper.append(file);
    }
    const remove = document.createElement("button"); remove.type = "button"; remove.textContent = "×"; remove.title = "添付を外す";
    remove.addEventListener("click", () => { pendingAttachments.splice(index, 1); renderPendingAttachments(); });
    wrapper.append(remove);
    return wrapper;
  }));
}

function clearPendingAttachments(): void {
  pendingAttachments = [];
  renderPendingAttachments();
}

async function createTask(operation: RemoteOperation, payload: RemoteTaskPayload): Promise<string> {
  if (!user || !elements.device.value) throw new Error("接続先PCを選択してください。");
  const taskRef = doc(collection(db, "users", user.uid, "devices", elements.device.value, "tasks"));
  const body: Record<string, unknown> = {
    operation, status: "queued", createdAt: serverTimestamp(), updatedAt: serverTimestamp(),
  };
  body.payload = payload;
  await setDoc(taskRef, body);
  return taskRef.id;
}

function reconcileCreatedChatSelection(): void {
  if (!pendingCreatedChat) return;
  const task = tasks.find((item) => item.id === pendingCreatedChat?.taskId);
  if (!task) return;
  const data = task.data();
  const chatId = taskChatId(data);
  const chats = chatsByDeviceProject.get(treeProjectKey(pendingCreatedChat.deviceId, pendingCreatedChat.projectId)) ?? [];
  const action = createdChatSelectionAction(
    text(data.status),
    chatId,
    chats.some((chat) => chat.id === chatId),
  );
  if (action === "wait") return;
  const target = pendingCreatedChat;
  pendingCreatedChat = undefined;
  if (action === "select") {
    const optimistic = optimisticInstructions.find((item) => item.id === target.optimisticId);
    if (optimistic) optimistic.chatId = chatId;
    selectChat(target.deviceId, target.projectId, chatId);
  }
}

function selectedOptimisticInstructions(): typeof optimisticInstructions {
  return optimisticInstructions.filter((item) => item.deviceId === elements.device.value
    && item.projectId === elements.project.value
    && (elements.chat.value ? item.chatId === elements.chat.value : !item.chatId));
}

function syncedInstructionCount(deviceId: string, projectId: string, chatId: string, content: string): number {
  if (`${deviceId}\n${chatId}` !== selectedHistoryKey || projectId !== elements.project.value) return 0;
  return selectedHistoryEntries.filter((entry) => entry.role === "user" && entry.content === content).length;
}

function reconcileOptimisticHistory(deviceId: string): void {
  const previousLength = optimisticInstructions.length;
  optimisticInstructions = optimisticInstructions.filter((item) => {
    if (item.deviceId !== deviceId || !item.chatId) return true;
    return syncedInstructionCount(item.deviceId, item.projectId, item.chatId, item.content) <= item.baselineCount;
  });
  if (optimisticInstructions.length !== previousLength) renderedConversationSignature = "";
}

function reconcileOptimisticTaskStatus(): void {
  let changed = false;
  for (const item of optimisticInstructions) {
    if (!item.taskId) continue;
    const task = tasks.find((candidate) => candidate.id === item.taskId);
    const status = task ? text(task.data().status) : "";
    if (["failed", "cancelled", "connection_lost"].includes(status) && item.state !== "failed") {
      item.state = "failed";
      changed = true;
    }
  }
  if (changed) {
    renderedConversationSignature = "";
    renderConversationHeader();
  }
}

function renderRunProgress(): void {
  const task = visibleActiveTask();
  if (!task) {
    eventUnsubscribe?.();
    eventUnsubscribe = undefined;
    activeTaskId = "";
    activeTaskEvents = [];
    activeApproval = undefined;
    const desktopRun = selectedDesktopRun();
    if (desktopRun) renderDesktopRunProgress(desktopRun);
    else {
      elements.runProgress.hidden = true;
      elements.approvalActions.hidden = true;
      elements.cancelRun.hidden = true;
    }
    return;
  }
  if (task.id !== activeTaskId) {
    eventUnsubscribe?.();
    activeTaskId = task.id;
    activeTaskEvents = [];
    activeApproval = undefined;
    eventUnsubscribe = onSnapshot(collection(task.ref, "events"), (snapshot) => {
      activeTaskEvents = snapshot.docs;
      renderActiveTask(task);
    }, showError);
  }
  renderActiveTask(task);
}

function selectedDesktopRun(): Record<string, unknown> | undefined {
  const chat = selectedChats(elements.project.value).find((item) => item.id === elements.chat.value);
  return chat && isObject(chat.data().activeRun) && text(chat.data().activeRun.id) ? chat.data().activeRun : undefined;
}

function selectedActiveRunId(): string {
  const desktopRunId = text(selectedDesktopRun()?.id);
  if (desktopRunId) return desktopRunId;
  const task = visibleActiveTask();
  return task ? taskRunId(task.data()) : "";
}

function selectedLiveProgressItems(): Record<string, unknown>[] {
  const task = selectedProgressTask();
  const chat = selectedChats(elements.project.value).find((item) => item.id === elements.chat.value);
  const persistedRunIds = selectedPersistedProgressRunIds();
  const runs: Record<string, unknown>[] = [];
  const completed = chat && isObject(chat.data().lastRunProgress) ? chat.data().lastRunProgress : undefined;
  if (completed && shouldKeepCompletedProgress(text(completed.id), persistedRunIds)) runs.push(completed);
  const active = selectedDesktopRun();
  if (active && !runs.some((run) => text(run.id) === text(active.id))) runs.push(active);
  if (task) {
    const taskRun = { id: taskRunId(task.data()), startedAt: timestamp(task.data().startedAt) ? new Date(timestamp(task.data().startedAt)).toISOString() : "", progressItems: task.data().progressItems };
    if (text(taskRun.id) && !runs.some((run) => text(run.id) === text(taskRun.id))) runs.push(taskRun);
  }
  return runs.sort((left, right) => text(left.startedAt).localeCompare(text(right.startedAt))).flatMap((run) => {
    const source = run.progressItems;
    const runId = text(run.id);
    return Array.isArray(source) ? source.filter(isObject).map((item) => ({
      ...item,
      createdAt: text(item.createdAt) || text(run.startedAt),
      displayKey: `live:${runId}:${number(item.firstSequence)}`,
    })) : [];
  });
}

function selectedProgressTask(): QueryDocumentSnapshot<DocumentData> | undefined {
  const active = visibleActiveTask();
  if (active) return active;
  const projectId = elements.project.value;
  const chatId = elements.chat.value;
  if (!projectId || !chatId) return undefined;
  const persistedRunIds = selectedPersistedProgressRunIds();
  return tasks.find((task) => {
    const data = task.data();
    if (taskProjectId(data) !== projectId || taskChatId(data) !== chatId || !Array.isArray(data.progressItems) || !data.progressItems.length) return false;
    return shouldKeepCompletedProgress(taskRunId(data), persistedRunIds);
  });
}

function selectedPersistedProgressRunIds(): string[] {
  return selectedHistoryEntries.filter((entry) => entry.role === "status").map((entry) => text(entry.runId)).filter(Boolean);
}

function progressSignature(items: Record<string, unknown>[]): string {
  return items.map((item) => [
    number(item.firstSequence),
    number(item.sequence),
    text(item.kind),
    text(item.createdAt),
    text(item.text).length,
    text(item.details).length,
  ].join(":")).join(",");
}

function renderDesktopRunProgress(run: Record<string, unknown>): void {
  const activity = activityCountLabel(number(run.reasoningActivityCount), number(run.workActivityCount));
  elements.runProgress.hidden = false;
  elements.runProgressText.textContent = `デスクトップで会話を実行中 — ${statusLabel(text(run.status) || "running")}${activity ? ` / ${activity}` : ""}`;
  elements.cancelRun.hidden = !text(run.id) || !["running", "waiting_for_approval"].includes(text(run.status) || "running");
  elements.approvalActions.hidden = true;
}

function renderActiveTask(task: QueryDocumentSnapshot<DocumentData>): void {
  const data = task.data();
  const status = text(data.status);
  const runId = taskRunId(data);
  const cloudRun = selectedDesktopRun();
  const activity = activityCountLabel(
    number(cloudRun?.reasoningActivityCount ?? data.reasoningActivityCount),
    number(cloudRun?.workActivityCount ?? data.workActivityCount),
  );
  const activityDetail = activity ? ` / ${activity}` : "";
  elements.runProgress.hidden = false;
  elements.runProgressText.textContent = `${operationLabel(text(data.operation))} — ${statusLabel(status)}${activityDetail}`;
  elements.cancelRun.hidden = !runId || !["queued", "claimed", "running", "waiting_for_approval"].includes(status);

  activeApproval = undefined;
  for (const event of [...activeTaskEvents].sort((a, b) => Number(b.data().sequence) - Number(a.data().sequence))) {
    const eventData = event.data();
    if (text(eventData.type) !== "approval" || !isObject(eventData.payload)) continue;
    const requestId = text(eventData.payload.requestId);
    if (requestId && runId) {
      activeApproval = {
        runId,
        requestId,
        summary: approvalSummary(text(eventData.payload.reason), text(eventData.payload.command)),
      };
      break;
    }
  }
  elements.approvalActions.hidden = !activeApproval || status !== "waiting_for_approval";
  elements.approvalSummary.textContent = activeApproval?.summary || "この操作の承認が必要です。";
}

async function cancelActiveRun(): Promise<void> {
  const task = visibleActiveTask();
  const runId = task ? taskRunId(task.data()) : text(selectedDesktopRun()?.id);
  if (!runId) return;
  try {
    await createTask("cancel_run", { runId });
    clearError();
  } catch (error) { showError(error); }
}

async function resolveApproval(decision: "accept" | "decline"): Promise<void> {
  if (!activeApproval) return showError("承認待ちの操作がありません。");
  try {
    await createTask("resolve_approval", { runId: activeApproval.runId, requestId: activeApproval.requestId, decision });
    clearError();
  } catch (error) { showError(error); }
}

function visibleActiveTask(): QueryDocumentSnapshot<DocumentData> | undefined {
  const projectId = elements.project.value;
  const chatId = elements.chat.value;
  if (!projectId || !chatId) return undefined;
  return tasks.find((task) => {
    const data = task.data();
    if (["get_runtime", "update_runtime"].includes(text(data.operation))) return false;
    if (!isActiveTaskStatus(text(data.status)) || taskProjectId(data) !== projectId) return false;
    const taskChat = taskChatId(data);
    return Boolean(taskChat) && taskChat === chatId;
  });
}

function taskProjectId(data: DocumentData): string {
  return isObject(data.payload) ? text(data.payload.projectId) : "";
}

function taskChatId(data: DocumentData): string {
  if (isObject(data.payload) && text(data.payload.chatId)) return text(data.payload.chatId);
  if (isObject(data.result) && isObject(data.result.chat)) return text(data.result.chat.id);
  return "";
}

function taskRunId(data: DocumentData): string {
  return text(data.runId) || (isObject(data.result) ? text(data.result.runId) : "");
}

function isActiveTaskStatus(status: string): boolean {
  return ["queued", "claimed", "running", "waiting_for_approval"].includes(status);
}

function operationLabel(operation: string): string {
  return ({ send_message: "会話を実行中", create_chat: "新しいチャットを作成中", steer_run: "追加指示を実行中", cancel_run: "実行を停止中", resolve_approval: "承認を処理中" } as Record<string, string>)[operation] || "操作を実行中";
}

function statusLabel(status: string): string {
  return ({ queued: "待機中", claimed: "接続中", running: "実行中", waiting_for_approval: "承認待ち" } as Record<string, string>)[status] || status;
}

function stopSubscriptions(): void {
  deviceUnsubscribe?.(); taskUnsubscribe?.(); eventUnsubscribe?.(); historyUnsubscribe?.();
  for (const unsubscribe of projectUnsubscribes.values()) unsubscribe();
  for (const unsubscribe of chatUnsubscribes.values()) unsubscribe();
  projectUnsubscribes.clear();
  chatUnsubscribes.clear();
  deviceUnsubscribe = taskUnsubscribe = eventUnsubscribe = historyUnsubscribe = undefined;
  subscribedDeviceId = "";
  renderedDeviceSignature = "";
  activeTaskId = "";
  activeTaskEvents = [];
  activeApproval = undefined;
  pendingCreatedChat = undefined;
  optimisticInstructions = [];
  expandedProgressIds.clear();
  runtimeKey = "";
  reasoningByModel = {};
  clearPendingAttachments();
  setMobileSidebarOpen(false);
  devices = [];
  tasks = [];
  projectsByDevice = new Map();
  chatsByDeviceProject = new Map();
  expandedDeviceIds.clear();
  expandedProjectIds.clear();
  renderedTreeSignature = "";
  renderedConversationSignature = "";
  renderedConversationSelection = "";
  selectedHistoryKey = "";
  selectedHistorySignature = "";
  selectedHistoryEntries = [];
  historyScrollVersion += 1;
  composerDraftContext = "";
  elements.content.value = "";
  elements.history.innerHTML = '<p class="empty muted">左のツリーからチャットを選択してください。</p>';
  elements.runProgress.hidden = true;
  elements.approvalActions.hidden = true;
  renderTree();
}

async function loadFirebaseConfig(): Promise<FirebasePublicConfig> {
  const response = await fetch("/firebase-config.json", { cache: "no-store" });
  if (!response.ok) throw new Error("firebase-config.json がありません。設定手順を確認してください。");
  return await response.json() as FirebasePublicConfig;
}

function replaceOptions(select: HTMLSelectElement, options: Array<{ value: string; label: string }>, selected: string, selectFirstWhenEmpty = false): void {
  select.replaceChildren(...options.map(({ value, label }) => {
    const option = document.createElement("option"); option.value = value; option.textContent = label; return option;
  }));
  if (options.some((item) => item.value === selected)) select.value = selected;
  else if (selected === "" && selectFirstWhenEmpty && options.length > 0) select.value = options[0].value;
  else select.value = "";
}

function syncHiddenChatOptions(projectId: string): void {
  if (projectId !== elements.project.value) return;
  const selected = elements.chat.value;
  const chats = selectedChats(projectId);
  replaceOptions(elements.chat, chats.map((item) => ({
    value: item.id,
    label: `${text(item.data().title) || "New Chat"}${text(item.data().status) !== "idle" ? ` — ${text(item.data().status)}` : ""}`,
  })), selected);
}

function syncSelectedDeviceControls(): void {
  if (!elements.device.value) return;
  const previousProjectId = elements.project.value;
  const nextProjects = selectedProjects();
  replaceOptions(elements.project, nextProjects.map((item) => ({ value: item.id, label: text(item.data().displayName) || item.id })), previousProjectId, true);
  if (elements.project.value !== previousProjectId) {
    elements.chat.value = "";
    elements.newChat.checked = Boolean(elements.project.value);
  }
  renderTree();
  renderConversationHeader();
  if (elements.project.value) void refreshRuntime();
}

async function refreshRuntime(force = false): Promise<void> {
  if (!elements.device.value || !elements.project.value) return;
  const key = `${elements.device.value}/${elements.project.value}/${elements.chat.value}`;
  if (!force && runtimeKey === key) return;
  runtimeKey = key;
  elements.usageRefresh.disabled = true;
  elements.model.disabled = true;
  elements.reasoning.disabled = true;
  elements.approvalLevel.disabled = true;
  try {
    const result = await createTaskAndWait("get_runtime", {
      projectId: elements.project.value,
      ...(elements.chat.value ? { chatId: elements.chat.value } : {}),
    });
    if (runtimeKey !== key) return;
    applyRuntime(result);
    clearError();
  } catch (error) {
    if (runtimeKey === key) {
      runtimeKey = "";
      showError(error);
    }
  } finally {
    elements.usageRefresh.disabled = false;
    if (runtimeKey === key && elements.project.value) {
      elements.model.disabled = false;
      elements.reasoning.disabled = false;
      elements.approvalLevel.disabled = false;
    }
  }
}

function applyRuntime(result: Record<string, unknown>): void {
  const settings = isObject(result.settings) ? result.settings : result;
  const models = isObject(result.models) ? result.models : {};
  const currentModel = text(settings.model);
  const availableModels = Array.isArray(models.availableModels) ? models.availableModels.filter((item): item is string => typeof item === "string")
    : Array.isArray(settings.availableModels) ? settings.availableModels.filter((item): item is string => typeof item === "string") : [];
  reasoningByModel = isObject(models.reasoningEffortsByModel)
    ? Object.fromEntries(Object.entries(models.reasoningEffortsByModel).map(([model, efforts]) => [model, Array.isArray(efforts) ? efforts.filter((item): item is string => typeof item === "string") : []]))
    : {};
  runtimeApplying = true;
  elements.approvalLevel.value = permissionModeForSettings(text(settings.permissionProfile), text(settings.approvalsReviewer));
  const modelOptions = [...new Set([currentModel, ...availableModels].filter(Boolean))];
  replaceSelectOptions(elements.model, [{ value: "", label: "既定" }, ...modelOptions.map((value) => ({ value, label: value }))], currentModel);
  updateReasoningOptions(text(settings.reasoningEffort));
  runtimeApplying = false;
  applyUsage(isObject(result.usage) ? result.usage : undefined, text(result.usageError));
}

function updateReasoningOptions(selected = ""): void {
  const fallback = ["", "low", "medium", "high", "xhigh", "max"];
  const efforts = [...new Set(["", ...(reasoningByModel[elements.model.value] ?? fallback)])];
  const labels: Record<string, string> = { "": "既定", minimal: "最小", low: "低", medium: "中", high: "高", xhigh: "最大", max: "最大+", ultra: "超高" };
  replaceSelectOptions(elements.reasoning, efforts.map((value) => ({ value, label: labels[value] || value })), selected);
}

async function runtimeSettingChanged(): Promise<void> {
  if (runtimeApplying || !elements.project.value) return;
  if (document.activeElement === elements.model) {
    runtimeApplying = true;
    updateReasoningOptions("");
    runtimeApplying = false;
  }
  elements.model.disabled = true;
  elements.reasoning.disabled = true;
  elements.approvalLevel.disabled = true;
  try {
    const permission = permissionSettingsForMode(elements.approvalLevel.value);
    await createTaskAndWait("update_runtime", {
      projectId: elements.project.value,
      ...(elements.chat.value ? { chatId: elements.chat.value } : {}),
      model: elements.model.value,
      reasoningEffort: elements.reasoning.value,
      ...permission,
    });
    runtimeKey = "";
    await refreshRuntime(true);
  } catch (error) { showError(error); }
  finally {
    elements.model.disabled = !elements.project.value;
    elements.reasoning.disabled = !elements.project.value;
    elements.approvalLevel.disabled = !elements.project.value;
  }
}

function applyUsage(usage: Record<string, unknown> | undefined, error: string): void {
  const provider = usage ? text(usage.provider) || "openai" : "";
  const isDeepSeek = provider === "deepseek";
  elements.usagePanel.classList.toggle("deepseek", isDeepSeek);
  elements.usageProvider.textContent = provider ? `提供元: ${provider === "deepseek" ? "DeepSeek" : "OpenAI / Codex"}` : "提供元: 取得不可";
  applyUsageWindow(elements.usageFiveText, elements.usageFiveBar, elements.usageFiveStart, elements.usageFiveEnd, usage && isObject(usage.fiveHour) ? usage.fiveHour : undefined, error);
  applyUsageWindow(elements.usageWeekText, elements.usageWeekBar, elements.usageWeekStart, elements.usageWeekEnd, usage && isObject(usage.weekly) ? usage.weekly : undefined, error);
  const credits = usage && isObject(usage.codexCredits) ? usage.codexCredits : undefined;
  const deepSeekBalance = usage && isObject(usage.deepseekBalance) ? usage.deepseekBalance : undefined;
  elements.usageCredits.textContent = isDeepSeek
    ? deepSeekBalanceLabel(deepSeekBalance)
    : credits ? remainingChargeLabel(credits) : error ? "取得不可" : "未取得";
}

function applyUsageWindow(label: HTMLElement, bar: HTMLElement, startLabel: HTMLElement, endLabel: HTMLElement, window: Record<string, unknown> | undefined, error: string): void {
  if (!window) {
    label.textContent = error ? "取得不可" : "未取得";
    startLabel.textContent = "";
    endLabel.textContent = "";
    bar.hidden = true;
    bar.parentElement?.classList.remove("available");
    return;
  }
  const used = typeof window.usedPercent === "number" ? Math.max(0, Math.min(100, window.usedPercent)) : 0;
  const remaining = Math.max(0, Math.min(100, typeof window.remainingPercent === "number" ? window.remainingPercent : 100 - used));
  const reset = text(window.resetsAt);
  label.textContent = `残り ${remaining.toFixed(0)}%（使用 ${used.toFixed(0)}%）${reset ? ` リセット ${formatUsageTime(new Date(reset))}` : ""}`;
  const resetAt = new Date(reset).getTime();
  const windowMinutes = typeof window.windowMinutes === "number" ? window.windowMinutes : 0;
  const startAt = resetAt - windowMinutes * 60_000;
  const elapsed = resetAt > startAt ? Math.max(0, Math.min(100, ((Date.now() - startAt) / (resetAt - startAt)) * 100)) : 0;
  bar.hidden = false;
  bar.parentElement?.classList.add("available");
  bar.style.left = `${elapsed}%`;
  bar.style.bottom = `${remaining}%`;
  bar.title = `現在: 残り ${remaining.toFixed(0)}%`;
  startLabel.textContent = Number.isFinite(startAt) ? formatUsageTime(new Date(startAt)) : "";
  endLabel.textContent = Number.isFinite(resetAt) ? formatUsageTime(new Date(resetAt)) : "";
}

function formatUsageTime(value: Date): string {
  if (Number.isNaN(value.getTime())) return "";
  const pad = (part: number) => String(part).padStart(2, "0");
  return `${pad(value.getMonth() + 1)}/${pad(value.getDate())} ${pad(value.getHours())}:${pad(value.getMinutes())}`;
}

function replaceSelectOptions(select: HTMLSelectElement, options: Array<{ value: string; label: string }>, selected: string): void {
  select.replaceChildren(...options.map(({ value, label }) => {
    const option = document.createElement("option"); option.value = value; option.textContent = label; return option;
  }));
  select.value = options.some((item) => item.value === selected) ? selected : "";
}

function timestamp(value: unknown): number {
  return isObject(value) && typeof value.toMillis === "function" ? Number(value.toMillis()) : 0;
}
function selectedProjects(): Array<QueryDocumentSnapshot<DocumentData>> {
  return projectsByDevice.get(elements.device.value) ?? [];
}
function selectedChats(projectId: string): Array<QueryDocumentSnapshot<DocumentData>> {
  return chatsByDeviceProject.get(treeProjectKey(elements.device.value, projectId)) ?? [];
}
function treeProjectKey(deviceId: string, projectId: string): string {
  return `${deviceId}/${projectId}`;
}
function formatDate(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}
function text(value: unknown): string { return typeof value === "string" ? value : ""; }
function number(value: unknown): number { return typeof value === "number" && Number.isFinite(value) ? value : 0; }
function isObject(value: unknown): value is Record<string, unknown> { return typeof value === "object" && value !== null; }
function isArchived(value: DocumentData): boolean {
  return value.archived === true || value.archivedAt != null || value.deleted === true || value.deletedAt != null || value.status === "archived" || value.status === "deleted";
}
type HistoryEntry = { id?: unknown; role: "user" | "assistant" | "status"; content: string; createdAt?: string; kind?: unknown; runId?: unknown; activityKind?: unknown; activityDetails?: unknown };

function isHistoryEntry(value: unknown): value is HistoryEntry {
  return isObject(value)
    && (value.role === "user" || value.role === "assistant" || value.role === "status")
    && typeof value.content === "string";
}
function historyKind(value: { role: "user" | "assistant" | "status"; kind?: unknown }): "instruction" | "work" | "conclusion" {
  if (value.role === "user") return "instruction";
  if (value.role === "status") return "work";
  return value.kind === "work" ? "work" : "conclusion";
}
function historyKindLabel(kind: "instruction" | "work" | "conclusion"): string {
  return ({ instruction: "指示", work: "作業内容", conclusion: "結論" })[kind];
}
function sortBySyncOrder<T extends QueryDocumentSnapshot<DocumentData>>(items: T[]): T[] {
  return [...items].sort((a, b) => {
    const left = typeof a.data().syncOrder === "number" ? a.data().syncOrder : Number.MAX_SAFE_INTEGER;
    const right = typeof b.data().syncOrder === "number" ? b.data().syncOrder : Number.MAX_SAFE_INTEGER;
    return left - right || a.id.localeCompare(b.id);
  });
}
function div(className: string): HTMLDivElement { const value = document.createElement("div"); value.className = className; return value; }
function paragraph(content: string, className: string): HTMLParagraphElement { const value = document.createElement("p"); value.textContent = content; value.className = className; return value; }
function byId<T extends HTMLElement>(id: string): T { const value = document.getElementById(id); if (!value) throw new Error(`UI要素がありません: ${id}`); return value as T; }
function clearError(): void { elements.error.textContent = ""; }
function showError(error: unknown): void { elements.error.textContent = error instanceof Error ? error.message : String(error); }
