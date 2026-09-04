import { readFile } from "node:fs/promises";
import type { AgentConfig } from "./config.js";
import type { CatalogHistoryItem, CatalogProject } from "./catalog-sync.js";

interface EndpointFile { host: string; port: number; pid: number }
export interface SseEvent { sequence: number; event: string; data: unknown; observedAt?: string }

export class LocalDaemon {
  private baseUrl = "";

  constructor(private readonly config: AgentConfig) {}

  async connect(): Promise<void> {
    const deadline = Date.now() + 180_000;
    while (Date.now() < deadline) {
      const endpoint = await this.readHealthyEndpoint();
      if (endpoint) {
        this.baseUrl = endpoint;
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    throw new Error("デスクトップアプリのdaemonへ接続できませんでした。");
  }

  async catalog(): Promise<{
    projects: CatalogProject[];
    revision: number;
    selectedProjectId?: string | null;
    selectedChatId?: string | null;
  }> {
    return await this.json("/remote/v1/catalog?includeHistory=false", { signal: AbortSignal.timeout(30_000) });
  }

  async chatHistory(projectId: string, chatId: string): Promise<CatalogHistoryItem[]> {
    const result = await this.json<{ history: CatalogHistoryItem[] }>(
      `/remote/v1/projects/${encodeURIComponent(projectId)}/chats/${encodeURIComponent(chatId)}/history`,
      { signal: AbortSignal.timeout(120_000) },
    );
    return Array.isArray(result.history) ? result.history : [];
  }

  async waitCatalogRevision(afterRevision: number): Promise<number> {
    const result = await this.json<{ revision: number }>(
      `/remote/v1/catalog-revision/wait?after=${encodeURIComponent(String(afterRevision))}`,
      { signal: AbortSignal.timeout(40_000) },
    );
    return Number.isInteger(result.revision) ? result.revision : afterRevision;
  }

  async execute(taskId: string, operation: string, payload: unknown): Promise<Record<string, unknown>> {
    return await this.json(`/remote/v1/tasks/${encodeURIComponent(taskId)}/execute`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ operation, payload }),
    });
  }

  async runtime(projectId: string, chatId?: string): Promise<Record<string, unknown>> {
    const settingsPath = chatId
      ? `/projects/${encodeURIComponent(projectId)}/chats/${encodeURIComponent(chatId)}/settings`
      : "/settings";
    const [settings, models] = await Promise.all([
      this.json<Record<string, unknown>>(settingsPath),
      this.json<Record<string, unknown>>("/models"),
    ]);
    try {
      const usage = await this.json<Record<string, unknown>>("/usage/capacity", { signal: AbortSignal.timeout(15_000) });
      return { settings, models, usage };
    } catch (error) {
      return { settings, models, usage: null, usageError: error instanceof Error ? error.message : String(error) };
    }
  }

  async updateRuntime(
    projectId: string,
    chatId: string | undefined,
    model: string,
    reasoningEffort: string,
    permissionProfile: string,
    approvalPolicy: string,
    approvalsReviewer: string,
  ): Promise<Record<string, unknown>> {
    const settingsPath = chatId
      ? `/projects/${encodeURIComponent(projectId)}/chats/${encodeURIComponent(chatId)}/settings`
      : "/settings";
    return await this.json(settingsPath, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ model, reasoningEffort, permissionProfile, approvalPolicy, approvalsReviewer }),
    });
  }

  async *events(runId: string, afterSequence?: number): AsyncGenerator<SseEvent> {
    const response = await fetch(`${this.baseUrl}${runEventsPath(runId, afterSequence)}`);
    if (!response.ok || !response.body) throw new Error(`イベント接続に失敗しました（HTTP ${response.status}）。`);
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      buffer += value || "";
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const parsed = parseSseBlock(block);
        if (parsed) yield parsed;
        boundary = buffer.indexOf("\n\n");
      }
      if (done) break;
    }
  }

  private async readHealthyEndpoint(): Promise<string | undefined> {
    try {
      const endpoint = JSON.parse(await readFile(this.config.endpointFile, "utf8")) as EndpointFile;
      if (endpoint.host !== "127.0.0.1" || !Number.isInteger(endpoint.port)) return undefined;
      const baseUrl = `http://127.0.0.1:${endpoint.port}`;
      const response = await fetch(`${baseUrl}/health`, { signal: AbortSignal.timeout(1000) });
      return response.ok ? baseUrl : undefined;
    } catch {
      return undefined;
    }
  }

  private async json<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, init);
    const text = await response.text();
    if (!response.ok) throw new Error(`daemon HTTP ${response.status}: ${text.slice(0, 1000)}`);
    return JSON.parse(text) as T;
  }
}

export function runEventsPath(runId: string, afterSequence?: number): string {
  const base = `/remote/v1/runs/${encodeURIComponent(runId)}/events`;
  return Number.isInteger(afterSequence) && Number(afterSequence) >= 0
    ? `${base}?after=${encodeURIComponent(String(afterSequence))}`
    : base;
}

function parseSseBlock(block: string): SseEvent | undefined {
  let sequence = 0;
  let event = "message";
  let data = "";
  for (const line of block.split("\n")) {
    if (line.startsWith("id: ")) sequence = Number(line.slice(4)) || 0;
    else if (line.startsWith("event: ")) event = line.slice(7);
    else if (line.startsWith("data: ")) data += line.slice(6);
  }
  if (!data) return undefined;
  const observedAt = new Date().toISOString();
  try { return { sequence, event, data: JSON.parse(data), observedAt }; }
  catch { return { sequence, event, data, observedAt }; }
}
