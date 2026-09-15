import type { RemoteProgressItem } from "./progress-sync.js";

export const TIMELINE_FLUSH_INTERVAL_MS = 5_000;
export const MAX_TIMELINE_ITEMS_PER_DOCUMENT = 40;

export function progressItemKey(item: RemoteProgressItem): string {
  return `${item.firstSequence}:${item.kind}`;
}

export function changedProgressItems(
  previous: RemoteProgressItem[],
  next: RemoteProgressItem[],
): RemoteProgressItem[] {
  const previousByKey = new Map(previous.map((item) => [progressItemKey(item), JSON.stringify(item)]));
  return next.filter((item) => previousByKey.get(progressItemKey(item)) !== JSON.stringify(item));
}

export function timelineItemBatches(items: RemoteProgressItem[]): RemoteProgressItem[][] {
  const ordered = [...items].sort((left, right) => left.firstSequence - right.firstSequence || left.sequence - right.sequence);
  const batches: RemoteProgressItem[][] = [];
  for (let offset = 0; offset < ordered.length; offset += MAX_TIMELINE_ITEMS_PER_DOCUMENT) {
    batches.push(ordered.slice(offset, offset + MAX_TIMELINE_ITEMS_PER_DOCUMENT));
  }
  return batches;
}

export function timelineDocumentId(runId: string, items: RemoteProgressItem[]): string {
  if (!items.length) throw new Error("タイムライン文書に進捗項目がありません。");
  const runKey = Buffer.from(runId, "utf8").toString("base64url");
  const first = Math.min(...items.map((item) => item.firstSequence));
  const last = Math.max(...items.map((item) => item.sequence));
  return `${runKey}_${String(first).padStart(12, "0")}_${String(last).padStart(12, "0")}`;
}
