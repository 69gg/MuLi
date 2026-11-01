import test from "node:test";
import assert from "node:assert/strict";
import { ApiError } from "../../web/api.mjs";
import { COMPOSER_DRAFT_KEY, createComposerDraft } from "../../web/composer-draft.mjs";
import { createPreviewBackend } from "../../web/preview.mjs";
import { clickMemoryButton, deferred, flush, harness } from "./helpers/app-dom-harness.mjs";

const storeId = "f".repeat(32);
const main = { id: "main", role: "main", mode: "auto", status: "idle" };
const older = { id: "1".repeat(32), name: "较早保存的参赛要求.pdf", mime_type: "application/pdf", size: 41 };
const recent = { id: "2".repeat(32), name: "当前项目说明.pdf", mime_type: "application/pdf", size: 53 };
const item = { id: "draft-recovery-task+原任务", title: "已保存的原任务", status: "in_progress", revision: 2, created_at: 100, updated_at: 200, has_brief: true };
const detail = { ...item, revision: 7, brief: { goal: "原任务完整说明", constraints: ["保留原定位", "先查证后改写"], evidence_refs: [] }, control: { epoch: 0, approved_epoch: 0, replan_request_id: null }, scope: { member_count: 0, active_workflow_count: 0, can_pause: false, blocked_reason: null } };

function pageStorage() {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem(key, value) { values.set(key, String(value)); this[key] = String(value); },
    removeItem(key) { values.delete(key); delete this[key]; },
  };
}

function serverFixture() {
  let refreshed = false;
  const controls = { metadata: null, submit: null, logout: null, conversation: null, auth: { required: true, authenticated: true }, status: {}, extraUploads: [] };
  const files = new Map([[older.id, older], [recent.id, recent]]);
  const latest = [recent, ...Array.from({ length: 199 }, (_, index) => ({ id: (index + 16).toString(16).padStart(32, "0"), name: `后来保存的资料${index}.txt`, mime_type: "text/plain", size: 4 }))];
  return {
    controls,
    refresh() { refreshed = true; },
    async request(path, options = {}) {
      if (path === "/sessions/main/messages" && options.method === "POST" && controls.submit) return controls.submit(options);
      if (path === "/auth/logout" && options.method === "POST" && controls.logout) return controls.logout(options);
      assert.equal(options.method ?? "GET", "GET", "preparing or restoring a work draft must not send or upload anything");
      if (path === "/auth/status") return { ...controls.auth };
      if (path === "/ui/event-position") return { after: "0" };
      if (path === "/status") return { configured: true, store_instance_id: storeId, model: "fixture-model", mode: "auto", web_voice: { enabled: false, available: false }, ...controls.status };
      if (path === "/conversation") return controls.conversation ? controls.conversation() : { session: { ...main }, messages: [], events: [], after: "0", live: { session_id: main.id, status: "idle", run_id: null, pending_approvals: [] } };
      if (path === "/sessions") return { items: [{ ...main }] };
      if (["/notifications", "/approvals"].includes(path)) return { items: [] };
      if (path === "/uploads") return { items: refreshed ? latest : [older, recent, ...controls.extraUploads] };
      if (path.startsWith("/uploads?")) {
        const ids = new URLSearchParams(path.split("?")[1]).getAll("ids");
        if (controls.metadata) return controls.metadata(ids, options);
        assert.deepEqual(ids, [older.id, recent.id], "restore must resolve the exact saved order beyond the recent upload window");
        return { items: ids.map((id) => files.get(id)), unavailable_ids: [] };
      }
      if (path.startsWith("/team?")) return { members: [], messages: [], total: 0, limit: 50, offset: 0, active_only: false, has_more: false };
      if (path.startsWith("/commitment-directory?")) return { items: [item], has_more: false, next_cursor: null, scanned: 1, json_bytes: 128, omitted: { oversized: 0, invalid: 0, missing: 0 }, omitted_total: 0, stopped: "end", consistency: "bounded_live" };
      if (path === `/commitments/${encodeURIComponent(item.id)}`) return detail;
      throw new Error(`unexpected read: ${path}`);
    },
  };
}

async function appInstance(t, backend, storage, { wait = true, upload, expectedMain = main.id } = {}) {
  const calls = { uploads: 0, asr: 0 };
  const h = harness((...args) => backend.request(...args), {
    composer: true, team: true, storage,
    upload: async (...args) => { calls.uploads += 1; if (upload) return upload(...args); throw new Error("refresh must not upload saved files"); },
    transcribe: async () => { calls.asr += 1; throw new Error("refresh must not start ASR"); },
  });
  const messages = h.document.createElement("div"); messages.setAttribute("id", "messages"); h.document.body.append(messages);
  h.ui.state.view = "chat";
  t.after(() => h.windowEvents.pagehide());
  const connecting = h.ui.connect(); await flush();
  if (wait) {
    await connecting; await flush();
    assert.equal(h.ui.state.connected, true, "the real authentication/status/conversation bootstrap must complete before exercising draft behavior");
    assert.equal(h.ui.state.auth.authenticated, true); assert.equal(h.ui.state.mainSessionId, expectedMain);
  }
  return { ...h, calls, connecting };
}

const posts = (h) => h.requests.filter((request) => request.method === "POST");
const inputOf = (h) => h.document.querySelector("#message-input");
function savedPage(text = "原页面工作草稿", files = [older, recent]) {
  const storage = pageStorage();
  assert.equal(createComposerDraft({ storage: () => storage, maxTextChars: 64000 }).save({ store: storeId, main: main.id }, text, files), true);
  return storage;
}

test("snapshot consumption failure stops the real submit before any POST and preserves the work", async (t) => {
  const storage = savedPage(), backend = serverFixture(), h = await appInstance(t, backend, storage);
  const raw = storage.getItem(COMPOSER_DRAFT_KEY);
  storage.removeItem = () => { throw new Error("browser refused invalidation"); };
  await h.document.querySelector("#composer").dispatch("submit"); await flush();
  assert.equal(posts(h).length, 0); assert.equal(inputOf(h).value, "原页面工作草稿");
  assert.deepEqual(Array.from(h.ui.state.attachments, (file) => file.id), [older.id, recent.id]);
  assert.equal(storage.getItem(COMPOSER_DRAFT_KEY), raw); assert.match(h.document.querySelector("#composer-draft-status").textContent, /尚未发送/);
});

test("a lost accepted-send receipt can refill untouched memory but never reappears after refresh or render", async (t) => {
  const storage = savedPage(), backend = serverFixture();
  backend.controls.submit = () => { assert.equal(storage.getItem(COMPOSER_DRAFT_KEY), null, "consume must precede the first chat side effect"); throw new ApiError("服务可能已接纳，回执中断"); };
  const first = await appInstance(t, backend, storage);
  await first.document.querySelector("#composer").dispatch("submit"); await flush();
  assert.equal(posts(first).length, 1); assert.equal(inputOf(first).value, "原页面工作草稿");
  first.ui.renderComposerAttachments(); await first.ui.switchView("chat");
  assert.equal(storage.getItem(COMPOSER_DRAFT_KEY), null, "error refill, resize, render and same-chat navigation are not edits");
  await first.ui.connect(); await flush();
  assert.equal(inputOf(first).value, "原页面工作草稿");
  assert.equal(storage.getItem(COMPOSER_DRAFT_KEY), null, "reconnection must not turn an unknown submitted draft back into an unsent backup");
  first.windowEvents.pagehide();
  const second = await appInstance(t, backend, storage);
  assert.equal(inputOf(second).value, ""); assert.equal(second.ui.state.attachments.length, 0); assert.equal(posts(second).length, 0);
});

test("only a new explicit edit backs up text refilled after an unknown send", async (t) => {
  const storage = savedPage("要主动核查的原提交", []), backend = serverFixture();
  backend.controls.submit = () => { throw new ApiError("网络回执丢失"); };
  const h = await appInstance(t, backend, storage); await h.document.querySelector("#composer").dispatch("submit"); await flush();
  assert.equal(storage.getItem(COMPOSER_DRAFT_KEY), null);
  inputOf(h).value += "\n先核查记录，不重做。"; await inputOf(h).dispatch("input");
  const record = JSON.parse(storage.getItem(COMPOSER_DRAFT_KEY)); assert.equal(record.text, inputOf(h).value);
  assert.equal(posts(h).length, 1);
});

for (const mode of ["missing file", "network failure"]) {
  test(`restored references preserve a visible ${mode} placeholder and block both sending entrances`, async (t) => {
    const storage = savedPage(), backend = serverFixture();
    backend.controls.metadata = async () => {
      if (mode === "network failure") throw new ApiError("核查网络中断");
      return { items: [older], unavailable_ids: [recent.id] };
    };
    const h = await appInstance(t, backend, storage);
    const selected = h.document.querySelector("#composer-attachments").textContent;
    assert.ok(selected.includes(recent.name)); assert.match(selected, mode === "missing file" ? /附件不可用/ : /核查失败/);
    if (mode === "network failure") assert.equal(selected.includes("附件不可用"), false);
    assert.equal(h.document.querySelector("#send-button").disabled, true);
    await h.document.querySelector("#composer").dispatch("submit"); assert.equal(posts(h).length, 0);
    backend.controls.metadata = async (ids) => ({ items: ids.map((id) => id === older.id ? older : recent), unavailable_ids: [] });
    await clickMemoryButton(h, h.document.querySelector("#composer-draft-recheck"));
    assert.equal(h.document.querySelector("#send-button").disabled, false);
    assert.deepEqual(Array.from(h.ui.state.attachments, (file) => file.id), [older.id, recent.id]);
  });
}

test("metadata waits cannot replay text or replace a newer attachment selection", async (t) => {
  const remote = deferred(), storage = savedPage(), backend = serverFixture();
  backend.controls.metadata = () => remote.promise;
  const h = await appInstance(t, backend, storage, { wait: false });
  assert.equal(h.document.querySelector("#send-button").disabled, true);
  inputOf(h).value = "核查期间的新文字"; await inputOf(h).dispatch("input");
  const fresh = { id: "3".repeat(32), name: "新选择的资料.txt", mime_type: "text/plain", size: 4 }; backend.controls.extraUploads = [fresh];
  h.ui.useUploadViews(); await h.ui.switchView("uploads");
  const row = h.other.querySelectorAll(".upload-row").find((entry) => entry.textContent.includes(fresh.name)); await clickMemoryButton(h, h.button("带入对话", row));
  const removed = h.document.querySelector("#composer-attachments").querySelectorAll("button").find((button) => button.dataset.id === older.id);
  await clickMemoryButton(h, removed);
  remote.resolve({ items: [older, recent], unavailable_ids: [] }); await h.connecting; await flush();
  assert.equal(inputOf(h).value, "核查期间的新文字");
  assert.deepEqual(Array.from(h.ui.state.attachments, (file) => file.id), [recent.id, fresh.id]);
  assert.equal(h.ui.state.attachments.some((file) => file.draftStatus), false); assert.equal(h.document.querySelector("#send-button").disabled, false);
});

for (const change of ["typed text", "new attachment"]) {
  test(`bootstrap restoration yields to ${change} created while the conversation read waits and the next refresh retains that new work`, async (t) => {
    const remote = deferred(), storage = pageStorage(), backend = serverFixture();
    const first = await appInstance(t, backend, storage); await organizeWork(first); first.windowEvents.pagehide();
    backend.controls.conversation = () => remote.promise;
    const h = await appInstance(t, backend, storage, { wait: false });
    if (change === "typed text") { inputOf(h).value = "等待连接期间写的新内容"; await inputOf(h).dispatch("input"); }
    else {
      h.ui.useUploadViews();
      for (const file of [recent, older]) {
        await h.ui.switchView("uploads"); const row = h.other.querySelectorAll(".upload-row").find((entry) => entry.textContent.includes(file.name)); await clickMemoryButton(h, h.button("带入对话", row));
      }
    }
    remote.resolve({ session: { ...main }, messages: [], events: [], after: "0" }); await h.connecting; await flush();
    const expectedText = change === "typed text" ? "等待连接期间写的新内容" : "", expectedIds = change === "new attachment" ? [recent.id, older.id] : [];
    assert.equal(inputOf(h).value, expectedText); assert.deepEqual(Array.from(h.ui.state.attachments, (file) => file.id), expectedIds);
    h.windowEvents.pagehide();
    backend.controls.metadata = async (ids) => ({ items: ids.map((id) => id === older.id ? older : recent), unavailable_ids: [] });
    const third = await appInstance(t, backend, storage);
    assert.equal(inputOf(third).value, expectedText, "the third real app must recover the new unsent work, not the superseded backup");
    assert.deepEqual(Array.from(third.ui.state.attachments, (file) => file.id), expectedIds, "the third real app must recover the latest explicit selection in its exact order");
    for (const app of [first, h, third]) { assert.equal(posts(app).length, 0); assert.equal(app.calls.uploads, 0); assert.equal(app.calls.asr, 0); }
  });
}

for (const change of ["store", "main"]) {
  test(`a bootstrap edit is not adopted as a saved draft for an unconfirmed different ${change}`, async (t) => {
    const storage = pageStorage(), backend = serverFixture(), remote = deferred();
    const first = await appInstance(t, backend, storage); await organizeWork(first); first.windowEvents.pagehide();
    const targetMain = change === "main" ? { ...main, id: "another-main" } : main;
    if (change === "store") backend.controls.status.store_instance_id = "e".repeat(32);
    backend.controls.conversation = () => remote.promise;
    const second = await appInstance(t, backend, storage, { wait: false });
    inputOf(second).value = "身份尚未接纳时的新编辑"; await inputOf(second).dispatch("input");
    remote.resolve({ session: { ...targetMain }, messages: [], events: [], after: "0" }); await second.connecting;
    second.ui.renderComposerAttachments(); await second.ui.switchView("chat");
    assert.equal(storage.getItem(COMPOSER_DRAFT_KEY), null, "a different accepted identity must not adopt an edit prepared before it was verified");
    second.windowEvents.pagehide(); const third = await appInstance(t, backend, storage, { expectedMain: targetMain.id });
    assert.equal(inputOf(third).value, ""); assert.equal(third.ui.state.attachments.length, 0);
    for (const app of [first, second, third]) { assert.equal(posts(app).length, 0); assert.equal(app.calls.uploads, 0); assert.equal(app.calls.asr, 0); }
  });
}

test("a real bootstrap edit with no former backup is saved only after this owner is accepted", async (t) => {
  const storage = pageStorage(), backend = serverFixture(), remote = deferred(); backend.controls.conversation = () => remote.promise;
  const first = await appInstance(t, backend, storage, { wait: false });
  inputOf(first).value = "首次连接期间就开始组织的草稿"; await inputOf(first).dispatch("input");
  assert.equal(storage.getItem(COMPOSER_DRAFT_KEY), null, "an unaccepted main identity cannot be written into a backup");
  remote.resolve({ session: { ...main }, messages: [], events: [], after: "0" }); await first.connecting;
  first.windowEvents.pagehide(); const second = await appInstance(t, backend, storage);
  assert.equal(inputOf(second).value, "首次连接期间就开始组织的草稿"); assert.equal(posts(second).length, 0); assert.equal(second.calls.uploads, 0); assert.equal(second.calls.asr, 0);
});

for (const outcome of ["success", "401"]) {
  test(`an old send ${outcome} cannot refill or unlock a new connection's draft`, async (t) => {
    const remote = deferred(), storage = savedPage(), backend = serverFixture(); backend.controls.submit = () => remote.promise;
    const h = await appInstance(t, backend, storage), sending = h.document.querySelector("#composer").dispatch("submit"); await flush();
    const old = posts(h)[0]; assert.equal(old.signal.aborted, false);
    await h.ui.connect(); await flush();
    inputOf(h).value = "新连接的草稿"; await inputOf(h).dispatch("input"); const selected = [recent]; h.ui.state.attachments = selected;
    assert.equal(old.signal.aborted, true);
    if (outcome === "success") remote.resolve({ run_id: "old-run" }); else remote.reject(new ApiError("旧提交延迟401", 401));
    await sending; await flush();
    assert.equal(inputOf(h).value, "新连接的草稿"); assert.equal(h.ui.state.attachments === selected, true);
    assert.equal(h.ui.state.auth.authenticated, true); assert.equal(h.ui.state.sending, false);
  });
}

test("a same-owner send failure never grafts old attachments onto a newly edited draft", async (t) => {
  const remote = deferred(), storage = savedPage(), backend = serverFixture(); backend.controls.submit = () => remote.promise;
  const h = await appInstance(t, backend, storage), sending = h.document.querySelector("#composer").dispatch("submit"); await flush();
  inputOf(h).value = "发送期间新写的下一件事"; await inputOf(h).dispatch("input");
  remote.reject(new ApiError("原提交回执中断")); await sending;
  assert.equal(inputOf(h).value, "发送期间新写的下一件事"); assert.equal(h.ui.state.attachments.length, 0);
  assert.deepEqual(JSON.parse(storage.getItem(COMPOSER_DRAFT_KEY)).attachments, []);
});

for (const outcome of ["success", "401"]) {
  test(`an old upload ${outcome} cannot append to a newly connected or discarded work draft`, async (t) => {
    const remote = deferred(), storage = pageStorage(), backend = serverFixture(); let uploadOptions;
    const h = await appInstance(t, backend, storage, { upload: (_file, options) => { uploadOptions = options; return remote.promise; } });
    const picker = h.document.querySelector("#file-input"); picker.files = [{ name: "old-upload.txt", type: "text/plain" }]; await picker.dispatch("change"); await flush();
    assert.equal(h.calls.uploads, 1); await h.ui.connect(); await flush();
    inputOf(h).value = "新连接保留的文字"; await inputOf(h).dispatch("input");
    assert.equal(uploadOptions.signal.aborted, true);
    if (outcome === "success") remote.resolve(older); else remote.reject(new ApiError("旧上传迟到401", 401));
    await flush();
    assert.equal(inputOf(h).value, "新连接保留的文字"); assert.equal(h.ui.state.attachments.length, 0); assert.equal(h.ui.state.auth.authenticated, true); assert.equal(h.ui.state.uploading, 0);
  });
}

test("discard removes only this snapshot and selected references without deleting files or stopping a run", async (t) => {
  const storage = savedPage(), backend = serverFixture(), h = await appInstance(t, backend, storage);
  storage.setItem("muli.task-control:keep", "unrelated");
  h.ui.state.session.status = "running"; h.ui.state.session.runId = "existing-run";
  await clickMemoryButton(h, h.document.querySelector("#composer-draft-discard"));
  assert.equal(inputOf(h).value, ""); assert.equal(h.ui.state.attachments.length, 0); assert.equal(storage.getItem(COMPOSER_DRAFT_KEY), null);
  assert.equal(storage.getItem("muli.task-control:keep"), "unrelated"); assert.equal(h.ui.state.session.runId, "existing-run"); assert.equal(h.ui.state.session.status, "running");
  assert.equal(h.ui.state.uploads.some((file) => file.id === older.id), true); assert.equal(posts(h).length, 0);
});

for (const change of ["store", "main"]) {
  test(`a different ${change} cannot restore the former identity's work`, async (t) => {
    const storage = savedPage(), backend = serverFixture();
    if (change === "store") backend.controls.status.store_instance_id = "e".repeat(32);
    else backend.controls.conversation = () => ({ session: { ...main, id: "another-main" }, messages: [], events: [], after: "0" });
    const h = await appInstance(t, backend, storage, { expectedMain: change === "main" ? "another-main" : main.id });
    assert.equal(inputOf(h).value, ""); assert.equal(h.ui.state.attachments.length, 0);
    assert.equal(storage.getItem(COMPOSER_DRAFT_KEY), null); assert.equal(posts(h).length, 0);
    assert.equal(h.requests.some((request) => request.path.startsWith("/uploads?")), false);
  });
}

test("a missing verified store identity disables restoration and saving while ordinary manual chat still works", async (t) => {
  const storage = savedPage(), before = storage.getItem(COMPOSER_DRAFT_KEY), backend = serverFixture(); backend.controls.status.store_instance_id = undefined;
  backend.controls.submit = () => ({ run_id: "ordinary-run" });
  const h = await appInstance(t, backend, storage);
  assert.equal(inputOf(h).value, ""); inputOf(h).value = "原有手动聊天"; await inputOf(h).dispatch("input");
  assert.equal(storage.getItem(COMPOSER_DRAFT_KEY), before);
  await h.document.querySelector("#composer").dispatch("submit"); await flush();
  assert.equal(posts(h).length, 1); assert.equal(posts(h)[0].body.content, "原有手动聊天");
});

test("expired authentication locks saved work until real bootstrap accepts the same store and main", async (t) => {
  const storage = savedPage(), backend = serverFixture(); backend.controls.auth = { required: true, authenticated: false };
  const h = await appInstance(t, backend, storage, { wait: false }); await h.connecting;
  assert.equal(h.ui.state.auth.authenticated, false); assert.equal(h.document.querySelector("#auth-gate").hidden, false); assert.equal(h.document.querySelector(".app-shell").inert, true);
  assert.equal(inputOf(h).value, ""); assert.equal(h.ui.state.attachments.length, 0); assert.ok(storage.getItem(COMPOSER_DRAFT_KEY));
  assert.equal(h.requests.some((request) => request.path === "/status" || request.path.startsWith("/uploads?")), false);
  backend.controls.auth = { required: true, authenticated: true }; await h.ui.connect();
  assert.equal(inputOf(h).value, "原页面工作草稿"); assert.deepEqual(Array.from(h.ui.state.attachments, (file) => file.id), [older.id, recent.id]); assert.equal(posts(h).length, 0);
});

test("explicit logout clears the draft before the request and its failure cannot reappear after login", async (t) => {
  const storage = savedPage(), backend = serverFixture(), remote = deferred();
  backend.controls.logout = () => { assert.equal(storage.getItem(COMPOSER_DRAFT_KEY), null); return remote.promise; };
  const h = await appInstance(t, backend, storage), logout = h.document.createElement("button"); logout.dataset.action = "logout"; h.document.body.append(logout);
  const loggingOut = clickMemoryButton(h, logout); await flush();
  assert.equal(inputOf(h).value, ""); assert.equal(h.ui.state.attachments.length, 0); assert.equal(h.ui.state.auth.authenticated, false);
  await h.ui.connect(); assert.equal(h.ui.state.auth.authenticated, true, "the fixture server must return a fresh authenticated response after the explicit login");
  inputOf(h).value = "重新登录后独立组织的新草稿"; await inputOf(h).dispatch("input");
  remote.reject(new ApiError("原退出请求断开", 401)); await loggingOut;
  assert.equal(h.ui.state.auth.authenticated, true); assert.equal(inputOf(h).value, "重新登录后独立组织的新草稿");
  assert.equal(JSON.parse(storage.getItem(COMPOSER_DRAFT_KEY)).text, inputOf(h).value);
});

test("discard during attachment verification revokes the real read and its late 401", async (t) => {
  const remote = deferred(), storage = savedPage(), backend = serverFixture(); let options;
  backend.controls.metadata = (_ids, value) => { options = value; return remote.promise; };
  const h = await appInstance(t, backend, storage, { wait: false });
  await clickMemoryButton(h, h.document.querySelector("#composer-draft-discard"));
  assert.equal(options.signal.aborted, true); inputOf(h).value = "丢弃后写的新草稿"; await inputOf(h).dispatch("input");
  remote.reject(new ApiError("旧核查迟到401", 401)); await h.connecting;
  assert.equal(inputOf(h).value, "丢弃后写的新草稿"); assert.equal(h.ui.state.attachments.length, 0); assert.equal(h.ui.state.auth.authenticated, true);
  assert.equal(JSON.parse(storage.getItem(COMPOSER_DRAFT_KEY)).text, inputOf(h).value); assert.equal(posts(h).length, 0);
});

test("ordinary text merged from completed ASR is saved while a pending voice draft is not", async (t) => {
  const storage = pageStorage(), backend = serverFixture(), first = await appInstance(t, backend, storage);
  inputOf(first).value = "语音前的原稿"; await inputOf(first).dispatch("input");
  first.publishVoiceDraft({ text: "已经合入文字框的转写" });
  assert.equal(inputOf(first).value, "语音前的原稿\n已经合入文字框的转写");
  first.publishVoiceDraft({ text: "尚待主人处理的转写", context: { revision: -1, text: "旧文字" } });
  assert.equal(first.ui.state.voiceDraft.text, "尚待主人处理的转写");
  assert.equal(storage.getItem(COMPOSER_DRAFT_KEY).includes("尚待主人处理"), false);
  first.windowEvents.pagehide(); const second = await appInstance(t, backend, storage);
  assert.equal(inputOf(second).value, "语音前的原稿\n已经合入文字框的转写"); assert.equal(second.ui.state.voiceDraft, null);
  assert.equal(second.calls.asr, 0); assert.equal(second.calls.uploads, 0); assert.equal(posts(second).length, 0);
});

test("denied browser writes preserve in-memory editing and do not block an ordinary submission with no old snapshot", async (t) => {
  const storage = pageStorage(), backend = serverFixture(); storage.setItem = () => { throw new Error("quota"); }; backend.controls.submit = () => ({ run_id: "memory-only-run" });
  const h = await appInstance(t, backend, storage); inputOf(h).value = "无法暂存时仍可编辑的原文字"; await inputOf(h).dispatch("input");
  assert.match(h.document.querySelector("#composer-draft-status").textContent, /未能保存/); assert.equal(inputOf(h).value, "无法暂存时仍可编辑的原文字");
  await h.document.querySelector("#composer").dispatch("submit"); await flush();
  assert.equal(posts(h).length, 1); assert.equal(posts(h)[0].body.content, "无法暂存时仍可编辑的原文字");
});

test("preview draft editing never touches the real browser storage and a fresh app resets its demo snapshot", async (t) => {
  const backend = serverFixture(); backend.controls.status.preview = true;
  const accesses = [], storage = Object.fromEntries(["getItem", "setItem", "removeItem"].map((method) => [method, () => { accesses.push(method); throw new Error("preview cannot use real session storage"); }]));
  const first = await appInstance(t, backend, storage); inputOf(first).value = "仅存在预览页面内存的草稿"; await inputOf(first).dispatch("input");
  assert.match(first.document.querySelector("#composer-draft-status").textContent, /已暂存/); first.windowEvents.pagehide();
  const second = await appInstance(t, backend, storage); assert.equal(inputOf(second).value, ""); assert.equal(posts(second).length, 0); assert.deepEqual(accesses, []);
});

test("preview metadata reads resolve exact ordered IDs from memory and distinguish an unavailable file", async (t) => {
  const backend = createPreviewBackend({ delay: 0 }); t.after(() => backend.dispose());
  const read = async (path) => { const response = await backend.fetch(`/api${path}`); return { status: response.status, body: await response.json() }; };
  const status = (await read("/status")).body; assert.match(status.store_instance_id, /^[a-f0-9]{32}$/);
  const file = backend.state.uploads[0], missing = "9".repeat(32);
  const result = await read(`/uploads?ids=${missing}&ids=${file.id}`);
  assert.equal(result.status, 200); assert.deepEqual(result.body.items.map((item) => item.id), [file.id]); assert.deepEqual(result.body.unavailable_ids, [missing]);
  assert.equal((await read(`/uploads?ids=${file.id}&ids=${file.id}`)).status, 422);
  backend.state.files.delete(file.id); const removed = await read(`/uploads?ids=${file.id}`);
  assert.deepEqual(removed.body.items, []); assert.deepEqual(removed.body.unavailable_ids, [file.id]);
  assert.equal((await read("/uploads")).body.items.some((item) => item.id === file.id), true, "the original recent-list contract remains independent");
});

async function organizeWork(h) {
  const input = h.document.querySelector("#message-input");
  input.value = "保留原项目定位🙂。\n先列新旧要求差异，今天只写本地草稿，不要外发。  "; await input.dispatch("input");
  h.ui.useUploadViews();
  for (const file of [older, recent]) {
    await h.ui.switchView("uploads");
    const row = h.other.querySelectorAll(".upload-row").find((entry) => entry.textContent.includes(file.name));
    assert.ok(row); await clickMemoryButton(h, h.button("带入对话", row));
  }
  h.ui.useTeamViews(); await h.ui.switchView("team");
  await clickMemoryButton(h, h.button("我的任务", h.other));
  const entry = h.other.querySelectorAll("[data-task-identifier]").find((button) => button.dataset.taskIdentifier === item.id);
  assert.ok(entry); await clickMemoryButton(h, entry);
  await clickMemoryButton(h, h.button("带着任务提问"));
  assert.ok(input.value.includes(JSON.stringify({ identifier: item.id, observed_revision: detail.revision })), "the actual task detail action must append its accurate reference");
  assert.deepEqual(Array.from(h.ui.state.attachments, (file) => file.id), [older.id, recent.id]);
  assert.equal(h.calls.uploads, 0); assert.equal(h.calls.asr, 0);
  return input.value;
}

for (const focus of ["exact text and task reference", "ordered saved attachment selection"]) {
  test(`refresh restores ${focus} in a second real app instance sharing the page session`, async (t) => {
    const storage = pageStorage(), backend = serverFixture();
    const first = await appInstance(t, backend, storage), prepared = await organizeWork(first);
    first.windowEvents.pagehide(); backend.refresh();
    const second = await appInstance(t, backend, storage);
    assert.equal(second.ui.state.uploads.length, 200);
    assert.equal(second.ui.state.uploads.some((file) => file.id === older.id), false, "the older selected file really lies outside the latest upload window");
    if (focus === "exact text and task reference") assert.equal(second.document.querySelector("#message-input").value, prepared, "refresh must preserve the real composed text and exact source reference byte for byte");
    else assert.deepEqual(Array.from(second.ui.state.attachments, (file) => file.id), [older.id, recent.id], "refresh must preserve the original saved attachment order");
    assert.equal(second.document.querySelector("#message-input").value, prepared);
    assert.deepEqual(Array.from(second.ui.state.attachments, (file) => file.id), [older.id, recent.id]);
    assert.equal(second.ui.state.voiceDraft, null);
    for (const h of [first, second]) {
      assert.equal(h.requests.some((request) => request.method && request.method !== "GET"), false);
      assert.equal(h.calls.uploads, 0); assert.equal(h.calls.asr, 0); assert.equal(h.ui.state.session.messages.length, 0);
    }
  });
}

test("configure prompt replaces only after confirmation, never sends, and can be undone", async (t) => {
  const h = await appInstance(t, serverFixture(), pageStorage());
  const input = inputOf(h), attachments = [older];
  input.value = "原有草稿"; await input.dispatch("input");
  h.ui.state.attachments = attachments; h.ui.renderComposerAttachments();
  assert.equal(h.ui.state.activeSession?.id, h.ui.state.mainSessionId);
  const trigger = h.document.createElement("button"); trigger.dataset.prompt = "请检查当前配置和未就绪的能力。"; h.document.body.append(trigger);
  await h.ui.handleAction("configure-prompt", trigger); await flush();
  assert.equal(input.value, "原有草稿");
  assert.equal(h.ui.state.activeSession?.id, h.ui.state.mainSessionId);
  assert.equal(h.dialog.open, true);
  assert.equal(h.document.querySelector("#dialog-title").textContent, "替换草稿");
  assert.match(h.document.querySelector("#dialog-content").textContent, /附件与语音保留/);
  assert.equal(posts(h).length, 0);
  await h.form.dispatch("submit"); await flush();
  assert.equal(input.value, "请检查当前配置和未就绪的能力。");
  assert.deepEqual(Array.from(h.ui.state.attachments, (file) => file.id), [older.id]);
  assert.equal(posts(h).length, 0);
  const undo = h.document.querySelector("#composer-prompt-undo");
  assert.equal(undo.hidden, false);
  await h.ui.handleAction("undo-composer-prompt", undo);
  assert.equal(input.value, "原有草稿");
  assert.deepEqual(Array.from(h.ui.state.attachments, (file) => file.id), [older.id]);
  assert.equal(posts(h).length, 0);
});

test("configuration overview replaces a draft only after confirmation and never auto-sends", async (t) => {
  const h = await appInstance(t, serverFixture(), pageStorage());
  h.ui.state.settingsDraft = { workspace_roots: [] };
  h.ui.state.status = { ...h.ui.state.status, configured: true };
  const input = inputOf(h);
  input.value = "原有草稿"; await input.dispatch("input");
  const form = h.ui.renderConfigurationOverview().querySelector("form");
  form.querySelector("textarea").value = "每天晚上九点提醒我整理笔记";
  await form.dispatch("submit"); await flush();
  assert.equal(input.value, "原有草稿");
  assert.equal(h.dialog.open, true);
  assert.equal(h.document.querySelector("#dialog-title").textContent, "替换草稿");
  await h.form.dispatch("submit"); await flush();
  assert.equal(input.value, "请调整沐璃的配置：\n每天晚上九点提醒我整理笔记");
  assert.equal(posts(h).length, 0);
});
