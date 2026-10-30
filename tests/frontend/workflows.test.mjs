import test from "node:test";
import { composerIdentity, createComposerDraft, MAX_DRAFT_ATTACHMENTS, sameComposerIdentity, validDraftAttachment } from "../../web/composer-draft.mjs";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";

// Compare DOM/slot identity through booleans: failed object diffs traverse the cyclic mock graph.
import { MODES } from "../../web/state.mjs";
import { ApiError } from "../../web/api.mjs";
import { VoiceSetup, voiceSetupLabel } from "../../web/voice-setup.mjs";

// Run the actual application functions and registered click/dialog/SSE handlers.
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
  const requests = [], toasts = [], errors = [], windowEvents = {}; let globalEvent = null;
  const context = {
    composerIdentity, createComposerDraft, MAX_DRAFT_ATTACHMENTS, sameComposerIdentity, validDraftAttachment,
    document, Node: class { static [Symbol.hasInstance](value) { return value instanceof DOMElement || value instanceof TextNode; } },
    window: { addEventListener: (name, callback) => { windowEvents[name] = callback; } },
    AbortController, TextEncoder, URLSearchParams, console, queueMicrotask, MODES, toasts, errors,
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
    renderChrome = () => {}; renderChat = () => {}; resizeComposer = () => {}; toast = (text) => toasts.push(text); showError = (error) => errors.push(error.message);
    const owner = beginConnection(); owner.ready = true;
    owner.sessions.start(); owner.notifications.start();
    globalThis.ui = { state, renderWorkflows, switchView, handleAction, startGlobalEvents, showLogin, beginConnection,
      stopEvents() { globalController?.abort(); },
      get activity() { return typeof workflowActivity === "undefined" ? null : workflowActivity; } };

  `, context);
  const ui = context.ui; ui.state.view = "workflows"; ui.state.viewLoading = false; ui.state.auth.authenticated = true;
  return { ui, requests, toasts, errors, document, dialog, form, other, windowEvents,
    button: (action, id) => other.querySelectorAll("button").find((node) => node.dataset.action === action && (id === undefined || node.dataset.id === id)),
    emitGlobal: (event) => globalEvent(event) };

}

const run = (id, status = "running", extra = {}) => ({ id, workflow_id: "definition", name: "青瓷工作流", status, created_at: 1, updated_at: 2, ...extra });
const page = (items, has_more = false) => ({ items, has_more, next_after: has_more ? items.at(-1).id : null });
const definition = { id: "definition", name: "青瓷工作流", mode: "ask", steps: [], runs: [{ id: "new-completed", status: "completed", nodes: {} }] };
const definitions = () => ({ items: [structuredClone(definition)] });
const activeCalls = (h) => h.requests.filter((request) => request.path.startsWith("/workflow-runs/active?"));
const schedule = (id, extra = {}) => ({ id, name: `定时研究 ${id}`, enabled: true, blocked: false, due_at: 10000, interval_seconds: 604800, last_run_id: null, last_status: null, ...extra });
const scheduleCalls = (h) => h.requests.filter((request) => request.path.startsWith("/workflow-schedules?"));
const schedulePage = (items, has_more = false) => ({ ...page(items, has_more), next_after_kind: has_more ? items.at(-1).kind : null });
async function click(h, button) {
  assert.ok(button instanceof DOMButton, "expected a real button node");
  for (const handler of h.document.events.click ?? []) await handler({ target: button, preventDefault() {} });
  await flush();
}

test("calendar and interval schedules with one ID cancel only the chosen kind", async () => {
  const items = [
    schedule("same-id", { kind: "calendar_workflow_schedule", calendar: { version: 1, timezone: "Asia/Shanghai", local_time: "08:00", weekdays: [1, 3] }, prompt: "private-calendar-task" }),
    schedule("same-id", { kind: "workflow_schedule" }),
  ];
  const h = harness(async (path, options) => {
    if (path === "/workflows") return definitions();
    if (path.startsWith("/workflow-runs/active?")) return page([]);
    if (options.method === "POST") {
      assert.equal(path, "/workflow-schedules/same-id/cancel?kind=calendar_workflow_schedule");
      items[0].enabled = false; return { ...items[0] };
    }
    return schedulePage(items);
  });
  await h.ui.switchView("workflows"); await click(h, h.button("toggle-workflow-schedules"));
  assert.equal(h.ui.activity.schedulesError, "");
  assert.ok(h.other.textContent.includes("Asia/Shanghai · 08:00 · 周一、周三"));
  assert.equal(h.other.textContent.includes("private-calendar-task"), false);
  const buttons = h.other.querySelectorAll('[data-action="cancel-workflow-schedule"]');
  assert.equal(buttons.length, 2);
  await click(h, buttons.find((button) => button.dataset.kind === "calendar_workflow_schedule"));
  assert.equal(items[0].enabled, false); assert.equal(items[1].enabled, true);
  const remaining = h.other.querySelectorAll('[data-action="cancel-workflow-schedule"]');
  assert.equal(remaining.length, 1); assert.equal(remaining[0].dataset.kind, "workflow_schedule");
  assert.equal(h.errors.length, 0);
});

test("schedule paging carries kind across identical IDs and restores the first page", async () => {
  const first = schedule("same-id", { kind: "calendar_workflow_schedule" });
  const second = schedule("same-id", { kind: "workflow_schedule" });
  const h = harness(async (path) => {
    if (path === "/workflows") return definitions();
    if (path.startsWith("/workflow-runs/active?")) return page([]);
    const query = new URLSearchParams(path.split("?")[1]);
    if (!query.get("after_id")) return schedulePage([first], true);
    assert.equal(query.get("after_id"), "same-id");
    assert.equal(query.get("after_kind"), "calendar_workflow_schedule");
    return schedulePage([second]);
  });
  await h.ui.switchView("workflows"); await click(h, h.button("toggle-workflow-schedules"));
  await click(h, h.other.querySelectorAll('[data-action="page-workflow-schedules"]').find((button) => button.dataset.direction === "next"));
  assert.equal(h.ui.activity.schedulesError, "");
  assert.equal(h.ui.activity.schedulesPage.items[0].kind, "workflow_schedule");
  await click(h, h.other.querySelectorAll('[data-action="page-workflow-schedules"]').find((button) => button.dataset.direction === "previous"));
  assert.equal(h.ui.activity.schedulesPage.items[0].kind, "calendar_workflow_schedule");
  assert.equal(new URLSearchParams(scheduleCalls(h).at(-1).path.split("?")[1]).has("after_kind"), false);
});

test("workflow events preserve the focused schedule kind before keyboard cancellation", async (t) => {
  const items = [schedule("same-id", { kind: "calendar_workflow_schedule" }), schedule("same-id", { kind: "workflow_schedule" })];
  const h = harness(async (path, options) => {
    if (path === "/workflows") return definitions();
    if (path === "/sessions") return { items: [] };
    if (path.startsWith("/workflow-runs/active?")) return page([]);
    if (options.method === "POST") {
      assert.equal(path, "/workflow-schedules/same-id/cancel");
      items[1].enabled = false; return { ...items[1] };
    }
    return schedulePage(items);
  });
  await h.ui.switchView("workflows"); await click(h, h.button("toggle-workflow-schedules"));
  h.ui.startGlobalEvents(); t.after(() => h.ui.stopEvents());
  h.other.querySelectorAll('[data-action="cancel-workflow-schedule"]').find((button) => button.dataset.kind === "workflow_schedule").focus();
  h.emitGlobal({ type: "workflow.updated", data: { workflow_id: "unlisted" } }); await flush();
  const focused = h.document.activeElement;
  assert.equal(focused.dataset.action, "cancel-workflow-schedule");
  assert.equal(focused.dataset.kind, "workflow_schedule");
  assert.equal(h.other.contains(focused), true);
  await click(h, focused);
  assert.equal(items[0].enabled, true); assert.equal(items[1].enabled, false);
});

test("schedule directory rejects unknown kinds and incomplete modern cursors", async () => {
  for (const payload of [schedulePage([schedule("bad", { kind: "unknown" })]), page([schedule("bad", { kind: "calendar_workflow_schedule" })], true)]) {
    const h = harness(async (path) => path === "/workflows" ? definitions() : path.startsWith("/workflow-runs/active?") ? page([]) : payload);
    await h.ui.switchView("workflows"); await click(h, h.button("toggle-workflow-schedules"));
    assert.ok(h.ui.activity.schedulesError);
    assert.equal(h.other.querySelectorAll('[data-action="cancel-workflow-schedule"]').length, 0);
  }
});

test("all active runs have independent stop buttons even when recent history is completed or the definition is absent", async () => {
  const active = [run("old-running"), run("same-definition-queued", "queued"), run("unlisted-definition", "pending", { workflow_id: "old-definition", name: "旧定义任务" })];
  const h = harness(async (path) => path === "/workflows" ? { items: [definition] } : page(active));
  await h.ui.switchView("workflows");
  assert.equal(activeCalls(h).length, 1);
  assert.deepEqual(h.other.querySelectorAll('[data-action="cancel-workflow-run"]').map((button) => button.dataset.id), active.map((entry) => entry.id));
  assert.ok(h.other.textContent.includes("最近运行 · 已完成"));
  assert.ok(h.other.textContent.includes("旧定义任务")); assert.ok(h.other.textContent.includes("等待中"));
});

test("an active listing error never becomes an empty or idle claim and does not hide definitions", async () => {
  const h = harness(async (path) => { if (path === "/workflows") return { items: [definition] }; throw new ApiError("活动记录暂不可用", 503); });
  await h.ui.switchView("workflows");
  assert.ok(h.other.textContent.includes("活动记录暂不可用"));
  assert.ok(h.other.querySelector('[data-action="run-workflow"]'));
  assert.equal(h.other.textContent.includes("没有进行中的工作流"), false);
});

test("active results render independently while definitions are pending and survive their failure", async () => {
  const pending = deferred();
  const h = harness(async (path) => path === "/workflows" ? pending.promise : page([run("visible-active")]));
  const opening = h.ui.switchView("workflows"); await flush();
  assert.ok(h.button("cancel-workflow-run", "visible-active"));
  assert.ok(h.other.textContent.includes("正在读取定义"));
  pending.reject(new ApiError("定义暂不可用", 503)); await opening;
  assert.ok(h.other.textContent.includes("定义暂不可用"));
  assert.ok(h.button("cancel-workflow-run", "visible-active"));
  assert.equal(h.other.textContent.includes("还没有工作流"), false);
});

test("only a successful first-page response can claim there are no active workflows", async () => {
  const pending = deferred();
  const h = harness(async (path) => path === "/workflows" ? definitions() : pending.promise);
  const opening = h.ui.switchView("workflows"); await flush();
  assert.ok(h.button("run-workflow"));
  assert.equal(h.other.textContent.includes("没有进行中的工作流"), false);
  pending.resolve(page([])); await opening;
  assert.ok(h.other.textContent.includes("没有进行中的工作流"));
});

test("paging is explicit, preserves whole cursor IDs, rereads previous pages and retains only current summaries", async () => {
  const first = Array.from({ length: 50 }, (_, index) => run(`run-${String(index).padStart(3, "0")}`, "running", { nodes: { private: "not a summary" }, result: "large private result" }));
  const h = harness(async (path) => {
    if (path === "/workflows") return definitions();
    const after = new URLSearchParams(path.split("?")[1]).get("after_id");
    return after ? page([run("run-050")]) : page(first, true);
  });
  await h.ui.switchView("workflows");
  assert.equal(activeCalls(h).length, 1);
  assert.equal(h.other.querySelectorAll('[data-action="cancel-workflow-run"]').length, 50);
  assert.ok(h.other.textContent.includes("本页 50 项 · 有后续"));
  assert.equal(h.ui.activity.page.items[0].nodes, undefined); assert.equal(h.ui.activity.page.items[0].result, undefined);
  await click(h, h.button("page-workflow-runs").parentNode.querySelectorAll("button").find((button) => button.dataset.direction === "next"));
  assert.equal(activeCalls(h).length, 2);
  assert.equal(new URLSearchParams(activeCalls(h)[1].path.split("?")[1]).get("after_id"), "run-049");
  assert.deepEqual(Array.from(h.ui.activity.page.items, (item) => item.id), ["run-050"]);
  assert.equal(h.other.textContent.includes("run-000"), false);
  assert.ok(h.other.textContent.includes("第 2 页 · 本页 1 项"));
  await click(h, h.button("page-workflow-runs"));
  assert.equal(activeCalls(h).length, 3);
  assert.equal(new URLSearchParams(activeCalls(h)[2].path.split("?")[1]).get("after_id"), "");
  assert.equal(h.ui.activity.page.items.length, 50);
  assert.deepEqual(Array.from(h.ui.activity.starts), [""]);
});

test("an emptied later page returns to the head once without following has_more", async () => {
  let reads = 0;
  const h = harness(async (path) => {
    if (path === "/workflows") return definitions();
    reads += 1;
    return reads === 2 ? page([]) : page([run(`head-${reads}`)], true);
  });
  await h.ui.switchView("workflows");
  await click(h, h.other.querySelectorAll('[data-action="page-workflow-runs"]').find((button) => button.dataset.direction === "next"));
  assert.equal(reads, 3);
  assert.equal(h.ui.activity.after, "");
  assert.ok(h.button("cancel-workflow-run", "head-3"));
  assert.equal(h.ui.activity.starts.length, 1);
});

test("a malformed cursor response is an error, retaining the last known page instead of fabricating EOF", async () => {
  let reads = 0;
  const h = harness(async (path) => {
    if (path === "/workflows") return definitions();
    reads += 1;
    return reads === 1 ? page([run("full-position")], true) : { items: [run("second")], has_more: true, next_after: "wrong-position" };
  });
  await h.ui.switchView("workflows");
  await click(h, h.other.querySelectorAll('[data-action="page-workflow-runs"]').find((button) => button.dataset.direction === "next"));
  assert.ok(h.other.textContent.includes("翻页位置无效"));
  assert.ok(h.button("cancel-workflow-run", "full-position"));
  assert.equal(h.other.textContent.includes("没有进行中的工作流"), false);
});

test("workflow events invalidate an in-flight later page, coalesce to one head read, and include unlisted definitions", async () => {
  const next = deferred(), refreshed = deferred(); let reads = 0;
  const h = harness(async (path) => {
    if (path === "/workflows") return definitions();
    if (path === "/sessions") return { items: [] };
    reads += 1;
    return reads === 1 ? page([run("initial")], true) : reads === 2 ? next.promise : refreshed.promise;
  });
  await h.ui.switchView("workflows"); h.ui.startGlobalEvents();
  const paging = click(h, h.other.querySelectorAll('[data-action="page-workflow-runs"]').find((button) => button.dataset.direction === "next")); await flush();
  for (let index = 0; index < 5; index += 1) h.emitGlobal({ type: "workflow.updated", data: { workflow_id: "unlisted-definition", run: { id: "hidden-new", status: "running" } } });
  assert.equal(activeCalls(h).length, 2);
  assert.equal(activeCalls(h)[1].signal.aborted, true);
  next.resolve(page([run("stale-next")])); await paging; await flush();
  assert.equal(activeCalls(h).length, 3);
  assert.equal(new URLSearchParams(activeCalls(h)[2].path.split("?")[1]).get("after_id"), "");
  assert.equal(h.other.textContent.includes("stale-next"), false);
  refreshed.resolve(page([run("hidden-new", "running", { workflow_id: "unlisted-definition" })])); await flush();
  assert.ok(h.button("cancel-workflow-run", "hidden-new"));
  assert.equal(h.ui.activity.starts.length, 1); h.ui.stopEvents();
});

test("leaving and reopening the view prevents late definitions or active pages from replacing the new view", async () => {
  const oldDefinitions = deferred(), oldPage = deferred(); let definitionsRead = 0, pagesRead = 0;
  const h = harness(async (path) => path === "/workflows" ? ++definitionsRead === 1 ? oldDefinitions.promise : definitions() : ++pagesRead === 1 ? oldPage.promise : page([run("current")]));
  const previous = h.ui.switchView("workflows"); await flush();
  await h.ui.switchView("chat"); await h.ui.switchView("workflows");
  oldDefinitions.resolve({ items: [{ ...definition, name: "迟到定义" }] }); oldPage.resolve(page([run("late-run")])); await previous;
  assert.equal(activeCalls(h)[0].signal.aborted, true);
  assert.equal(h.other.textContent.includes("迟到定义"), false); assert.equal(h.other.textContent.includes("late-run"), false);
  assert.ok(h.button("cancel-workflow-run", "current"));
});

test("a late 401 clears the active page and prevents pending responses from restoring it", async () => {
  const active = deferred();
  const h = harness(async (path) => path === "/workflows" ? definitions() : active.promise);
  const opening = h.ui.switchView("workflows"); await flush();
  active.reject(new ApiError("请重新登录", 401)); await opening;
  assert.equal(h.ui.state.auth.authenticated, false); assert.equal(h.ui.activity === null, true);
  assert.equal(h.other.querySelector("#workflow-active-runs").textContent, "");
  assert.equal(h.document.activeElement === h.document.querySelector("#login-password"), true);
});

test("pagehide discards the current page and ignores an active refresh that finishes later", async () => {
  const later = deferred(); let reads = 0;
  const h = harness(async (path) => path === "/workflows" ? definitions() : ++reads === 1 ? page([run("private-run")]) : later.promise);
  await h.ui.switchView("workflows");
  const refreshing = click(h, h.button("refresh-workflow-runs")); await flush();
  h.windowEvents.pagehide();
  assert.equal(h.ui.activity === null, true); assert.equal(h.other.querySelector("#workflow-active-runs").textContent, "");
  later.resolve(page([run("late-private-run")])); await refreshing;
  assert.equal(h.other.textContent.includes("late-private-run"), false);
});

test("stopping one concurrent run uses its full ID once and keeps the other active", async () => {
  const stop = deferred(); const fullID = "run-/中文?\u0000完整"; let stopped = false;
  const h = harness(async (path, options) => {
    if (path === "/workflows") return definitions();
    if (options.method === "POST") { await stop.promise; stopped = true; return { status: "cancelled" }; }
    return page(stopped ? [run("second-running")] : [run(fullID), run("second-running")]);
  });
  await h.ui.switchView("workflows");
  const button = h.button("cancel-workflow-run", fullID);
  const stopping = click(h, button); await flush();
  assert.equal(h.requests.filter((request) => request.method === "POST").length, 1);
  assert.equal(h.requests.find((request) => request.method === "POST").path, `/workflow-runs/${encodeURIComponent(fullID)}/cancel`);
  assert.equal(h.button("cancel-workflow-run", fullID).disabled, true);
  assert.equal(h.button("cancel-workflow-run", "second-running").disabled, false);
  assert.equal(h.toasts.some((text) => text.includes("已停止")), false);
  await h.ui.handleAction("cancel-workflow-run", button);
  assert.equal(h.requests.filter((request) => request.method === "POST").length, 1);
  stop.resolve(); await stopping; await flush();
  assert.equal(h.button("cancel-workflow-run", fullID) === undefined, true);
  assert.ok(h.button("cancel-workflow-run", "second-running"));
  assert.ok(h.toasts.includes("已提交停止请求"));
});

test("a failed stop leaves both active rows and shows the actual error", async () => {
  const h = harness(async (path, options) => {
    if (path === "/workflows") return definitions();
    if (options.method === "POST") throw new ApiError("停止失败，运行仍在收尾", 409);
    return page([run("first"), run("second")]);
  });
  await h.ui.switchView("workflows"); await click(h, h.button("cancel-workflow-run", "first"));
  assert.deepEqual(h.errors, ["停止失败，运行仍在收尾"]);
  assert.equal(h.button("cancel-workflow-run", "first").disabled, false);
  assert.ok(h.button("cancel-workflow-run", "second")); assert.equal(activeCalls(h).length, 1);
});

test("a successful run refreshes active rows even without an SSE event", async () => {
  let launched = false;
  const h = harness(async (path, options) => {
    if (path === "/workflows") return definitions();
    if (path === "/sessions") return { items: [] };
    if (options.method === "POST") { launched = true; return { run_id: "started-now" }; }
    return page(launched ? [run("started-now")] : []);
  });
  await h.ui.switchView("workflows"); await click(h, h.button("run-workflow", "definition"));
  assert.equal(activeCalls(h).length, 2);
  assert.ok(h.button("cancel-workflow-run", "started-now"));
  assert.equal(h.other.textContent.includes("没有进行中的工作流"), false);
});

test("definition deletion displays the real 409 without removing the definition or active row", async () => {
  const h = harness(async (path, options) => {
    if (options.method === "DELETE") throw new ApiError("该工作流仍有活动运行", 409);
    return path === "/workflows" ? definitions() : page([run("older-active")]);
  });
  await h.ui.switchView("workflows"); await click(h, h.button("delete-workflow")); await h.form.dispatch("submit");
  assert.equal(h.dialog.open, true);
  assert.equal(h.document.querySelector("#dialog-error").textContent, "该工作流仍有活动运行");
  assert.ok(h.button("delete-workflow", "definition")); assert.ok(h.button("cancel-workflow-run", "older-active"));
});

test("native DOM keeps names and IDs as text with no conditional false/null children", async () => {
  const name = "<img src=x onerror=alert(1)>\u0000青瓷";
  const h = harness(async (path) => path === "/workflows" ? definitions() : page([run("完整-运行-ID", "pending", { name })]));
  await h.ui.switchView("workflows");
  const region = h.other.querySelector("#workflow-active-runs");
  assert.ok(region.textContent.includes(name)); assert.ok(region.textContent.includes("完整-运行-ID"));
  assert.equal(region.querySelector("img") === null, true);
  assert.equal(region.childNodes.some((node) => node instanceof TextNode && ["false", "null", "undefined"].includes(node.textContent)), false);
  const native = h.document.createElement("div"); native.replaceChildren(false, null, undefined);
  assert.equal(native.textContent, "falsenullundefined");
});

test("refresh preserves open history and same-run focus, falling back when that run finishes without stealing external focus", async () => {
  let items = [run("focused-run"), run("other-run")];
  const h = harness(async (path) => path === "/workflows" ? definitions() : path === "/sessions" ? { items: [] } : page(items));
  await h.ui.switchView("workflows"); h.ui.startGlobalEvents();
  h.other.querySelector(".workflow-run").open = true;
  h.button("cancel-workflow-run", "focused-run").focus();
  h.emitGlobal({ type: "workflow.updated", data: { workflow_id: "unlisted" } }); await flush();
  assert.equal(h.document.activeElement === h.button("cancel-workflow-run", "focused-run"), true);
  assert.equal(h.other.querySelector(".workflow-run").open, true);
  items = [run("other-run")];
  h.emitGlobal({ type: "workflow.updated", data: { workflow_id: "unlisted" } }); await flush();
  assert.equal(h.document.activeElement.getAttribute("id"), "workflow-active-heading");
  const outside = h.document.querySelector("#message-input"); outside.focus();
  h.emitGlobal({ type: "workflow.updated", data: { workflow_id: "unlisted" } }); await flush();
  assert.equal(h.document.activeElement === outside, true); h.ui.stopEvents();
});

test("scheduled delegations stay lazy and page explicitly using complete cursors and bounded nullable summaries", async () => {
  const fullID = "scheduled-/完整?cursor\u0000原值";
  const first = Array.from({ length: 50 }, (_, index) => schedule(index === 49 ? fullID : `scheduled-${index}`, { blocked: index === 0, prompt: "private task body", reply_origin: { private: true } }));
  const next = [schedule("admitted-once", { due_at: null, last_run_id: "actual-admitted-run", last_status: "running" }), schedule("disabled-legacy", { enabled: false, due_at: null })];
  const h = harness(async (path) => {
    if (path === "/workflows") return definitions();
    if (path.startsWith("/workflow-runs/active?")) return page([]);
    assert.ok(path.startsWith("/workflow-schedules?"));
    const query = new URLSearchParams(path.split("?")[1]);
    assert.equal(query.get("limit"), "50");
    return query.get("after_id") ? page(next) : page(first, true);
  });
  await h.ui.switchView("workflows");
  assert.equal(scheduleCalls(h).length, 0);
  assert.equal(h.button("toggle-workflow-schedules").getAttribute("aria-expanded"), "false");
  assert.equal(h.other.querySelector("#workflow-schedules").hidden, true);
  await click(h, h.button("toggle-workflow-schedules"));
  assert.equal(scheduleCalls(h).length, 1);
  assert.equal(scheduleCalls(h)[0].cache, "no-store");
  assert.equal(new URLSearchParams(scheduleCalls(h)[0].path.split("?")[1]).get("after_id"), "");
  assert.equal(h.button("toggle-workflow-schedules").getAttribute("aria-expanded"), "true");
  assert.equal(h.other.querySelectorAll('[data-action="cancel-workflow-schedule"]').length, 50);
  assert.equal(h.ui.activity.schedulesPage.items[0].prompt, undefined);
  assert.equal(h.ui.activity.schedulesPage.items[0].reply_origin, undefined);
  assert.equal(h.other.textContent.includes("private task body"), false);
  assert.ok(h.other.querySelector("#workflow-schedules").textContent.includes("暂缓执行"));
  await click(h, h.other.querySelectorAll('[data-action="page-workflow-schedules"]').find((button) => button.dataset.direction === "next"));
  assert.equal(scheduleCalls(h).length, 2);
  assert.equal(new URLSearchParams(scheduleCalls(h)[1].path.split("?")[1]).get("after_id"), fullID);
  assert.deepEqual(Array.from(h.ui.activity.schedulesPage.items, (item) => item.id), ["admitted-once", "disabled-legacy"]);
  assert.equal(h.ui.activity.schedulesPage.items[0].due_at, null);
  assert.equal(h.ui.activity.schedulesPage.items[1].last_run_id, null);
  assert.equal(h.ui.activity.schedulesPage.items[1].last_status, null);
  assert.equal(h.ui.activity.schedulesError, "");
  assert.equal(h.other.querySelector("#workflow-schedules").textContent.includes("null"), false);
  assert.ok(h.other.querySelector("#workflow-schedules").textContent.includes("actual-admitted-run"));
  assert.ok(h.other.querySelector("#workflow-schedules").textContent.includes("运行中"));
  assert.ok(h.button("cancel-workflow-schedule", "admitted-once"));
  assert.equal(h.button("cancel-workflow-schedule", "disabled-legacy") === undefined, true);
  assert.equal(h.button("cancel-workflow-schedule", fullID) === undefined, true);
  await click(h, h.other.querySelectorAll('[data-action="page-workflow-schedules"]').find((button) => button.dataset.direction === "previous"));
  assert.equal(scheduleCalls(h).length, 3);
  assert.equal(new URLSearchParams(scheduleCalls(h)[2].path.split("?")[1]).get("after_id"), "");
  assert.deepEqual(Array.from(h.ui.activity.schedulesStarts), [""]);
  assert.ok(h.button("cancel-workflow-schedule", fullID));
});

test("a failed schedule cancellation never invents success and a successful retry rereads the real summary", async () => {
  const pending = deferred(), fullID = "schedule-/中文?\u0000准确";
  let attempts = 0, stopped = false;
  const h = harness(async (path, options) => {
    if (path === "/workflows") return definitions();
    if (path.startsWith("/workflow-runs/active?")) return page([]);
    if (options.method === "POST") {
      assert.equal(path, `/workflow-schedules/${encodeURIComponent(fullID)}/cancel`);
      attempts += 1;
      if (attempts === 1) return pending.promise;
      stopped = true;
      return { id: fullID, enabled: false };
    }
    assert.ok(path.startsWith("/workflow-schedules?"));
    return page([schedule(fullID, { enabled: !stopped })]);
  });
  await h.ui.switchView("workflows");
  await click(h, h.button("toggle-workflow-schedules"));
  const original = h.button("cancel-workflow-schedule", fullID);
  const cancelling = click(h, original); await flush();
  assert.equal(attempts, 1);
  assert.equal(h.button("cancel-workflow-schedule", fullID).disabled, true);
  assert.equal(h.ui.activity.schedulesPage.items[0].enabled, true);
  assert.equal(h.toasts.includes("已提交取消请求"), false);
  await h.ui.handleAction("cancel-workflow-schedule", original);
  assert.equal(attempts, 1);
  pending.reject(new ApiError("取消失败，原委托仍有效", 409)); await cancelling;
  assert.deepEqual(h.errors, ["取消失败，原委托仍有效"]);
  assert.equal(h.ui.activity.schedulesPage.items[0].enabled, true);
  assert.equal(h.button("cancel-workflow-schedule", fullID).disabled, false);
  assert.equal(scheduleCalls(h).length, 1);
  assert.equal(h.toasts.includes("已提交取消请求"), false);
  await click(h, h.button("cancel-workflow-schedule", fullID));
  assert.equal(attempts, 2);
  assert.equal(scheduleCalls(h).length, 2);
  assert.equal(h.ui.activity.schedulesPage.items[0].enabled, false);
  assert.equal(h.button("cancel-workflow-schedule", fullID) === undefined, true);
  assert.ok(h.toasts.includes("已提交取消请求"));
});

test("collapsing scheduled delegations aborts the lazy read and a late page cannot replace the reopened head", async () => {
  const late = deferred(); let reads = 0;
  const h = harness(async (path) => {
    if (path === "/workflows") return definitions();
    if (path.startsWith("/workflow-runs/active?")) return page([]);
    assert.ok(path.startsWith("/workflow-schedules?"));
    reads += 1;
    return reads === 1 ? late.promise : page([schedule("current-head")]);
  });
  await h.ui.switchView("workflows");
  const opening = click(h, h.button("toggle-workflow-schedules")); await flush();
  const slot = h.ui.activity;
  await click(h, h.button("toggle-workflow-schedules"));
  assert.equal(scheduleCalls(h)[0].signal.aborted, true);
  assert.equal(slot.schedulesPage === null, true);
  assert.equal(h.other.querySelector("#workflow-schedules").hidden, true);
  await click(h, h.button("toggle-workflow-schedules"));
  assert.equal(scheduleCalls(h).length, 2);
  assert.ok(h.button("cancel-workflow-schedule", "current-head"));
  late.resolve(page([schedule("late-collapsed-row")])); await opening;
  assert.equal(h.ui.activity === slot, true);
  assert.equal(h.other.textContent.includes("late-collapsed-row"), false);
  assert.deepEqual(Array.from(slot.schedulesPage.items, (item) => item.id), ["current-head"]);
});

test("leaving the workflow view invalidates its pending schedules and an old 401 cannot clear the reopened connection", async () => {
  const late = deferred(); let reads = 0;
  const h = harness(async (path) => {
    if (path === "/workflows") return definitions();
    if (path.startsWith("/workflow-runs/active?")) return page([]);
    assert.ok(path.startsWith("/workflow-schedules?"));
    reads += 1;
    return reads === 1 ? late.promise : page([schedule("reopened-current")]);
  });
  await h.ui.switchView("workflows");
  const opening = click(h, h.button("toggle-workflow-schedules")); await flush();
  const oldSlot = h.ui.activity;
  await h.ui.switchView("chat"); await h.ui.switchView("workflows");
  assert.equal(scheduleCalls(h)[0].signal.aborted, true);
  assert.equal(h.ui.activity === oldSlot, false);
  assert.equal(scheduleCalls(h).length, 1);
  assert.equal(h.button("toggle-workflow-schedules").getAttribute("aria-expanded"), "false");
  await click(h, h.button("toggle-workflow-schedules"));
  late.reject(new ApiError("旧页面的迟到401", 401)); await opening;
  assert.equal(h.ui.state.auth.authenticated, true);
  assert.equal(h.ui.activity === null, false);
  assert.ok(h.button("cancel-workflow-schedule", "reopened-current"));
  assert.deepEqual(Array.from(h.ui.activity.schedulesPage.items, (item) => item.id), ["reopened-current"]);
});

test("authentication loss and pagehide discard schedule summaries and reject late refresh repopulation", async () => {
  for (const boundary of ["login", "pagehide"]) {
    const late = deferred(); let reads = 0;
    const h = harness(async (path) => {
      if (path === "/workflows") return definitions();
      if (path.startsWith("/workflow-runs/active?")) return page([]);
      assert.ok(path.startsWith("/workflow-schedules?"));
      reads += 1;
      return reads === 1 ? page([schedule("private-visible-row")]) : late.promise;
    });
    await h.ui.switchView("workflows");
    await click(h, h.button("toggle-workflow-schedules"));
    assert.ok(h.button("cancel-workflow-schedule", "private-visible-row"));
    const refreshing = click(h, h.button("refresh-workflow-schedules")); await flush();
    if (boundary === "login") h.ui.showLogin(); else h.windowEvents.pagehide();
    assert.equal(h.ui.activity === null, true);
    assert.equal(scheduleCalls(h)[1].signal.aborted, true);
    assert.equal(h.other.querySelector("#workflow-schedules")?.textContent ?? "", "");
    late.resolve(page([schedule("late-private-row")])); await refreshing;
    assert.equal(h.other.textContent.includes("private-visible-row"), false);
    assert.equal(h.other.textContent.includes("late-private-row"), false);
    assert.equal(h.ui.activity === null, true);
    if (boundary === "login") assert.equal(h.ui.state.auth.authenticated, false);
  }
});

test("an old pending schedule cancellation cannot log out or refresh a new authenticated connection", async () => {
  const lateCancel = deferred(); let reads = 0;
  const h = harness(async (path, options) => {
    if (path === "/workflows") return definitions();
    if (path.startsWith("/workflow-runs/active?")) return page([]);
    if (options.method === "POST") {
      assert.equal(path, "/workflow-schedules/old-cancel/cancel");
      return lateCancel.promise;
    }
    assert.ok(path.startsWith("/workflow-schedules?"));
    reads += 1;
    return page([schedule(reads === 1 ? "old-cancel" : "new-private-row")]);
  });
  await h.ui.switchView("workflows");
  await click(h, h.button("toggle-workflow-schedules"));
  const oldSlot = h.ui.activity;
  const cancelling = click(h, h.button("cancel-workflow-schedule", "old-cancel")); await flush();
  const posted = h.requests.find((request) => request.method === "POST");
  assert.ok(posted);
  assert.equal(h.toasts.includes("已提交取消请求"), false);

  h.ui.showLogin();
  assert.equal(h.ui.state.auth.authenticated, false);
  assert.equal(posted.signal.aborted, true);
  // Use the real connection lifecycle after a fresh authentication; the harness
  // does not implement a browser cookie/login server.
  const owner = h.ui.beginConnection(); owner.ready = true;
  h.ui.state.auth.authenticated = true;
  h.document.querySelector("#auth-gate").hidden = true;
  await h.ui.switchView("workflows");
  await click(h, h.button("toggle-workflow-schedules"));
  const newSlot = h.ui.activity;
  const currentRow = h.button("cancel-workflow-schedule", "new-private-row");
  assert.equal(newSlot === oldSlot, false);
  assert.ok(currentRow);
  const readsBeforeReply = scheduleCalls(h).length;

  lateCancel.reject(new ApiError("旧取消的迟到401", 401)); await cancelling;
  assert.equal(h.ui.state.auth.authenticated, true);
  assert.equal(owner.controller.signal.aborted, false);
  assert.equal(h.ui.activity === newSlot, true);
  assert.equal(h.button("cancel-workflow-schedule", "new-private-row") === currentRow, true);
  assert.deepEqual(Array.from(newSlot.schedulesPage.items, (item) => item.id), ["new-private-row"]);
  assert.equal(h.document.querySelector("#auth-gate").hidden, true);
  assert.equal(scheduleCalls(h).length, readsBeforeReply);
  assert.equal(h.toasts.includes("已提交取消请求"), false);
});

async function sourceDraftHarness(inspect) {
  const source = run("source-result", "failed", { parent_session_id: "main", nodes: {} });
  const h = harness(async (path) => {
    if (path === "/workflows") return { items: [{ ...definition, runs: [source] }] };
    if (path.startsWith("/workflow-runs/active?")) return page([]);
    assert.equal(path, "/workflow-runs/source-result/inspection");
    return inspect();
  });
  h.ui.state.connected = true;
  h.ui.state.mainSessionId = "main";
  h.ui.state.activeSession = { id: "main" };
  const input = h.document.querySelector("#message-input");
  input.value = "沿用原资料，只做新的本地草稿。";
  input.maxLength = 10000;
  await h.ui.switchView("workflows");
  return { h, input };
}

test("workflow source action appends an exact reference without sending or losing attachments and voice", async () => {
  const revision = "a".repeat(64);
  const { h, input } = await sourceDraftHarness(() => ({ run_id: "source-result", revision }));
  const attachments = h.ui.state.attachments = [{ id: "local-file" }];
  const voice = h.ui.state.voiceDraft = { text: "待处理的转写" };
  await click(h, h.button("workflow-source-draft", "source-result"));
  assert.equal(h.ui.state.view, "chat");
  assert.ok(input.value.startsWith("沿用原资料，只做新的本地草稿。\n\n"));
  assert.ok(input.value.includes(JSON.stringify({ run_id: "source-result", revision })));
  assert.equal(h.ui.state.attachments === attachments, true);
  assert.equal(h.ui.state.voiceDraft === voice, true);
  assert.equal(h.document.activeElement === input, true);
  assert.equal(h.requests.some((request) => request.method === "POST"), false);
  assert.deepEqual(h.errors, []);
});

test("late workflow source reading cannot replace an edited draft or a new navigation", async (t) => {
  for (const change of ["draft", "navigation"]) await t.test(change, async () => {
    const pending = deferred();
    const { h, input } = await sourceDraftHarness(() => pending.promise);
    const reading = click(h, h.button("workflow-source-draft", "source-result"));
    await flush();
    input.value = "后来输入的新要求";
    if (change === "navigation") await h.ui.switchView("chat");
    pending.resolve({ run_id: "source-result", revision: "b".repeat(64) });
    await reading;
    assert.equal(input.value, "后来输入的新要求");
    assert.equal(h.ui.state.view, change === "navigation" ? "chat" : "workflows");
    assert.equal(h.requests.some((request) => request.method === "POST"), false);
    assert.deepEqual(h.errors, []);
  });
});

test("invalid workflow source evidence leaves the current draft intact", async () => {
  const { h, input } = await sourceDraftHarness(() => ({ run_id: "other-result", revision: "c".repeat(64) }));
  const text = input.value;
  await click(h, h.button("workflow-source-draft", "source-result"));
  assert.equal(input.value, text);
  assert.equal(h.ui.state.view, "workflows");
  assert.equal(h.errors.length, 1);
  assert.equal(h.button("workflow-source-draft", "source-result").disabled, false);
});
