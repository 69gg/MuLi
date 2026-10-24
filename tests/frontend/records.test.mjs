import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";
import { ApiError } from "../../web/api.mjs";
import { createSessionState, hydrateSession, applyEvent, messagePresentation, isRunning } from "../../web/state.mjs";
import { attachmentKind, attachmentURL, formatBytes } from "../../web/attachments.mjs";
import { MODEL_ROLES } from "../../web/settings.mjs";

// Exercise the actual records page, global SSE and dialog handlers with native DOM text semantics.
// No browser dependency, service, model, account or real database is involved.
// Compare DOM/slot identity through booleans: failed object diffs traverse the cyclic mock graph.
const app = await readFile(new URL("../../web/app.mjs", import.meta.url), "utf8");
const application = app.slice(app.indexOf("const ICONS ="), app.indexOf("\nfillIcons();"));
const flush = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

class TextNode {
  constructor(text) { this.textContent = String(text); this.parentNode = null; }
}

class DOMElement {
  constructor(tag, document) {
    this.tagName = tag.toUpperCase(); this.document = document; this.childNodes = []; this.parentNode = null;
    this.dataset = {}; this.attributes = {}; this.events = {}; this.className = "";
    this.scrollTop = 0; this._value = ""; this.name = ""; this.type = ""; this.hidden = false; this.disabled = false; this.open = false;
    this.classList = { toggle() {}, add() {}, remove() {} };
  }
  get children() { return this.childNodes.filter((node) => node instanceof DOMElement); }
  get value() {
    if (this.tagName !== "SELECT") return this._value;
    const options = this.children.filter((node) => node.tagName === "OPTION");
    return this.selectionSet ? options.find((option) => option.value === this._value)?.value ?? "" : options[0]?.value ?? "";
  }
  set value(value) { this._value = String(value); this.selectionSet = true; }
  get firstElementChild() { return this.children[0] ?? null; }
  get textContent() { return this.childNodes.map((node) => node.textContent).join(""); }
  set textContent(text) { this.replaceChildren(new TextNode(text)); }
  set innerHTML(value) { this.iconHTML = value; }
  append(...children) {
    for (let child of children) {
      if (!(child instanceof DOMElement) && !(child instanceof TextNode)) child = new TextNode(child);
      child.parentNode = this; this.childNodes.push(child);
    }
  }
  prepend(...children) {
    const previous = this.childNodes; this.childNodes = []; this.append(...children);
    this.childNodes.push(...previous);
  }
  replaceChildren(...children) {
    if (this.children.some((child) => child.contains(this.document.activeElement))) this.document.activeElement = this.document.body;
    for (const child of this.childNodes) child.parentNode = null;
    this.childNodes = []; this.append(...children);
  }
  setAttribute(key, value) {
    if (key.startsWith("data-")) this.dataset[key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = String(value);
    else this.attributes[key] = String(value);
  }
  getAttribute(key) {
    if (key.startsWith("data-")) return this.dataset[key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())];
    return this.attributes[key] ?? this[key];
  }
  addEventListener(name, handler) { (this.events[name] ??= []).push(handler); }
  async dispatch(name) { for (const handler of this.events[name] ?? []) await handler({ target: this, currentTarget: this, preventDefault() {} }); }
  contains(node) { return this === node || this.children.some((child) => child.contains(node)); }
  focus() { this.document.activeElement = this; }
  matches(selector) {
    const excluded = selector.match(/:not\(([^)]+)\)/);
    if (excluded && this.matches(excluded[1])) return false;
    selector = selector.replace(/:not\([^)]+\)/g, "");
    const tag = selector.match(/^[a-z][\w-]*/i)?.[0];
    if (tag && this.tagName !== tag.toUpperCase()) return false;
    const id = selector.match(/#([\w-]+)/)?.[1];
    if (id && this.getAttribute("id") !== id) return false;
    if ([...selector.matchAll(/\.([\w-]+)/g)].some(([, name]) => !this.className.split(/\s+/).includes(name))) return false;
    return [...selector.matchAll(/\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]/g)].every(([, key, value]) => value === undefined ? this.getAttribute(key) !== undefined : this.getAttribute(key) === value);
  }
  querySelectorAll(selector) {
    const all = this.children.flatMap((child) => [child, ...child.querySelectorAll("*")]);
    return all.filter((node) => selector.split(",").some((group) => {
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
  showModal() { this.open = true; }
  close() { if (!this.open) return; this.open = false; queueMicrotask(() => void this.dispatch("close")); }
}

class DOMButton extends DOMElement {}

function harness(handler = async () => { throw new Error("unexpected request"); }) {
  const document = { events: {}, activeElement: null, createTextNode: (text) => new TextNode(text), addEventListener(name, callback) { (this.events[name] ??= []).push(callback); } };
  document.createElement = (tag) => tag === "button" ? new DOMButton(tag, document) : new DOMElement(tag, document);
  document.createElementNS = (_namespace, tag) => document.createElement(tag);
  document.body = document.createElement("body");
  document.querySelector = (selector) => document.body.querySelector(selector);
  document.querySelectorAll = (selector) => document.body.querySelectorAll(selector);
  document.contains = (node) => document.body.contains(node);
  const node = (tag, id, parent = document.body) => { const value = document.createElement(tag); value.setAttribute("id", id); parent.append(value); return value; };
  const dialog = node("dialog", "app-dialog"); node("h2", "dialog-title", dialog);
  const form = node("form", "dialog-form", dialog); node("div", "dialog-content", form); node("p", "dialog-error", form);
  const actions = node("div", "dialog-actions", form); actions.className = "dialog-actions";
  const close = node("button", "dialog-close", actions); close.dataset.action = "close-dialog"; node("button", "dialog-submit", actions);
  const other = node("div", "other-view"); const shell = node("div", "app-shell"); shell.className = "app-shell";
  node("div", "auth-gate"); node("input", "login-password");
  for (const id of ["composer", "login-form", "message-input", "mode-select", "file-input", "conversation-scroll"]) node("div", id);
  const heading = node("div", "other-heading", other); heading.className = "view-heading"; node("h2", "view-title", heading);
  const requests = [], windowEvents = {}, streams = new Map();
  const context = {
    document, Node: class { static [Symbol.hasInstance](value) { return value instanceof DOMElement || value instanceof TextNode; } },
    window: { addEventListener: (name, callback) => { windowEvents[name] = callback; } },
    AbortController, TextEncoder, URLSearchParams, console, queueMicrotask,
    setTimeout: () => 0, clearTimeout() {},
    createSessionState, hydrateSession, applyEvent, messagePresentation, isRunning, attachmentKind, attachmentURL, formatBytes, MODEL_ROLES,
    createTeamPager: () => ({}), captureTeamView: () => null, restoreTeamView() {},
    renderMarkdown: (text) => { const node = document.createElement("p"); node.textContent = text; return node; },
    uploadFile() {}, transcribeAttachment() {}, VoiceInput: class { cancel() {} }, sessionStorage: {},
    formatTimestamp: String, formatFullDate: String,
    apiRequest: async (path, options = {}) => { requests.push({ path, ...options }); return handler(path, options); },
    readEvents: async (path, options) => { streams.set(path, options.onEvent); await new Promise((resolve) => options.signal.addEventListener("abort", resolve, { once: true })); },
  };
  runInNewContext(application + `
    renderChrome = () => {}; renderChat = () => {}; toast = () => {}; resizeComposer = () => {}; resetMessageNodes = () => {};
    refreshNotifications = async () => {}; queueRender = () => {};
    const activateConnection = () => {
      const owner = beginConnection(); owner.ready = true;
      owner.sessions.start(); owner.notifications.start(); return owner;
    };
    activateConnection();
    globalThis.ui = { state, renderOtherView, renderModelSettings, renderSessions, refreshSessions, loadRecordDirectory, releaseRecordDirectory, recordDirectoryPage, showHistoryArchive, loadSession, openDialog, showLogin, switchView, handleAction, startGlobalEvents, startSessionEvents,
      activateConnection, connect,
      get slot() { return recordDirectory; }, get historySlot() { return historyDialog; }, get draftRevision() { return draftRevision; },
      stopEvents() { globalController?.abort(); sessionController?.abort(); } };
  `, context);
  const ui = context.ui; ui.state.view = "chat"; ui.state.auth.authenticated = true;
  ui.state.connected = true; ui.state.mainSessionId = "main"; ui.state.activeSession = { id: "main" };
  const body = () => document.querySelector("#dialog-content").textContent;
  const button = (text, root = other) => root.querySelectorAll("button").find((entry) => entry.textContent === text);
  async function click(value) {
    assert.ok(value instanceof DOMButton, "expected a real button node");
    assert.equal(value.disabled, false, "expected an enabled button");
    value.focus(); await value.dispatch("click");
    for (const callback of document.events.click ?? []) await callback({ target: value, preventDefault() {} });
    await flush();
  }
  return { ui, requests, document, dialog, form, other, body, button, click, windowEvents, node, emit: (path, event) => streams.get(path)(event) };
}

const record = (id = "saved", extra = {}) => ({ id, title: `记录 ${id}`, status: "idle", created_at: 100, updated_at: 200, ...extra });
const page = (items = [record()], extra = {}) => ({ items, next_cursor: null, has_more: false, ...extra });
const params = (path) => new URL(path, "http://fixture.test").searchParams;
const input = (h) => h.other.querySelector("input[type=search]");
const rows = (h) => h.other.querySelectorAll(".record-row");
const position = (h) => h.other.querySelector(".record-page-position");
async function search(h, query) {
  const field = input(h); field.value = query; field.focus(); await field.dispatch("input");
  await h.other.querySelector("form").dispatch("submit"); await flush();
}

test("the full directory has an independent read path and displays persisted titles without copying the recent sessions window", async () => {
  const h = harness(async (path) => { assert.equal(path, "/session-records?query=&limit=50"); return page([record("older-than-200")]); });
  h.ui.state.sessions = [record("recent")];
  await h.ui.switchView("records");
  assert.equal(rows(h).length, 1); assert.ok(h.other.textContent.includes("记录 older-than-200"));
  assert.equal(h.other.textContent.includes("记录 recent"), false);
  assert.equal(h.ui.state.sessions[0].id, "recent");
  assert.equal(h.requests[0].cache, "no-store");
  assert.equal(h.other.querySelectorAll(".record-directory").length, 1);
  assert.ok(h.other.children[0].children.indexOf(h.other.querySelector("nav")) < h.other.children[0].children.indexOf(h.other.querySelector("#record-directory-result")));
  assert.equal(h.button("较新一页").disabled, true); assert.equal(h.button("更早一页").disabled, true);
});

test("search submits exact literal Unicode, whitespace, NUL and percent values; typing alone sends no request", async () => {
  const h = harness(async () => page()); await h.ui.switchView("records");
  const query = "  A%_中\0🙂  ", original = input(h);
  original.value = query; await original.dispatch("input");
  assert.equal(h.requests.length, 1);
  await original.parentNode.parentNode.dispatch("submit"); await flush();
  assert.equal(params(h.requests[1].path).get("query"), query);
  assert.equal(input(h) === original, true); assert.equal(input(h).value, query);
  await h.click(h.button("清空筛选"));
  assert.equal(params(h.requests[2].path).get("query"), ""); assert.equal(input(h).value, "");
});

test("the query limit counts Unicode characters, accepts 120 astral emoji and rejects 121 before HTTP", async () => {
  const h = harness(async () => page()); await h.ui.switchView("records");
  await search(h, "🙂".repeat(120));
  assert.equal(params(h.requests.at(-1).path).get("query"), "🙂".repeat(120));
  const before = h.requests.length; await search(h, "🙂".repeat(121));
  assert.equal(h.requests.length, before); assert.ok(h.other.textContent.includes("最多 120 个字符"));
  assert.equal(h.button("重试") === undefined, true);
  await search(h, "继续"); assert.equal(params(h.requests.at(-1).path).get("query"), "继续");
});

test("opaque cursors remain whole and each explicit page replaces summaries instead of accumulating rows", async () => {
  const first = "opaque+/=一", second = "other+/%二";
  const h = harness(async (path) => {
    const cursor = params(path).get("cursor");
    if (cursor === first) return page([record("middle")], { has_more: true, next_cursor: second });
    if (cursor === second) return page([record("earliest")]);
    return page([record("latest-a"), record("latest-b")], { has_more: true, next_cursor: first });
  });
  await h.ui.switchView("records"); h.other.scrollTop = 7000;
  await h.click(h.button("更早一页"));
  assert.equal(params(h.requests[1].path).get("cursor"), first); assert.equal(h.other.scrollTop, 0);
  assert.equal(rows(h).length, 1); assert.equal(h.other.textContent.includes("latest-a"), false);
  assert.equal(h.document.activeElement === position(h), true);
  await h.click(h.button("更早一页"));
  assert.equal(params(h.requests[2].path).get("cursor"), second); assert.equal(h.button("更早一页").disabled, true);
  await h.click(h.button("较新一页"));
  assert.equal(params(h.requests[3].path).get("cursor"), first); assert.equal(rows(h).length, 1);
  assert.deepEqual(Array.from(h.ui.slot.starts), [null, first]);
});

test("an empty later page falls back only once and never automatically follows a has_more cursor", async () => {
  let reads = 0;
  const h = harness(async () => ++reads === 1 ? page([record()], { has_more: true, next_cursor: "later" }) : page([]));
  await h.ui.switchView("records"); await h.click(h.button("更早一页"));
  assert.equal(reads, 3); assert.equal(rows(h).length, 0); assert.ok(h.other.textContent.includes("还没有运行记录"));
  assert.equal(h.ui.slot.cursor, null); assert.equal(h.button("更早一页").disabled, true);
});

test("empty search results and an empty archive are explicit successful states, not repeated requests", async () => {
  const h = harness(async () => page([])); await h.ui.switchView("records");
  assert.ok(h.other.textContent.includes("还没有运行记录"));
  await search(h, "旧标题"); assert.ok(h.other.textContent.includes("没有找到记录"));
  assert.equal(h.requests.length, 2); assert.equal(h.ui.slot.page.has_more, false);
});

test("malformed pages and cyclic continuation are errors, never fabricated EOF or rounded cursor requests", async () => {
  const invalid = [
    { items: [], has_more: true, next_cursor: "x" }, { items: [record()], has_more: true, next_cursor: 123 },
    { items: [record()], has_more: false, next_cursor: "x" }, { items: [record(), record()], has_more: false, next_cursor: null },
    { items: [record("bad", { id: "" })], has_more: false, next_cursor: null },
    { items: [record("bad", { title: 42 })], has_more: false, next_cursor: null },
    { items: [record("bad", { status: {} })], has_more: false, next_cursor: null },
    { items: Array.from({ length: 51 }, (_, id) => record(String(id))), has_more: false, next_cursor: null },
    { items: [record()] },
  ];
  for (const response of invalid) {
    const h = harness(async () => response); await h.ui.switchView("records");
    assert.equal(h.ui.slot.page === null, true); assert.equal(rows(h).length, 0);
    assert.ok(h.other.querySelector('[role="alert"]')); assert.equal(h.requests.length, 1);
    assert.equal(h.other.textContent.includes("已到最早"), false);
  }
  const h = harness(async (path) => page([record(params(path).get("cursor") || "first")], { has_more: true, next_cursor: "same" }));
  await h.ui.switchView("records"); await h.click(h.button("更早一页"));
  assert.ok(h.other.textContent.includes("翻页位置无效")); assert.equal(h.ui.slot.page === null, true);
});

test("failed page reads clear old rows, retain a visible error and retry the same exact query and cursor", async () => {
  let fail = true;
  const h = harness(async (path) => {
    if (!params(path).has("cursor")) return page([record("first")], { has_more: true, next_cursor: "retry+/=" });
    if (fail) { fail = false; throw new ApiError("fixture directory offline", 503); }
    return page([record("recovered")]);
  });
  await h.ui.switchView("records"); await h.click(h.button("更早一页"));
  assert.equal(rows(h).length, 0); assert.ok(h.other.textContent.includes("fixture directory offline"));
  assert.equal(h.other.textContent.includes("没有找到记录"), false); assert.equal(h.other.textContent.includes("已到最早"), false);
  const failedPath = h.requests.at(-1).path;
  await h.click(h.button("重试")); assert.equal(h.requests.at(-1).path, failedPath);
  assert.equal(rows(h).length, 1); assert.ok(h.other.textContent.includes("recovered"));
});

test("an unsupported service does not silently fall back to the recent 200 records", async () => {
  const h = harness(async () => { throw new ApiError("记录不存在", 404); });
  h.ui.state.sessions = [record("recent")]; await h.ui.switchView("records");
  assert.ok(h.other.textContent.includes("尚不支持完整记录目录"));
  assert.equal(h.requests.length, 1); assert.equal(rows(h).length, 0);
});

test("two searches may finish out of order without replacing the newer page or unsubmitted input", async () => {
  const pending = deferred();
  const h = harness(async (path) => params(path).get("query") === "A" ? pending.promise : page([record(params(path).get("query") || "initial")]));
  await h.ui.switchView("records"); await search(h, "A");
  const old = h.requests.at(-1); await search(h, "B");
  input(h).value = "未提交的 C"; input(h).selectionStart = 2; input(h).selectionEnd = 5; await input(h).dispatch("input");
  pending.resolve(page([record("late-A")])); await flush();
  assert.equal(old.signal.aborted, true); assert.equal(h.ui.slot.query, "B");
  assert.ok(h.other.textContent.includes("记录 B")); assert.equal(h.other.textContent.includes("late-A"), false);
  assert.ok(position(h).textContent.includes("“B”")); assert.equal(position(h).textContent.includes("未提交的 C"), false);
  assert.equal(input(h).value, "未提交的 C"); assert.equal(input(h).selectionStart, 2); assert.equal(input(h).selectionEnd, 5);
  assert.equal(h.document.activeElement === input(h), true);
});

test("leaving and reopening invalidates an old directory even if its request ignores abort", async () => {
  const pending = deferred(); let reads = 0;
  const h = harness(async () => ++reads === 1 ? pending.promise : page([record("reopened")]));
  const first = h.ui.switchView("records"); await flush(); const oldSlot = h.ui.slot;
  await h.ui.switchView("chat");
  assert.equal(oldSlot.page === null, true); assert.equal(oldSlot.controller === null, true); assert.equal(oldSlot.root.textContent, "");
  await h.ui.switchView("records"); pending.resolve(page([record("late-old")])); await first;
  assert.ok(h.other.textContent.includes("reopened")); assert.equal(h.other.textContent.includes("late-old"), false);
  assert.equal(h.ui.slot !== oldSlot, true);
});

test("401 clears the retained directory and late successful reads cannot refill the password gate", async () => {
  const pending = deferred();
  const h = harness(async (path) => {
    const query = params(path).get("query");
    if (query === "pending") return pending.promise;
    if (query === "unauthorized") throw new ApiError("需要登录", 401);
    return page([record("private")]);
  });
  await h.ui.switchView("records"); const oldSlot = h.ui.slot;
  await search(h, "pending"); await search(h, "unauthorized");
  assert.equal(h.ui.slot === null, true); assert.equal(oldSlot.page === null, true); assert.equal(oldSlot.root.textContent, "");
  assert.equal(h.ui.state.recordQuery, ""); assert.equal(h.ui.state.auth.authenticated, false);
  pending.resolve(page([record("late-private")])); await flush();
  assert.equal(oldSlot.root.textContent, ""); assert.equal(h.document.activeElement === h.document.querySelector("#login-password"), true);
});

test("pagehide releases summaries and controllers and forbids late refill", async () => {
  const pending = deferred(); let reads = 0;
  const h = harness(async () => ++reads === 1 ? page([record()], { has_more: true, next_cursor: "later" }) : pending.promise);
  await h.ui.switchView("records"); await h.click(h.button("更早一页")); const oldSlot = h.ui.slot;
  h.windowEvents.pagehide(); pending.resolve(page([record("late")])); await flush();
  assert.equal(h.ui.slot === null, true); assert.equal(oldSlot.page === null, true); assert.equal(oldSlot.root.textContent, "");
  assert.equal(h.requests.at(-1).signal.aborted, true);
});

test("real global session/workflow/run events only mark the directory dirty and preserve keyboard position, page and live drafts", async () => {
  const h = harness(async (path) => path === "/sessions" ? { items: [record("main", { status: "running" })] } : page([record("directory")], { has_more: true, next_cursor: "older" }));
  await h.ui.switchView("records"); const original = input(h), slot = h.ui.slot, savedPage = slot.page;
  original.value = "未提交"; original.selectionStart = 1; original.selectionEnd = 2; original.focus(); await original.dispatch("input");
  h.document.querySelector("#message-input").value = "主草稿"; h.ui.state.attachments = [{ id: "draft-file" }];
  h.ui.startGlobalEvents(); await flush();
  try {
    for (const type of ["run.completed", "session.updated", "workflow.updated"]) h.emit("/events", { id: type, type, data: {} });
    await flush(); await flush(); h.ui.renderOtherView();
    assert.equal(input(h) === original, true); assert.equal(h.document.activeElement === original, true);
    assert.equal(original.value, "未提交"); assert.equal(original.selectionStart, 1); assert.equal(original.selectionEnd, 2);
    assert.equal(slot.page === savedPage, true); assert.equal(slot.dirty, true);
    assert.equal(h.requests.filter((request) => request.path.startsWith("/session-records")).length, 1);
    assert.ok(h.other.textContent.includes("记录有更新"));
    assert.equal(h.document.querySelector("#message-input").value, "主草稿"); assert.equal(h.ui.state.attachments[0].id, "draft-file");
    assert.equal(h.ui.state.activeSession.status, "running");
  } finally { h.ui.stopEvents(); }
});

test("explicit refresh returns to the head using the committed query without overwriting unsubmitted text", async () => {
  const h = harness(async () => page([record()], { has_more: true, next_cursor: "older" }));
  await h.ui.switchView("records"); await search(h, "已提交"); await h.click(h.button("更早一页"));
  input(h).value = "正在输入"; await input(h).dispatch("input"); h.other.scrollTop = 5000;
  await h.click(h.button("刷新列表"));
  assert.equal(params(h.requests.at(-1).path).get("query"), "已提交"); assert.equal(params(h.requests.at(-1).path).has("cursor"), false);
  assert.equal(input(h).value, "正在输入"); assert.equal(h.ui.slot.starts.length, 1); assert.equal(h.other.scrollTop, 0);
});

test("restored authentication reopens an independent directory rather than leaving a cleared records page", async () => {
  let reads = 0;
  const h = harness(async () => page([record(`authenticated-${++reads}`)]));
  await h.ui.switchView("records"); const old = h.ui.slot; h.ui.showLogin();
  h.ui.state.auth.authenticated = true; h.ui.activateConnection(); h.ui.renderSessions(); await flush();
  assert.equal(h.ui.slot !== old, true); assert.equal(h.ui.slot.page.items[0].id, "authenticated-2");
  assert.equal(rows(h).length, 1); assert.equal(h.requests.length, 2); assert.equal(old.root.textContent, "");
});

test("BFCache return restores the released records directory and committed query without changing chat drafts", async () => {
  let reads = 0, positions = 0;
  const h = harness(async (path) => {
    if (path.startsWith("/session-records?")) return page([record(`directory-${++reads}`)]);
    if (path === "/auth/status") return { required: false, authenticated: true };
    if (path === "/ui/event-position") return { after: String(++positions * 20) };
    if (path === "/status") return { mode: "auto" };
    if (path === "/conversation") return { session: { id: "main", status: "idle" }, messages: [], events: [] };
    return { items: [] };
  });
  await h.ui.switchView("records"); await search(h, "保留筛选");
  const old = h.ui.slot; const draft = h.document.querySelector("#message-input");
  draft.value = "尚未发送的主草稿"; h.ui.state.attachments = [{ id: "draft-file" }];
  h.document.visibilityState = "visible"; h.document.querySelector("#auth-gate").hidden = true;
  h.windowEvents.pagehide(); await flush();
  assert.equal(h.ui.slot === null, true); assert.equal(old.root.textContent, "");
  h.windowEvents.pageshow({ persisted: true }); await flush(); await flush();
  assert.equal(h.ui.state.view, "records"); assert.equal(h.ui.slot === null, false);
  assert.equal(h.ui.state.recordQuery, "保留筛选"); assert.equal(input(h).value, "保留筛选");
  assert.equal(rows(h).length, 1); assert.equal(h.ui.slot.page.items[0].id, "directory-3");
  assert.equal(reads, 3); assert.equal(positions, 1); assert.equal(h.ui.state.connected, true);
  assert.equal(draft.value, "尚未发送的主草稿"); assert.equal(h.ui.state.attachments[0].id, "draft-file");
  await search(h, "返回后可操作"); assert.equal(reads, 4);
  h.ui.stopEvents();
});

test("reconnecting with a retained directory only marks its current page dirty", async () => {
  let reads = 0;
  const h = harness(async (path) => {
    if (path.startsWith("/session-records?")) { reads += 1; return page([record("retained")]); }
    if (path === "/auth/status") return { required: false, authenticated: true };
    if (path === "/ui/event-position") return { after: "20" };
    if (path === "/status") return { mode: "auto" };
    if (path === "/conversation") return { session: { id: "main", status: "idle" }, messages: [], events: [] };
    return { items: [] };
  });
  await h.ui.switchView("records"); const slot = h.ui.slot, field = input(h);
  field.value = "未提交的目录输入"; field.focus(); await h.ui.connect();
  assert.equal(h.ui.slot === slot, true); assert.equal(slot.dirty, true); assert.equal(reads, 1);
  assert.equal(input(h) === field, true); assert.equal(field.value, "未提交的目录输入");
  assert.equal(h.document.activeElement === field, true); h.ui.stopEvents();
});

test("nullable metadata, unknown status and invalid dates remain neutral; titles are pure text with no false/null children", async () => {
  const title = '<img src=x onerror="bad()">\0🙂';
  const h = harness(async () => page([
    record("constructor", { title, status: "constructor", created_at: -1, updated_at: "yesterday" }),
    record("null-metadata", { title: null, status: null, created_at: 0, updated_at: 1e300 }),
    { id: "missing-metadata" }, record("empty-title", { title: "", status: "", created_at: null, updated_at: null }),
  ]));
  await h.ui.switchView("records");
  assert.equal(rows(h)[0].querySelector("h3").textContent, title); assert.equal(h.other.querySelectorAll("img").length, 0);
  assert.equal(rows(h)[1].querySelector("h3").textContent, "标题未记录");
  assert.equal(rows(h)[2].querySelector("h3").textContent, "标题未记录");
  assert.equal(rows(h)[3].querySelector("h3").textContent, "空标题");
  assert.equal(h.ui.slot.page.items[1].title, null); assert.equal(h.ui.slot.page.items[2].title, null);
  assert.equal(h.ui.slot.page.items[1].status, null); assert.equal(h.ui.slot.page.items[2].status, null);
  assert.equal(h.ui.slot.page.items[3].title, ""); assert.equal(h.ui.slot.page.items[3].status, "");
  assert.ok(h.other.textContent.includes("状态：constructor")); assert.ok(h.other.textContent.includes("状态未记录"));
  assert.equal(h.other.querySelectorAll("time").every((node) => node.textContent === "日期未记录"), true);
  assert.equal(h.ui.slot.page.items.every((item) => item.created_at === null && item.updated_at === null), true);
  assert.equal(h.other.textContent.includes("已完成"), false); assert.equal(h.other.textContent.includes("false"), false); assert.equal(h.other.textContent.includes("null"), false);
});

test("model token requirements follow the selected protocol without losing edits or requiring an empty secondary role", async () => {
  const h = harness();
  h.ui.state.settingsDraft = { models: { main: { protocol: "openai_chat" }, worker: { protocol: "anthropic" } } };
  h.ui.state.modelRole = "main";
  const main = h.ui.renderModelSettings(); h.other.append(main);
  const token = main.querySelectorAll("input").find((input) => input.name === "models.main.max_output_tokens");
  const protocol = main.querySelector("select");
  assert.equal(token.value, ""); assert.equal(token.required, false);
  assert.equal(token.closest("details") === null, true);
  token.value = "2048"; protocol.value = "anthropic"; await protocol.dispatch("change");
  assert.equal(token.required, true); assert.equal(token.value, "2048");
  protocol.value = "openai_responses"; await protocol.dispatch("change");
  assert.equal(token.required, false);
  h.ui.state.modelRole = "worker";
  const worker = h.ui.renderModelSettings(); h.other.replaceChildren(worker);
  const workerToken = worker.querySelectorAll("input").find((input) => input.name === "models.worker.max_output_tokens");
  const model = worker.querySelectorAll("input").find((input) => input.name === "models.worker.model");
  assert.equal(workerToken.required, false);
  model.value = "configured-model"; await model.dispatch("input");
  assert.equal(workerToken.required, true);
  model.value = ""; await model.dispatch("input");
  assert.equal(workerToken.required, false);
});

test("the main record returns to the only live conversation without opening or creating another session", async () => {
  const h = harness(async () => page([record("main", { title: "主会话真实标题" })]));
  await h.ui.switchView("records"); assert.ok(h.other.textContent.includes("主会话真实标题"));
  assert.equal(h.other.querySelector('[data-action="delete-session"]') === null, true);
  await h.click(h.button("回到对话"));
  assert.equal(h.ui.state.view, "chat"); assert.equal(h.ui.state.activeSession.id, "main"); assert.equal(h.ui.slot === null, true);
  assert.equal(h.requests.length, 1);
});

test("the main record opens its saved archive from settings and returns focus to that entry", async () => {
  const h = harness(async (path) => {
    if (path.startsWith("/session-records")) return page([record("main")]);
    assert.equal(path, "/sessions/main/messages?limit=50");
    return { session_id: "main", messages: [], through_seq: 0, next_before_seq: null, has_older: false };
  });
  await h.ui.switchView("records");
  const trigger = h.button("查看正文归档"); await h.click(trigger);
  assert.equal(h.ui.state.view, "records");
  assert.equal(h.ui.historySlot.sessionId, "main");
  assert.deepEqual(h.requests.map((entry) => entry.path), ["/session-records?query=&limit=50", "/sessions/main/messages?limit=50"]);
  assert.equal(h.requests.every((entry) => !entry.method), true);
  h.dialog.close(); await flush();
  assert.equal(h.document.activeElement === trigger, true);
});

test("old detail and its archive reuse the full session ID and close back to the current record button", async () => {
  const id = "old+/完整";
  const h = harness(async (path) => {
    if (path.startsWith("/session-records")) return page([record(id)]);
    if (path === `/sessions/${encodeURIComponent(id)}`) return { session: { id, title: "旧任务" }, messages: [] };
    throw new Error("unexpected request");
  });
  await h.ui.switchView("records"); const trigger = h.button("查看"); await h.click(trigger);
  assert.equal(h.requests.at(-1).path, `/sessions/${encodeURIComponent(id)}`);
  assert.ok(h.button("查看正文归档", h.dialog));
  h.dialog.close(); await flush();
  assert.equal(h.document.activeElement === trigger, true); assert.equal(h.ui.slot.page.items[0].id, id);
});

test("a page response never takes focus from an open record dialog and closing falls back to the page position", async () => {
  const pending = deferred(); let directoryReads = 0;
  const h = harness(async (path) => {
    if (path.startsWith("/session-records")) return ++directoryReads === 1 ? page([record("first")]) : pending.promise;
    return { session: { id: "first", title: "详情" }, messages: [] };
  });
  await h.ui.switchView("records"); const trigger = h.button("查看");
  await h.click(trigger); const close = h.button("关闭", h.dialog) || h.document.querySelector("#dialog-close"); close.focus();
  const refresh = h.ui.loadRecordDirectory(h.ui.slot, { focus: true });
  pending.resolve(page([record("replacement")])); await refresh;
  assert.equal(h.document.activeElement === close, true);
  h.dialog.close(); await flush(); assert.equal(h.document.activeElement === position(h), true);
});

test("deletion cancellation and rejection retain the record and return to the same full-ID delete button", async () => {
  const id = "old+/完整";
  const h = harness(async (path, options) => {
    if (options.method === "DELETE") { assert.equal(path, `/sessions/${encodeURIComponent(id)}`); throw new ApiError("该记录属于持久队友", 409); }
    return page([record(id)]);
  });
  await h.ui.switchView("records"); const trigger = h.other.querySelector('[data-action="delete-session"]');
  await h.click(trigger); h.dialog.close(); await flush();
  assert.equal(h.requests.length, 1); assert.equal(h.document.activeElement === trigger, true);
  await h.click(trigger); await h.form.dispatch("submit"); await flush();
  assert.equal(h.dialog.open, true); assert.ok(h.document.querySelector("#dialog-error").textContent.includes("持久队友"));
  assert.equal(rows(h).length, 1); assert.equal(h.requests.filter((request) => request.path.startsWith("/session-records")).length, 1);
  h.dialog.close(); await flush(); assert.equal(h.document.activeElement === trigger, true);
});

test("successful deletion rereads only the current directory, preserves another record and closes at a valid keyboard position", async () => {
  let deleted = false;
  const h = harness(async (path, options) => {
    if (options.method === "DELETE") { deleted = true; return { deleted: true }; }
    if (path === "/sessions") return { items: [] };
    return page(deleted ? [record("kept")] : [record("deleted"), record("kept")]);
  });
  await h.ui.switchView("records"); await h.click(h.other.querySelector('[data-action="delete-session"]'));
  await h.form.dispatch("submit"); await flush();
  assert.equal(h.dialog.open, false); assert.equal(rows(h).length, 1); assert.ok(h.other.textContent.includes("记录 kept"));
  assert.equal(h.other.textContent.includes("记录 deleted"), false); assert.equal(h.document.activeElement === position(h), true);
  assert.equal(h.requests.filter((request) => request.path.startsWith("/session-records")).length, 2);
});
