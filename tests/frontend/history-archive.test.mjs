import test from "node:test";
import { composerIdentity, createComposerDraft, MAX_DRAFT_ATTACHMENTS, sameComposerIdentity, validDraftAttachment } from "../../web/composer-draft.mjs";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";
import { ApiError } from "../../web/api.mjs";
import { VoiceSetup, voiceSetupLabel } from "../../web/voice-setup.mjs";
import { createSessionState, applyEvent, messagePresentation, isRunning } from "../../web/state.mjs";
import { attachmentKind, attachmentURL, formatBytes } from "../../web/attachments.mjs";

// Exercise actual app functions, dialog/SSE handlers and native text conversion.
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
    this._value = ""; this.name = ""; this.type = ""; this.hidden = false; this.disabled = false; this.open = false;
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
  for (const id of ["composer", "login-form", "message-input", "mode-select", "file-input", "conversation-scroll", "composer-draft-state", "composer-draft-status", "composer-draft-recheck", "composer-draft-discard", "composer-prompt-undo"]) node("div", id);
  const heading = node("div", "other-heading", other); heading.className = "view-heading"; node("h2", "view-title", heading);
  const requests = [], windowEvents = {}, streams = new Map();
  const context = {
    composerIdentity, createComposerDraft, MAX_DRAFT_ATTACHMENTS, sameComposerIdentity, validDraftAttachment,
    document, Node: class { static [Symbol.hasInstance](value) { return value instanceof DOMElement || value instanceof TextNode; } },
    window: { addEventListener: (name, callback) => { windowEvents[name] = callback; } },
    AbortController, TextEncoder, URLSearchParams, console, queueMicrotask,
    setTimeout: () => 0, clearTimeout() {},
    createSessionState, applyEvent, messagePresentation, isRunning, attachmentKind, attachmentURL, formatBytes,
    createTeamPager: () => ({}), captureTeamView: () => null, restoreTeamView() {},
    renderMarkdown: (text) => { const node = document.createElement("p"); node.textContent = text; return node; },
    uploadFile() {}, transcribeAttachment() {}, VoiceInput: class { cancel() {} }, VoiceSetup, voiceSetupLabel, sessionStorage: {},
    formatTimestamp: String, formatFullDate: String,
    apiRequest: async (path, options = {}) => { requests.push({ path, ...options }); return handler(path, options); },
    readEvents: async (path, options) => { streams.set(path, options.onEvent); await new Promise((resolve) => options.signal.addEventListener("abort", resolve, { once: true })); },
  };
  runInNewContext(application + `
    renderChrome = () => {}; renderChat = () => {}; renderOtherView = () => {}; toast = () => {}; resizeComposer = () => {};
    refreshSessions = async () => {}; refreshNotifications = async () => {}; queueRender = () => {};
    const owner = beginConnection(); owner.ready = true;
    owner.sessions.start(); owner.notifications.start();
    globalThis.ui = { state, showHistoryArchive, loadSession, openDialog, showLogin, switchView, handleAction, startGlobalEvents, startSessionEvents,
      get slot() { return historyDialog; }, get draftRevision() { return draftRevision; },
      stopEvents() { globalController?.abort(); sessionController?.abort(); } };
  `, context);
  const ui = context.ui; ui.state.view = "chat"; ui.state.auth.authenticated = true;
  ui.state.connected = true; ui.state.mainSessionId = "main"; ui.state.activeSession = { id: "main" };
  const body = () => document.querySelector("#dialog-content").textContent;
  const button = (text, root = dialog) => root.querySelectorAll("button").find((entry) => entry.textContent === text);
  async function click(value) {
    assert.ok(value instanceof DOMButton, "expected a real button node");
    assert.equal(value.disabled, false, "expected an enabled button");
    value.focus(); await value.dispatch("click");
    for (const callback of document.events.click ?? []) await callback({ target: value, preventDefault() {} });
    await flush();
  }
  return { ui, requests, document, dialog, form, other, body, button, click, windowEvents, node, emit: (path, event) => streams.get(path)(event) };
}

const message = (seq = 1, extra = {}) => ({ id: `message-${seq}`, seq, revision: `revision-${seq}`, role: "user", content: `saved-${seq}`, content_bytes: Buffer.byteLength(`saved-${seq}`), content_truncated: false, content_provenance: "accepted", ...extra });
const directory = (messages = [message()], extra = {}) => ({ session_id: "main", messages, through_seq: messages.at(-1)?.seq ?? 0, next_before_seq: null, has_older: false, ...extra });
function contentPage(item, content = item.content, offset = 0, total = item.content_bytes, extra = {}) {
  const length = Buffer.byteLength(content);
  return { session_id: "main", message_id: item.id, seq: item.seq, revision: item.revision, role: item.role, content_provenance: item.content_provenance, field: "content", content, total_bytes: total, offset_bytes: offset, length_bytes: length, next_offset_bytes: offset + length, has_more: offset + length < total, ...extra };
}

test("the main archive starts at latest saved records and does not derive a cursor from pending ingress", async () => {
  const h = harness(async () => directory());
  h.ui.state.session.messages = [{ id: "pending", seq: 9000, role: "user", delivery_status: "pending" }];
  await h.ui.showHistoryArchive("main");
  assert.deepEqual(h.requests.map((entry) => entry.path), ["/sessions/main/messages?limit=50"]);
  assert.equal(h.requests[0].cache, "no-store");
  assert.ok(h.requests[0].signal instanceof AbortSignal);
  assert.equal(h.ui.slot.through, 1); assert.equal(h.body().includes("pending"), false);
  assert.equal(h.document.activeElement === h.document.querySelector("#dialog-title"), true);
});

test("directory navigation uses actual returned boundaries, one DOM page and a fixed append ceiling", async () => {
  const first = [message(98), message(100)];
  const h = harness(async (path) => path.includes("before_seq=98") ? directory([message(90)], { through_seq: 100, has_older: true, next_before_seq: 90 }) : directory(first, { has_older: true, next_before_seq: 98 }));
  await h.ui.showHistoryArchive("main");
  assert.equal(h.requests.length, 1); assert.equal(h.button("较新一页").disabled, true);
  const children = h.ui.slot.content.children;
  assert.ok(children.indexOf(h.dialog.querySelector("nav")) < children.indexOf(h.dialog.querySelector(".history-message")), "page navigation must precede long record lists");
  h.document.querySelector("#dialog-content").scrollTop = 6000;
  await h.click(h.button("更早一页"));
  assert.equal(h.document.querySelector("#dialog-content").scrollTop, 0);
  assert.equal(h.requests[1].path, "/sessions/main/messages?limit=50&before_seq=98&through_seq=100");
  assert.equal(h.dialog.querySelectorAll(".history-message").length, 1);
  assert.equal(h.body().includes("saved-100"), false); assert.equal(h.ui.slot.bodyPage === null, true);
  assert.equal(h.document.activeElement.className, "history-position");
  await h.click(h.button("较新一页"));
  assert.equal(h.requests[2].path, "/sessions/main/messages?limit=50&through_seq=100");
  assert.equal(h.dialog.querySelectorAll(".history-message").length, 2);
  assert.equal(h.ui.slot.starts.length, 1);
});

test("empty archives and exhausted older pages are explicit and never loop or fetch automatically", async () => {
  const h = harness(async () => directory([])); await h.ui.showHistoryArchive("main");
  assert.ok(h.body().includes("此页没有记录"));
  assert.equal(h.button("更早一页").disabled, true); assert.equal(h.requests.length, 1);
  assert.equal(h.ui.slot.through, 0);
  const older = harness(async (path) => path.includes("before_seq") ? directory([], { through_seq: 5 }) : directory([message(5)], { has_older: true, next_before_seq: 5 }));
  await older.ui.showHistoryArchive("main"); await older.click(older.button("更早一页"));
  assert.equal(older.requests.length, 2); assert.equal(older.button("更早一页").disabled, true);
  assert.equal(older.button("较新一页").disabled, false);
});

test("archive roles, identity and provenance stay neutral; omission is not owner identity or original completeness", async () => {
  const records = [message(1, { principal: "system:attention", source: "reminder" }), message(2), message(3, { role: "tool", content_provenance: "runtime_excerpt" }), message(4, { role: "constructor", metadata_omitted: ["role", "principal"], content_provenance: "unknown" }), message(5, { role: "system" })];
  const h = harness(async () => directory(records)); await h.ui.showHistoryArchive("main");
  for (const text of ["系统资料", "用户记录", "工具输出", "未知角色", "系统记录", "system:attention", "reminder", "身份：未记录", "保存时已节选", "完整性未标记", "部分来源元数据未显示"]) assert.ok(h.body().includes(text), text);
  for (const text of ["你的消息", "沐璃的回复", "主人来源", "原始完整"]) assert.equal(h.body().includes(text), false, text);
});

test("UTF-8 body pages send revision on first read, keep one segment and reread previous byte boundaries", async () => {
  const first = "中文🙂\0第一段", second = "<img src=x onerror=leak()>末段";
  const offset = Buffer.byteLength(first), total = offset + Buffer.byteLength(second);
  const item = message(9, { content: first, content_bytes: total, content_truncated: true, principal: "system:attention", source: "system" });
  const h = harness(async (path) => path.includes("/content?") ? contentPage(item, path.includes(`offset_bytes=${offset}`) ? second : first, path.includes(`offset_bytes=${offset}`) ? offset : 0, total, { principal: item.principal, source: item.source }) : directory([item]));
  await h.ui.showHistoryArchive("main"); await h.click(h.button("查看正文"));
  assert.equal(h.requests[1].path, "/sessions/main/messages/message-9/content?offset_bytes=0&revision=revision-9");
  assert.equal(h.ui.slot.page === null, true); assert.equal("content" in h.ui.slot.selected, false);
  assert.equal(h.dialog.querySelector("pre").textContent, first); assert.equal(h.button("上一段").disabled, true);
  const children = h.ui.slot.content.children;
  assert.ok(children.indexOf(h.dialog.querySelector("nav")) < children.indexOf(h.dialog.querySelector("pre")), "segment navigation must precede long body text");
  h.document.querySelector("#dialog-content").scrollTop = 6000;
  await h.click(h.button("下一段"));
  assert.equal(h.document.querySelector("#dialog-content").scrollTop, 0);
  assert.ok(h.requests[2].path.includes(`offset_bytes=${offset}`));
  assert.equal(h.dialog.querySelectorAll("pre").length, 1); assert.equal(h.dialog.querySelector("pre").textContent, second);
  assert.equal(h.dialog.querySelectorAll("img").length, 0); assert.equal(h.body().includes(first), false);
  assert.equal(h.button("下一段").disabled, true); assert.ok(h.body().includes("已到所存正文末尾"));
  await h.click(h.button("上一段"));
  assert.equal(h.requests[3].path, h.requests[1].path); assert.equal(h.ui.slot.offsets.length, 1);
  await h.click(h.button("返回记录"));
  assert.equal(h.ui.slot.bodyPage === null, true); assert.equal(h.ui.slot.selected === null, true);
  assert.equal(h.document.activeElement.dataset.historyMessageId, item.id);
});

test("missing or non-text content has no fabricated read button and an empty string is readable", async () => {
  const h = harness(async () => directory([message(1, { content: null, content_bytes: null, content_unavailable: true }), message(2, { content: "", content_bytes: 0 })]));
  await h.ui.showHistoryArchive("main");
  assert.ok(h.body().includes("此消息无文本正文"));
  const buttons = h.dialog.querySelectorAll("[data-history-message-id]");
  assert.deepEqual(buttons.map((button) => button.dataset.historyMessageId), ["message-2"]);
});

test("unsafe integers and malformed directory pages produce an error without sending rounded continuation", async () => {
  const unsafe = JSON.parse('{"seq":9007199254740993}').seq;
  const invalid = [
    directory([message(unsafe)]),
    directory([message(1)], { through_seq: unsafe }),
    directory([message(1)], { has_older: true, next_before_seq: unsafe }),
    directory([message(1)], { session_id: "other" }),
    directory([message(1), message(1)]),
    directory([message(3), message(2)]),
    directory([message(1)], { has_older: true, next_before_seq: 2 }),
    directory([], { has_older: true, next_before_seq: 1 }),
    directory([message(1)], { next_before_seq: 1 }),
    directory([message(1, { content_bytes: 1 })]),
    directory([message(1, { content_truncated: true })]),
    directory(Array.from({ length: 51 }, (_, index) => message(index + 1))),
  ];
  for (const response of invalid) {
    const h = harness(async () => response); await h.ui.showHistoryArchive("main");
    assert.equal(h.ui.slot.page === null, true); assert.ok(h.dialog.querySelector('[role="alert"]'));
    assert.equal(h.button("更早一页") === undefined, true); assert.equal(h.requests.length, 1);
  }
});

test("an older page cannot change the ceiling, cross its exclusive boundary or leave stale records", async () => {
  for (const invalid of [directory([message(3)], { through_seq: 8 }), directory([message(5)], { through_seq: 9 })]) {
    const h = harness(async (path) => path.includes("before_seq") ? invalid : directory([message(5)], { through_seq: 9, has_older: true, next_before_seq: 5 }));
    await h.ui.showHistoryArchive("main"); await h.click(h.button("更早一页"));
    assert.equal(h.ui.slot.page === null, true); assert.equal(h.body().includes("saved-5"), false);
    assert.ok(h.dialog.querySelector('[role="alert"]')); assert.equal(h.requests.length, 2);
  }
});

test("network and safe-range server errors are explicit, retry the exact page and do not claim EOF", async () => {
  let fail = true;
  const h = harness(async (path) => {
    if (!path.includes("before_seq")) return directory([message(5)], { has_older: true, next_before_seq: 5 });
    if (fail) { fail = false; throw new ApiError("归档序号超出网页可用范围", 400, { detail: { code: "archive_sequence_range" } }); }
    return directory([message(2)], { through_seq: 5 });
  });
  await h.ui.showHistoryArchive("main"); await h.click(h.button("更早一页"));
  assert.ok(h.body().includes("归档序号超出网页可用范围")); assert.equal(h.body().includes("已到最早"), false);
  await h.click(h.button("重试"));
  assert.equal(h.requests[2].path, h.requests[1].path); assert.ok(h.body().includes("saved-2"));
  assert.equal(h.document.activeElement.className, "history-position");
});

test("body responses reject wrong identity, revision, byte spans and non-advancing continuation", async () => {
  const item = message(4);
  const mutations = [
    { session_id: "other" }, { message_id: "other" }, { revision: "other" }, { seq: 5 },
    { seq: JSON.parse("9007199254740993") }, { offset_bytes: 1 }, { length_bytes: 1 }, { next_offset_bytes: 1 },
    { total_bytes: item.content_bytes + 1 }, { has_more: true },
    { content: "", length_bytes: 0, next_offset_bytes: 0, has_more: true },
  ];
  for (const fields of mutations) {
    const h = harness(async (path) => path.includes("/content?") ? contentPage(item, item.content, 0, item.content_bytes, fields) : directory([item]));
    await h.ui.showHistoryArchive("main"); await h.click(h.button("查看正文"));
    assert.equal(h.ui.slot.bodyPage === null, true); assert.equal(h.dialog.querySelector("pre") === null, true);
    assert.ok(h.body().includes("正文响应不一致")); assert.equal(h.requests.length, 2);
  }
});

test("deleted or revised body pages clear previous text and require returning to fresh records", async () => {
  for (const status of [404, 409]) {
    let bodyReads = 0, revision = "revision-5";
    const item = message(5, { content: "first", content_bytes: 10, content_truncated: true });
    const h = harness(async (path) => {
      if (!path.includes("/content?")) return directory([{ ...item, revision }]);
      bodyReads += 1;
      if (bodyReads === 1) return contentPage(item, "first");
      revision = "fresh-revision"; throw new ApiError("changed", status);
    });
    await h.ui.showHistoryArchive("main"); await h.click(h.button("查看正文"));
    await h.click(h.button("下一段"));
    assert.equal(h.dialog.querySelector("pre") === null, true); assert.equal(h.body().includes("first"), false);
    assert.equal(h.button("重试") === undefined, true); assert.equal(h.ui.slot.offsets.length, 1);
    await h.click(h.button("返回记录"));
    assert.equal(h.ui.slot.page.messages[0].revision, revision);
    assert.equal(h.document.activeElement.dataset.historyMessageId, item.id);
  }
});

test("body read failures retry the same byte offset and revision without replaying model input", async () => {
  const item = message(7); let fail = true;
  const h = harness(async (path) => {
    if (!path.includes("/content?")) return directory([item]);
    if (fail) { fail = false; throw new ApiError("暂时离线"); }
    return contentPage(item);
  });
  await h.ui.showHistoryArchive("main"); await h.click(h.button("查看正文"));
  assert.ok(h.body().includes("暂时离线")); assert.equal(h.ui.slot.bodyPage === null, true);
  await h.click(h.button("重试"));
  assert.equal(h.requests[2].path, h.requests[1].path);
  assert.ok(h.requests.every((entry) => entry.method === undefined));
  assert.equal(h.dialog.querySelector("pre").textContent, item.content);
});

test("returning to records during a body read invalidates its response within the same dialog", async () => {
  const gate = deferred(), item = message(3);
  const h = harness(async (path) => path.includes("/content?") ? gate.promise : directory([item]));
  await h.ui.showHistoryArchive("main"); await h.click(h.button("查看正文"));
  const slot = h.ui.slot; await h.click(h.button("返回记录"));
  assert.equal(h.ui.slot === slot, true); assert.equal(h.requests[1].signal.aborted, true);
  gate.resolve(contentPage(item)); await flush();
  assert.equal(slot.mode, "list"); assert.equal(slot.bodyPage === null, true);
  assert.equal(h.dialog.querySelector("pre") === null, true); assert.ok(h.button("查看正文"));
});

test("native replaceChildren converts false and null to text but archive conditional rows never pass such values", async () => {
  const h = harness(async () => directory());
  const raw = h.document.createElement("div"); raw.replaceChildren(false, null, undefined);
  assert.equal(raw.textContent, "falsenullundefined");
  await h.ui.showHistoryArchive("main");
  assert.ok(h.ui.slot.content.childNodes.every((node) => node instanceof DOMElement));
  const row = h.dialog.querySelector(".history-message");
  assert.ok(row.childNodes.every((node) => node instanceof DOMElement));
});

test("closing an in-flight archive aborts it, clears retained pages and ignores a late response", async () => {
  const gate = deferred(), h = harness(async () => gate.promise);
  const trigger = h.node("button", "archive-trigger", h.other);
  const reading = h.ui.showHistoryArchive("main", { trigger }); const slot = h.ui.slot;
  h.dialog.close(); await flush();
  assert.equal(h.requests[0].signal.aborted, true); assert.equal(h.ui.slot === null, true);
  assert.equal(slot.page === null, true); assert.equal(slot.bodyPage === null, true); assert.equal(slot.starts.length, 0);
  assert.equal(h.document.activeElement === trigger, true);
  gate.resolve(directory([message(1, { content: "LATE_SECRET", content_bytes: 11 })])); await reading;
  assert.equal(h.body().includes("LATE_SECRET"), false); assert.equal(h.dialog.open, false);
});

test("a different session or replacement dialog owns its DOM even if the previous response ignores abort", async () => {
  for (const replacement of ["archive", "generic"]) {
    const gate = deferred();
    const h = harness(async (path) => path.startsWith("/sessions/old/") ? gate.promise : directory([message(2)], { session_id: "new" }));
    const reading = h.ui.showHistoryArchive("old");
    if (replacement === "archive") await h.ui.showHistoryArchive("new");
    else { const content = h.document.createElement("p"); content.textContent = "NEW_DIALOG"; h.ui.openDialog({ title: "新窗口", content, readOnly: true }); }
    assert.equal(h.requests[0].signal.aborted, true);
    gate.resolve(directory([message(1)], { session_id: "old" })); await reading;
    assert.equal(h.body().includes("saved-1"), false);
    assert.ok(h.body().includes(replacement === "archive" ? "saved-2" : "NEW_DIALOG"));
  }
});

test("leaving the view or page aborts reading and late bodies cannot refill the closed archive", async () => {
  for (const leave of ["view", "page"]) {
    const gate = deferred();
    const h = harness(async (path) => path === "/uploads" ? { items: [] } : gate.promise);
    const reading = h.ui.showHistoryArchive("main");
    if (leave === "view") await h.ui.switchView("uploads");
    else h.windowEvents.pagehide();
    await flush(); assert.equal(h.requests[0].signal.aborted, true); assert.equal(h.ui.slot === null, true);
    gate.resolve(directory()); await reading;
    assert.equal(h.dialog.open, false); assert.equal(h.body().includes("saved-1"), false);
  }
});

test("401 closes the archive and clears data before an older request can arrive", async () => {
  const gate = deferred();
  const h = harness(async (path) => { if (path.startsWith("/sessions/old/")) return gate.promise; throw new ApiError("请重新登录", 401); });
  const first = h.ui.showHistoryArchive("old"); await h.ui.showHistoryArchive("main");
  assert.equal(h.ui.state.auth.authenticated, false); assert.equal(h.ui.slot === null, true);
  assert.equal(h.dialog.open, false); assert.equal(h.body(), "");
  assert.equal(h.document.activeElement === h.document.querySelector("#login-password"), true);
  gate.resolve(directory([message(1)], { session_id: "old" })); await first;
  assert.equal(h.body(), ""); assert.equal(h.ui.slot === null, true);
});

test("401 while continuing a body clears the retained text and selected identity", async () => {
  const item = message(1, { content: "private", content_bytes: 14, content_truncated: true }); let reads = 0;
  const h = harness(async (path) => {
    if (!path.includes("/content?")) return directory([item]);
    if (reads++ === 0) return contentPage(item);
    throw new ApiError("登录已失效", 401);
  });
  await h.ui.showHistoryArchive("main"); await h.click(h.button("查看正文"));
  const slot = h.ui.slot; assert.equal(slot.bodyPage.content, "private");
  await h.click(h.button("下一段"));
  assert.equal(h.body(), ""); assert.equal(h.ui.slot === null, true);
  assert.equal(slot.bodyPage === null, true); assert.equal(slot.selected === null, true);
  assert.equal(h.document.activeElement === h.document.querySelector("#login-password"), true);
});

test("old task details retain media and expose the same archive even when there are no visible messages", async () => {
  for (const messages of [[{ id: "media-message", role: "user", content: "旧任务", attachments: [{ id: "image", name: "图片.png", mime_type: "image/png", size: 10, kind: "image" }, { id: "audio", name: "声音.wav", mime_type: "audio/wav", size: 10, kind: "audio" }] }], []]) {
    const h = harness(async (path) => path.includes("/messages?") ? directory([], { session_id: "worker" }) : { session: { id: "worker", title: "<旧任务>" }, messages });
    await h.ui.loadSession("worker");
    assert.ok(h.button("查看正文归档") instanceof DOMButton);
    if (messages.length) { assert.equal(h.dialog.querySelectorAll("img").length, 1); assert.equal(h.dialog.querySelectorAll("audio").length, 1); assert.equal(h.dialog.querySelectorAll("a[download]").length, 2); }
    await h.click(h.button("查看正文归档"));
    assert.equal(h.requests.at(-1).path, "/sessions/worker/messages?limit=50");
    assert.ok(h.button("返回任务详情") instanceof DOMButton);
    await h.click(h.button("返回任务详情")); assert.ok(h.button("查看正文归档"));
  }
});

test("late old task details cannot overwrite another task or the newly opened archive", async () => {
  for (const replacement of ["detail", "archive"]) {
    const gate = deferred();
    const h = harness(async (path) => path === "/sessions/old" ? gate.promise : path.includes("/messages?") ? directory([]) : { session: { id: "new", title: "NEW_TASK" }, messages: [] });
    const first = h.ui.loadSession("old");
    if (replacement === "detail") await h.ui.loadSession("new");
    else await h.ui.showHistoryArchive("main");
    gate.resolve({ session: { id: "old", title: "LATE_TITLE" }, messages: [{ role: "assistant", content: "LATE_BODY" }] }); await first;
    assert.equal(h.body().includes("LATE_BODY"), false); assert.equal(h.document.querySelector("#dialog-title").textContent.includes("LATE_TITLE"), false);
    assert.equal(h.requests[0].signal.aborted, true);
  }
});

test("new SSE changes the live state without reloading the archive, altering drafts or taking focus", async () => {
  const h = harness(async () => directory([message(8)]));
  h.ui.startSessionEvents("main"); h.ui.startGlobalEvents();
  const input = h.document.querySelector("#message-input"); input.value = "UNSENT_DRAFT";
  h.ui.state.attachments = [{ id: "draft-attachment" }]; await input.dispatch("input");
  const revision = h.ui.draftRevision;
  await h.ui.showHistoryArchive("main"); h.button("查看正文").focus(); const focused = h.document.activeElement;
  const before = h.body(); const slot = h.ui.slot;
  h.emit("/sessions/main/events", { id: "new-event", session_id: "main", type: "message.completed", data: { message: { id: "new", role: "assistant", content: "LIVE_ONLY" } } });
  h.emit("/events", { id: "done-event", type: "run.completed", data: {} });
  await flush();
  assert.ok(h.ui.state.session.messages.some((item) => item.content === "LIVE_ONLY"));
  assert.equal(h.body(), before); assert.equal(h.ui.slot === slot, true); assert.equal(slot.through, 8);
  assert.equal(h.requests.length, 1); assert.equal(input.value, "UNSENT_DRAFT");
  assert.equal(h.ui.state.attachments[0].id, "draft-attachment"); assert.equal(h.ui.draftRevision, revision);
  assert.equal(h.document.activeElement === focused, true); h.ui.stopEvents();
});

test("close restores the same record button after a management redraw and body loading does not steal unrelated focus", async () => {
  const gate = deferred(), item = message(1);
  const h = harness(async (path) => path.includes("/content?") ? gate.promise : directory([item]));
  h.ui.state.view = "records";
  const original = h.node("button", "record-button", h.other); original.dataset.action = "workflow-session"; original.dataset.session = "main";
  await h.ui.showHistoryArchive("main", { trigger: original });
  const reading = h.click(h.button("查看正文")); await flush();
  h.document.querySelector("#dialog-close").focus();
  gate.resolve(contentPage(item)); await reading; await flush();
  assert.equal(h.document.activeElement === h.document.querySelector("#dialog-close"), true);
  // The original node is gone; restoring uses the exact session, not a neighbour.
  const otherButton = h.document.createElement("button"); otherButton.dataset.action = "workflow-session"; otherButton.dataset.session = "other";
  const replacement = h.document.createElement("button"); replacement.dataset.action = "workflow-session"; replacement.dataset.session = "main";
  h.other.replaceChildren(otherButton, replacement); h.dialog.close(); await flush();
  assert.equal(h.document.activeElement === replacement, true);
});
