import assert from "node:assert/strict";
import test from "node:test";
import { parseMarkdown, parseMarkdownInline } from "./markdown.js";

test("最低限のインラインMarkdownを解析する", () => {
  assert.deepEqual(parseMarkdownInline("**太字** *斜体* `code` [link](https://example.com) ![alt](https://example.com/a.png)"), [
    { type: "strong", children: [{ type: "text", value: "太字" }] },
    { type: "text", value: " " },
    { type: "em", children: [{ type: "text", value: "斜体" }] },
    { type: "text", value: " " },
    { type: "code", value: "code" },
    { type: "text", value: " " },
    { type: "link", href: "https://example.com", children: [{ type: "text", value: "link" }] },
    { type: "text", value: " " },
    { type: "image", alt: "alt", src: "https://example.com/a.png" },
  ]);
});

test("コードブロックと表を解析する", () => {
  const blocks = parseMarkdown("```ts\nconst ok = true;\n```\n\n| 名前 | 値 |\n| --- | --- |\n| A | **1** |");
  assert.equal(blocks[0].type, "code");
  assert.equal(blocks[1].type, "table");
  if (blocks[1].type === "table") assert.equal(blocks[1].rows.length, 1);
});

test("レベル1から6の見出しを解析する", () => {
  const blocks = parseMarkdown("# 見出し1\n## **見出し2**\n###### 見出し6\n\n```md\n# コード内\n```");
  assert.deepEqual(blocks.slice(0, 3).map((block) => block.type === "heading" ? block.level : 0), [1, 2, 6]);
  assert.equal(blocks[1].type, "heading");
  if (blocks[1].type === "heading") assert.equal(blocks[1].children[0].type, "strong");
  assert.equal(blocks[3].type, "code");
});

test("順序なしと順序付きの箇条書きを解析する", () => {
  const blocks = parseMarkdown("前文\n- **項目1**\n* 項目2\n\n1. 最初\n2. 次");
  assert.equal(blocks[0].type, "paragraph");
  assert.deepEqual(blocks[1], {
    type: "list",
    ordered: false,
    items: [
      [{ type: "strong", children: [{ type: "text", value: "項目1" }] }],
      [{ type: "text", value: "項目2" }],
    ],
  });
  assert.deepEqual(blocks[2], {
    type: "list",
    ordered: true,
    items: [
      [{ type: "text", value: "最初" }],
      [{ type: "text", value: "次" }],
    ],
  });
});

test("生HTMLはMarkdownとして解釈しない", () => {
  assert.deepEqual(parseMarkdownInline("<script>alert(1)</script>"), [{ type: "text", value: "<script>alert(1)</script>" }]);
});

test("引用と取り消し線をデスクトップ版同様に解析する", () => {
  assert.deepEqual(parseMarkdown("> 方針\n> 続き"), [{
    type: "blockquote",
    children: [{ type: "text", value: "方針\n続き" }],
  }]);
  assert.deepEqual(parseMarkdownInline("~~取り消し `code`~~"), [{
    type: "delete",
    children: [{ type: "text", value: "取り消し " }, { type: "code", value: "code" }],
  }]);
  assert.deepEqual(parseMarkdownInline("[リンク](<https://example.com/a>)"), [{
    type: "link",
    href: "https://example.com/a",
    children: [{ type: "text", value: "リンク" }],
  }]);
  assert.deepEqual(parseMarkdown("前文\n> 引用").map((block) => block.type), ["paragraph", "blockquote"]);
});
