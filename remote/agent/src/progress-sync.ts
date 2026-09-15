import type { SseEvent } from "./daemon.js";

export interface RemoteProgressItem {
  firstSequence: number;
  sequence: number;
  kind: "reasoning" | "work";
  method: string;
  text: string;
  createdAt: string;
  details?: string;
}

const MAX_ITEMS = 24;
const MAX_TEXT = 2_000;
const MAX_DETAILS = 4_000;

export function appendProgressItem(items: RemoteProgressItem[], event: SseEvent): RemoteProgressItem[] {
  const data = isObject(event.data) ? event.data : {};
  const method = stringField(data, "method");
  const rawDetails = stringField(data, "details");
  const parsedDetails = objectJson(rawDetails);
  const item = parsedDetails && isObject(parsedDetails.item) ? parsedDetails.item : undefined;
  const itemType = item ? stringField(item, "type") : "";
  if (["agentMessage", "userMessage", "contextCompaction"].includes(itemType)
    || method.startsWith("item/agentMessage")) return items;
  const delta = isDelta(method);
  if (itemType === "reasoning" && !delta) return items;
  const kind = method.startsWith("item/reasoning/") || itemType === "reasoning" ? "reasoning" : "work";
  const text = firstText(data, ["text", "message", "summary", "method"]).slice(0, MAX_TEXT);
  const details = (delta ? progressDetails(rawDetails) : "").slice(0, MAX_DETAILS);
  if (!text && !details) return items;

  const next = [...items];
  const previous = next[next.length - 1];
  if (isDelta(method) && previous?.method === method && previous.kind === kind) {
    next[next.length - 1] = {
      ...previous,
      sequence: event.sequence,
      text: text || previous.text,
      createdAt: previous.createdAt || event.observedAt || new Date().toISOString(),
      details: `${previous.details || ""}${details || text}`.slice(-MAX_DETAILS),
    };
  } else {
    next.push({
      firstSequence: event.sequence,
      sequence: event.sequence,
      kind,
      method: method.slice(0, 300),
      text,
      createdAt: event.observedAt || new Date().toISOString(),
      ...(details ? { details } : {}),
    });
  }
  return next.slice(-MAX_ITEMS);
}

export function isCountableProgressEvent(event: SseEvent): boolean {
  if (event.event !== "progress" || !isObject(event.data)) return false;
  return !isDelta(stringField(event.data, "method"));
}

function objectJson(value: string): Record<string, unknown> | undefined {
  if (!value) return undefined;
  try {
    const parsed = JSON.parse(value) as unknown;
    return isObject(parsed) ? parsed : undefined;
  } catch {
    return undefined;
  }
}

function progressDetails(value: string): string {
  if (!value) return "";
  try {
    const parsed = JSON.parse(value) as unknown;
    if (isObject(parsed) && typeof parsed.delta === "string") return parsed.delta;
  } catch {
    // Some daemon events intentionally carry plain text details.
  }
  return value;
}

function isDelta(method: string): boolean {
  return method.toLowerCase().includes("delta");
}

function firstText(value: Record<string, unknown>, fields: string[]): string {
  for (const field of fields) {
    const result = stringField(value, field);
    if (result) return result;
  }
  return "";
}

function stringField(value: Record<string, unknown>, field: string): string {
  return typeof value[field] === "string" ? value[field] : "";
}

function isObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
