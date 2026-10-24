import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";
import { ApiError } from "../../web/api.mjs";
import { MODES, createSessionState, hydrateSession, applyEvent, isRunning } from "../../web/state.mjs";
import { createTeamPager } from "../../web/team.mjs";

// Reuse the existing workflows/connection VM and minimal DOM approach. Only
// transport promises are controlled; application bootstrap/render/actions run.
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
    this.value = ""; this.hidden = false; this.disabled = false; this.open = false;
    this.classList = { toggle() {}, add() {}, remove() {} };
  }
  get children() { return this.childNodes.filter((node) => node instanceof DOMElement); }
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
  remove() {
    if (!this.parentNode) return;
    if (this.contains(this.document.activeElement)) this.document.activeElement = this.document.body;
    this.parentNode.childNodes = this.parentNode.childNodes.filter((child) => child !== this); this.parentNode = null;
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
  contains(node) { return this === node || this.children.some((child) => child.contains(node)); }
  focus() { this.document.activeElement = this; }
  matches(selector) {
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
  close() { this.open = false; }
}

class DOMButton extends DOMElement {}

function payload(path) {
  if (path === "/auth/status") return { required: false, authenticated: true };
  if (path === "/ui/event-position") return { after: "20" };
  if (path === "/status") return { configured: true, mode: "ask" };
  if (path === "/conversation") return { session: { id: "main", status: "idle", mode: "ask" }, messages: [], events: [] };
  return { items: [] };
}

function harness(t, handler = async (path) => payload(path)) {
  const requests = [], streams = [], timers = new Map(), errors = [], toasts = [], windowEvents = {};
  const document = { events: {}, visibilityState: "visible", activeElement: null, createTextNode: (text) => new TextNode(text), addEventListener(name, callback) { (this.events[name] ??= []).push(callback); } };
  document.createElement = (tag) => tag === "button" ? new DOMButton(tag, document) : new DOMElement(tag, document);
  document.createElementNS = (_namespace, tag) => document.createElement(tag);
  document.body = document.createElement("body");
  document.contains = (node) => document.body.contains(node);
  document.querySelector = (selector) => document.body.querySelector(selector);
  document.querySelectorAll = (selector) => document.body.querySelectorAll(selector);
  const node = (tag, id, parent = document.body) => { const value = document.createElement(tag); value.setAttribute("id", id); parent.append(value); return value; };
  const shell = node("div", "app-shell"); shell.className = "app-shell";
  const dialog = node("dialog", "app-dialog"); node("h2", "dialog-title", dialog);
  const form = node("form", "dialog-form", dialog); node("div", "dialog-content", form); node("p", "dialog-error", form);
  const actions = node("div", "dialog-actions", form); actions.className = "dialog-actions";
  node("button", "dialog-close", actions); node("button", "dialog-submit", actions);
  for (const id of ["auth-gate", "login-password", "composer", "login-form", "message-input", "mode-select", "file-input", "conversation-scroll", "approval-region", "other-view"]) node("div", id);
  let timerSequence = 0;
  const context = {
    document, Node: class { static [Symbol.hasInstance](value) { return value instanceof DOMElement || value instanceof TextNode; } },
    window: { addEventListener(name, callback) { windowEvents[name] = callback; } },
    AbortController, URLSearchParams, TextEncoder, console, queueMicrotask, toasts, errors,
    MODES, createSessionState, hydrateSession, applyEvent, isRunning, createTeamPager,
    VoiceInput: class { cancel() {} }, uploadFile() {}, transcribeAttachment() {},
    captureTeamView: () => null, restoreTeamView() {}, sessionStorage: {},
    formatTimestamp: String, formatFullDate: String,
    setTimeout(callback, delay) { const id = ++timerSequence; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    apiRequest: async (path, options = {}) => { requests.push({ path, ...options }); return handler(path, options); },
    readEvents: async (path, options) => {
      const pending = deferred(); streams.push({ path, ...options, ...pending }); await pending.promise;
    },
  };
  runInNewContext(application + `
    renderChrome = renderComposer = renderSessions = resetMessageNodes = () => {};
    renderChat = queueRender = () => renderApprovals();
    toast = (message) => toasts.push(message); showError = (error) => errors.push(error.message);
    globalThis.ui = { state, connect, beginConnection, invalidateConnection, showLogin,
      renderApprovals, renderNotifications, renderOtherView, switchView, handleAction,
      refreshApprovals, approve,
      get owner() { return connection; } };
  `, context);
  const ui = context.ui;
  t.after(() => { ui.invalidateConnection(); for (const stream of streams) stream.resolve(); });
  return {
    ui, requests, streams, timers, errors, toasts, document, windowEvents,
    region: document.querySelector("#approval-region"), other: document.querySelector("#other-view"),
    latestStream: (path = "/events") => streams.filter((stream) => stream.path === path).at(-1),
  };
}

const approval = (id, session = "worker-session-A", run = "worker-run-A") => ({ id, session_id: session, run_id: run, status: "pending", tool: "fs_write", arguments: { path: "synthetic-result.txt", content: "仅测试资料" }, reason: "此队友需要主人确认", risk: "write", created_at: 1 });
const approvalReads = (h) => h.requests.filter((entry) => entry.path === "/approvals");
const rows = (h) => h.region.querySelectorAll(".approval-item");
const decisionButton = (h, id, decision = "approve", root = h.region) => root.querySelectorAll('[data-action="approval"]').find((button) => button.dataset.id === id && button.dataset.decision === decision);
const decisions = (h) => h.requests.filter((entry) => entry.method === "POST" && entry.path.startsWith("/approvals/"));

async function click(h, button) {
  assert.ok(button instanceof DOMButton, "expected a real button node");
  for (const handler of h.document.events.click ?? []) await handler({ target: button, preventDefault() {} });
  await flush();
}

test("bootstrap renders global live approvals without switching sessions", async (t) => {
  const pending = [approval("first"), approval("second", "worker-session-B", "worker-run-B")];
  const h = harness(t, async (path) => path === "/approvals" ? { items: pending } : payload(path));
  await h.ui.connect();
  assert.equal(h.ui.state.connected, true, "existing authenticated bootstrap must finish");
  assert.equal(h.ui.state.activeSession.id, "main");
  assert.equal(approvalReads(h).length, 1, "bootstrap must read the global live approval list once");
  assert.deepEqual(rows(h).map((row) => row.dataset.id), ["first", "second"]);
  for (const item of pending) {
    const row = rows(h).find((entry) => entry.dataset.id === item.id);
    assert.ok(row.textContent.includes(item.session_id)); assert.ok(row.textContent.includes(item.run_id));
    assert.ok(row.textContent.includes(item.tool)); assert.ok(row.textContent.includes(item.arguments.content));
  }
  assert.equal(h.ui.state.session.approvals.length, 0, "global projection must not forge approvals for the selected main run");
  assert.equal(h.requests.some((entry) => entry.path.startsWith("/sessions/worker")), false);
  assert.equal(h.timers.size, 0, "the approval inbox uses no polling timer");
});

test("notification approvals share the live projection and another device resolution removes only its item", async (t) => {
  let pending = [approval("first"), approval("second", "worker-session-B", "worker-run-B")];
  const h = harness(t, async (path) => path === "/approvals" ? { items: pending } : payload(path));
  await h.ui.connect(); await h.ui.switchView("notifications");
  const tab = h.other.querySelector('[data-notification-tab="approvals"]');
  assert.ok(tab && tab.textContent.includes("待批准"));
  await click(h, tab);
  assert.equal(h.ui.state.notificationTab, "approvals");
  assert.deepEqual(h.other.querySelectorAll(".approval-item").map((row) => row.dataset.id), ["first", "second"]);
  assert.equal(h.ui.state.activeSession.id, "main");
  pending = [pending[1]];
  h.latestStream().onEvent({ id: "21", type: "approval.resolved", session_id: "worker-session-A", data: { id: "first", decision: "approve" } });
  await flush();
  assert.deepEqual(rows(h).map((row) => row.dataset.id), ["second"]);
  assert.deepEqual(h.other.querySelectorAll(".approval-item").map((row) => row.dataset.id), ["second"]);
  pending = [];
  h.latestStream().onEvent({ id: "22", type: "session.recovered", session_id: "worker-session-B", data: { run_id: "worker-run-B" } });
  await flush();
  assert.equal(rows(h).length, 0); assert.equal(h.other.querySelectorAll(".approval-item").length, 0);
  // An old event is never itself an executable approval. Only the fresh live
  // endpoint may reintroduce a button, and that endpoint is still empty.
  h.latestStream().onEvent({ id: "23", type: "approval.requested", session_id: "worker-session-A", data: approval("historical") });
  await flush();
  assert.equal(rows(h).length, 0); assert.equal(h.ui.state.approvals.length, 0);
  assert.equal(decisions(h).length, 0); assert.equal(h.timers.size, 0);
});

test("one decision is protected from double clicks while another remains independent and a failed POST stays pending", async (t) => {
  let pending = [approval("first"), approval("second", "worker-session-B", "worker-run-B")];
  const firstPost = deferred(), secondPost = deferred();
  const h = harness(t, async (path, options) => {
    if (options.method === "POST") return path === "/approvals/first" ? firstPost.promise : secondPost.promise;
    return path === "/approvals" ? { items: pending } : payload(path);
  });
  await h.ui.connect();
  const firstButton = decisionButton(h, "first"), secondButton = decisionButton(h, "second", "reject");
  const firstAction = h.ui.approve("first", "approve", firstButton);
  await flush();
  assert.equal(decisionButton(h, "first").disabled, true);
  assert.equal(decisionButton(h, "second", "reject").disabled, false);
  await h.ui.approve("first", "approve", firstButton);
  assert.equal(decisions(h).length, 1);
  const secondAction = h.ui.approve("second", "reject", secondButton);
  await flush(); assert.equal(decisions(h).length, 2);
  assert.deepEqual(decisions(h).map((entry) => [entry.path, entry.body.decision]), [["/approvals/first", "approve"], ["/approvals/second", "reject"]]);
  assert.equal(rows(h).length, 2, "an in-flight request is not a resolved approval");
  secondPost.reject(new ApiError("此处合成传输失败", 503)); await secondAction;
  assert.ok(h.errors.includes("此处合成传输失败"));
  assert.ok(decisionButton(h, "second", "reject")); assert.equal(decisionButton(h, "second", "reject").disabled, false);
  assert.equal(h.toasts.length, 0, "the failed decision must not report success");
  pending = [pending[1]]; firstPost.resolve({ resolved: true }); await firstAction;
  assert.deepEqual(rows(h).map((row) => row.dataset.id), ["second"]);
  assert.equal(h.toasts.length, 1); assert.ok(h.toasts[0].includes("批准"));
  assert.equal(h.ui.state.session.approvals.length, 0);
});

test("a malformed decision receipt never removes the live item or reports success", async (t) => {
  const item = approval("first");
  const h = harness(t, async (path, options) => {
    if (options.method === "POST") return { resolved: false };
    return path === "/approvals" ? { items: [item] } : payload(path);
  });
  await h.ui.connect();
  await h.ui.approve("first", "approve", decisionButton(h, "first"));
  assert.deepEqual(rows(h).map((row) => row.dataset.id), ["first"]);
  assert.equal(decisionButton(h, "first").disabled, false);
  assert.equal(h.toasts.length, 0); assert.equal(h.errors.length, 1);
  assert.equal(decisions(h).length, 1); assert.equal(approvalReads(h).length, 2);
});

test("late approval reads and an old 401 cannot publish into a replacement connection", async (t) => {
  for (const outcome of ["success", "unauthorized"]) {
    const late = deferred(); let reads = 0;
    const h = harness(t, async (path) => {
      if (path !== "/approvals") return payload(path);
      reads += 1;
      if (reads === 2) return late.promise;
      return { items: [approval(reads === 1 ? "old" : "new", "new-session", "new-run")] };
    });
    await h.ui.connect(); const oldStream = h.latestStream();
    const oldRead = h.ui.refreshApprovals(); await flush();
    await h.ui.connect(); const owner = h.ui.owner;
    if (outcome === "success") late.resolve({ items: [approval("stale-secret")] });
    else late.reject(new ApiError("old authentication expired", 401));
    await oldRead; await flush();
    assert.equal(h.ui.owner === owner, true); assert.equal(h.ui.state.auth.authenticated, true);
    assert.deepEqual(rows(h).map((row) => row.dataset.id), ["new"]);
    assert.equal(h.ui.state.approvalsError, "");
    const requests = h.requests.length;
    oldStream.onEvent({ id: "24", type: "approval.requested", data: approval("stale-event") });
    oldStream.reject(new ApiError("old stream expired", 401)); await flush();
    assert.equal(h.requests.length, requests); assert.equal(h.ui.owner === owner, true);
    assert.equal(h.ui.state.auth.authenticated, true); assert.equal(h.timers.size, 0);
  }
});

test("late decision success or 401 cannot delete the new connection item with the same ID", async (t) => {
  for (const outcome of ["success", "unauthorized"]) {
    const late = deferred(); let reads = 0;
    const h = harness(t, async (path, options) => {
      if (options.method === "POST") return late.promise;
      if (path === "/approvals") return { items: [approval("shared-id", "worker-session", ++reads === 1 ? "old-run" : "new-run")] };
      return payload(path);
    });
    await h.ui.connect(); const oldButton = decisionButton(h, "shared-id");
    const deciding = h.ui.approve("shared-id", "approve", oldButton); await flush();
    await h.ui.connect(); const owner = h.ui.owner;
    if (outcome === "success") late.resolve({ resolved: true });
    else late.reject(new ApiError("old decision authentication expired", 401));
    await deciding; await flush();
    assert.equal(h.ui.owner === owner, true); assert.equal(h.ui.state.auth.authenticated, true);
    assert.equal(rows(h).length, 1); assert.ok(rows(h)[0].textContent.includes("new-run"));
    assert.equal(decisionButton(h, "shared-id").disabled, false);
    assert.equal(h.toasts.length, 0); assert.equal(h.errors.length, 0);
    await h.ui.approve("shared-id", "approve", oldButton);
    assert.equal(decisions(h).length, 1, "a detached button cannot authorize the replacement live item");
  }
});

test("logout clears sensitive approval views and a late read cannot restore them", async (t) => {
  const late = deferred(); let reads = 0;
  const h = harness(t, async (path) => {
    if (path === "/approvals") return ++reads < 3 ? { items: [approval("private-item")] } : late.promise;
    return payload(path);
  });
  await h.ui.connect(); await h.ui.switchView("notifications");
  await click(h, h.other.querySelector('[data-notification-tab="approvals"]'));
  assert.equal(h.other.querySelectorAll(".approval-item").length, 1);
  const oldButton = h.other.querySelector('[data-action="approval"]');
  const read = h.ui.refreshApprovals(); await flush();
  h.ui.showLogin();
  assert.equal(h.ui.owner === null, true); assert.equal(h.ui.state.auth.authenticated, false);
  assert.equal(h.ui.state.approvals.length, 0); assert.equal(h.ui.state.approvalsError, "");
  assert.equal(rows(h).length, 0); assert.equal(h.other.textContent.includes("private-item"), false);
  late.resolve({ items: [approval("private-item")] }); await read; await flush();
  await h.ui.approve("private-item", "approve", oldButton);
  assert.equal(h.ui.state.approvals.length, 0); assert.equal(rows(h).length, 0);
  assert.equal(decisions(h).length, 0); assert.equal(h.timers.size, 0);
});

test("a same-connection old GET cannot resurrect an approval already resolved by another device", async (t) => {
  const oldRead = deferred(), freshRead = deferred(); let reads = 0;
  const item = approval("resolved-elsewhere");
  const h = harness(t, async (path) => {
    if (path !== "/approvals") return payload(path);
    reads += 1;
    if (reads === 1) return { items: [item] };
    return reads === 2 ? oldRead.promise : freshRead.promise;
  });
  await h.ui.connect(); const owner = h.ui.owner, oldButton = decisionButton(h, item.id);
  const refreshing = h.ui.refreshApprovals(); await flush();
  assert.equal(approvalReads(h).length, 2);
  h.latestStream().onEvent({ id: "25", type: "approval.resolved", session_id: item.session_id, data: { id: item.id, decision: "approve" } });
  await flush();
  assert.equal(h.ui.owner === owner, true); assert.equal(rows(h).length, 0);
  assert.equal(h.ui.state.approvals.length, 0);
  // The existing lane serializes reads. Its next read is already requested;
  // release the old snapshot while keeping that newer read unresolved.
  oldRead.resolve({ items: [item] }); await refreshing; await flush();
  assert.equal(approvalReads(h).length, 3);
  assert.equal(rows(h).length, 0, "the old snapshot must not restore a clickable card while the fresh GET is pending");
  assert.equal(h.ui.state.approvals.length, 0);
  await h.ui.approve(item.id, "approve", oldButton);
  assert.equal(decisions(h).length, 0);
  freshRead.resolve({ items: [] }); await flush();
  assert.equal(rows(h).length, 0); assert.equal(h.ui.owner === owner, true);
});

test("the initial SSE open does not duplicate bootstrap but a reconnect revalidates live approvals", async (t) => {
  let pending = [approval("before-disconnect")];
  const h = harness(t, async (path) => path === "/approvals" ? { items: pending } : payload(path));
  await h.ui.connect(); const stream = h.latestStream(), owner = h.ui.owner;
  assert.equal(approvalReads(h).length, 1);
  stream.onOpen(); await flush();
  assert.equal(approvalReads(h).length, 1, "the first stream open uses the bootstrap projection");
  pending = [];
  stream.onOpen(); await flush();
  assert.equal(approvalReads(h).length, 2);
  assert.equal(rows(h).length, 0); assert.equal(h.ui.state.approvals.length, 0);
  assert.equal(h.ui.owner === owner, true); assert.equal(h.timers.size, 0);
});

test("an unrelated notification refresh keeps the connected approval button focused", async (t) => {
  const item = approval("keyboard-decision");
  let notificationRead = null;
  const h = harness(t, async (path) => {
    if (path === "/approvals") return { items: [item] };
    if (path === "/notifications" && notificationRead) return notificationRead.promise;
    return payload(path);
  });
  await h.ui.connect(); await h.ui.switchView("notifications");
  await click(h, h.other.querySelector('[data-notification-tab="approvals"]'));
  const owner = h.ui.owner, button = decisionButton(h, item.id, "reject", h.other);
  assert.ok(button instanceof DOMButton); button.focus();
  assert.equal(h.document.contains(button), true);
  assert.equal(h.document.activeElement === button, true);
  const notificationReads = h.requests.filter((entry) => entry.path === "/notifications").length;
  const liveReads = approvalReads(h).length;
  notificationRead = deferred();
  h.latestStream().onEvent({ id: "26", type: "notification.created", session_id: "unrelated-session", data: { id: "unrelated-notification" } });
  await flush();
  assert.equal(h.requests.filter((entry) => entry.path === "/notifications").length, notificationReads + 1);
  assert.equal(approvalReads(h).length, liveReads, "an unrelated notification does not request a new approval projection");
  notificationRead.resolve({ items: [{ id: "unrelated-notification", session_id: "unrelated-session", title: "另一项工作已完成", read: false, created_at: 1 }] });
  await flush();
  assert.deepEqual(Array.from(h.ui.state.notifications, (entry) => entry.id), ["unrelated-notification"], "the late notification GET must actually publish");
  const focused = h.document.activeElement;
  assert.equal(h.document.contains(focused), true, "a detached old button is not retained keyboard focus");
  assert.equal(focused instanceof DOMButton, true);
  assert.equal(focused.dataset.id, item.id); assert.equal(focused.dataset.decision, "reject");
  assert.equal(focused === decisionButton(h, item.id, "reject", h.other), true);
  assert.equal(focused.disabled, false);
  assert.equal(h.ui.state.notificationTab, "approvals"); assert.equal(h.ui.owner === owner, true);
  assert.equal(decisions(h).length, 0); assert.equal(h.timers.size, 0);
});
