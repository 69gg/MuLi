import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";
import { MODES } from "../../web/state.mjs";
import { ApiError } from "../../web/api.mjs";

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
    document, Node: class { static [Symbol.hasInstance](value) { return value instanceof DOMElement || value instanceof TextNode; } },
    window: { addEventListener: (name, callback) => { windowEvents[name] = callback; } },
    AbortController, TextEncoder, URLSearchParams, console, queueMicrotask, MODES, toasts, errors,
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
    renderChrome = () => {}; renderChat = () => {}; toast = (text) => toasts.push(text); showError = (error) => errors.push(error.message);
    globalThis.ui = { state, renderWorkflows, switchView, handleAction, startGlobalEvents, showLogin,
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
async function click(h, button) {
  assert.ok(button instanceof DOMButton, "expected a real button node");
  for (const handler of h.document.events.click ?? []) await handler({ target: button, preventDefault() {} });
  await flush();
}

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
  assert.equal(h.ui.state.auth.authenticated, false); assert.equal(h.ui.activity, null);
  assert.equal(h.other.querySelector("#workflow-active-runs").textContent, "");
  assert.equal(h.document.activeElement, h.document.querySelector("#login-password"));
});

test("pagehide discards the current page and ignores an active refresh that finishes later", async () => {
  const later = deferred(); let reads = 0;
  const h = harness(async (path) => path === "/workflows" ? definitions() : ++reads === 1 ? page([run("private-run")]) : later.promise);
  await h.ui.switchView("workflows");
  const refreshing = click(h, h.button("refresh-workflow-runs")); await flush();
  h.windowEvents.pagehide();
  assert.equal(h.ui.activity, null); assert.equal(h.other.querySelector("#workflow-active-runs").textContent, "");
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
  assert.equal(h.button("cancel-workflow-run", fullID), undefined);
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
  assert.equal(region.querySelector("img"), null);
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
  assert.equal(h.document.activeElement, h.button("cancel-workflow-run", "focused-run"));
  assert.equal(h.other.querySelector(".workflow-run").open, true);
  items = [run("other-run")];
  h.emitGlobal({ type: "workflow.updated", data: { workflow_id: "unlisted" } }); await flush();
  assert.equal(h.document.activeElement.getAttribute("id"), "workflow-active-heading");
  const outside = h.document.querySelector("#message-input"); outside.focus();
  h.emitGlobal({ type: "workflow.updated", data: { workflow_id: "unlisted" } }); await flush();
  assert.equal(h.document.activeElement, outside); h.ui.stopEvents();
});
