export type MarkdownInline =
  | { type: "text"; value: string }
  | { type: "strong" | "em"; children: MarkdownInline[] }
  | { type: "code"; value: string }
  | { type: "link"; href: string; children: MarkdownInline[] }
  | { type: "image"; src: string; alt: string };

export type MarkdownBlock =
  | { type: "paragraph"; children: MarkdownInline[] }
  | { type: "heading"; level: number; children: MarkdownInline[] }
  | { type: "list"; ordered: boolean; items: MarkdownInline[][] }
  | { type: "code"; language: string; value: string }
  | { type: "table"; header: MarkdownInline[][]; rows: MarkdownInline[][][] };

export function parseMarkdown(source: string): MarkdownBlock[] {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const blocks: MarkdownBlock[] = [];
  for (let index = 0; index < lines.length;) {
    if (!lines[index].trim()) { index += 1; continue; }
    const heading = lines[index].match(/^\s*(#{1,6})\s+(.+?)(?:\s+#+)?\s*$/);
    if (heading) {
      blocks.push({ type: "heading", level: heading[1].length, children: parseMarkdownInline(heading[2]) });
      index += 1;
      continue;
    }
    const fence = lines[index].match(/^\s*```([^`]*)$/);
    if (fence) {
      const body: string[] = [];
      index += 1;
      while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) body.push(lines[index++]);
      if (index < lines.length) index += 1;
      blocks.push({ type: "code", language: fence[1].trim(), value: body.join("\n") });
      continue;
    }
    if (index + 1 < lines.length && isTableSeparator(lines[index + 1])) {
      const header = splitTableRow(lines[index]).map(parseMarkdownInline);
      const rows: MarkdownInline[][][] = [];
      index += 2;
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
        rows.push(splitTableRow(lines[index]).map(parseMarkdownInline));
        index += 1;
      }
      blocks.push({ type: "table", header, rows });
      continue;
    }
    const listItem = parseListItem(lines[index]);
    if (listItem) {
      const items: MarkdownInline[][] = [];
      const ordered = listItem.ordered;
      while (index < lines.length) {
        const item = parseListItem(lines[index]);
        if (!item || item.ordered !== ordered) break;
        items.push(parseMarkdownInline(item.content));
        index += 1;
      }
      blocks.push({ type: "list", ordered, items });
      continue;
    }
    const paragraph: string[] = [];
    while (index < lines.length && lines[index].trim()) {
      if (paragraph.length && (/^\s*(?:```|#{1,6}\s+)/.test(lines[index]) || parseListItem(lines[index]) || (index + 1 < lines.length && isTableSeparator(lines[index + 1])))) break;
      paragraph.push(lines[index++]);
    }
    blocks.push({ type: "paragraph", children: parseMarkdownInline(paragraph.join("\n")) });
  }
  return blocks;
}

export function parseMarkdownInline(source: string): MarkdownInline[] {
  const nodes: MarkdownInline[] = [];
  let plain = "";
  const flush = () => { if (plain) { nodes.push({ type: "text", value: plain }); plain = ""; } };
  for (let index = 0; index < source.length;) {
    if (source.startsWith("![", index)) {
      const close = source.indexOf("](", index + 2); const end = close >= 0 ? source.indexOf(")", close + 2) : -1;
      if (close >= 0 && end >= 0) {
        flush(); nodes.push({ type: "image", alt: source.slice(index + 2, close), src: source.slice(close + 2, end).trim() }); index = end + 1; continue;
      }
    }
    if (source[index] === "[") {
      const close = source.indexOf("](", index + 1); const end = close >= 0 ? source.indexOf(")", close + 2) : -1;
      if (close >= 0 && end >= 0) {
        flush(); nodes.push({ type: "link", href: source.slice(close + 2, end).trim(), children: parseMarkdownInline(source.slice(index + 1, close)) }); index = end + 1; continue;
      }
    }
    if (source.startsWith("**", index)) {
      const end = source.indexOf("**", index + 2);
      if (end >= 0) { flush(); nodes.push({ type: "strong", children: parseMarkdownInline(source.slice(index + 2, end)) }); index = end + 2; continue; }
    }
    if (source[index] === "`") {
      const end = source.indexOf("`", index + 1);
      if (end >= 0) { flush(); nodes.push({ type: "code", value: source.slice(index + 1, end) }); index = end + 1; continue; }
    }
    if (source[index] === "*") {
      const end = source.indexOf("*", index + 1);
      if (end >= 0) { flush(); nodes.push({ type: "em", children: parseMarkdownInline(source.slice(index + 1, end)) }); index = end + 1; continue; }
    }
    plain += source[index++];
  }
  flush();
  return nodes;
}

export function renderMarkdown(container: HTMLElement, source: string): void {
  container.classList.add("markdown");
  for (const block of parseMarkdown(source)) {
    if (block.type === "paragraph") {
      const paragraph = document.createElement("p"); appendInline(paragraph, block.children); container.append(paragraph);
    } else if (block.type === "heading") {
      const heading = document.createElement(`h${block.level}`);
      appendInline(heading, block.children); container.append(heading);
    } else if (block.type === "code") {
      const pre = document.createElement("pre"); const code = document.createElement("code");
      if (block.language) code.dataset.language = block.language;
      code.textContent = block.value; pre.append(code); container.append(pre);
    } else if (block.type === "list") {
      const list = document.createElement(block.ordered ? "ol" : "ul");
      for (const item of block.items) { const listItem = document.createElement("li"); appendInline(listItem, item); list.append(listItem); }
      container.append(list);
    } else {
      const wrapper = document.createElement("div"); wrapper.className = "markdown-table-wrap";
      const table = document.createElement("table"); const head = document.createElement("thead"); const headRow = document.createElement("tr");
      for (const cell of block.header) { const th = document.createElement("th"); appendInline(th, cell); headRow.append(th); }
      head.append(headRow); table.append(head);
      const body = document.createElement("tbody");
      for (const row of block.rows) { const tr = document.createElement("tr"); for (const cell of row) { const td = document.createElement("td"); appendInline(td, cell); tr.append(td); } body.append(tr); }
      table.append(body); wrapper.append(table); container.append(wrapper);
    }
  }
}

function parseListItem(line: string): { ordered: boolean; content: string } | undefined {
  const unordered = line.match(/^\s*[-+*]\s+(.+)$/);
  if (unordered) return { ordered: false, content: unordered[1] };
  const ordered = line.match(/^\s*\d+\.\s+(.+)$/);
  return ordered ? { ordered: true, content: ordered[1] } : undefined;
}

function appendInline(parent: HTMLElement, nodes: MarkdownInline[]): void {
  for (const node of nodes) {
    if (node.type === "text") { parent.append(document.createTextNode(node.value)); continue; }
    if (node.type === "code") { const code = document.createElement("code"); code.textContent = node.value; parent.append(code); continue; }
    if (node.type === "image") {
      const src = safeHttpUrl(node.src); if (!src) { parent.append(document.createTextNode(`![${node.alt}](${node.src})`)); continue; }
      const image = document.createElement("img"); image.src = src; image.alt = node.alt; image.loading = "lazy"; image.referrerPolicy = "no-referrer"; parent.append(image); continue;
    }
    const element = document.createElement(node.type === "strong" ? "strong" : node.type === "em" ? "em" : "a");
    if (node.type === "link") {
      const href = safeHttpUrl(node.href); if (!href) { appendInline(parent, node.children); continue; }
      element.setAttribute("href", href); element.setAttribute("target", "_blank"); element.setAttribute("rel", "noopener noreferrer");
    }
    appendInline(element, node.children); parent.append(element);
  }
}

function safeHttpUrl(value: string): string | undefined {
  try {
    const url = new URL(value, document.baseURI);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : undefined;
  } catch { return undefined; }
}

function isTableSeparator(line: string): boolean {
  const cells = splitTableRow(line);
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function splitTableRow(line: string): string[] {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}
