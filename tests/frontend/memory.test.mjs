import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";
import { ApiError } from "../../web/api.mjs";
import { VoiceSetup, voiceSetupLabel } from "../../web/voice-setup.mjs";

// Run the actual application functions and registered dialog close handler.
// No browser library, server, model, account or real database is involved.
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

function catalogSource(overrides = {}) {
  return { source_id: "attachment:source?&one", source: "attachment:document:v1", session_id: "original-session", role: "tool", principal: "external:attachment", trusted_owner: false, revision: "a".repeat(64), created_at: 1, total_bytes: 24, job: { state: "failed", attempts: 2, last_error: "RuntimeError" }, ...overrides };
}

function catalogPage(items = [], overrides = {}) {
  return { items, examined: items.length, omitted: 0, has_more: false, next_cursor: null, consistency: "live", order: "newest_registered_first", ...overrides };
}

async function openCatalog(h) {
  h.ui.useMemoryViews(); h.other.replaceChildren(h.ui.renderMemories());
  await clickMemoryButton(h, h.other.querySelector('[data-memory-tab="sources"]'));
}

function harness(handler = async () => { throw new Error("unexpected request"); }) {
  const document = { events: {}, activeElement: null, createTextNode: (text) => new TextNode(text), addEventListener(name, callback) { (this.events[name] ??= []).push(callback); } };
  document.createElement = (tag) => tag === "button" ? new DOMButton(tag, document) : new DOMElement(tag, document);
  document.createElementNS = (_namespace, tag) => document.createElement(tag);
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
    VoiceInput: class { cancel() {} }, VoiceSetup, voiceSetupLabel, restoreTeamView() {}, sessionStorage: {},
    formatTimestamp: (value) => value === undefined ? "未记录" : String(value), formatFullDate: String,
    apiRequest: async (path, options = {}) => { requests.push({ path, ...options }); return handler(path, options); },
    readEvents: async (_path, options) => { globalEvent = options.onEvent; await new Promise((resolve) => options.signal.addEventListener("abort", resolve, { once: true })); },
    FormData: class { constructor(root) { this.values = new Map(root.querySelectorAll("[name]").map((input) => [input.name, input.value])); } get(name) { return this.values.get(name) ?? null; } },
  };
  runInNewContext(application + `
    const realOtherView = renderOtherView;
    renderChrome = () => {}; renderOtherView = () => {}; renderChat = () => {}; toast = () => {};
    const activateConnection = () => {
      const owner = beginConnection(); owner.ready = true;
      owner.sessions.start(); owner.notifications.start(); return owner;
    };
    activateConnection();
    globalThis.ui = { state, renderMemories, showMemoryEvidence, editMemory, openDialog, showLogin, invalidateMemoryEvidence, loadMemorySource, loadMemoryHistory, switchView, handleAction, startGlobalEvents, searchMemories,
      activateConnection,
      useMemoryViews() { renderOtherView = () => { if (state.view === "memories") realOtherView(); }; },
      stopEvents() { globalController?.abort(); },
      get slot() { return memoryDialog; }, get path() { return memoryPath; }, get catalog() { return memorySources; }, get submit() { return dialogSubmit; } };
  `, context);
  const ui = context.ui; ui.state.view = "memories"; ui.state.auth.authenticated = true;
  const body = () => document.querySelector("#dialog-content").textContent;
  const button = (text, root = dialog) => root.querySelectorAll("button").find((entry) => entry.textContent === text);
  const expand = async (text) => { const details = dialog.querySelectorAll("details").find((entry) => entry.firstElementChild.textContent === text); assert.ok(details, `missing ${text}`); details.open = true; await details.dispatch("toggle"); await flush(); };
  return { ui, requests, document, dialog, form, other, body, button, expand, windowEvents, emitGlobal: (event) => globalEvent(event) };
}

test("source tab explicitly advances empty candidate windows and keeps its outer layout", async () => {
  const item = catalogSource();
  const h = harness(async (path) => {
    assert.ok(path.startsWith("/memory/sources?"));
    const query = new URLSearchParams(path.split("?")[1]);
    assert.equal(query.get("limit"), "20");
    return query.get("cursor") || query.get("state") ? catalogPage([item]) : catalogPage([], { examined: 2, omitted: 2, has_more: true, next_cursor: "next-window" });
  });
  await openCatalog(h);
  const root = h.other.firstElementChild, panel = h.ui.catalog;
  assert.equal(h.requests.length, 1); assert.ok(h.other.textContent.includes("本窗口没有匹配来源，请继续翻页"));
  assert.equal(h.button("继续翻页", h.other).disabled, false);
  await clickMemoryButton(h, h.button("继续翻页", h.other));
  assert.equal(new URLSearchParams(h.requests[1].path.split("?")[1]).get("cursor"), "next-window");
  assert.equal(h.other.firstElementChild === root, true); assert.equal(h.ui.catalog === panel, true);
  for (const text of [item.source_id, item.principal, item.source, item.session_id, "整理失败", "RuntimeError"]) assert.ok(h.other.textContent.includes(text), text);
  assert.ok(h.other.textContent.includes("查看已保存资料及整理进度。"));
  const details = h.other.querySelector(".memory-row details.advanced-settings");
  assert.ok(details); assert.equal(details.firstElementChild.textContent, "来源详情"); assert.equal(details.open, false);
  assert.equal(h.requests.length, 2); assert.equal(h.button("继续翻页", h.other).disabled, true);
  const filter = h.other.querySelector('select[aria-label="来源整理状态"]');
  filter.value = "failed"; await filter.dispatch("change"); await flush();
  const filtered = new URLSearchParams(h.requests[2].path.split("?")[1]);
  assert.equal(filtered.get("state"), "failed"); assert.equal(filtered.has("cursor"), false);
  await clickMemoryButton(h, h.button("刷新来源", h.other));
  assert.equal(h.requests.length, 4); assert.equal(h.requests[3].path, h.requests[2].path);
  assert.equal(h.other.firstElementChild === root, true);
});

test("a catalog source opens bounded source-only retrieval and can return to the global query", async () => {
  const source = catalogSource(), scoped = fixtureMemory("scoped", { source_id: source.source_id, content: "仅此资料的备份步骤", matched_by: "reranked" }), global = fixtureMemory("global", { content: "全库结果" });
  const h = harness(async (path) => {
    if (path.startsWith("/memory/sources?")) return catalogPage([source]);
    if (path.startsWith("/memory/query?")) {
      const parameters = new URLSearchParams(path.split("?")[1]);
      assert.equal(parameters.get("source_id"), source.source_id); assert.equal(parameters.get("limit"), "20");
      return { memories: [scoped], source_ids: [source.source_id], truncated: true, degraded: ["semantic_unavailable:TimeoutError"] };
    }
    assert.equal(path, "/memories?query=%E5%A4%87%E4%BB%BD"); return { items: [global] };
  });
  await openCatalog(h); await clickMemoryButton(h, h.button("在此来源内查找", h.other));
  assert.equal(h.ui.state.memoryTab, "list"); assert.equal(h.ui.state.memorySourceId, source.source_id);
  assert.ok(h.other.textContent.includes(source.source_id)); assert.ok(h.other.textContent.includes(scoped.content));
  assert.ok(h.other.textContent.includes("部分结果因预算省略")); assert.ok(h.other.textContent.includes("语义检索暂不可用"));
  assert.equal(h.other.querySelector("#memory-search").getAttribute("placeholder"), "在此来源内检索");
  await h.ui.searchMemories("备份");
  assert.equal(new URLSearchParams(h.requests.at(-1).path.split("?")[1]).get("query"), "备份");
  await clickMemoryButton(h, h.button("查全库", h.other));
  assert.equal(h.ui.state.memorySourceId, null); assert.equal(h.ui.state.memoryQuery, "备份");
  assert.ok(h.other.textContent.includes(global.content)); assert.equal(h.other.textContent.includes(scoped.content), false);
  assert.equal(h.other.textContent.includes("语义检索暂不可用"), false);
});

test("switching out of a source cancels late retrieval rather than reviving scoped results", async () => {
  const pending = deferred(), source = catalogSource(), stale = fixtureMemory("stale", { source_id: source.source_id, content: "STALE_SCOPE_BODY" }), current = fixtureMemory("current", { content: "CURRENT_GLOBAL_BODY" });
  const h = harness(async (path) => path.startsWith("/memory/sources?") ? catalogPage([source]) : path.startsWith("/memory/query?") ? pending.promise : { items: [current] });
  await openCatalog(h); await clickMemoryButton(h, h.button("在此来源内查找", h.other));
  const scopedRequest = h.requests.at(-1);
  await clickMemoryButton(h, h.button("查全库", h.other)); assert.equal(scopedRequest.signal.aborted, true);
  pending.resolve({ source_ids: [source.source_id], memories: [stale], degraded: [], truncated: false }); await flush();
  assert.equal(h.ui.state.memorySourceId, null); assert.equal(h.ui.state.memories[0].id, current.id);
  assert.equal(h.other.textContent.includes(stale.content), false); assert.ok(h.other.textContent.includes(current.content));
});

test("source query rejects a response with mismatched scope rather than showing another source", async () => {
  const source = catalogSource(), foreign = fixtureMemory("foreign", { source_id: "another-source", content: "不得展示另一来源正文" });
  const h = harness(async (path) => path.startsWith("/memory/sources?") ? catalogPage([source]) : { source_ids: [source.source_id], memories: [foreign], degraded: [], truncated: false });
  await openCatalog(h); await clickMemoryButton(h, h.button("在此来源内查找", h.other));
  assert.equal(h.ui.state.memorySourceId, source.source_id); assert.equal(h.ui.state.memories.length, 0);
  assert.equal(h.other.textContent.includes(foreign.content), false);
  assert.equal(h.requests.length, 2, "scope mismatch must not fall back to a global request");
});

test("retained source action buttons cannot reopen a reader or retry after paging or leaving", async () => {
  const first = catalogSource(), second = catalogSource({ source_id: "second-source" });
  const h = harness(async (path) => {
    assert.ok(path.startsWith("/memory/sources?"));
    const cursor = new URLSearchParams(path.split("?")[1]).get("cursor");
    return cursor ? catalogPage([second]) : catalogPage([first], { has_more: true, next_cursor: "second-window" });
  });
  await openCatalog(h);
  const oldRead = h.button("核原文", h.other), oldRetry = h.button("重试此来源", h.other);
  await clickMemoryButton(h, h.button("继续翻页", h.other));
  assert.equal(h.requests.length, 2);
  await clickMemoryButton(h, oldRead); await clickMemoryButton(h, oldRetry);
  assert.equal(h.requests.length, 2); assert.equal(h.dialog.open, false); assert.equal(h.ui.slot === null, true);
  const readBeforeLeave = h.button("核原文", h.other), retryBeforeLeave = h.button("重试此来源", h.other);
  await h.ui.switchView("chat");
  assert.equal(h.ui.catalog === null, true);
  await clickMemoryButton(h, readBeforeLeave); await clickMemoryButton(h, retryBeforeLeave);
  assert.equal(h.requests.length, 2); assert.equal(h.dialog.open, false); assert.equal(h.ui.slot === null, true);
});

test("leaving the sources tab or losing authentication discards a late private catalog", async () => {
  for (const leave of [async (h) => clickMemoryButton(h, h.other.querySelector('[data-memory-tab="graph"]')), async (h) => h.ui.showLogin(), async (h) => h.windowEvents.pagehide()]) {
    const pending = deferred(), h = harness(async () => pending.promise);
    const opening = openCatalog(h); await flush();
    const signal = h.requests[0].signal;
    await leave(h); assert.equal(signal.aborted, true); assert.equal(h.ui.catalog === null, true);
    pending.resolve(catalogPage([catalogSource({ principal: "LATE_PRIVATE_CATALOG" })])); await opening;
    assert.equal(h.other.textContent.includes("LATE_PRIVATE_CATALOG"), false);
    assert.equal(h.ui.catalog === null, true);
  }
});

test("catalog source reader uses exact source identity and UTF-8 paging without a memory id", async () => {
  const first = "目录原文🙂", last = "<script>字面尾部</script>";
  const offset = Buffer.byteLength(first), total = offset + Buffer.byteLength(last);
  const item = catalogSource({ total_bytes: total });
  const h = harness(async (path) => {
    if (path.startsWith("/memory/sources?")) return catalogPage([item]);
    assert.ok(path.startsWith("/memory/source?"));
    const query = new URLSearchParams(path.split("?")[1]);
    assert.equal(query.get("id"), item.source_id); assert.equal(query.get("revision"), item.revision);
    const start = Number(query.get("offset_bytes")), content = start === 0 ? first : last;
    return { source_id: item.source_id, revision: item.revision, field: "content", content, offset_bytes: start, length_bytes: Buffer.byteLength(content), next_offset_bytes: start + Buffer.byteLength(content), total_bytes: total, has_more: start === 0 };
  });
  await openCatalog(h); assert.equal(h.requests.length, 1);
  await clickMemoryButton(h, h.button("核原文", h.other));
  assert.ok(h.body().includes(first)); assert.equal(h.requests.length, 2);
  await clickMemoryButton(h, h.button("下一段"));
  assert.ok(h.body().includes(last)); assert.equal(h.body().includes(first), false);
  assert.equal(h.dialog.querySelectorAll("script").length, 0); assert.equal(h.button("下一段").disabled, true);
  assert.equal(new URLSearchParams(h.requests.at(-1).path.split("?")[1]).get("offset_bytes"), String(offset));
  await clickMemoryButton(h, h.button("上一段"));
  assert.ok(h.body().includes(first)); assert.equal(h.body().includes(last), false);
});

test("closing the catalog source reader aborts and rejects a late body", async () => {
  const pending = deferred(), item = catalogSource();
  const h = harness(async (path) => path.startsWith("/memory/sources?") ? catalogPage([item]) : pending.promise);
  await openCatalog(h);
  await clickMemoryButton(h, h.button("核原文", h.other));
  const signal = h.requests.at(-1).signal, slot = h.ui.slot;
  h.dialog.close(); await flush();
  assert.equal(signal.aborted, true); assert.equal(h.ui.slot === null, true);
  pending.resolve({ source_id: item.source_id, revision: item.revision, field: "content", content: "LATE_PRIVATE_BODY", offset_bytes: 0, length_bytes: 17, next_offset_bytes: 17, total_bytes: 17, has_more: false });
  await flush();
  assert.equal(h.body(), ""); assert.equal(slot.sourceRegion.textContent, ""); assert.equal(h.dialog.open, false);
});

test("single-source retry requires confirmation and renders the actual receipt without polling", async () => {
  for (const [reason, state, accepted, description] of [["queued", "queued", true, "尚未完成整理"], ["already_pending", "processing", false, "未重复排队"], ["already_done", "done", false, "未重新执行"], ["capacity_full", "failed", false, "本次未接纳"]]) {
    const item = catalogSource();
    const h = harness(async (path, options) => {
      if (path.startsWith("/memory/sources?")) return catalogPage([item]);
      assert.equal(path, "/memory/sources/retry"); assert.equal(options.method, "POST");
      assert.deepEqual(JSON.parse(options.body), { source_id: item.source_id, revision: item.revision });
      return { source_id: item.source_id, revision: item.revision, previous_state: "failed", state, accepted, reason };
    });
    await openCatalog(h);
    await clickMemoryButton(h, h.button("重试此来源", h.other));
    assert.equal(h.requests.length, 1); assert.equal(h.dialog.open, true);
    assert.ok(h.body().includes(item.source_id) && h.body().includes(item.revision) && h.body().includes("可能再次收费"));
    assert.equal(h.document.querySelector("#dialog-submit").textContent, "确认重试");
    await h.form.dispatch("submit");
    assert.equal(h.requests.length, 2); assert.equal(h.dialog.open, true); assert.equal(h.ui.submit, null);
    assert.ok(h.body().includes(description)); assert.ok(h.other.textContent.includes(description));
    assert.ok(h.body().includes("排队不代表完成"));
    h.dialog.close(); await flush();
    assert.equal(h.requests.length, 2); assert.ok(h.other.textContent.includes(description));
  }
});

test("closing an in-flight source retry aborts and a late receipt cannot replace another dialog", async () => {
  const pending = deferred(), item = catalogSource();
  const h = harness(async (path) => path.startsWith("/memory/sources?") ? catalogPage([item]) : pending.promise);
  await openCatalog(h); await clickMemoryButton(h, h.button("重试此来源", h.other));
  const submission = h.form.dispatch("submit"), signal = h.requests.at(-1).signal;
  h.dialog.close(); await flush(); assert.equal(signal.aborted, true);
  h.ui.openDialog({ title: "另一对话框", content: h.document.createTextNode("保留当前内容"), readOnly: true });
  pending.resolve({ source_id: item.source_id, revision: item.revision, previous_state: "failed", state: "queued", accepted: true, reason: "queued" });
  await submission;
  assert.equal(h.document.querySelector("#dialog-title").textContent, "另一对话框");
  assert.equal(h.body(), "保留当前内容"); assert.equal(h.ui.catalog.receipt, null);
  assert.equal(h.requests.length, 2);
});

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
  for (const text of [item.content, "记忆 ID", item.id, "记忆版本", "原始引文", item.quote, "来源标签", "原始来源", "system:attention", "未标为主人来源", "已替代", "修改历史"]) assert.ok(h.body().includes(text), text);
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
    assert.equal(h.ui.slot.page === null, true); assert.equal(h.body().includes("PRIVATE"), false);
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
  assert.equal(h.ui.slot.page === null, true); assert.equal(h.body().includes("PRIVATE"), false); assert.equal(h.body().includes(item.quote), false);
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
  h.dialog.close(); await flush(); assert.equal(h.ui.slot === null, true);
  assert.ok(signals.every((signal) => signal.aborted)); assert.equal(slot.content.textContent, ""); assert.equal(slot.item, null);
  source.resolve(sourcePage(item, "LATE_PRIVATE")); history.resolve({ items: [{ snapshot: { content: "LATE_HISTORY" } }] }); await flush();
  assert.equal(h.body(), ""); assert.equal(slot.sourceRegion.textContent, ""); assert.equal(slot.historyList.textContent, "");
});

test("changing management view or pagehide releases private evidence and pending reads", async () => {
  for (const leave of [(h) => h.ui.switchView("chat"), (h) => h.windowEvents.pagehide()]) {
    const source = deferred(), item = fixtureMemory();
    const h = harness(async (path) => path.includes("/source?") ? source.promise : item);
    await h.ui.showMemoryEvidence(item.id); await h.expand("查看原文"); const signal = h.requests.at(-1).signal;
    await leave(h); assert.equal(h.ui.slot === null, true); assert.equal(signal.aborted, true); assert.equal(h.body(), "");
    source.resolve(sourcePage(item, "LATE_PRIVATE")); await flush(); assert.equal(h.body(), "");
  }
});

test("401 clears the modal immediately and late source data cannot reopen it", async () => {
  const source = deferred(), history = deferred(), item = fixtureMemory();
  const h = harness(async (path) => path.includes("/source?") ? source.promise : path.endsWith("/revisions") ? history.promise : item);
  await h.ui.showMemoryEvidence(item.id); await h.expand("查看原文"); await h.expand("修改历史");
  history.reject(new ApiError("请登录", 401)); await flush();
  assert.equal(h.ui.slot === null, true); assert.equal(h.dialog.open, false); assert.equal(h.ui.state.auth.authenticated, false); assert.equal(h.body(), "");
  assert.equal(h.document.activeElement === h.document.querySelector("#login-password"), true);
  source.resolve(sourcePage(item, "LATE_PRIVATE")); await flush(); assert.equal(h.body(), "");
});

test("source-level forgetting clears all currently displayed evidence and aborts a pending next page", async () => {
  const item = fixtureMemory(), next = deferred();
  const h = harness(async (path) => !path.includes("/source?") ? item : path.includes("offset_bytes=0") ? sourcePage(item, "PRIVATE", 0, 10) : next.promise);
  await h.ui.showMemoryEvidence(item.id); await h.expand("查看原文"); await h.button("下一段").dispatch("click");
  const signal = h.requests.at(-1).signal;
  h.ui.startGlobalEvents();
  h.emitGlobal({ type: "memory.forgotten", data: { memory_ids: ["another-memory"], source_id: item.source_id } });
  assert.equal(signal.aborted, true); assert.equal(h.ui.slot.page === null, true); assert.equal(h.body().includes("PRIVATE"), false); assert.equal(h.body().includes(item.quote), false);
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

for (const type of ["memory.updated", "memory.forgotten"]) {
  test(`${type} invalidates an unknown source while a graph-only memory detail is pending`, async () => {
    const detail = deferred(), item = fixtureMemory();
    const h = harness(async () => detail.promise);
    assert.equal(h.ui.state.memories.length, 0);
    const pending = h.ui.showMemoryEvidence(item.id);
    h.ui.startGlobalEvents();
    h.emitGlobal({ type, data: { memory_ids: ["another-memory"], source_id: item.source_id } });
    assert.equal(h.requests[0].signal.aborted, true);
    assert.ok(h.button("重新核查"));
    detail.resolve(item); await pending;
    assert.equal(h.body().includes(item.content), false); assert.equal(h.body().includes(item.quote), false);
    assert.equal(h.requests.length, 1); h.ui.stopEvents();
  });
}

test("a known unrelated or absent source does not invalidate a pending memory detail", async () => {
  for (const sourceId of ["different-source", null]) {
    const detail = deferred(), item = fixtureMemory("memory-one", { source_id: sourceId, origin: null });
    const h = harness(async () => detail.promise); h.ui.state.memories = [item];
    const pending = h.ui.showMemoryEvidence(item.id);
    for (const type of ["memory.updated", "memory.forgotten"]) h.ui.invalidateMemoryEvidence({ type, data: { memory_ids: ["another-memory"], source_id: "source-one" } });
    assert.equal(h.requests[0].signal.aborted, false);
    detail.resolve(item); await pending; assert.ok(h.body().includes(item.content));
  }
});

test("an unrelated id-only update does not invalidate an unresolved source", async () => {
  const detail = deferred(), item = fixtureMemory(), h = harness(async () => detail.promise);
  const pending = h.ui.showMemoryEvidence(item.id);
  h.ui.invalidateMemoryEvidence({ type: "memory.updated", data: { memory_ids: ["another-memory"], source_id: null } });
  assert.equal(h.requests[0].signal.aborted, false);
  detail.resolve(item); await pending; assert.ok(h.body().includes(item.content));
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

test("an own-save event before the response permits a fresh check and returns focus to its heading", async () => {
  const item = fixtureMemory(), updated = { ...item, content: "OWN_SAVED", version: 2 }; let saved = false, h;
  h = harness(async (_path, options) => {
    if (options.method === "PATCH") {
      saved = true;
      h.ui.invalidateMemoryEvidence({ type: "memory.updated", data: { memory_ids: [item.id], source_id: item.source_id } });
      return updated;
    }
    return saved ? updated : item;
  });
  await h.ui.editMemory(item.id);
  const input = h.dialog.querySelector("textarea"); input.value = updated.content;
  assert.equal(h.document.activeElement === input, true);
  await h.form.dispatch("submit");
  assert.equal(h.dialog.open, true); assert.ok(h.body().includes(updated.content)); assert.equal(h.ui.slot.invalidated, false);
  assert.equal(h.requests.filter((entry) => entry.method === "PATCH").length, 1);
  const heading = h.document.querySelector("#dialog-title");
  assert.equal(h.document.activeElement === heading, true); assert.equal(heading.tabIndex, -1);
});

test("ordinary asynchronous detail and source reads keep the reader's current focus", async () => {
  const detail = deferred(), source = deferred(), item = fixtureMemory();
  const h = harness(async (path) => path.includes("/source?") ? source.promise : detail.promise);
  const pending = h.ui.showMemoryEvidence(item.id), close = h.document.querySelector("#dialog-close");
  close.focus(); detail.resolve(item); await pending;
  assert.equal(h.document.activeElement === close, true);
  await h.expand("查看原文"); close.focus();
  source.resolve(sourcePage(item, "原文")); await flush();
  assert.equal(h.document.activeElement === close, true);
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
  assert.equal(h.body(), ""); assert.equal(h.ui.slot === null, true); assert.equal(h.requests.length, 2);
});

test("closing after the underlying list rerenders restores the same memory check button", async () => {
  const item = fixtureMemory(), h = harness(async () => item); h.ui.state.memories = [item];
  h.other.replaceChildren(h.ui.renderMemories()); h.other.querySelector("[data-action=memory-evidence]").focus();
  await h.ui.showMemoryEvidence(item.id); h.other.replaceChildren(h.ui.renderMemories());
  const currentButton = h.other.querySelector("[data-action=memory-evidence]");
  h.dialog.close(); await flush(); assert.equal(h.document.activeElement === currentButton, true);
});

test("a late source cannot overwrite another modal, including its heading and actions", async () => {
  const source = deferred(), item = fixtureMemory();
  const h = harness(async (path) => path.includes("/source?") ? source.promise : item);
  await h.ui.showMemoryEvidence(item.id); await h.expand("查看原文"); const signal = h.requests.at(-1).signal;
  const content = h.document.createElement("p"); content.textContent = "其他内容";
  h.ui.openDialog({ title: "其他设置", content, readOnly: true });
  source.resolve(sourcePage(item, "LATE_PRIVATE")); await flush();
  assert.equal(signal.aborted, true); assert.equal(h.ui.slot === null, true); assert.equal(h.body(), "其他内容");
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
  await flush(); assert.equal(h.requests.length, 2); assert.equal(h.ui.slot.page === null, true);
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

function graphMemory(id = "graph-result") {
  return fixtureMemory(id, { matched_by: "graph", graph_via: {
    revision: 7, seed_id: "entity-muli", complete: true,
    seed: { entity_id: "entity-muli", name: "MuLi", canonical: "muli", alias: "Lumi", kind: "source", memory_id: "alias-memory", source_id: "alias-source", has_more: true },
    steps: [
      { memory_id: "first-edge-memory", source_id: "first-edge-source", subject_id: "entity-muli", subject: "MuLi", predicate: "维护", object_id: "entity-project", object: "项目甲", from_id: "entity-muli", to_id: "entity-project", direction: "forward" },
      { memory_id: id, source_id: "source-one", subject_id: "entity-project", subject: "项目甲", predicate: "使用", object_id: "entity-tool", object: "工具乙", from_id: "entity-project", to_id: "entity-tool", direction: "forward" },
    ],
  } });
}

function mountGraphResults(h, items, query = "Lumi") {
  h.ui.useMemoryViews(); h.ui.state.memories = items; h.ui.state.memoryQuery = query;
  h.other.replaceChildren(h.ui.renderMemories());
  return h.other.querySelector("[data-memory-path]");
}

test("fresh graph evidence stays in the chosen source and a scope change aborts the reader", async () => {
  const source = catalogSource({ source_id: "source-one" }), item = graphMemory(), pending = deferred();
  item.graph_via.seed.source_id = source.source_id;
  for (const step of item.graph_via.steps) step.source_id = source.source_id;
  let queries = 0;
  const h = harness(async (path) => {
    if (path.startsWith("/memory/sources?")) return catalogPage([source]);
    if (path.startsWith("/memory/query?")) {
      const params = new URLSearchParams(path.split("?")[1]);
      assert.equal(params.get("source_id"), source.source_id);
      queries += 1;
      return queries <= 2 ? { source_ids: [source.source_id], memories: [item] } : pending.promise;
    }
    assert.equal(path, "/memories?query=Lumi"); return { items: [] };
  });
  await openCatalog(h); await clickMemoryButton(h, h.button("在此来源内查找", h.other));
  await h.ui.searchMemories("Lumi");
  await clickMemoryButton(h, h.other.querySelector("[data-memory-path]"));
  const signal = h.requests.at(-1).signal;
  assert.equal(queries, 3); assert.ok(h.ui.path);
  await clickMemoryButton(h, h.button("查全库", h.other));
  assert.equal(signal.aborted, true); assert.equal(h.ui.path === null, true);
  pending.resolve({ source_ids: [source.source_id], memories: [item] }); await flush();
  assert.equal(h.ui.path === null, true); assert.equal(h.other.textContent.includes(item.content), false);
});

async function clickMemoryButton(h, button) {
  assert.ok(button instanceof DOMButton);
  await button.dispatch("click");
  for (const handler of h.document.events.click ?? []) await handler({ target: button, preventDefault() {} });
  await flush();
}

test("DOM replacement keeps primitive text values rather than filtering boolean or nullish children", () => {
  const h = harness(), root = h.document.createElement("div");
  // Native DOM converts non-Node arguments to text; this substitute must not hide them.
  root.replaceChildren(false, true, null, undefined, 0, "");
  assert.deepEqual(root.childNodes.map((node) => node.textContent), ["false", "true", "null", "undefined", "0", ""]);
  assert.ok(root.childNodes.every((node) => node instanceof TextNode));
  root.replaceChildren(); assert.equal(root.childNodes.length, 0);
});

function comparisonChange(source, offset, content) {
  return { offset_bytes: offset, length_bytes: Buffer.byteLength(content), content, read_arguments: { id: source.source_id, revision: source.revision, offset_bytes: offset, ...(content ? { length_bytes: Buffer.byteLength(content) } : {}) } };
}

function comparisonOriginalPage(source, content, offset = 0) {
  const length = Buffer.byteLength(content);
  return { source_id: source.source_id, revision: source.revision, field: "content", content, offset_bytes: offset, length_bytes: length, next_offset_bytes: offset + length, total_bytes: source.total_bytes, has_more: offset + length < source.total_bytes };
}

function comparisonFixture() {
  const left = catalogSource({ source_id: `attachment:${"a".repeat(32)}`, session_id: "", total_bytes: 8 });
  const right = catalogSource({ source_id: `attachment:${"b".repeat(32)}`, session_id: "", revision: "b".repeat(64), total_bytes: 12 });
  const identity = (source) => ({ ...source, attachment_id: source.source_id.slice(11), format: "text", scope: "utf8_text", source_revision: "c".repeat(64), document_revision: null });
  const header = { status: "changed", algorithm: "line_sequence_matcher_v1", left: identity(left), right: identity(right) };
  return { left, right, first: { ...header, items: [{ kind: "replace", left: comparisonChange(left, 0, "甲\n"), right: comparisonChange(right, 0, "乙\n") }], has_more: true, next_cursor: "signed-next" }, last: { ...header, items: [{ kind: "insert", left: comparisonChange(left, 8, ""), right: comparisonChange(right, 8, "新\n") }], has_more: false, next_cursor: null } };
}

function comparisonContextFixture() {
  const f = comparisonFixture(), prefix = "前🙂\0\n", tail = "<script>尾\n</script>", offset = Buffer.byteLength(prefix);
  for (const side of ["left", "right"]) {
    const change = f.first.items[0][side].content;
    f[side].total_bytes = Buffer.byteLength(prefix + change + tail);
    f.first[side].total_bytes = f[side].total_bytes;
    f.first.items[0][side] = comparisonChange(f[side], offset, change);
  }
  f.first.has_more = false; f.first.next_cursor = null;
  return { ...f, prefix, tail, offset };
}

async function beginComparison(h) {
  await openCatalog(h);
  await clickMemoryButton(h, h.other.querySelectorAll("button").filter((button) => button.textContent === "选作比较左侧")[0]);
  await clickMemoryButton(h, h.other.querySelectorAll("button").filter((button) => button.textContent === "与所选比较")[1]);
}

test("explicit source comparison keeps ordered revisions and advances complete diff pages", async () => {
  const f = comparisonFixture();
  const h = harness(async (path, options) => {
    if (path.startsWith("/memory/sources?")) return catalogPage([f.left, f.right]);
    assert.equal(path, "/memory/sources/compare");
    assert.equal(options.method, "POST");
    assert.deepEqual({ ...options.body }, { left_id: f.left.source_id, left_revision: f.left.revision, right_id: f.right.source_id, right_revision: f.right.revision, ...(options.body.cursor ? { cursor: "signed-next" } : {}) });
    return options.body.cursor ? f.last : f.first;
  });
  await beginComparison(h);
  assert.ok(h.body().includes("仍有后续差异")); assert.ok(h.body().includes("甲\n"));
  assert.ok(h.button("核对左侧原文")); assert.ok(h.button("核对右侧原文"));
  const oldRead = h.button("核对左侧此处原文"), oldNext = h.button("继续核对差异");
  await clickMemoryButton(h, h.button("继续核对差异"));
  assert.ok(h.body().includes("已到末尾")); assert.ok(h.body().includes("新增")); assert.ok(h.body().includes("新\n"));
  assert.equal(h.requests.filter((request) => request.path === "/memory/sources/compare").length, 2);
  assert.equal(h.button("继续核对差异"), undefined);
  const count = h.requests.length;
  await clickMemoryButton(h, oldRead); await clickMemoryButton(h, oldNext);
  assert.equal(h.requests.length, count); assert.ok(h.body().includes("新\n"));
});

for (const side of ["left", "right"]) test(`a ${side} difference opens its UTF-8 byte position and reuses bounded original paging`, async () => {
  const f = comparisonContextFixture(), change = f.first.items[0][side], source = f[side];
  const h = harness(async (path) => {
    if (path.startsWith("/memory/sources?")) return catalogPage([f.left, f.right]);
    if (path === "/memory/sources/compare") return f.first;
    assert.ok(path.startsWith("/memory/source?"));
    const query = new URLSearchParams(path.split("?")[1]), offset = Number(query.get("offset_bytes"));
    assert.equal(query.get("id"), source.source_id); assert.equal(query.get("revision"), source.revision);
    assert.equal(query.has("length_bytes"), false);
    assert.ok([0, f.offset, f.offset + change.length_bytes].includes(offset));
    const content = offset === 0 ? f.prefix : offset === f.offset ? change.content : f.tail;
    return comparisonOriginalPage(source, content, offset);
  });
  await beginComparison(h);
  const catalogTrigger = h.ui.slot.catalogTrigger;
  await clickMemoryButton(h, h.button(`核对${side === "left" ? "左" : "右"}侧此处原文`));
  assert.equal(Number(new URLSearchParams(h.requests.at(-1).path.split("?")[1]).get("offset_bytes")), f.offset);
  assert.ok(h.body().includes(change.content)); assert.ok(h.body().includes("从变化位置起 · 第 1 段"));
  assert.equal(h.button("上一段").disabled, true); assert.deepEqual([...h.ui.slot.offsets], [f.offset]);
  const oldNext = h.button("下一段");
  await clickMemoryButton(h, oldNext);
  assert.ok(h.body().includes(f.tail)); assert.equal(h.body().includes(change.content), false);
  assert.equal(h.dialog.querySelectorAll("script").length, 0);
  const count = h.requests.length; await clickMemoryButton(h, oldNext); assert.equal(h.requests.length, count);
  await clickMemoryButton(h, h.button("上一段"));
  assert.ok(h.body().includes(change.content)); assert.equal(h.body().includes(f.tail), false);
  await clickMemoryButton(h, h.button("从头阅读"));
  assert.ok(h.body().includes(f.prefix)); assert.equal(h.body().includes(change.content), false);
  assert.deepEqual([...h.ui.slot.offsets], [0]); assert.equal(h.button("从头阅读"), undefined);
  assert.equal(new URLSearchParams(h.requests.at(-1).path.split("?")[1]).get("offset_bytes"), "0");
  h.dialog.close(); await flush();
  assert.equal(h.document.activeElement === catalogTrigger, true);
  assert.equal(h.requests.some((entry) => entry.path.includes("/sessions/")), false);
});

test("returning from an original rereads the same diff cursor and restores its entry then the catalog trigger", async () => {
  const f = comparisonFixture(), returned = deferred(); let comparisons = 0;
  const h = harness(async (path, options) => {
    if (path.startsWith("/memory/sources?")) return catalogPage([f.left, f.right]);
    if (path === "/memory/sources/compare") {
      comparisons++;
      if (comparisons > 1) assert.equal(options.body.cursor, "signed-next");
      return comparisons === 1 ? f.first : comparisons === 2 ? f.last : returned.promise;
    }
    const query = new URLSearchParams(path.split("?")[1]);
    assert.equal(query.get("id"), f.right.source_id); assert.equal(query.get("revision"), f.right.revision);
    assert.equal(query.get("offset_bytes"), "8");
    return comparisonOriginalPage(f.right, "新\n", 8);
  });
  await beginComparison(h); const catalogTrigger = h.ui.slot.catalogTrigger;
  await clickMemoryButton(h, h.button("继续核对差异"));
  const oldSlot = h.ui.slot, oldEntry = h.button("核对右侧此处原文");
  await clickMemoryButton(h, oldEntry);
  assert.equal(oldSlot.content.textContent, ""); assert.equal(oldSlot.page, null);
  const sourceSlot = h.ui.slot, back = h.button("返回这段差异");
  assert.deepEqual(Object.keys(sourceSlot.comparisonReturn).sort(), ["cursor", "focus", "pair", "trigger"]);
  await clickMemoryButton(h, back);
  assert.equal(comparisons, 3); assert.equal(h.body().includes("新\n"), false);
  assert.equal(sourceSlot.content.textContent, ""); assert.equal(sourceSlot.comparisonReturn, null);
  const request = h.requests.at(-1);
  assert.deepEqual({ ...request.body }, { left_id: f.left.source_id, left_revision: f.left.revision, right_id: f.right.source_id, right_revision: f.right.revision, cursor: "signed-next" });
  returned.resolve(f.last); await flush();
  assert.ok(h.body().includes("新\n")); assert.equal(h.document.activeElement === h.button("核对右侧此处原文"), true);
  assert.equal(h.document.activeElement === oldEntry, false);
  const count = h.requests.length; await clickMemoryButton(h, back); await clickMemoryButton(h, oldEntry);
  assert.equal(h.requests.length, count);
  h.dialog.close(); await flush();
  assert.equal(h.document.activeElement === catalogTrigger, true);
});

for (const [name, side, offset, total] of [["insert position", "left", 4, 8], ["delete position", "right", 4, 12], ["EOF", "left", 8, 8], ["empty source", "left", 0, 0]]) test(`an empty comparison side at ${name} is an anchor rather than invented original text`, async () => {
  const f = comparisonFixture(), other = side === "left" ? "right" : "left";
  f[side].total_bytes = total; f.first[side].total_bytes = total;
  if (!total) { f[other].total_bytes = 4; f.first[other].total_bytes = 4; }
  f.first.items = [{ kind: side === "left" ? "insert" : "delete", [side]: comparisonChange(f[side], offset, ""), [other]: comparisonChange(f[other], offset, "新\n") }];
  f.first.has_more = false; f.first.next_cursor = null;
  const h = harness(async (path) => {
    if (path.startsWith("/memory/sources?")) return catalogPage([f.left, f.right]);
    if (path === "/memory/sources/compare") return f.first;
    const query = new URLSearchParams(path.split("?")[1]), start = Number(query.get("offset_bytes"));
    assert.equal(query.get("id"), f[side].source_id); assert.equal(query.get("revision"), f[side].revision);
    assert.equal(query.has("length_bytes"), false);
    assert.ok(start === offset && offset < total || start === 0);
    return comparisonOriginalPage(f[side], start === 0 ? "前\n" : "尾\n".repeat((total - offset) / 4), start);
  });
  await beginComparison(h); const count = h.requests.length;
  await clickMemoryButton(h, h.button(`核对${side === "left" ? "左" : "右"}侧此处原文`));
  if (offset === total) {
    assert.equal(h.requests.length, count); assert.equal(h.ui.slot.page, null);
    assert.ok(h.body().includes(total ? "变化位置在原文末尾" : "此侧正文为空"));
    assert.equal(h.body().includes("第 1 段"), false);
  } else {
    assert.equal(h.requests.length, count + 1); assert.ok(h.body().includes("此侧没有对应正文"));
    assert.equal(h.ui.slot.page.offset_bytes, offset); assert.ok(h.body().includes("尾\n"));
  }
  assert.ok(h.button("返回这段差异"));
  if (offset) {
    await clickMemoryButton(h, h.button("从头阅读"));
    assert.equal(h.ui.slot.page.offset_bytes, 0); assert.ok(h.body().includes("前\n"));
    assert.equal(h.body().includes("变化位置在原文末尾"), false);
  } else assert.equal(h.button("从头阅读"), undefined);
});

for (const forgotten of ["left", "right"]) test(`forgetting the ${forgotten} source while reading the other invalidates the original and return path`, async () => {
  const f = comparisonContextFixture(), side = forgotten === "left" ? "right" : "left", next = deferred(), change = f.first.items[0][side];
  const h = harness(async (path) => {
    if (path.startsWith("/memory/sources?")) return catalogPage([f.left, f.right]);
    if (path === "/memory/sources/compare") return f.first;
    const offset = Number(new URLSearchParams(path.split("?")[1]).get("offset_bytes"));
    return offset === f.offset ? comparisonOriginalPage(f[side], change.content, offset) : next.promise;
  });
  await beginComparison(h);
  const oldEntry = h.button(`核对${side === "left" ? "左" : "右"}侧此处原文`);
  await clickMemoryButton(h, oldEntry);
  const back = h.button("返回这段差异"), start = h.button("从头阅读");
  assert.deepEqual([...h.ui.slot.comparisonSources], [f.left.source_id, f.right.source_id]);
  await clickMemoryButton(h, h.button("下一段")); const pending = h.requests.at(-1);
  h.ui.startGlobalEvents();
  h.emitGlobal({ type: "memory.forgotten", data: { source_id: f[forgotten].source_id } });
  assert.equal(pending.signal.aborted, true); assert.equal(h.ui.slot.page, null); assert.equal(h.ui.slot.comparisonReturn, null);
  assert.equal(h.body().includes(change.content), false); assert.equal(h.button("返回这段差异"), undefined);
  const count = h.requests.length;
  await clickMemoryButton(h, back); await clickMemoryButton(h, start); await clickMemoryButton(h, oldEntry);
  assert.equal(h.requests.length, count);
  next.resolve(comparisonOriginalPage(f[side], f.tail, f.offset + change.length_bytes)); await flush();
  assert.equal(h.body().includes(f.tail), false); assert.equal(h.button("返回这段差异"), undefined);
  h.ui.stopEvents();
});

for (const status of [404, 409]) test(`a positioned original ${status} clears its comparison return context`, async () => {
  const f = comparisonContextFixture();
  const h = harness(async (path) => {
    if (path.startsWith("/memory/sources?")) return catalogPage([f.left, f.right]);
    if (path === "/memory/sources/compare") return f.first;
    throw new ApiError("原文已变化", status);
  });
  await beginComparison(h); const entry = h.button("核对左侧此处原文");
  await clickMemoryButton(h, entry);
  assert.equal(h.ui.slot.page, null); assert.equal(h.ui.slot.comparisonReturn, null);
  assert.equal(h.button("返回这段差异"), undefined); assert.equal(h.body().includes("甲\n"), false);
  assert.ok(h.body().includes(status === 404 ? "原文已不可用" : "原文已更新"));
  const count = h.requests.length; await clickMemoryButton(h, entry); assert.equal(h.requests.length, count);
  assert.equal(h.requests.some((request) => request.path.includes("/sessions/")), false);
});

test("comparison rejects incomplete source identity before displaying its body", async () => {
  const f = comparisonFixture(), malformed = structuredClone(f.first);
  delete malformed.left.source_revision;
  malformed.items[0].left.content = "LEAK";
  malformed.items[0].left.length_bytes = 4;
  const h = harness(async (path) => path.startsWith("/memory/sources?") ? catalogPage([f.left, f.right]) : malformed);
  await beginComparison(h);
  assert.ok(h.body().includes("响应不完整")); assert.equal(h.body().includes("LEAK"), false);
});

for (const action of ["close", "forget-left", "forget-right"]) test(`comparison ${action} aborts a pending request and discards its late result`, async () => {
  const f = comparisonFixture(), pending = deferred();
  const h = harness(async (path) => path.startsWith("/memory/sources?") ? catalogPage([f.left, f.right]) : pending.promise);
  await beginComparison(h);
  const request = h.requests.at(-1);
  if (action === "close") h.dialog.close();
  else h.ui.invalidateMemoryEvidence({ data: { source_id: action === "forget-left" ? f.left.source_id : f.right.source_id } });
  await flush(); assert.equal(request.signal.aborted, true);
  pending.resolve(f.first); await flush();
  assert.equal(h.body().includes("甲\n"), false); assert.equal(h.button("继续核对差异"), undefined);
});

test("graph path renders only nodes for complete results and adds one note for truncated results", async () => {
  for (const truncated of [false, undefined, null, true]) {
    const item = graphMemory(), h = harness(async () => ({ items: [item], truncated }));
    await clickMemoryButton(h, mountGraphResults(h, [item]));
    const region = h.ui.path.region;
    assert.ok(region.childNodes.every((node) => node instanceof DOMElement), `unexpected text node with truncated=${truncated}`);
    assert.equal(region.childNodes.length, truncated === true ? 4 : 3);
    assert.equal(region.textContent.includes("部分检索结果"), truncated === true);
  }
});

test("only complete graph results expose a real disclosure, while whole-path omissions are explicit", () => {
  const valid = graphMemory(), omitted = { ...graphMemory("omitted"), path_omitted: true }, old = fixtureMemory("old", { matched_by: "graph" });
  const invalid = graphMemory("invalid"); invalid.graph_via.complete = false;
  const text = fixtureMemory("text", { matched_by: "lexical" }), h = harness();
  mountGraphResults(h, [valid, omitted, old, invalid, text]);
  const toggles = h.other.querySelectorAll("[data-memory-path]");
  assert.equal(toggles.length, 1); assert.equal(toggles[0].dataset.memoryPath, valid.id);
  assert.equal(toggles[0].getAttribute("aria-expanded"), "false");
  const region = h.other.querySelector(`#${toggles[0].getAttribute("aria-controls")}`);
  assert.equal(region.hidden, true); assert.equal(region.textContent, "");
  assert.ok(h.other.textContent.includes("召回路径因预算省略")); assert.ok(h.other.textContent.includes("文本匹配")); assert.equal(h.requests.length, 0);
});

test("opening a graph path freshly reads the original query once and exposes exact source references", async () => {
  const item = graphMemory(), h = harness(async () => ({ items: [item], revision: 7, truncated: true }));
  const toggle = mountGraphResults(h, [item], "Lumi / 中文");
  await clickMemoryButton(h, toggle);
  assert.equal(h.requests.length, 1); assert.equal(h.requests[0].path, "/memories?query=Lumi%20%2F%20%E4%B8%AD%E6%96%87");
  assert.equal(h.requests[0].cache, "no-store"); assert.equal(toggle.getAttribute("aria-expanded"), "true");
  const region = h.ui.path.region;
  for (const text of ["本次检索路径", "Lumi → MuLi", "原始登记", "MuLi —维护→ 项目甲", "项目甲 —使用→ 工具乙", "还有登记未展示", "部分检索结果"]) assert.ok(region.textContent.includes(text), text);
  assert.equal(region.textContent.includes("多来源"), false);
  assert.deepEqual(region.querySelectorAll("[data-memory-path-ref]").map((button) => button.dataset.id), ["alias-memory", "first-edge-memory", item.id]);
});

test("reverse traversal preserves predicate direction rather than inventing forward relations", async () => {
  const item = graphMemory();
  item.graph_via.steps[0] = { ...item.graph_via.steps[0], subject_id: "entity-project", subject: "项目甲", object_id: "entity-muli", object: "MuLi", direction: "reverse" };
  const h = harness(async () => ({ items: [item] }));
  await clickMemoryButton(h, mountGraphResults(h, [item]));
  assert.ok(h.ui.path.region.textContent.includes("MuLi ←维护— 项目甲"));
  assert.equal(h.ui.path.region.textContent.includes("MuLi —维护→ 项目甲"), false);
});

test("manual, direct and legacy seeds keep their real evidence limits and never fabricate source links", async () => {
  for (const [kind, label, canCheck] of [["manual", "手工登记", true], ["direct", "规范名称", false], ["legacy_unknown", "旧索引，依据未登记", false]]) {
    const item = graphMemory(); Object.assign(item.graph_via.seed, { kind, source_id: null, memory_id: canCheck ? "manual-memory" : null, alias: kind === "direct" ? null : "Lumi", has_more: false });
    const h = harness(async () => ({ items: [item] })); await clickMemoryButton(h, mountGraphResults(h, [item]));
    const region = h.ui.path.region;
    assert.ok(region.textContent.includes(label)); assert.equal(!!region.querySelector('[data-memory-path-ref="seed"]'), canCheck);
    assert.equal(region.textContent.includes("主人来源"), false); assert.equal(h.requests.length, 1);
  }
});

const badPaths = [
  ["incomplete", (item) => { item.graph_via.complete = false; }],
  ["seed mismatch", (item) => { item.graph_via.seed.entity_id = "wrong-seed"; }],
  ["disconnected step", (item) => { item.graph_via.steps[1].from_id = "wrong-start"; }],
  ["predicate direction mismatch", (item) => { item.graph_via.steps[0].direction = "reverse"; }],
  ["wrong final memory", (item) => { item.graph_via.steps[1].memory_id = "another-fact"; }],
  ["missing display name", (item) => { delete item.graph_via.steps[1].object; }],
  ["empty path", (item) => { item.graph_via.steps = []; }],
];
for (const [label, mutate] of badPaths) {
  test(`a fresh ${label} path is rejected without displaying a fabricated chain`, async () => {
    const item = graphMemory(), invalid = graphMemory(); mutate(invalid);
    const h = harness(async () => ({ items: [invalid] })); await clickMemoryButton(h, mountGraphResults(h, [item]));
    assert.equal(h.ui.path.via === null, true); assert.equal(h.ui.path.region.querySelectorAll("[data-memory-path-ref]").length, 0);
    assert.ok(h.ui.path.region.textContent.includes("当前未返回该路径")); assert.equal(h.requests.length, 1);
  });
}

test("fresh omitted, missing, lexical-only and mismatched-revision results never reuse a cached path", async () => {
  const item = graphMemory();
  for (const payload of [{ items: [{ ...item, path_omitted: true }] }, { items: [] }, { items: [fixtureMemory(item.id, { matched_by: "lexical" })] }, { items: [item], revision: 8 }]) {
    const h = harness(async () => payload); await clickMemoryButton(h, mountGraphResults(h, [item]));
    assert.equal(h.ui.path.via === null, true); assert.equal(h.ui.path.region.querySelectorAll("[data-memory-path-ref]").length, 0);
    assert.ok(h.ui.path.region.textContent.includes(payload.items[0]?.path_omitted ? "因预算省略" : "当前未返回该路径")); assert.equal(h.requests.length, 1);
  }
});

test("collapsing a pending path aborts its read and a late response cannot reopen it", async () => {
  const page = deferred(), item = graphMemory(), h = harness(async () => page.promise);
  const toggle = mountGraphResults(h, [item]); await clickMemoryButton(h, toggle);
  const region = h.ui.path.region, signal = h.requests[0].signal;
  await clickMemoryButton(h, toggle);
  assert.equal(h.ui.path === null, true); assert.equal(signal.aborted, true); assert.equal(region.hidden, true); assert.equal(region.textContent, "");
  page.resolve({ items: [item] }); await flush();
  assert.equal(region.textContent, ""); assert.equal(toggle.getAttribute("aria-expanded"), "false"); assert.equal(h.document.activeElement === toggle, true);
});

test("opening another result keeps one path and ignores the first late response", async () => {
  const page = deferred(), first = graphMemory("first-result"), second = graphMemory("second-result"); let calls = 0;
  const h = harness(async () => ++calls === 1 ? page.promise : { items: [first, second] });
  mountGraphResults(h, [first, second]); const toggles = h.other.querySelectorAll("[data-memory-path]");
  await clickMemoryButton(h, toggles[0]); const oldRegion = h.ui.path.region;
  await clickMemoryButton(h, toggles[1]); assert.equal(h.requests[0].signal.aborted, true);
  page.resolve({ items: [first] }); await flush();
  assert.equal(h.ui.path.id, second.id); assert.equal(oldRegion.textContent, ""); assert.equal(oldRegion.hidden, true);
  assert.equal(h.ui.path.region.querySelector('[data-memory-path-ref="step-1"]').dataset.id, second.id);
});

for (const type of ["memory.updated", "memory.forgotten"]) {
  test(`${type} invalidates an in-flight graph path even when the changed fact is not its final memory`, async () => {
    const page = deferred(), item = graphMemory(), h = harness(async () => page.promise);
    await clickMemoryButton(h, mountGraphResults(h, [item])); const region = h.ui.path.region;
    h.ui.startGlobalEvents(); h.emitGlobal({ type, data: { memory_ids: ["not-the-final-memory"], source_id: "any-source" } });
    assert.equal(h.requests[0].signal.aborted, true); assert.equal(h.ui.path.via === null, true); assert.ok(region.textContent.includes("关系已有变更"));
    page.resolve({ items: [item] }); await flush(); assert.equal(region.querySelectorAll("[data-memory-path-ref]").length, 0); assert.equal(h.requests.length, 1); h.ui.stopEvents();
  });
}

test("a forgotten contribution is replaced only by an explicit fresh path read", async () => {
  const original = graphMemory(), replacement = graphMemory(); replacement.graph_via.revision = 8; replacement.graph_via.seed.memory_id = "remaining-alias-memory"; let calls = 0;
  const h = harness(async () => ({ items: [++calls === 1 ? original : replacement] }));
  await clickMemoryButton(h, mountGraphResults(h, [original])); h.ui.startGlobalEvents();
  h.emitGlobal({ type: "memory.forgotten", data: { memory_ids: ["alias-memory"], source_id: "alias-source" } });
  assert.equal(h.ui.path.region.querySelectorAll("[data-memory-path-ref]").length, 0); assert.equal(h.requests.length, 1);
  await clickMemoryButton(h, h.button("重新检索", h.other));
  assert.equal(h.ui.path.region.querySelector('[data-memory-path-ref="seed"]').dataset.id, "remaining-alias-memory"); assert.equal(h.requests.length, 2); h.ui.stopEvents();
});

test("a graph change clears its path without interrupting an unrelated original source or correction draft", async () => {
  const item = graphMemory(), proof = fixtureMemory("alias-memory", { source_id: "unrelated-source" });
  const h = harness(async (path) => path.startsWith("/memories?") ? { items: [item] } : proof);
  await clickMemoryButton(h, mountGraphResults(h, [item]));
  await clickMemoryButton(h, h.other.querySelector('[data-memory-path-ref="seed"]'));
  assert.ok(h.body().includes(proof.content)); h.ui.startGlobalEvents();
  h.emitGlobal({ type: "memory.updated", data: { memory_ids: ["different-memory"], source_id: "different-source" } });
  assert.equal(h.ui.path.via === null, true); assert.ok(h.body().includes(proof.content));
  h.dialog.close(); await flush(); assert.equal(h.document.activeElement === h.ui.path.button, true);
  await h.ui.editMemory(proof.id); h.dialog.querySelector("textarea").value = "未保存的要求";
  h.emitGlobal({ type: "memory.forgotten", data: { memory_ids: ["another-memory"], source_id: "another-source" } });
  assert.equal(h.dialog.querySelector("textarea").value, "未保存的要求"); assert.equal(h.ui.slot.invalidated, false); h.ui.stopEvents();
});

test("repeated memory references return focus to the exact step rather than the first matching memory", async () => {
  const item = graphMemory(); item.graph_via.steps[0].memory_id = item.id;
  const h = harness(async (path) => path.startsWith("/memories?") ? { items: [item] } : item);
  await clickMemoryButton(h, mountGraphResults(h, [item]));
  const second = h.other.querySelector('[data-memory-path-ref="step-1"]');
  await clickMemoryButton(h, second); h.dialog.close(); await flush();
  assert.equal(h.document.activeElement === second, true);
});

test("a changed query cancels its path and old search responses cannot replace the latest result", async () => {
  const path = deferred(), old = deferred(), latest = fixtureMemory("latest", { content: "最新结果" }), item = graphMemory();
  const h = harness(async (url) => url.endsWith("Lumi") ? path.promise : url.endsWith("old") ? old.promise : { items: [latest] });
  await clickMemoryButton(h, mountGraphResults(h, [item])); const region = h.ui.path.region;
  const input = h.other.querySelector("#memory-search"), form = h.other.querySelector("form"); input.value = "old";
  const older = form.dispatch("submit"); assert.equal(h.requests[0].signal.aborted, true); assert.equal(h.ui.path === null, true);
  await h.ui.searchMemories("new"); assert.equal(h.requests[1].signal.aborted, true);
  path.resolve({ items: [item] }); old.resolve({ items: [item] }); await older; await flush();
  assert.equal(h.ui.state.memoryQuery, "new"); assert.equal(h.ui.state.memories[0].id, latest.id); assert.ok(h.other.textContent.includes(latest.content)); assert.equal(region.textContent, "");
});

test("view/tab changes, rerenders and pagehide clear pending path data without allowing late revival", async () => {
  for (const leave of [
    (h) => h.ui.switchView("chat"),
    (h) => clickMemoryButton(h, h.other.querySelector('[data-memory-tab="graph"]')),
    (h) => h.other.replaceChildren(h.ui.renderMemories()),
    (h) => h.windowEvents.pagehide(),
  ]) {
    const page = deferred(), item = graphMemory(), h = harness(async () => page.promise);
    await clickMemoryButton(h, mountGraphResults(h, [item])); const region = h.ui.path.region;
    await leave(h); assert.equal(h.requests[0].signal.aborted, true); assert.equal(h.ui.path === null, true); assert.equal(region.textContent, "");
    page.resolve({ items: [item] }); await flush(); assert.equal(region.textContent, "");
  }
});

test("a path 401 clears private references and returns to the password gate without retrying", async () => {
  const page = deferred(), item = graphMemory(), h = harness(async () => page.promise);
  await clickMemoryButton(h, mountGraphResults(h, [item])); const region = h.ui.path.region;
  page.reject(new ApiError("请登录", 401)); await flush();
  assert.equal(h.ui.path === null, true); assert.equal(region.textContent, ""); assert.equal(h.ui.state.auth.authenticated, false);
  assert.equal(h.document.activeElement === h.document.querySelector("#login-password"), true); assert.equal(h.requests.length, 1);
});

test("path errors wait for explicit retry and the original query is reused", async () => {
  const item = graphMemory(); let calls = 0;
  const h = harness(async () => { if (++calls === 1) throw new ApiError("服务暂不可用", 503); return { items: [item] }; });
  await clickMemoryButton(h, mountGraphResults(h, [item]));
  assert.ok(h.ui.path.region.textContent.includes("服务暂不可用")); assert.equal(h.requests.length, 1);
  await clickMemoryButton(h, h.button("重新检索", h.other)); assert.equal(h.requests.length, 2); assert.equal(h.requests[1].path, h.requests[0].path); assert.ok(h.ui.path.via);
});

test("HTML, NUL and long names remain plain text and opaque memory references stay complete", async () => {
  const id = `memory-${"原样🙂".repeat(60)}`, item = graphMemory(id);
  item.graph_via.seed.name = '<img src=x onerror="bad()">\0MuLi'; item.graph_via.steps[0].subject = item.graph_via.seed.name;
  item.graph_via.steps[1].object = `工具\0${"中文🙂".repeat(90)}`;
  const h = harness(async () => ({ items: [item] })); await clickMemoryButton(h, mountGraphResults(h, [item]));
  const region = h.ui.path.region;
  assert.ok(region.textContent.includes(item.graph_via.seed.name)); assert.ok(region.textContent.includes(item.graph_via.steps[1].object));
  assert.equal(region.querySelectorAll("img").length, 0); assert.equal(region.querySelector('[data-memory-path-ref="step-1"]').dataset.id, id);
});

test("a bounded graph labels partial relationships without pretending it is the full query path", () => {
  const h = harness(); h.ui.state.memoryTab = "graph";
  h.ui.state.memoryGraph = { nodes: ["MuLi", "项目甲"], edges: [{ memory_id: "edge", subject: "MuLi", predicate: "维护", object: "项目甲" }], truncated: true };
  h.other.replaceChildren(h.ui.renderMemories());
  assert.ok(h.other.textContent.includes("部分关系")); assert.equal(h.other.querySelectorAll("[data-memory-path]").length, 0); assert.equal(h.requests.length, 0);
});

test("a queued click from a detached old disclosure cannot replace the current path", async () => {
  const old = graphMemory("old-result"), current = graphMemory("current-result"), h = harness(async () => ({ items: [current] }));
  const detached = mountGraphResults(h, [old]);
  const toggle = mountGraphResults(h, [current]); await clickMemoryButton(h, toggle);
  await clickMemoryButton(h, detached);
  assert.equal(h.ui.path.id, current.id); assert.equal(h.requests.length, 1); assert.equal(toggle.getAttribute("aria-expanded"), "true");
});

test("a disclosure rendered for the previous query cannot read against a newer pending query", async () => {
  const page = deferred(), item = graphMemory(), h = harness(async () => page.promise);
  const oldToggle = mountGraphResults(h, [item]);
  const pending = h.ui.searchMemories("changed-query"); await clickMemoryButton(h, oldToggle);
  assert.equal(h.requests.length, 1); assert.equal(h.ui.path === null, true);
  page.resolve({ items: [] }); await pending;
});

test("an explicit path retry retains a stable keyboard position throughout loading and completion", async () => {
  const page = deferred(), item = graphMemory(); let calls = 0;
  const h = harness(async () => { if (++calls === 1) throw new ApiError("暂不可用", 503); return page.promise; });
  const toggle = mountGraphResults(h, [item]); await clickMemoryButton(h, toggle);
  const retry = h.button("重新检索", h.other); retry.focus(); await clickMemoryButton(h, retry);
  assert.equal(h.document.activeElement === toggle, true);
  page.resolve({ items: [item] }); await flush(); assert.equal(h.document.activeElement === toggle, true);
});
