export interface CatalogHistoryItem {
  id: string;
  role: string;
  content: string;
  createdAt: string;
  kind: string;
  runId?: string;
  activityKind?: "reasoning" | "work";
  activityDetails?: string;
}

export interface CatalogChat {
  id: string;
  title: string;
  status: string;
  updatedAt?: string;
  historyRevision?: string;
  activeRunId?: string;
  activeRunEventSequence?: number;
  historyChunkHashes?: string[];
}

export interface CatalogProject {
  id: string;
  name: string;
  chats: CatalogChat[];
}

export interface CloudCatalogProjectRecord {
  id: string;
  name: string;
  syncOrder: number;
}

export interface CloudCatalogChatRecord {
  id: string;
  projectId: string;
  title: string;
  status: string;
  syncOrder: number;
  updatedAt?: string;
  historyRevision?: string;
  historyChunkHashes?: string[];
}

export interface CatalogChanges {
  changedProjects: Array<{ project: CatalogProject; syncOrder: number }>;
  changedChats: Array<{ projectId: string; chat: CatalogChat; syncOrder: number }>;
  removedProjects: Array<{ projectId: string; chatIds: string[] }>;
  removedChats: Array<{ projectId: string; chatId: string }>;
  unchangedRecords: number;
}

export type PrioritizedCatalogChange =
  | { kind: "project"; project: CatalogProject; syncOrder: number }
  | { kind: "chat"; projectId: string; chat: CatalogChat; syncOrder: number };

export function catalogRetryDecision(
  failures: number,
  observedRevision: number,
  lastSuccessfulRevision: number,
): { stopRevision?: number; backoffMs: number } {
  if (failures >= 3 && observedRevision !== lastSuccessfulRevision) {
    return { stopRevision: observedRevision, backoffMs: 0 };
  }
  return { backoffMs: Math.min(300_000, 30_000 * (2 ** Math.max(0, failures - 1))) };
}

/**
 * Compare the local catalog with the last successfully synchronized catalog.
 * Timestamps used only for Firestore bookkeeping are deliberately excluded.
 */
export function diffCatalog(previous: CatalogProject[] | undefined, current: CatalogProject[]): CatalogChanges {
  const changedProjects: CatalogChanges["changedProjects"] = [];
  const changedChats: CatalogChanges["changedChats"] = [];
  const removedProjects: CatalogChanges["removedProjects"] = [];
  const removedChats: CatalogChanges["removedChats"] = [];
  const previousProjects = new Map((previous ?? []).map((project) => [project.id, project]));
  const currentProjectIds = new Set(current.map((project) => project.id));
  let unchangedRecords = 0;

  current.forEach((project, projectIndex) => {
    const previousProject = previousProjects.get(project.id);
    if (!previousProject || projectFingerprint(project, projectIndex) !== projectFingerprint(previousProject, previousIndex(previous, project.id))) {
      changedProjects.push({ project, syncOrder: projectIndex });
    } else {
      unchangedRecords += 1;
    }

    const previousChats = new Map((previousProject?.chats ?? []).map((chat) => [chat.id, chat]));
    const currentChatIds = new Set(project.chats.map((chat) => chat.id));
    project.chats.forEach((chat, chatIndex) => {
      const previousChat = previousChats.get(chat.id);
      if (!previousChat || chatFingerprint(chat, chatIndex) !== chatFingerprint(previousChat, previousIndex(previousProject?.chats, chat.id))) {
        changedChats.push({ projectId: project.id, chat, syncOrder: chatIndex });
      } else {
        unchangedRecords += 1;
      }
    });
    for (const previousChat of previousProject?.chats ?? []) {
      if (!currentChatIds.has(previousChat.id)) {
        removedChats.push({ projectId: project.id, chatId: previousChat.id });
      }
    }
  });

  for (const previousProject of previous ?? []) {
    if (!currentProjectIds.has(previousProject.id)) {
      removedProjects.push({ projectId: previousProject.id, chatIds: previousProject.chats.map((chat) => chat.id) });
    }
  }

  return { changedProjects, changedChats, removedProjects, removedChats, unchangedRecords };
}

export function catalogRecordCount(projects: CatalogProject[]): number {
  return projects.reduce((total, project) => total + 1 + project.chats.length, 0);
}

/** Build the previous sync baseline from lightweight Firestore catalog docs. */
export function cloudCatalogSnapshot(
  projectRecords: CloudCatalogProjectRecord[],
  chatRecords: CloudCatalogChatRecord[],
): CatalogProject[] {
  const projects = new Map<string, { project: CatalogProject; syncOrder: number }>();
  for (const record of projectRecords) {
    projects.set(record.id, {
      project: { id: record.id, name: record.name, chats: [] },
      syncOrder: record.syncOrder,
    });
  }
  for (const record of chatRecords) {
    let target = projects.get(record.projectId);
    if (!target) {
      target = {
        project: { id: record.projectId, name: "", chats: [] },
        syncOrder: Number.MAX_SAFE_INTEGER,
      };
      projects.set(record.projectId, target);
    }
    target.project.chats.push({
      id: record.id,
      title: record.title,
      status: record.status,
      ...(record.updatedAt ? { updatedAt: record.updatedAt } : {}),
      ...(record.historyRevision ? { historyRevision: record.historyRevision } : {}),
      ...(record.historyChunkHashes ? { historyChunkHashes: record.historyChunkHashes } : {}),
    });
  }
  const chatOrders = new Map(chatRecords.map((record) => [`${record.projectId}\n${record.id}`, record.syncOrder]));
  for (const { project } of projects.values()) {
    project.chats.sort((left, right) =>
      (chatOrders.get(`${project.id}\n${left.id}`) ?? Number.MAX_SAFE_INTEGER)
      - (chatOrders.get(`${project.id}\n${right.id}`) ?? Number.MAX_SAFE_INTEGER));
  }
  return [...projects.values()]
    .sort((left, right) => left.syncOrder - right.syncOrder)
    .map(({ project }) => project);
}

/**
 * Put the selected project/chat first for transfer without changing the
 * desktop-owned syncOrder stored in Firestore.
 */
export function prioritizeCatalogChanges(
  changes: CatalogChanges,
  projects: CatalogProject[],
  selectedProjectId?: string | null,
  selectedChatId?: string | null,
): PrioritizedCatalogChange[] {
  const projectOrder = new Map(projects.map((project, index) => [project.id, index]));
  const records: PrioritizedCatalogChange[] = [
    ...changes.changedProjects.map(({ project, syncOrder }) => ({ kind: "project" as const, project, syncOrder })),
    ...changes.changedChats.map(({ projectId, chat, syncOrder }) => ({ kind: "chat" as const, projectId, chat, syncOrder })),
  ];
  return records.sort((left, right) => {
    const leftProjectId = left.kind === "project" ? left.project.id : left.projectId;
    const rightProjectId = right.kind === "project" ? right.project.id : right.projectId;
    const leftSelectedProject = left.kind === "project" && leftProjectId === selectedProjectId;
    const rightSelectedProject = right.kind === "project" && rightProjectId === selectedProjectId;
    const leftSelectedChat = left.kind === "chat" && leftProjectId === selectedProjectId && left.chat.id === selectedChatId;
    const rightSelectedChat = right.kind === "chat" && rightProjectId === selectedProjectId && right.chat.id === selectedChatId;
    const leftPriority = leftSelectedProject ? 0 : leftSelectedChat ? 1 : 2;
    const rightPriority = rightSelectedProject ? 0 : rightSelectedChat ? 1 : 2;
    if (leftPriority !== rightPriority) return leftPriority - rightPriority;
    const projectDifference = (projectOrder.get(leftProjectId) ?? Number.MAX_SAFE_INTEGER)
      - (projectOrder.get(rightProjectId) ?? Number.MAX_SAFE_INTEGER);
    if (projectDifference !== 0) return projectDifference;
    if (left.kind !== right.kind) return left.kind === "project" ? -1 : 1;
    return left.syncOrder - right.syncOrder;
  });
}

function projectFingerprint(project: CatalogProject, syncOrder: number): string {
  return JSON.stringify({ id: project.id, name: project.name, syncOrder });
}

function chatFingerprint(chat: CatalogChat, syncOrder: number): string {
  return JSON.stringify({
    id: chat.id,
    title: chat.title,
    status: chat.status,
    syncOrder,
    updatedAt: chat.updatedAt ?? null,
    historyRevision: chat.historyRevision ?? "",
    activeRunId: chat.activeRunId ?? null,
    activeRunEventSequence: chat.activeRunEventSequence ?? 0,
  });
}

function previousIndex<T extends { id: string }>(items: T[] | undefined, id: string): number {
  return items?.findIndex((item) => item.id === id) ?? -1;
}
