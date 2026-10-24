import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";
import * as sessionState from "../../web/state.mjs";
import { attachmentKind, attachmentURL, formatBytes } from "../../web/attachments.mjs";

const app = await readFile(new URL("../../web/app.mjs", import.meta.url), "utf8");
const application = app.slice(app.indexOf("const ICONS ="), app.indexOf("\nfillIcons();"));

// A small DOM, not a replacement renderer. Layout is deterministic text-sized
// geometry; these tests cover scroll policy, not browser/CSS pixel layout.
class TextNode {
  constructor(text) { this.textContent = String(text); this.parentNode = null; }
}

class DOMElement {
  constructor(tag, document) {
    this.tagName = tag.toUpperCase(); this.document = document; this.parentNode = null; this.childNodes = [];
    this.dataset = {}; this.attributes = {}; this.events = {}; this.className = "";
    this.hidden = false; this.disabled = false; this.open = false; this.tabIndex = -1;
    this._clientHeight = 0; this._scrollTop = 0; this.scrollCalls = []; this.insertions = 0;
    this.classList = {
      contains: (name) => this.className.split(/\s+/).includes(name),
      toggle: (name, force) => {
        const classes = new Set(this.className.split(/\s+/).filter(Boolean));
        const enabled = force ?? !classes.has(name);
        if (enabled) classes.add(name); else classes.delete(name);
        this.className = [...classes].join(" "); return enabled;
      },
      add: (...names) => names.forEach((name) => this.classList.toggle(name, true)),
      remove: (...names) => names.forEach((name) => this.classList.toggle(name, false)),
    };
  }
  get children() { return this.childNodes.filter((node) => node instanceof DOMElement); }
  get firstChild() { return this.childNodes[0] ?? null; }
  get firstElementChild() { return this.children[0] ?? null; }
  get nextSibling() { return this.parentNode?.childNodes[this.parentNode.childNodes.indexOf(this) + 1] ?? null; }
  get textContent() { return this.childNodes.map((node) => node.textContent).join(""); }
  set textContent(text) { this.replaceChildren(new TextNode(text)); }
  set innerHTML(value) { this.iconHTML = value; }
  get clientHeight() {
    if (!this.document.body?.contains(this)) return 0;
    if (this.classList.contains("reasoning-content")) {
      if (!this.closest("details")?.open) return 0;
      return this._clientHeight || 240;
    }
    return this._clientHeight;
  }
  set clientHeight(value) { this._clientHeight = value; }
  get scrollHeight() { return Math.max(this.clientHeight, this.textContent.length); }
  get scrollTop() { return this._scrollTop; }
  set scrollTop(value) { this._scrollTop = Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight)); }
  scrollTo(options) { this.scrollCalls.push(options); this.scrollTop = options.top; }
  contains(node) { return this === node || this.children.some((child) => child.contains(node)); }
  focus() { this.document.activeElement = this; }
  detach(node) {
    const parent = node.parentNode;
    if (!parent) return;
    if (node === this.document.activeElement || node.contains?.(this.document.activeElement)) this.document.activeElement = this.document.body;
    parent.childNodes.splice(parent.childNodes.indexOf(node), 1); node.parentNode = null;
  }
  append(...nodes) {
    for (let node of nodes) {
      if (!(node instanceof DOMElement) && !(node instanceof TextNode)) node = new TextNode(node);
      this.detach(node); node.parentNode = this; this.childNodes.push(node);
    }
  }
  insertBefore(node, reference) {
    if (reference !== null && reference.parentNode !== this) throw new Error("reference is not a child");
    if (node === reference) return node;
    this.insertions += 1; this.detach(node);
    const position = reference === null ? this.childNodes.length : this.childNodes.indexOf(reference);
    node.parentNode = this; this.childNodes.splice(position, 0, node); return node;
  }
  remove() { this.detach(this); }
  replaceChildren(...nodes) {
    for (const node of [...this.childNodes]) this.detach(node);
    this.append(...nodes);
  }
  setAttribute(key, value) {
    if (key.startsWith("data-")) this.dataset[key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = String(value);
    else this.attributes[key] = String(value);
  }
  getAttribute(key) {
    if (key.startsWith("data-")) return this.dataset[key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())];
    return this.attributes[key] ?? this[key];
  }
  addEventListener(name, callback) { (this.events[name] ??= []).push(callback); }
  dispatch(name) {
    return Promise.all((this.events[name] ?? []).map((callback) => callback({ target: this, currentTarget: this, preventDefault() {} })));
  }
  matches(selector) {
    const tag = selector.match(/^[a-z][\w-]*/i)?.[0];
    if (tag && this.tagName !== tag.toUpperCase()) return false;
    const id = selector.match(/#([\w-]+)/)?.[1];
    if (id && this.getAttribute("id") !== id) return false;
    if ([...selector.matchAll(/\.([\w-]+)/g)].some(([, name]) => !this.classList.contains(name))) return false;
    return [...selector.matchAll(/\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]/g)].every(([, key, value]) => value === undefined ? this.getAttribute(key) !== undefined : String(this.getAttribute(key)) === value);
  }
  querySelectorAll(selector) {
    const descendants = this.children.flatMap((node) => [node, ...node.querySelectorAll("*")]);
    return descendants.filter((node) => selector.split(",").some((group) => {
      const parts = group.trim().split(/\s+/); let current = node;
      if (!current.matches(parts.pop())) return false;
      while (parts.length) {
        const part = parts.pop(); current = current.parentNode;
        while (current && !current.matches(part)) current = current.parentNode;
        if (!current) return false;
      }
      return true;
    }));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
  closest(selector) { return this.matches(selector) ? this : this.parentNode?.closest(selector) ?? null; }
}

function harness() {
  const document = {
    activeElement: null, addEventListener() {},
    createTextNode: (text) => new TextNode(text),
    createElement: (tag) => new DOMElement(tag, document),
    createElementNS: (_namespace, tag) => new DOMElement(tag, document),
  };
  document.body = document.createElement("body"); document.activeElement = document.body;
  document.querySelector = (selector) => document.body.querySelector(selector);
  document.querySelectorAll = (selector) => document.body.querySelectorAll(selector);
  document.contains = (node) => document.body.contains(node);
  const node = (tag, id, parent = document.body) => {
    const result = document.createElement(tag); result.setAttribute("id", id); parent.append(result); return result;
  };
  const scroll = node("div", "conversation-scroll"); scroll.clientHeight = 200;
  const messages = node("div", "messages", scroll);
  for (const id of ["welcome", "scroll-bottom", "run-status", "composer", "login-form", "message-input", "mode-select", "file-input", "dialog-form", "app-dialog"]) node("div", id);
  const unexpectedRequest = () => { throw new Error("renderer must not call a service"); };
  const context = {
    ...sessionState, document, console, AbortController, TextEncoder, URLSearchParams,
    Node: class { static [Symbol.hasInstance](value) { return value instanceof DOMElement || value instanceof TextNode; } },
    window: { addEventListener() {} },
    attachmentKind, attachmentURL, formatBytes,
    createTeamPager: () => ({}), captureTeamView: () => null, restoreTeamView() {},
    VoiceInput: class { cancel() {} }, uploadFile: unexpectedRequest, transcribeAttachment: unexpectedRequest,
    apiRequest: unexpectedRequest, readEvents: unexpectedRequest,
    sessionStorage: {}, setTimeout: () => 0, clearTimeout() {},
    renderMarkdown: (text) => { const result = document.createElement("p"); result.textContent = text; return result; },
  };
  runInNewContext(application + `
    renderChrome = () => {}; renderComposer = () => {}; renderApprovals = () => {};
    globalThis.ui = { state, renderChat };
  `, context);
  const ui = context.ui; ui.state.view = "chat"; ui.state.activeSession = { id: "main" };
  let sequence = 0;
  const emit = (type, data) => {
    sequence += 1;
    ui.state.session = sessionState.applyEvent(ui.state.session, { id: String(sequence), type, data, session_id: "main", created_at: sequence });
  };
  const start = () => {
    emit("message.completed", { message: { id: "owner-1", role: "user", principal: "owner", content: "请查看笔记" } });
    emit("run.started", { run_id: "run-1" });
  };
  return { ui, document, scroll, messages, emit, start, render: () => ui.renderChat() };
}

test("the real chat renderer interleaves owner, reasoning, tool and reply in one timeline", () => {
  const h = harness(); h.start();
  h.emit("reasoning.delta", { content: "先读取资料。" });
  h.emit("tool.started", { run_id: "run-1", call_id: "read-1", name: "fs_read", arguments: { path: "notes.txt" } });
  h.emit("tool.completed", { run_id: "run-1", call_id: "read-1", status: "completed", result: { text: "笔记正文" } });
  h.emit("message.delta", { content: "已阅读笔记。" }); h.render();
  assert.deepEqual(h.messages.children.map((node) => node.tagName), ["ARTICLE", "DETAILS", "DETAILS", "ARTICLE"]);
  assert.deepEqual(h.messages.children.map((node) => node.dataset.timelineId), sessionState.conversationItems(h.ui.state.session).map((item) => String(item.id)));
  const [owner, reasoning, tool, reply] = h.messages.children;
  assert.equal(owner.matches("article.message.user"), true); assert.equal(owner.dataset.messageId, "owner-1");
  assert.equal(reasoning.matches("details.reasoning-step"), true); assert.equal(reasoning.open, true);
  const content = reasoning.querySelector(".reasoning-content");
  assert.equal(content.textContent, "先读取资料。"); assert.equal(content.getAttribute("role"), "region"); assert.equal(content.tabIndex, 0);
  assert.equal(tool.matches("details.tool-step.completed"), true); assert.equal(tool.open, false);
  assert.equal(tool.querySelector(".tool-name").textContent, "fs_read");
  assert.ok(tool.querySelector(".tool-step-status").textContent.length > 0);
  assert.ok(tool.querySelector(".tool-arguments").textContent.includes("notes.txt"));
  assert.ok(tool.querySelector(".tool-result").textContent.includes("笔记正文"));
  assert.equal(reply.matches("article.message.assistant"), true); assert.equal(reply.querySelector(".message-body").textContent, "已阅读笔记。");
  assert.equal(reply.querySelector(".message-avatar img").getAttribute("src"), "./assets/muli-avatar.webp");
});

test("tool completion updates the existing details without resetting expansion or summary focus", () => {
  const h = harness(); h.start();
  h.emit("tool.started", { run_id: "run-1", call_id: "read-1", name: "fs_read", arguments: { path: "notes.txt" } }); h.render();
  const tool = h.messages.querySelector(".tool-step"), summary = tool.querySelector("summary");
  assert.equal(tool.open, false); assert.equal(tool.classList.contains("running"), true);
  tool.open = true; summary.focus(); const insertions = h.messages.insertions;
  h.emit("tool.completed", { run_id: "run-1", call_id: "read-1", status: "completed", result: { text: "<script>不执行</script>" } }); h.render();
  assert.equal(h.messages.querySelector(".tool-step") === tool, true);
  assert.equal(tool.querySelector("summary") === summary, true); assert.equal(h.document.activeElement === summary, true);
  assert.equal(tool.open, true); assert.equal(tool.classList.contains("completed"), true); assert.equal(tool.classList.contains("running"), false);
  assert.equal(h.messages.insertions, insertions, "unchanged timeline order must not move focused subtrees");
  assert.ok(tool.querySelector(".tool-result").textContent.includes("<script>不执行</script>"));
  assert.equal(tool.querySelectorAll("script").length, 0);
  tool.open = false; h.render(); assert.equal(tool.open, false, "a later update must also preserve explicit collapse");
});

test("streamed reasoning preserves upward reading and focus, then follows only after returning to its bottom", () => {
  const h = harness(); h.start(); h.emit("reasoning.delta", { content: "甲".repeat(1000) }); h.render();
  const reasoning = h.messages.querySelector(".reasoning-step"), content = reasoning.querySelector(".reasoning-content");
  content.clientHeight = 100; content.scrollTop = 200; content.dispatch("scroll"); content.focus();
  h.emit("reasoning.delta", { content: "乙".repeat(200) }); h.render();
  assert.equal(h.messages.querySelector(".reasoning-content") === content, true);
  assert.equal(content.scrollTop, 200); assert.equal(h.document.activeElement === content, true);
  content.scrollTop = content.scrollHeight; content.dispatch("scroll");
  h.emit("reasoning.delta", { content: "丙".repeat(200) }); h.render();
  assert.equal(content.scrollTop, content.scrollHeight - content.clientHeight);
  reasoning.open = false;
  const collapsedPosition = content.scrollTop;
  assert.equal(content.clientHeight, 0);
  h.emit("reasoning.delta", { content: "丁" }); h.render();
  assert.equal(reasoning.open, false, "streaming must not reopen a user-collapsed thought");
  assert.equal(content.scrollTop, collapsedPosition, "a collapsed thought must not scroll invisibly");
});

test("the first long reasoning delta follows its tail only after the real renderer attaches it", () => {
  const h = harness(); h.start();
  const detached = h.document.createElement("details"); detached.open = true;
  const probe = h.document.createElement("div"); probe.className = "reasoning-content";
  detached.append(probe);
  assert.equal(probe.clientHeight, 0, "detached nodes must not offer fake layout to the renderer");
  h.emit("reasoning.delta", { content: "初次长思考".repeat(200) }); h.render();
  const content = h.messages.querySelector(".reasoning-content");
  assert.ok(content.clientHeight > 0); assert.ok(content.scrollHeight > content.clientHeight);
  assert.equal(content.scrollTop, content.scrollHeight - content.clientHeight);
  h.emit("tool.started", { run_id: "run-1", call_id: "read-1", name: "fs_read", arguments: { path: "notes.txt" } }); h.render();
  assert.equal(h.messages.querySelector(".reasoning-content") === content, true);
  assert.equal(content.scrollTop, content.scrollHeight - content.clientHeight, "no second reasoning delta is needed to reach the tail");
});

test("a completed historical thought starts at its beginning rather than inheriting streaming follow", () => {
  const h = harness(); h.start();
  h.emit("reasoning.delta", { content: "已完成的历史思考".repeat(200) });
  h.emit("run.completed", { run_id: "run-1" }); h.render();
  const reasoning = h.messages.querySelector(".reasoning-step"), content = reasoning.querySelector(".reasoning-content");
  assert.equal(reasoning.open, true); assert.equal(reasoning.classList.contains("streaming"), false);
  assert.ok(content.clientHeight > 0); assert.ok(content.scrollHeight > content.clientHeight);
  assert.equal(content.scrollTop, 0);
});

test("new timeline content leaves the main history reader in place and follows an already-bottom reader", () => {
  const h = harness(); h.start(); h.emit("message.delta", { content: "答".repeat(1200) }); h.render();
  h.scroll.scrollTop = 100; h.scroll.dispatch("scroll"); h.scroll.scrollCalls.length = 0;
  h.emit("message.delta", { content: "续".repeat(300) }); h.render();
  assert.equal(h.scroll.scrollTop, 100); assert.equal(h.scroll.scrollCalls.length, 0);
  h.scroll.scrollTop = h.scroll.scrollHeight; h.scroll.dispatch("scroll");
  h.emit("message.delta", { content: "尾".repeat(300) }); h.render();
  assert.equal(h.scroll.scrollCalls.length, 1);
  assert.equal(h.scroll.scrollTop, h.scroll.scrollHeight - h.scroll.clientHeight);
});

test("the registered mode change handler finishes without the removed timeline renderer", async () => {
  const h = harness(); h.ui.state.activeSession = null;
  const mode = h.document.querySelector("#mode-select");
  assert.equal(mode.events.change.length, 1, "exercise the actual application listener");
  mode.value = "yolo";
  await mode.dispatch("change");
  assert.equal(h.ui.state.mode, "yolo");
});

test("both real HTML entry points omit the removed side timeline and standalone reasoning region", async () => {
  for (const entry of ["index.html", "preview.html"]) {
    const html = await readFile(new URL(`../../web/${entry}`, import.meta.url), "utf8");
    assert.match(html, /\bid=["']messages["']/, `${entry} retains the shared central timeline`);
    assert.doesNotMatch(html, /<aside\b/i, entry);
    assert.doesNotMatch(html, /\bid=["'](?:timeline(?:-[\w-]+)?|reasoning-region)["']/i, entry);
    assert.doesNotMatch(html, /\bdata-action=["'](?:toggle-timeline|toggle-reasoning)["']/i, entry);
    assert.doesNotMatch(html, /\bdata-prompt\b|\bstarter-prompts\b/i, entry);
    assert.match(html, /<link\b[^>]*rel="icon"[^>]*href="\.\/assets\/muli-icon\.png"[^>]*type="image\/png"/, entry);
    assert.match(html, /<img\b[^>]*src="\.\/assets\/muli-avatar\.webp"/, entry);
    assert.match(html, /<img\b[^>]*class="welcome-illustration"[^>]*src="\.\/assets\/muli-empty\.webp"[^>]*width="380"[^>]*height="253"/, entry);
    assert.doesNotMatch(html, /muli-(?:avatar|empty)\.svg/, entry);
  }
});
