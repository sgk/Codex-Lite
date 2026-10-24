// Stable identity of the newest completed, visible assistant message.
// Changes to text, run metadata, or older history must not create unread mail.
export function latestMessageVersion(history: Array<{ id: string; role: string; kind: string; content: string; createdAt: string }>): string {
  let latest = "";
  for (const item of history) {
    if (item.role !== "assistant" || !["work", "conclusion"].includes(item.kind) || !item.content.trim()) continue;
    const time = Date.parse(item.createdAt);
    if (!Number.isFinite(time)) throw new Error("未読判定用メッセージの時刻が不正です。");
    const version = `${new Date(time).toISOString()}\n${item.id}`;
    if (version > latest) latest = version;
  }
  return latest;
}

export function messageReadState(previous: { version: string; unread: boolean } | undefined, version: string, visible: boolean): { version: string; unread: boolean } {
  if (!previous) return { version, unread: false };
  const newMessage = version > previous.version && version.split("\n")[1] !== previous.version.split("\n")[1];
  return {
    version: version > previous.version ? version : previous.version,
    unread: !visible && (previous.unread || newMessage),
  };
}
