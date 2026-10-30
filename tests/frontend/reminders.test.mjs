import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";

import { toDateTimeLocal, fromDateTimeLocal } from "../../web/state.mjs";
import { createPreviewBackend, createPreviewState } from "../../web/preview.mjs";

// Execute the actual reminder renderer, dialog callbacks, action dispatcher and
// global-event handler with a small DOM, without loading a browser or server.
const app = await readFile(new URL("../../web/app.mjs", import.meta.url), "utf8");
const section = (start, end) => app.slice(app.indexOf(start), app.indexOf(end, app.indexOf(start)));
const source = [
  section("function element(", "\nfunction toast("),
  section("function confirmAction(", "\nfunction viewHeading("),
  section("function emptyState(", "\nasync function switchView("),
  section("function intervalLabel(", "\nasync function markRead("),
  section("async function handleAction(", '\ndocument.addEventListener("click"'),
  section("function startGlobalEvents(", "\nfunction startSessionEvents("),
].join("\n");

class DOMNode {}
class TextNode extends DOMNode {
  constructor(text) { super(); this.textContent = String(text); }
}
class DOMElement extends DOMNode {
  constructor(tag) {
    super(); this.tagName = tag.toUpperCase(); this.childNodes = []; this.dataset = {};
    this.attributes = {}; this.events = {}; this.className = ""; this.value = "";
    this.name = ""; this.type = ""; this.checked = false; this.disabled = false; this.hidden = false;
  }
  get children() { return this.childNodes.filter((node) => node instanceof DOMElement); }
  get textContent() { return this.childNodes.map((node) => node.textContent).join(""); }
  set textContent(text) { this.childNodes = [new TextNode(text)]; }
  set innerHTML(value) { this.iconHTML = value; }
  append(...nodes) { this.childNodes.push(...nodes.map((node) => node instanceof DOMNode ? node : new TextNode(node))); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, handler) { (this.events[name] ??= []).push(handler); }
  querySelectorAll(selector) {
    const nodes = this.children.flatMap((node) => [node, ...node.querySelectorAll("*")]);
    return nodes.filter((node) => {
      if (selector === "*") return true;
      if (selector === "[name]") return !!node.name;
      const name = selector.match(/^\[name="([^"]+)"\]$/)?.[1];
      return name === undefined ? node.tagName === selector.toUpperCase() : node.name === name;
    });
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
}

const due = () => Math.floor(Date.now() / 60000) * 60 + 3600;
function pair(extra = {}) {
  const common = { id: "same/id", due_at: due(), enabled: true, fired_count: 0, interval_seconds: null, content: "", ...extra };
  return [
    { ...common, kind: "reminder", title: "普通提醒" },
    { ...common, kind: "calendar_reminder", title: "日历提醒", calendar: { version: 1, timezone: "Asia/Shanghai", local_time: "07:30", weekdays: [1, 3, 5] } },
  ];
}

function harness(items = pair()) {
  const requests = [], messages = [];
  let dialog = null, globalEvent = null;
  const state = { reminders: structuredClone(items), remindersError: "", view: "notifications", notificationTab: "reminders" };
  const context = {
    state, Node: DOMNode, ICONS: {}, Date, URLSearchParams, AbortController,
    document: { createElement: (tag) => new DOMElement(tag), createTextNode: (text) => new TextNode(text) },
    $: (selector, root) => root.querySelector(selector),
    formatFullDate: String, toDateTimeLocal, fromDateTimeLocal,
    openDialog: (options) => { dialog = options; },
    request: async (path, options) => { requests.push({ path, method: options.method, body: options.body === undefined ? undefined : JSON.parse(JSON.stringify(options.body)) }); return {}; },
    switchView: async () => {}, toast: (message) => messages.push(message),
    connection: { ready: true, position: "0" }, globalController: null,
    connectionCurrent: () => true, renderOtherView: () => {},
    releaseAnswerSpeech() {},
    channelDeliveryDialog: null, channelDeliveryDialogCurrent: () => false,
    refreshApprovals: async () => {}, refreshNotifications: async () => {}, refreshSessions: async () => {},
    subscribe: (_path, _controller, options) => { globalEvent = options.onEvent; },
    FormData: class {
      constructor(root) {
        this.values = new Map(root.querySelectorAll("[name]")
          .filter((input) => input.type !== "checkbox" || input.checked)
          .map((input) => [input.name, input.type === "checkbox" ? "on" : input.value]));
      }
      get(name) { return this.values.get(name) ?? null; }
    },
  };
  runInNewContext(`${source}\nglobalThis.ui = { renderReminders, handleAction, startGlobalEvents };`, context);
  let root = context.ui.renderReminders();
  return {
    state, requests, messages, get root() { return root; }, get dialog() { return dialog; },
    render() { root = context.ui.renderReminders(); return root; },
    button(action, kind) { return root.querySelectorAll("button").find((button) => button.dataset.action === action && button.dataset.kind === kind); },
    async action(action, kind) {
      const button = this.button(action, kind); assert.equal(!!button, true);
      await context.ui.handleAction(action, button); return button;
    },
    async submit(changes = {}) {
      assert.equal(!!dialog, true);
      for (const [name, value] of Object.entries(changes)) {
        const input = dialog.content.querySelector(`[name="${name}"]`); assert.equal(!!input, true);
        if (input.type === "checkbox") input.checked = value;
        else input.value = String(value);
      }
      await dialog.onSubmit(dialog.content);
    },
    event(event) { if (!globalEvent) context.ui.startGlobalEvents(); globalEvent(event); },
  };
}

test("calendar rows show their rule and paused state while every management button carries its namespace", () => {
  const h = harness(pair({ enabled: false, fired_count: 2 }));
  const rows = h.root.querySelectorAll("article");
  const calendar = rows.find((row) => row.textContent.includes("日历提醒"));
  const plain = rows.find((row) => row.textContent.includes("普通提醒"));
  assert.equal(calendar.textContent.includes("Asia/Shanghai · 07:30 · 周一、周三、周五"), true);
  assert.equal(calendar.textContent.includes("已暂停"), true);
  assert.equal(calendar.textContent.includes("已完成"), false);
  assert.equal(plain.textContent.includes("已完成"), true);
  assert.deepEqual(h.root.querySelectorAll("button").map((button) => button.dataset.kind).sort(), ["calendar_reminder", "calendar_reminder", "calendar_reminder", "reminder", "reminder", "reminder"]);
});

for (const kind of ["reminder", "calendar_reminder"]) {
  test(`editing ${kind} selects the exact same-ID row and preserves its scheduling contract`, async () => {
    const h = harness();
    await h.action("edit-reminder", kind);
    const title = h.dialog.content.querySelector('[name="title"]');
    assert.equal(title.value, kind === "calendar_reminder" ? "日历提醒" : "普通提醒");
    assert.equal(!!h.dialog.content.querySelector('[name="interval"]'), kind === "reminder");
    assert.equal(!!h.dialog.content.querySelector('[name="custom-interval"]'), kind === "reminder");
    const next = due() + 7200;
    await h.submit({ title: "改好标题", content: "保留规则", due_at: toDateTimeLocal(next) });
    assert.equal(h.requests.length, 1);
    assert.equal(h.requests[0].path, `/reminders/same%2Fid?kind=${kind}`);
    assert.equal(h.requests[0].method, "PATCH");
    assert.equal(h.requests[0].body.due_at, next);
    assert.equal(h.requests[0].body.title, "改好标题");
    assert.equal(h.requests[0].body.content, "保留规则");
    assert.equal(h.requests[0].body.enabled, true);
    assert.equal(Object.hasOwn(h.requests[0].body, "interval_seconds"), kind === "reminder");
    assert.equal(Object.hasOwn(h.requests[0].body, "calendar"), false);
    assert.equal(Object.hasOwn(h.requests[0].body, "kind"), false);
  });

  test(`pausing ${kind} sends an exact namespace request even when its ID is shared`, async () => {
    const h = harness();
    const button = await h.action("toggle-reminder", kind);
    assert.deepEqual(h.requests, [{ path: `/reminders/same%2Fid?kind=${kind}`, method: "PATCH", body: { enabled: false } }]);
    assert.equal(button.disabled, false);
  });

  test(`deleting ${kind} confirms and sends only its own namespace`, async () => {
    const h = harness();
    await h.action("delete-reminder", kind);
    assert.equal(h.requests.length, 0);
    assert.equal(h.dialog.title, "删除提醒");
    await h.submit();
    assert.deepEqual(h.requests, [{ path: `/reminders/same%2Fid?kind=${kind}`, method: "DELETE", body: undefined }]);
  });
}

test("an expired paused calendar opens its own edit dialog without exposing fixed intervals", async () => {
  const h = harness(pair({ enabled: false, due_at: Date.now() / 1000 - 60 }));
  await h.action("toggle-reminder", "calendar_reminder");
  assert.equal(h.requests.length, 0);
  assert.equal(h.dialog.content.querySelector('[name="title"]').value, "日历提醒");
  assert.equal(!!h.dialog.content.querySelector('[name="interval"]'), false);
});

test("new ordinary reminders retain their existing form and POST contract", async () => {
  const h = harness([]);
  const button = new DOMElement("button");
  // The new action has no row or namespace, unlike the three list actions.
  h.root.append(button); button.dataset.action = "new-reminder";
  await h.action("new-reminder", undefined);
  assert.equal(h.dialog.title, "新建提醒");
  assert.equal(!!h.dialog.content.querySelector('[name="interval"]'), true);
  await h.submit({ title: "普通新建", content: "", due_at: toDateTimeLocal(due()) });
  assert.equal(h.requests[0].path, "/reminders");
  assert.equal(h.requests[0].method, "POST");
  assert.equal(h.requests[0].body.interval_seconds, null);
  assert.equal(Object.hasOwn(h.requests[0].body, "enabled"), false);
});

test("reminder SSE updates replace only their own kind and keep legacy ordinary events compatible", () => {
  const h = harness();
  const calendar = h.state.reminders.find((item) => item.kind === "calendar_reminder");
  h.event({ type: "reminder.updated", data: { ...calendar, title: "更新日历" } });
  assert.equal(h.state.reminders.length, 2);
  assert.equal(h.state.reminders.find((item) => item.kind === "reminder").title, "普通提醒");
  h.event({ type: "reminder.updated", data: { id: "same/id", title: "旧普通事件" } });
  assert.equal(h.state.reminders.length, 2);
  assert.equal(h.state.reminders.find((item) => item.kind === "calendar_reminder").title, "更新日历");
  assert.equal(h.state.reminders.find((item) => item.kind === undefined).title, "旧普通事件");
  for (const kind of [null, "unknown"]) h.event({ type: "reminder.updated", data: { id: "same/id", kind, title: "无效类别" } });
  assert.equal(h.state.reminders.length, 2);
  assert.equal(h.state.reminders.some((item) => item.title === "无效类别"), false);
});

async function previewRequest(backend, path, method = "GET", body) {
  const response = await backend.fetch(`/api${path}`, { method, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  return { status: response.status, body: await response.json() };
}

test("preview same-ID mutations use query kind and never fall back or delete both namespaces", async (t) => {
  const state = createPreviewState(); state.reminders = pair();
  const backend = createPreviewBackend({ state, delay: 0 }); t.after(() => backend.dispose());
  const listed = await previewRequest(backend, "/reminders");
  assert.deepEqual(listed.body.items.map((item) => item.kind).sort(), ["calendar_reminder", "reminder"]);
  const calendarBefore = structuredClone(state.reminders[1]);
  assert.equal((await previewRequest(backend, "/reminders/same%2Fid", "PATCH", { title: "默认普通" })).status, 200);
  assert.deepEqual(state.reminders[1], calendarBefore);
  assert.equal((await previewRequest(backend, "/reminders/same%2Fid?kind=calendar_reminder", "PATCH", { title: "只改日历", enabled: false })).status, 200);
  assert.equal(state.reminders[0].title, "默认普通");
  assert.equal(state.reminders[1].title, "只改日历");
  assert.equal(state.reminders[1].enabled, false);
  assert.equal((await previewRequest(backend, "/reminders/same%2Fid?kind=calendar_reminder", "DELETE")).status, 200);
  assert.deepEqual(state.reminders.map((item) => item.kind), ["reminder"]);
  assert.equal((await previewRequest(backend, "/reminders/same%2Fid?kind=calendar_reminder", "PATCH", { title: "禁止回退" })).status, 404);
  assert.equal((await previewRequest(backend, "/reminders/same%2Fid?kind=calendar_reminder", "DELETE")).status, 404);
  assert.equal(state.reminders[0].title, "默认普通");
});

test("preview rejects namespace and calendar-rule confusion without partial edits", async (t) => {
  const state = createPreviewState(); state.reminders = pair();
  const backend = createPreviewBackend({ state, delay: 0 }); t.after(() => backend.dispose());
  const before = structuredClone(state.reminders);
  for (const kind of ["", "unknown", "null"]) {
    for (const method of ["PATCH", "DELETE"]) assert.equal((await previewRequest(backend, `/reminders/same%2Fid?kind=${kind}`, method, { title: "禁止修改" })).status, 422);
  }
  for (const changes of [{ kind: "reminder" }, { id: "other-id" }, { interval_seconds: 3600 }, { calendar: { timezone: "UTC" } }, { followup: null }]) {
    assert.equal((await previewRequest(backend, "/reminders/same%2Fid?kind=calendar_reminder", "PATCH", { title: "不能部分生效", ...changes })).status, 400);
  }
  assert.deepEqual(state.reminders, before);
  assert.equal((await previewRequest(backend, "/reminders/same%2Fid", "DELETE")).status, 200);
  assert.deepEqual(state.reminders.map((item) => item.kind), ["calendar_reminder"]);
  assert.equal((await previewRequest(backend, "/reminders/same%2Fid", "DELETE")).status, 404);
});

test("preview ordinary creation owns its kind instead of accepting a body-selected namespace", async (t) => {
  const backend = createPreviewBackend({ delay: 0 }); t.after(() => backend.dispose());
  const created = await previewRequest(backend, "/reminders", "POST", { title: "普通演示", due_at: due(), interval_seconds: null, kind: "calendar_reminder" });
  assert.equal(created.status, 200);
  assert.equal(created.body.kind, "reminder");
  assert.equal((await previewRequest(backend, `/reminders/${created.body.id}?kind=calendar_reminder`, "DELETE")).status, 404);
  assert.equal(backend.state.reminders.some((item) => item.id === created.body.id && item.kind === "reminder"), true);
});
