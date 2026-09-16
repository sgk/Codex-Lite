import type { SseEvent } from "./daemon.js";

export function isCountableProgressEvent(event: SseEvent): boolean {
  if (event.event !== "progress" || !isObject(event.data)) return false;
  const method = stringField(event.data, "method");
  if (isDelta(method) || method.startsWith("item/reasoning/") || method.startsWith("item/agentMessage/")) return false;
  const parsedDetails = objectJson(stringField(event.data, "details"));
  const item = parsedDetails && isObject(parsedDetails.item) ? parsedDetails.item : undefined;
  const itemType = item ? stringField(item, "type") : "";
  if (["agentMessage", "userMessage", "reasoning", "contextCompaction"].includes(itemType)) return false;
  return method === "item/completed" || method.endsWith("/completed") || method.endsWith("_end");
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

function isDelta(method: string): boolean {
  return method.toLowerCase().includes("delta");
}

function stringField(value: Record<string, unknown>, field: string): string {
  return typeof value[field] === "string" ? value[field] : "";
}

function isObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
