import { createHash } from "node:crypto";
import type { CatalogHistoryItem } from "./catalog-sync.js";

export interface HistoryChunk {
  id: string;
  index: number;
  hash: string;
  payload: string;
}

const MAX_CHUNK_BYTES = 400_000;
const MAX_CHUNK_ITEMS = 160;

export function historyChunks(chatId: string, entries: CatalogHistoryItem[]): HistoryChunk[] {
  if (entries.length === 0) return [];
  const prefix = createHash("sha256").update(chatId).digest("hex").slice(0, 24);
  const payloads: string[] = [];
  let currentParts: string[] = [];
  let currentBytes = 0;
  let currentItems = 0;

  const flush = (): void => {
    if (currentBytes === 0) return;
    payloads.push(currentParts.join(""));
    currentParts = [];
    currentBytes = 0;
    currentItems = 0;
  };
  const append = (value: string): void => {
    let remaining = Buffer.from(value, "utf8");
    while (remaining.length > 0) {
      if (currentBytes >= MAX_CHUNK_BYTES) flush();
      const capacity = MAX_CHUNK_BYTES - currentBytes;
      let take = Math.min(capacity, remaining.length);
      if (take < remaining.length) {
        while (take > 0 && (remaining[take] & 0xc0) === 0x80) take -= 1;
      }
      if (take === 0) {
        flush();
        continue;
      }
      const part = remaining.subarray(0, take).toString("utf8");
      currentParts.push(part);
      currentBytes += take;
      remaining = remaining.subarray(take);
    }
  };

  append("[");
  entries.forEach((entry, index) => {
    if (currentItems >= MAX_CHUNK_ITEMS) flush();
    append(index === 0 ? "" : ",");
    append(JSON.stringify(entry));
    currentItems += 1;
  });
  append("]");
  flush();

  return payloads.map((payload, index) => ({
    id: `${prefix}-${String(index).padStart(6, "0")}`,
    index,
    hash: createHash("sha256").update(payload).digest("hex"),
    payload,
  }));
}
