import test from "node:test";
import assert from "node:assert/strict";
import { DOMElement, TextNode } from "./helpers/conversation-dom-harness.mjs";

const document = {
  createElement: (tag) => new DOMElement(tag, document),
  createTextNode: (text) => new TextNode(text),
  createDocumentFragment: () => new DOMElement("#fragment", document),
};
globalThis.document = document;
const { renderMarkdown } = await import("../../web/markdown.mjs");

function render(text) {
  const root = document.createElement("div");
  root.append(renderMarkdown(text));
  return root;
}

test("GFM tables render header, body and column alignment", () => {
  const root = render("| 名称 | 数量 |\n| :--- | ---: |\n| 苹果 | 3 |\n| 香蕉 | 12 |");
  const table = root.querySelector(".markdown-table");
  assert.ok(table, "a table must be rendered");
  assert.deepEqual(table.querySelectorAll("th").map((cell) => cell.textContent), ["名称", "数量"]);
  assert.deepEqual(table.querySelectorAll("td").map((cell) => cell.textContent), ["苹果", "3", "香蕉", "12"]);
  assert.deepEqual(table.querySelectorAll("th").map((cell) => cell.style.textAlign), ["left", "right"]);
  assert.deepEqual(table.querySelectorAll("td").map((cell) => cell.style.textAlign), ["left", "right", "left", "right"]);
  assert.ok(root.querySelector(".markdown-table-wrap"), "the table must be horizontally scrollable");
});

test("table cells keep inline formatting and escaped pipes", () => {
  const root = render("| 命令 | 说明 |\n| --- | --- |\n| `a \\| b` | **重点** |");
  const cells = root.querySelectorAll("td");
  assert.equal(cells[0].textContent, "a | b");
  assert.equal(cells[0].querySelector("code").textContent, "a | b");
  assert.equal(cells[1].querySelector("strong").textContent, "重点");
});

test("fenced code blocks keep their body verbatim and expose the language", () => {
  const root = render("```js\nconst a = 1;\nconsole.log(a);\n```");
  const pre = root.querySelector("pre");
  assert.equal(pre.dataset.language, "js");
  assert.equal(pre.querySelector("code").textContent, "const a = 1;\nconsole.log(a);\n");
  assert.equal(root.querySelectorAll("strong").length, 0, "code content must not be parsed as markdown");
});

test("an unterminated streamed code block still shows a code frame", () => {
  const root = render("```python\nprint('hi')");
  assert.ok(root.querySelector("pre"));
  assert.equal(root.querySelector("code").textContent, "print('hi')\n");
});

test("tilde fences and language-less fences render as code blocks", () => {
  const root = render("~~~\nplain\n~~~\n\n```\nother\n```");
  assert.equal(root.querySelectorAll("pre").length, 2);
  assert.equal(root.querySelectorAll("pre code")[0].textContent, "plain\n");
  assert.equal(root.querySelectorAll("pre")[1].dataset.language, undefined);
});

test("consecutive lines join one paragraph and blank lines split paragraphs", () => {
  const root = render("第一行\n第二行\n\n新段落");
  assert.equal(root.querySelectorAll("p").length, 2);
  assert.equal(root.querySelectorAll("br").length, 1);
  assert.equal(root.querySelectorAll("p")[0].textContent, "第一行第二行");
});

test("nested and ordered lists keep their hierarchy", () => {
  const root = render("- 一\n  - 一甲\n  - 一乙\n- 二\n\n3. 第三\n4. 第四");
  const lists = root.querySelectorAll("ul");
  assert.equal(lists.length, 2);
  assert.equal(lists[0].children.length, 2);
  assert.equal(lists[0].children[0].querySelector("ul").children.length, 2);
  const ordered = root.querySelector("ol");
  assert.equal(ordered.start, 3);
  assert.deepEqual(ordered.querySelectorAll("li").map((item) => item.textContent), ["第三", "第四"]);
});

test("inline code, emphasis, strikethrough and safe links render", () => {
  const root = render("**粗** *斜* ~~删~~ `码` [正常](https://example.com)");
  assert.equal(root.querySelector("strong").textContent, "粗");
  assert.equal(root.querySelector("em").textContent, "斜");
  assert.equal(root.querySelector("del").textContent, "删");
  assert.equal(root.querySelector("code").textContent, "码");
  const links = root.querySelectorAll("a");
  assert.equal(links.length, 1);
  assert.equal(links[0].getAttribute("href"), "https://example.com/");
  assert.equal(links[0].getAttribute("target"), "_blank");
});

test("unsafe link protocols degrade to plain text", () => {
  const root = render("[危险](javascript:alert(1))");
  assert.equal(root.querySelectorAll("a").length, 0);
  assert.equal(root.querySelector("span").textContent, "危险");
});

test("table-like text inside a code block stays code", () => {
  const root = render("```\n| a | b |\n| --- | --- |\n```");
  assert.equal(root.querySelector("table"), null);
  assert.ok(root.querySelector("pre").textContent.includes("| a | b |"));
});

test("blockquotes render their nested blocks", () => {
  const root = render("> ```js\n> const x = 1;\n> ```");
  const quote = root.querySelector("blockquote");
  assert.ok(quote);
  assert.equal(quote.querySelector("pre code").textContent, "const x = 1;\n");
});

test("horizontal rules and headings keep their structure", () => {
  const root = render("# 标题\n\n---\n\n### 小节");
  assert.equal(root.querySelector("h2").textContent, "标题");
  assert.equal(root.querySelector("h4").textContent, "小节");
  assert.ok(root.querySelector("hr"));
});
