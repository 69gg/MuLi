import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";
import { ApiError } from "../../web/api.mjs";

// Run the actual application functions and registered dialog close handler.
// No browser library, server, model, account or real database is involved.
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
  replaceChildren(...children) { for (const child of this.childNodes) child.parentNode = null; this.childNodes = []; this.append(...children); }
  setAttribute(key, value) {
    if (key.startsWith("data-")) this.dataset[key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = String(value);
    else this.attributes[key] = String(value);
  }
  getAttribute(key) {
    if (key.startsWith("data-")) return this.dataset[key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())];
    return this.attributes[key] ?? this[key];
  }
  addEventListener(name, handler) { (this.events[name] ??= []).push(handler); }
  async dispatch(name) { for (const handler of this.events[name] ?? []) await handler({ target: this, preventDefault() {} }); }
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

function fixtureMemory(id = "memory-one", overrides = {}) {
  return { id, content: "当前记忆：偏好青瓷蓝", kind: "preference", layer: "preference", source: "可编辑标签", source_id: "source-one", quote: "原始引文：喜欢蓝色", trusted_owner: false, confidence: .8, status: "active", relations: [], entities: [], conflicts: [], version: 1, origin: { id: "source-one", session_id: "old-session", source: "system", role: "user", principal: "system:attention", trusted_owner: false, digest: "digest-one", created_at: 1 }, ...overrides };
}

function sourcePage(item, content, offset = 0, total = offset + Buffer.byteLength(content), extra = {}) {
  const length = Buffer.byteLength(content);
  return { memory_id: item.id, source_id: item.source_id, ...item.origin, field: "content", revision: item.origin.digest, content, offset_bytes: offset, length_bytes: length, next_offset_bytes: offset + length, total_bytes: total, has_more: offset + length < total, ...extra };
}

function harness(handler = async () => { throw new Error("unexpected request"); }) {
  const document = { events: {}, activeElement: null, createTextNode: (text) => new TextNode(text), addEventListener(name, callback) { (this.events[name] ??= []).push(callback); } };
  document.createElement = (tag) => tag === "button" ? new DOMButton(tag, document) : new DOMElement(tag, document);
  document.body = document.createElement("body");
  document.querySelector = (selector) => document.body.querySelector(selector);
  document.querySelectorAll = (selector) => document.body.querySelectorAll(selector);
  const node = (tag, id, parent = document.body) => { const value = document.createElement(tag); value.setAttribute("id", id); parent.append(value); return value; };
  const dialog = node("dialog", "app-dialog"); node("h2", "dialog-title", dialog);
  const form = node("form", "dialog-form", dialog); node("div", "dialog-content", form); node("p", "dialog-error", form);
  const actions = node("div", "dialog-actions", form); actions.className = "dialog-actions";
  const close = node("button", "dialog-close", actions); close.dataset.action = "close-dialog"; node("button", "dialog-submit", actions);
  const other = node("div", "other-view"); const shell = node("div", "app-shell"); shell.className = "app-shell";
  node("div", "auth-gate"); node("input", "login-password");
  for (const id of ["composer", "login-form", "message-input", "mode-select", "file-input", "conversation-scroll"]) node("div", id);
  const requests = [], windowEvents = {}; let globalEvent = null;
  const context = {
    document, Node: class { static [Symbol.hasInstance](value) { return value instanceof DOMElement || value instanceof TextNode; } },
    window: { addEventListener: (name, callback) => { windowEvents[name] = callback; } },
    AbortController, TextEncoder, URLSearchParams, console, queueMicrotask,
    setTimeout: () => 0, clearTimeout() {},
    createSessionState: () => ({ messages: [] }), createTeamPager: () => ({}), captureTeamView: () => null,
    uploadFile() {}, transcribeAttachment() {},
    VoiceInput: class { cancel() {} }, restoreTeamView() {}, sessionStorage: {},
    formatTimestamp: (value) => value === undefined ? "未记录" : String(value), formatFullDate: String,
    apiRequest: async (path, options = {}) => { requests.push({ path, ...options }); return handler(path, options); },
    readEvents: async (_path, options) => { globalEvent = options.onEvent; await new Promise((resolve) => options.signal.addEventListener("abort", resolve, { once: true })); },
    FormData: class { constructor(root) { this.values = new Map(root.querySelectorAll("[name]").map((input) => [input.name, input.value])); } get(name) { return this.values.get(name) ?? null; } },
  };
  runInNewContext(application + `
    renderChrome = () => {}; renderOtherView = () => {}; renderChat = () => {}; toast = () => {};
    globalThis.ui = { state, renderMemories, showMemoryEvidence, editMemory, openDialog, showLogin, invalidateMemoryEvidence, loadMemorySource, loadMemoryHistory, switchView, handleAction, startGlobalEvents,
      stopEvents() { globalController?.abort(); },
      get slot() { return memoryDialog; }, get submit() { return dialogSubmit; } };
  `, context);
  const ui = context.ui; ui.state.view = "memories"; ui.state.auth.authenticated = true;
  const body = () => document.querySelector("#dialog-content").textContent;
  const button = (text, root = dialog) => root.querySelectorAll("button").find((entry) => entry.textContent === text);
  const expand = async (text) => { const details = dialog.querySelectorAll("details").find((entry) => entry.firstElementChild.textContent === text); assert.ok(details, `missing ${text}`); details.open = true; await details.dispatch("toggle"); await flush(); };
  return { ui, requests, document, dialog, form, other, body, button, expand, windowEvents, emitGlobal: (event) => globalEvent(event) };
}

test("every memory has real check/edit/delete buttons even without relations or an origin", () => {
  const h = harness();
  h.ui.state.memories = [fixtureMemory(), fixtureMemory("manual", { source_id: null, origin: null })];
  const rows = h.ui.renderMemories().querySelectorAll(".memory-row");
  for (const row of rows) {
    const buttons = row.querySelectorAll("button");
    assert.ok(buttons.every((button) => button instanceof DOMButton));
    for (const action of ["memory-evidence", "edit-memory", "delete-memory"]) assert.ok(buttons.some((button) => button.dataset.action === action));
  }
});

test("checking always reads fresh detail, separates original quote and true identity, and does not prefetch", async () => {
  const item = fixtureMemory("memory-one", { content: '<img src=x onerror="leak()">更正的正文', status: "superseded", conflicts: ["other"] });
  const h = harness(async () => item); h.ui.state.memories = [{ ...item, content: "STALE_CACHE" }];
  await h.ui.showMemoryEvidence(item.id);
  assert.deepEqual(h.requests.map((entry) => entry.path), ["/memories/memory-one"]);
  assert.equal(h.requests[0].cache, "no-store");
  for (const text of [item.content, "原始引文", item.quote, "来源标签", "原始来源", "system:attention", "未标为主人来源", "已替代", "修改历史"]) assert.ok(h.body().includes(text), text);
  assert.equal(h.body().includes("STALE_CACHE"), false);
  assert.equal(h.dialog.querySelectorAll("img").length, 0);
  assert.ok(h.button("更正") instanceof DOMButton);
  assert.ok(h.button("核查另一条") instanceof DOMButton);
});

test("missing and forgotten origins stay unavailable without reading a different conversation", async () => {
  for (const source_id of [null, "forgotten-source"]) {
    const item = fixtureMemory("memory-one", { source_id, origin: null });
    const h = harness(async () => item); await h.ui.showMemoryEvidence(item.id);
    assert.ok(h.body().includes(source_id ? "原文已不可用" : "无关联原文"));
    assert.equal(h.requests.length, 1);
    assert.equal(h.dialog.querySelectorAll("summary").some((node) => node.textContent === "查看原文"), false);
  }
});

test("UTF-8 source pages carry the first digest, keep only one page and reread previous offsets", async () => {
  const item = fixtureMemory(), first = "中文🙂\0第一段", second = "<script>字面原文</script>";
  const offset = Buffer.byteLength(first), total = offset + Buffer.byteLength(second);
  const h = harness(async (path) => !path.includes("/source?") ? item : new URLSearchParams(path.split("?")[1]).get("offset_bytes") === "0" ? sourcePage(item, first, 0, total) : sourcePage(item, second, offset, total));
  await h.ui.showMemoryEvidence(item.id); await h.expand("查看原文");
  assert.ok(h.body().includes(first)); assert.equal(h.requests.length, 2);
  const query = new URLSearchParams(h.requests[1].path.split("?")[1]);
  assert.equal(query.get("revision"), item.origin.digest); assert.equal(query.get("offset_bytes"), "0"); assert.equal(query.has("length_bytes"), false);
  h.button("下一段").focus(); await h.button("下一段").dispatch("click"); await flush();
  assert.equal(h.ui.slot.page.content, second); assert.equal(h.body().includes(first), false);
  assert.ok(h.body().includes(second)); assert.equal(h.dialog.querySelectorAll("script").length, 0);
  assert.ok(h.body().includes("第 2 段 · 已到末尾")); assert.equal(h.button("下一段").disabled, true);
  assert.equal(h.document.activeElement.className, "memory-source-position");
  await h.button("上一段").dispatch("click"); await flush();
  assert.equal(h.ui.slot.page.content, first); assert.equal(h.body().includes(second), false);
  assert.equal(h.requests.length, 4); assert.equal(h.ui.slot.offsets.length, 1);
});

test("a failed next page retains the current page and retries that offset without pretending EOF", async () => {
  const item = fixtureMemory(); let fail = true;
  const h = harness(async (path) => {
    if (!path.includes("/source?")) return item;
    if (path.includes("offset_bytes=0")) return sourcePage(item, "FIRST", 0, 9);
    if (fail) throw new ApiError("临时断线");
    return sourcePage(item, "LAST", 5, 9);
  });
  await h.ui.showMemoryEvidence(item.id); await h.expand("查看原文");
  await h.button("下一段").dispatch("click"); await flush();
  assert.ok(h.body().includes("FIRST") && h.body().includes("临时断线") && h.body().includes("尚有后文"));
  fail = false; await h.button("重试").dispatch("click"); await flush();
  assert.ok(h.body().includes("LAST")); assert.equal(h.body().includes("FIRST"), false);
  assert.equal(h.requests.at(-1).path, h.requests.at(-2).path);
});

for (const [name, override] of [["memory identity", { memory_id: "other" }], ["source identity", { source_id: "other" }], ["revision", { revision: "old" }], ["UTF-8 length", { length_bytes: 1 }], ["nonadvancing continuation", { content: "", length_bytes: 0, next_offset_bytes: 0, total_bytes: 9, has_more: true }]]) {
  test(`an invalid ${name} discards the page and requires a fresh check`, async () => {
    const item = fixtureMemory();
    const h = harness(async (path) => path.includes("/source?") ? sourcePage(item, "PRIVATE", 0, 7, override) : item);
    await h.ui.showMemoryEvidence(item.id); await h.expand("查看原文");
    assert.equal(h.ui.slot.page, null); assert.equal(h.body().includes("PRIVATE"), false);
    assert.equal(h.body().includes(item.quote), false); assert.ok(h.button("重新核查"));
  });
}

for (const status of [404, 409]) test(`source ${status} clears existing evidence and never falls back to session history`, async () => {
  const item = fixtureMemory(); let unavailable = false;
  const h = harness(async (path) => {
    if (!path.includes("/source?")) return item;
    if (unavailable) throw new ApiError("原文变化", status, { detail: { code: status === 404 ? "evidence_not_found" : "evidence_changed" } });
    return sourcePage(item, "PRIVATE", 0, 9);
  });
  await h.ui.showMemoryEvidence(item.id); await h.expand("查看原文"); unavailable = true;
  await h.button("下一段").dispatch("click"); await flush();
  assert.equal(h.ui.slot.page, null); assert.equal(h.body().includes("PRIVATE"), false); assert.equal(h.body().includes(item.quote), false);
  assert.ok(h.button("重新核查")); assert.equal(h.requests.some((entry) => entry.path.includes("/sessions/")), false);
});

test("switching entries ignores a late old detail and does not replace the current dialog", async () => {
  const pending = deferred(), other = fixtureMemory("memory-two", { content: "SECOND_MEMORY" });
  const h = harness(async (path) => path === "/memories/memory-one" ? pending.promise : other);
  const first = h.ui.showMemoryEvidence("memory-one"); const oldSignal = h.requests[0].signal;
  await h.ui.showMemoryEvidence(other.id); assert.equal(oldSignal.aborted, true);
  pending.resolve(fixtureMemory()); await first;
  assert.equal(h.ui.slot.id, other.id); assert.ok(h.body().includes(other.content)); assert.equal(h.body().includes("当前记忆："), false);
});

test("closing aborts and clears source/history, and both late responses remain invisible", async () => {
  const source = deferred(), history = deferred(), item = fixtureMemory();
  const h = harness(async (path) => path.includes("/source?") ? source.promise : path.endsWith("/revisions") ? history.promise : item);
  await h.ui.showMemoryEvidence(item.id); await h.expand("查看原文"); await h.expand("修改历史");
  const slot = h.ui.slot, signals = h.requests.slice(1).map((entry) => entry.signal);
  h.dialog.close(); await flush(); assert.equal(h.ui.slot, null);
  assert.ok(signals.every((signal) => signal.aborted)); assert.equal(slot.content.textContent, ""); assert.equal(slot.item, null);
  source.resolve(sourcePage(item, "LATE_PRIVATE")); history.resolve({ items: [{ snapshot: { content: "LATE_HISTORY" } }] }); await flush();
  assert.equal(h.body(), ""); assert.equal(slot.sourceRegion.textContent, ""); assert.equal(slot.historyList.textContent, "");
});

test("changing management view or pagehide releases private evidence and pending reads", async () => {
  for (const leave of [(h) => h.ui.switchView("chat"), (h) => h.windowEvents.pagehide()]) {
    const source = deferred(), item = fixtureMemory();
    const h = harness(async (path) => path.includes("/source?") ? source.promise : item);
    await h.ui.showMemoryEvidence(item.id); await h.expand("查看原文"); const signal = h.requests.at(-1).signal;
    await leave(h); assert.equal(h.ui.slot, null); assert.equal(signal.aborted, true); assert.equal(h.body(), "");
    source.resolve(sourcePage(item, "LATE_PRIVATE")); await flush(); assert.equal(h.body(), "");
  }
});

test("401 clears the modal immediately and late source data cannot reopen it", async () => {
  const source = deferred(), history = deferred(), item = fixtureMemory();
  const h = harness(async (path) => path.includes("/source?") ? source.promise : path.endsWith("/revisions") ? history.promise : item);
  await h.ui.showMemoryEvidence(item.id); await h.expand("查看原文"); await h.expand("修改历史");
  history.reject(new ApiError("请登录", 401)); await flush();
  assert.equal(h.ui.slot, null); assert.equal(h.dialog.open, false); assert.equal(h.ui.state.auth.authenticated, false); assert.equal(h.body(), "");
  assert.equal(h.document.activeElement, h.document.querySelector("#login-password"));
  source.resolve(sourcePage(item, "LATE_PRIVATE")); await flush(); assert.equal(h.body(), "");
});

test("source-level forgetting clears all currently displayed evidence and aborts a pending next page", async () => {
  const item = fixtureMemory(), next = deferred();
  const h = harness(async (path) => !path.includes("/source?") ? item : path.includes("offset_bytes=0") ? sourcePage(item, "PRIVATE", 0, 10) : next.promise);
  await h.ui.showMemoryEvidence(item.id); await h.expand("查看原文"); await h.button("下一段").dispatch("click");
  const signal = h.requests.at(-1).signal;
  h.ui.startGlobalEvents();
  h.emitGlobal({ type: "memory.forgotten", data: { memory_ids: ["another-memory"], source_id: item.source_id } });
  assert.equal(signal.aborted, true); assert.equal(h.ui.slot.page, null); assert.equal(h.body().includes("PRIVATE"), false); assert.equal(h.body().includes(item.quote), false);
  next.resolve(sourcePage(item, "OLD", 7, 10)); await flush(); assert.equal(h.body().includes("OLD"), false); h.ui.stopEvents();
});

test("an update during initial loading invalidates the same source and ignores its old detail", async () => {
  const detail = deferred(), item = fixtureMemory();
  const h = harness(async () => detail.promise); h.ui.state.memories = [item];
  const pending = h.ui.showMemoryEvidence(item.id);
  h.ui.invalidateMemoryEvidence({ type: "memory.updated", data: { memory_ids: [], source_id: item.source_id } });
  assert.equal(h.requests[0].signal.aborted, true);
  detail.resolve(item); await pending; assert.equal(h.body().includes(item.quote), false); assert.ok(h.button("重新核查"));
});

test("correction uses a fresh detail, submits only editable fields, then checks the saved memory and history", async () => {
  const item = fixtureMemory(), updated = { ...item, content: "更正后的正文", version: 2 }; let saved = false;
  const h = harness(async (path, options) => {
    if (options.method === "PATCH") { saved = true; return updated; }
    if (path.endsWith("/revisions")) return { items: [{ changed_at: 2, snapshot: item }] };
    return saved ? updated : item;
  });
  h.ui.state.memories = [{ ...item, content: "STALE_CACHE" }]; await h.ui.editMemory(item.id);
  const input = h.dialog.querySelector("textarea"); assert.equal(input.value, item.content); input.value = updated.content;
  const names = h.dialog.querySelectorAll("[name]").map((node) => node.name);
  for (const field of ["trusted_owner", "source_id", "quote", "layer", "claim_key", "status"]) assert.equal(names.includes(field), false);
  assert.ok(h.body().includes("来源标签")); await h.form.dispatch("submit"); assert.equal(h.dialog.open, true);
  const patch = h.requests.find((entry) => entry.method === "PATCH");
  assert.deepEqual(Object.keys(patch.body).sort(), ["confidence", "content", "entities", "kind", "relations", "source"]);
  assert.equal(patch.body.content, updated.content);
  assert.ok(h.body().includes(updated.content) && h.body().includes("原始引文") && h.body().includes(item.quote));
  assert.equal(h.requests.filter((entry) => entry.path === "/memories/memory-one" && !entry.method).length, 2);
  await h.expand("修改历史"); assert.ok(h.body().includes(item.content)); assert.ok(h.body().includes(updated.content));
});

test("an external update preserves an open correction draft and requires rechecking before saving", async () => {
  const item = fixtureMemory(), h = harness(async () => item); await h.ui.editMemory(item.id);
  const textarea = h.dialog.querySelector("textarea"); textarea.value = "未保存的更正";
  h.ui.invalidateMemoryEvidence({ type: "memory.updated", data: { memory_ids: ["unrelated"], source_id: null } });
  assert.equal(h.ui.slot.invalidated, false);
  h.ui.invalidateMemoryEvidence({ type: "memory.updated", data: { memory_ids: [item.id], source_id: null } });
  assert.equal(textarea.value, "未保存的更正"); assert.equal(h.document.querySelector("#dialog-submit").disabled, true);
  await assert.rejects(() => h.ui.submit(h.form), /重新核查/);
  assert.equal(h.requests.length, 1); assert.ok(h.button("重新核查"));
});

test("a save completing after close cannot reopen private detail", async () => {
  const save = deferred(), item = fixtureMemory();
  const h = harness(async (_path, options) => options.method === "PATCH" ? save.promise : item);
  await h.ui.editMemory(item.id); const submission = h.ui.submit(h.form); h.dialog.close(); await flush();
  save.resolve(item); await submission;
  assert.equal(h.body(), ""); assert.equal(h.ui.slot, null); assert.equal(h.requests.length, 2);
});

test("closing after the underlying list rerenders restores the same memory check button", async () => {
  const item = fixtureMemory(), h = harness(async () => item); h.ui.state.memories = [item];
  h.other.replaceChildren(h.ui.renderMemories()); h.other.querySelector("[data-action=memory-evidence]").focus();
  await h.ui.showMemoryEvidence(item.id); h.other.replaceChildren(h.ui.renderMemories());
  const currentButton = h.other.querySelector("[data-action=memory-evidence]");
  h.dialog.close(); await flush(); assert.equal(h.document.activeElement, currentButton);
});

test("a late source cannot overwrite another modal, including its heading and actions", async () => {
  const source = deferred(), item = fixtureMemory();
  const h = harness(async (path) => path.includes("/source?") ? source.promise : item);
  await h.ui.showMemoryEvidence(item.id); await h.expand("查看原文"); const signal = h.requests.at(-1).signal;
  const content = h.document.createElement("p"); content.textContent = "其他内容";
  h.ui.openDialog({ title: "其他设置", content, readOnly: true });
  source.resolve(sourcePage(item, "LATE_PRIVATE")); await flush();
  assert.equal(signal.aborted, true); assert.equal(h.ui.slot, null); assert.equal(h.body(), "其他内容");
  assert.equal(h.document.querySelector("#dialog-title").textContent, "其他设置");
});

test("the queued close event of an old modal cannot clear a newly opened memory", async () => {
  const item = fixtureMemory(), next = fixtureMemory("memory-two", { content: "新记忆正文" });
  const h = harness(async (path) => path.endsWith(item.id) ? item : next);
  await h.ui.showMemoryEvidence(item.id); h.dialog.close(); await h.ui.showMemoryEvidence(next.id); await flush();
  assert.equal(h.dialog.open, true); assert.equal(h.ui.slot.id, next.id); assert.ok(h.body().includes(next.content));
});

test("the existing delete confirmation describes source-wide forgetting and cancel sends nothing", async () => {
  const h = harness(), button = h.document.createElement("button"); button.dataset.id = "memory-one";
  await h.ui.handleAction("delete-memory", button);
  assert.ok(h.body().includes("同源记忆也会一并遗忘")); assert.equal(h.requests.length, 0);
  h.dialog.close(); await flush(); assert.equal(h.requests.length, 0);
});

test("queued toggles from detached old details cannot prefetch after a new check", async () => {
  const item = fixtureMemory(), h = harness(async () => item); await h.ui.showMemoryEvidence(item.id);
  const oldDetails = h.dialog.querySelectorAll("details");
  h.ui.invalidateMemoryEvidence({ type: "memory.updated", data: { memory_ids: [item.id] } });
  await h.button("重新核查").dispatch("click"); await flush();
  for (const detail of oldDetails) { detail.open = true; await detail.dispatch("toggle"); }
  await flush(); assert.equal(h.requests.length, 2); assert.equal(h.ui.slot.page, null);
});

test("all persisted memory types have a Chinese option and survive the actual correction form submission", async () => {
  for (const kind of ["identity", "episodic", "semantic", "procedural", "observation", "preference", "fact", "procedure", "summary"]) {
    const item = fixtureMemory("memory-one", { kind }), h = harness(async () => item);
    await h.ui.editMemory(item.id);
    const select = h.dialog.querySelector('[name="kind"]');
    assert.equal(select.value, kind, `${kind} must not become an empty or fallback selection`);
    assert.match(select.children.find((option) => option.value === kind).textContent, /[\u4e00-\u9fff]/);
    await h.form.dispatch("submit");
    assert.equal(h.requests.find((request) => request.method === "PATCH")?.body.kind, kind);
  }
});

test("an unknown persisted type remains a current option and is never silently replaced", async () => {
  const item = fixtureMemory("memory-one", { kind: "future_kind" }), h = harness(async () => item);
  await h.ui.editMemory(item.id);
  const select = h.dialog.querySelector('[name="kind"]');
  assert.equal(select.value, item.kind); assert.ok(select.children.some((option) => option.value === item.kind && option.textContent.includes("当前类型")));
  await h.form.dispatch("submit");
  assert.equal(h.requests.find((request) => request.method === "PATCH")?.body.kind, item.kind);
});
