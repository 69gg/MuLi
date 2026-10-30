import test from "node:test";
import { composerIdentity, createComposerDraft, MAX_DRAFT_ATTACHMENTS, sameComposerIdentity, validDraftAttachment } from "../../web/composer-draft.mjs";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";
import { ApiError } from "../../web/api.mjs";
import { createSessionState, hydrateSession, applyEvent, isRunning } from "../../web/state.mjs";
import { createTeamPager } from "../../web/team.mjs";
import { createTaskControl } from "../../web/task-control.mjs";
import { VoiceSetup, voiceSetupLabel } from "../../web/voice-setup.mjs";

// Exercise the real connection code; controlled promises/timers create races
// without a browser, network, service, wall-clock retry or large object diff.
const app = await readFile(new URL("../../web/app.mjs", import.meta.url), "utf8");
const application = app.slice(app.indexOf("const ICONS ="), app.indexOf("\nfillIcons();"));
const flush = () => new Promise((resolve) => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function payload(path) {
  if (path === "/auth/status") return { required: false, authenticated: true };
  if (path === "/ui/event-position") return { after: "20" };
  if (path === "/status") return { mode: "auto" };
  if (path === "/conversation") return { session: { id: "main", status: "idle" }, messages: [], events: [] };
  return { items: [] };
}

function harness(t, handler = async (path) => payload(path)) {
  const requests = [], streams = [], nodes = new Map(), timers = new Map();
  const windowEvents = {}, documentEvents = {};
  let timerSequence = 0;
  const document = {
    visibilityState: "visible", activeElement: null,
    addEventListener(name, callback) { documentEvents[name] = callback; },
    querySelector: node, querySelectorAll: () => [],
  };
  function node(selector) {
    if (!nodes.has(selector)) nodes.set(selector, {
      hidden: selector === "#auth-gate", open: false, value: "", dataset: {},
      addEventListener() {}, querySelector: node, querySelectorAll: () => [],
      replaceChildren() {}, close() { this.open = false; },
      focus() { document.activeElement = this; },
    });
    return nodes.get(selector);
  }
  const context = {
    composerIdentity, createComposerDraft, MAX_DRAFT_ATTACHMENTS, sameComposerIdentity, validDraftAttachment,
    document, window: { addEventListener(name, callback) { windowEvents[name] = callback; } },
    AbortController, URLSearchParams, console,
    createSessionState, hydrateSession, applyEvent, isRunning, createTeamPager, VoiceSetup, voiceSetupLabel,
    VoiceInput: class { cancel() {} }, uploadFile() {}, transcribeAttachment() {},
    setTimeout(callback, delay) { const id = ++timerSequence; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    apiRequest: async (path, options = {}) => { requests.push({ path, ...options }); return handler(path, options); },
    readEvents: async (path, options) => {
      const pending = deferred(); streams.push({ path, ...options, ...pending });
      // A transport may ignore abort. Ownership must still reject its callbacks.
      await pending.promise;
    },
  };
  runInNewContext(application + `
    renderChrome = renderComposer = renderSessions = renderChat = renderOtherView = () => {};
    resetMessageNodes = queueRender = renderTaskBriefDialog = () => {};
    globalThis.ui = { state, connect, beginConnection, invalidateConnection, connectionCurrent,
      createSummaryLane, ownedRequest, startGlobalEvents, startSessionEvents,
      refreshSessions, refreshNotifications, refreshUploads, refreshTeam, markRead, showLogin,
      switchView, refreshTaskBriefDialog, revalidateVisibleState,
      activate() {
        const owner = beginConnection(); owner.ready = true;
        owner.sessions.start(); owner.notifications.start();
        state.auth = { required: false, authenticated: true }; return owner;
      },
      setTaskDialog(slot) {
        slot.owner = connection; slot.viewVersion = viewRequestVersion;
        taskDialog = slot; $("#app-dialog").open = true;
        $("#dialog-content").firstElementChild = slot.content;
      },
      get taskDialog() { return taskDialog; },
      get owner() { return connection; }
    };
  `, context);
  const ui = context.ui;
  t.after(() => { ui.invalidateConnection(); for (const stream of streams) stream.resolve(); });
  return {
    ui, requests, streams, timers, windowEvents, documentEvents, document, node,
    latestStream: (path = "/events") => streams.filter((stream) => stream.path === path).at(-1),
    retry() {
      assert.equal(timers.size, 1, "exactly one reconnect should be waiting");
      const timer = timers.values().next().value; timer.callback();
    },
  };
}

test("bootstrap captures an exact global position before reading snapshots or starting streams", async (t) => {
  const position = deferred(), after = "9007199254740993";
  const h = harness(t, async (path) => path === "/ui/event-position" ? position.promise : payload(path));
  const connecting = h.ui.connect(); await flush();
  assert.deepEqual(h.requests.map((entry) => entry.path), ["/auth/status", "/ui/event-position"]);
  assert.equal(h.requests[1].cache, "no-store"); assert.equal(h.streams.length, 0);
  position.resolve({ after }); await connecting;
  assert.equal(h.latestStream().after, after);
  assert.equal(h.latestStream("/sessions/main/events").after, "");
  assert.equal(h.ui.state.connected, true);
  assert.equal(h.requests.filter((entry) => entry.path === "/sessions").length, 1);
  assert.equal(h.requests.filter((entry) => entry.path === "/notifications").length, 1);
});

test("an unavailable or malformed event position fails before snapshot reads and global replay", async (t) => {
  for (const response of [null, { after: 1 }, { after: "" }, { after: "01" }, { after: "-1" }, { after: "9223372036854775808" }]) {
    const h = harness(t, async (path) => path === "/ui/event-position" ? response : payload(path));
    await h.ui.connect();
    assert.equal(h.ui.owner === null, true); assert.equal(h.ui.state.connected, false);
    assert.equal(h.streams.length, 0); assert.equal(h.requests.length, 2);
  }
  const h = harness(t, async (path) => {
    if (path === "/ui/event-position") throw new ApiError("unavailable", 503);
    return payload(path);
  });
  await h.ui.connect(); assert.equal(h.streams.length, 0); assert.equal(h.requests.length, 2);
});

test("a replacement connection rejects late reads and old 401 without logging out its owner", async (t) => {
  const late = deferred();
  const h = harness(t, async (path) => path === "/slow" ? late.promise : payload(path));
  const old = h.ui.activate();
  const outcome = h.ui.ownedRequest("/slow", {}, old).catch((error) => error.name);
  const owner = h.ui.activate(); late.reject(new ApiError("old login expired", 401));
  assert.equal(await outcome, "AbortError");
  assert.equal(h.ui.owner === owner, true); assert.equal(h.ui.state.auth.authenticated, true);
  assert.equal(h.requests[0].signal.aborted, true);
});

test("a replaced voice setup cannot publish or log out a new connection on late 401", async (t) => {
  const late = deferred();
  const h = harness(t, async (path) => path === "/voice/setup" ? late.promise : payload(path));
  h.ui.activate();
  Object.assign(h.ui.state, { connected: true, view: "settings", settingsTab: "inputs" });
  const setup = h.ui.state.voiceSetupController, recording = setup.run("enroll");
  const request = h.requests.find((item) => item.path === "/voice/setup");
  assert.equal(setup.status().phase, "recording");
  const owner = h.ui.activate(); late.reject(new ApiError("old login expired", 401));
  await recording;
  assert.equal(request.signal.aborted, true);
  assert.equal(h.ui.owner === owner, true); assert.equal(h.ui.state.auth.authenticated, true);
  assert.equal(setup.status().phase, "idle"); assert.equal(setup.status().error, "");
});

test("an old bootstrap cannot hydrate or start streams after a new connection finishes", async (t) => {
  const oldStatus = deferred(); let statusReads = 0, conversations = 0;
  const h = harness(t, async (path) => {
    if (path === "/status" && ++statusReads === 1) return oldStatus.promise;
    if (path === "/conversation") return { session: { id: `main-${++conversations}`, status: "idle" }, messages: [], events: [] };
    return payload(path);
  });
  const old = h.ui.connect(); await flush(); await h.ui.connect();
  oldStatus.resolve({ mode: "ask" }); await old;
  assert.equal(h.ui.state.mainSessionId, "main-2"); assert.equal(h.ui.state.mode, "auto");
  assert.deepEqual(h.streams.map((stream) => stream.path), ["/sessions/main-2/events", "/events"]);
});

test("session bootstrap resumes its exact snapshot position and a live page settles after server recovery", async (t) => {
  const after = "9007199254740993";
  const h = harness(t, async (path) => path !== "/conversation" ? payload(path) : {
    session: { id: "main", status: "running" }, messages: [], after,
    events: [{ id: "1", type: "run.started", data: { id: "run" } }],
    live: { session_id: "main", run_id: "run", status: "approval", run: { id: "run", status: "running" },
      pending_approvals: [{ id: "pending", session_id: "main", run_id: "run", status: "pending" }] },
  });
  await h.ui.connect();
  const stream = h.latestStream("/sessions/main/events");
  assert.equal(stream.after, after); assert.equal(h.ui.state.session.status, "approval");
  stream.onEvent({ id: "9007199254740994", type: "session.recovered", session_id: "main", data: { kind: "run", run_id: "run", session_status: "idle" } });
  assert.equal(h.ui.state.session.status, "interrupted"); assert.equal(h.ui.state.session.approvals.length, 0);
  stream.onEvent({ id: "9007199254740992", type: "approval.requested", data: { id: "old" } });
  assert.equal(h.ui.state.session.approvals.length, 0);
});

test("invalid conversation continuation positions never start either stream", async (t) => {
  const h = harness(t, async (path) => path === "/conversation" ? { ...payload(path), after: "01" } : payload(path));
  await h.ui.connect();
  assert.equal(h.ui.owner === null, true); assert.equal(h.ui.state.connected, false);
  assert.equal(h.streams.length, 0);
});

test("late stream callbacks and 401 from a replaced owner cannot refresh or show login", async (t) => {
  const h = harness(t); await h.ui.connect(); const stale = h.latestStream();
  const owner = h.ui.activate(); const count = h.requests.length;
  stale.onOpen(); stale.onEvent({ id: "stale", type: "run.completed", data: {} });
  stale.reject(new ApiError("old stream expired", 401)); await flush();
  assert.equal(h.ui.owner === owner, true); assert.equal(h.ui.state.auth.authenticated, true);
  assert.equal(h.ui.state.streamConnected, false); assert.equal(h.requests.length, count);
  assert.equal(h.timers.size, 0);
});

test("a current stream 401 clears the login state and aborts both streams without retrying", async (t) => {
  const h = harness(t); await h.ui.connect();
  h.latestStream().reject(new ApiError("login expired", 401)); await flush();
  assert.equal(h.ui.owner === null, true); assert.equal(h.ui.state.auth.authenticated, false);
  assert.equal(h.ui.state.connected, false); assert.equal(h.node("#auth-gate").hidden, false);
  assert.equal(h.streams.every((stream) => stream.signal.aborted), true); assert.equal(h.timers.size, 0);
});

test("global reconnects preserve exact ID-only positions and explicit empty resets", async (t) => {
  const h = harness(t); await h.ui.connect(); const first = h.latestStream();
  first.onId(""); first.reject(new ApiError("offline", 503)); await flush();
  h.retry(); await flush(); const second = h.latestStream(); assert.equal(second.after, "");
  second.onId("9007199254740995"); second.resolve(); await flush();
  h.retry(); await flush(); assert.equal(h.latestStream().after, "9007199254740995");
  assert.equal(h.requests.filter((entry) => entry.path === "/ui/event-position").length, 1);
});

test("pagehide invalidates work and a persisted pageshow captures a fresh position once", async (t) => {
  let positions = 0;
  const h = harness(t, async (path) => path === "/ui/event-position" ? { after: String(++positions * 20) } : payload(path));
  await h.ui.connect(); const old = h.latestStream();
  h.windowEvents.pagehide(); assert.equal(old.signal.aborted, true);
  assert.equal(h.ui.owner === null, true); assert.equal(h.ui.state.connected, false);
  h.windowEvents.pageshow({ persisted: true }); h.windowEvents.online(); await flush();
  assert.equal(positions, 2); assert.equal(h.latestStream().after, "40");
  assert.equal(h.ui.state.connected, true);
});

test("browser recovery waits for visibility and never bypasses a visible login gate", async (t) => {
  const h = harness(t); h.document.visibilityState = "hidden"; h.windowEvents.online();
  assert.equal(h.requests.length, 0);
  h.document.visibilityState = "visible"; h.ui.showLogin(); h.windowEvents.online();
  h.windowEvents.pageshow({ persisted: true }); h.documentEvents.visibilitychange(); await flush();
  assert.equal(h.requests.length, 0); assert.equal(h.ui.state.auth.authenticated, false);
});

test("summary bursts share one next read and each explicit wait finishes at its own pass", async (t) => {
  const reads = [];
  const h = harness(t, async (path) => {
    if (path !== "/sessions") return payload(path);
    const pending = deferred(); reads.push(pending); return pending.promise;
  });
  h.ui.activate(); const first = h.ui.refreshSessions(), next = h.ui.refreshSessions();
  for (let index = 0; index < 1000; index += 1) assert.equal(h.ui.refreshSessions() === next, true);
  assert.equal(reads.length, 1);
  reads[0].resolve({ items: [{ id: "first" }] }); assert.equal(await first, true);
  assert.equal(reads.length, 2); assert.equal(h.ui.state.sessions[0].id, "first");
  const third = h.ui.refreshSessions(); let thirdFinished = false;
  void third.then(() => { thirdFinished = true; });
  reads[1].resolve({ items: [{ id: "second" }] }); assert.equal(await next, true);
  assert.equal(thirdFinished, false); assert.equal(reads.length, 3);
  reads[2].resolve({ items: [{ id: "third" }] }); assert.equal(await third, true);
  assert.equal(h.ui.state.sessions[0].id, "third");
});

test("summary failures settle current and queued waits without looping, then allow a real retry", async (t) => {
  const reads = [];
  const h = harness(t, async () => { const pending = deferred(); reads.push(pending); return pending.promise; });
  h.ui.activate(); const active = h.ui.refreshSessions(), queued = h.ui.refreshSessions();
  reads[0].reject(new ApiError("offline", 503));
  assert.equal(await active, false); assert.equal(await queued, false); await flush();
  assert.equal(reads.length, 1); assert.equal(h.ui.state.sessionsError, "offline");
  const retry = h.ui.refreshSessions(); reads[1].resolve({ items: [{ id: "fresh" }] });
  assert.equal(await retry, true); assert.equal(h.ui.state.sessionsError, "");
});

test("connection invalidation settles summary waits immediately and ignores their late payload", async (t) => {
  const slow = deferred(); const h = harness(t, async () => slow.promise);
  h.ui.activate(); const active = h.ui.refreshSessions(), queued = h.ui.refreshSessions();
  h.ui.invalidateConnection(); assert.equal(await active, false); assert.equal(await queued, false);
  h.ui.activate(); slow.resolve({ items: [{ id: "old" }] }); await flush();
  assert.equal(h.ui.state.sessions.length, 0); assert.equal(h.ui.state.auth.authenticated, true);
});

test("mark read waits for a post-mutation notification pass while an earlier snapshot is pending", async (t) => {
  const reads = [];
  const h = harness(t, async (path, options) => {
    if (options.method === "PATCH") return {};
    const pending = deferred(); reads.push(pending); return pending.promise;
  });
  h.ui.activate(); const initial = h.ui.refreshNotifications(); let marked = false;
  const marking = h.ui.markRead("notification").then(() => { marked = true; }); await flush();
  assert.equal(reads.length, 1);
  reads[0].resolve({ items: [{ id: "notification", read: false }] }); await initial; await flush();
  assert.equal(reads.length, 2); assert.equal(marked, false);
  reads[1].resolve({ items: [{ id: "notification", read: true }] }); await marking;
  assert.equal(marked, true); assert.equal(h.ui.state.notifications[0].read, true);
});

test("replaced upload and team refreshes cannot publish stale data or log out the new owner", async (t) => {
  for (const view of ["uploads", "team"]) {
    const late = deferred(); let reads = 0;
    const h = harness(t, async () => ++reads === 1 ? late.promise : view === "uploads" ? { items: [{ id: "fresh" }] } : { members: [{ id: "fresh" }], limit: 50, offset: 0, active_only: false });
    h.ui.activate(); h.ui.state.view = view;
    const refresh = () => view === "uploads" ? h.ui.refreshUploads() : h.ui.refreshTeam();
    const stale = refresh(); h.ui.activate(); await refresh();
    late.reject(new ApiError("old owner expired", 401)); await stale;
    assert.equal(h.ui.state.auth.authenticated, true);
    assert.equal((view === "uploads" ? h.ui.state.uploads : h.ui.state.team.members)[0].id, "fresh");
  }
});

test("real global events merge independent session and notification reads while both are pending", async (t) => {
  const reads = new Map([["/sessions", []], ["/notifications", []]]);
  const h = harness(t, async (path) => {
    const pending = deferred(); reads.get(path).push(pending); return pending.promise;
  });
  h.ui.activate(); h.ui.startGlobalEvents(); const stream = h.latestStream();
  for (let index = 1; index <= 1000; index += 1) stream.onEvent({ id: String(index), type: "run.completed", data: {} });
  assert.equal(reads.get("/sessions").length, 1); assert.equal(reads.get("/notifications").length, 1);
  reads.get("/sessions")[0].resolve({ items: [{ id: "sessions-first" }] }); await flush();
  assert.equal(reads.get("/sessions").length, 2); assert.equal(reads.get("/notifications").length, 1);
  reads.get("/notifications")[0].resolve({ items: [{ id: "notifications-first" }] }); await flush();
  assert.equal(reads.get("/notifications").length, 2);
  reads.get("/sessions")[1].resolve({ items: [{ id: "sessions-final" }] });
  reads.get("/notifications")[1].resolve({ items: [{ id: "notifications-final" }] }); await flush();
  assert.equal(h.requests.length, 4); assert.equal(h.ui.state.sessions[0].id, "sessions-final");
  assert.equal(h.ui.state.notifications[0].id, "notifications-final");
});

test("new connection closes the old task brief and its completion cannot unlock a reopened read", async (t) => {
  const reads = [];
  const h = harness(t, async () => { const pending = deferred(); reads.push(pending); return pending.promise; });
  h.ui.activate();
  const slot = { content: {}, loading: false, model: createTaskControl("brief") };
  h.ui.setTaskDialog(slot); const stale = h.ui.refreshTaskBriefDialog();
  const previous = slot.readController; const owner = h.ui.activate();
  assert.equal(previous.signal.aborted, true); assert.equal(h.ui.taskDialog === null, true);
  assert.equal(h.node("#app-dialog").open, false); assert.equal(slot.model.state.detail, null);
  h.ui.revalidateVisibleState(owner); assert.equal(reads.length, 1, "换连接不得自动重开旧任务说明");
  const reopened = { content: {}, loading: false, model: createTaskControl("brief") };
  h.ui.setTaskDialog(reopened); h.ui.revalidateVisibleState(owner);
  assert.equal(reads.length, 2);
  const detail = (title) => ({ id: "brief", revision: 1, title, brief: { goal: title, constraints: [], evidence_refs: [] } });
  reads[0].resolve(detail("stale")); await stale;
  assert.equal(reopened.loading, true); assert.equal(reopened.model.state.detail, null); assert.equal(slot.model.state.detail, null);
  reads[1].resolve(detail("fresh")); await flush();
  assert.equal(reopened.loading, false); assert.equal(reopened.model.state.detail.title, "fresh");
  assert.equal(slot.model.state.detail, null, "旧完成不能恢复已清理的任务说明");
});

test("post-connect revalidation refreshes the visible projection without requiring historical events", async (t) => {
  const h = harness(t, async (path) => path === "/uploads" ? { items: [{ id: "missed-while-away" }] } : payload(path));
  h.ui.state.view = "uploads"; await h.ui.connect(); await flush();
  assert.equal(h.requests.filter((entry) => entry.path === "/uploads").length, 2);
  assert.equal(h.ui.state.uploads[0].id, "missed-while-away");
});
