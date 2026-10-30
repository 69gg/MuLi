import test from "node:test";
import assert from "node:assert/strict";
import { ApiError } from "../../web/api.mjs";
import { attachmentURL } from "../../web/attachments.mjs";
import { createPreviewBackend } from "../../web/preview.mjs";
import { hydrateSession } from "../../web/state.mjs";
import { deferred, flush, harness } from "./helpers/conversation-dom-harness.mjs";

const identity = { session_id: "main", run_id: "run-1", message_id: "saved-final", revision: "c".repeat(32) };
const first = "已读完笔记，并给出完整答复。\n";
const last = "这是只有完整已保存答复才有的唯一末尾。";
const final = {
  id: identity.message_id, session_id: identity.session_id, role: "assistant", content: first + last,
  evidence_revision: identity.revision, tool_calls: [], created_at: 9,
};
const speech = {
  id: "7".repeat(32), name: "完整答复.wav", mime_type: "audio/wav", size: 48, source: "tts", created_at: 10,
  url: `/api/uploads/${"7".repeat(32)}/download`, preview_url: `/api/uploads/${"7".repeat(32)}/content`, kind: "audio",
};
const speechPath = `/sessions/${identity.session_id}/messages/${identity.message_id}/speech`;
const receipt = () => ({ ...identity, attachment: { ...speech } });
const posts = (h) => h.requests.filter((request) => request.method === "POST");
const speechButtons = (h, root = h.messages) => root.querySelectorAll(".message-actions button").filter((button) => button.textContent.trim() === "生成语音" || button.getAttribute("aria-label") === "生成语音");

function savedAnswerHarness(handler, { completed = true, completion = {}, message = {} } = {}) {
  const h = harness({ interactive: true, handler });
  h.ui.state.status = { configured: true, model: "fixture-model", web_voice: { enabled: false, available: false } };
  h.start();
  h.emit("message.delta", { content: first });
  h.emit("reasoning.delta", { content: "思考标记：先核对工具读出的笔记，不能把这一段交给语音。" });
  h.emit("tool.started", { run_id: identity.run_id, call_id: "read-notes", name: "fs_read", arguments: { path: "notes.txt" } });
  h.emit("tool.completed", { run_id: identity.run_id, call_id: "read-notes", status: "completed", result: { text: "工具标记：这里是资料正文。" } });
  h.emit("message.delta", { content: last });
  h.emit("message.completed", { message: { ...final, ...message } });
  if (completed) h.emit("run.completed", { run_id: identity.run_id, result_message_id: identity.message_id, ...completion });
  h.render();
  return h;
}

function speechButton(h) {
  const buttons = speechButtons(h);
  assert.equal(buttons.length, 1, "the saved final answer must expose one 生成语音 message action");
  return buttons[0];
}

function assertSavedFinalFixture(h) {
  const fragments = h.messages.querySelectorAll(`article[data-message-id="${identity.message_id}"]`);
  assert.equal(fragments.length, 2, "the actual event projection must split this canonical answer around reasoning and tools");
  assert.equal(fragments.map((node) => node.querySelector(".message-body").textContent).join(""), final.content);
  assert.ok(h.messages.querySelector(".reasoning-step"));
  assert.ok(h.messages.querySelector(".tool-step.completed"));
  const canonical = h.ui.state.session.messages.find((message) => message.id === identity.message_id);
  assert.ok(canonical); assert.equal(canonical.content, final.content);
  assert.equal(canonical.evidence_revision, identity.revision);
  assert.equal(Object.hasOwn(canonical, "content_provenance"), false, "normal runtime finals do not require tool-output provenance");
  return fragments;
}

test("the real saved final action confirms once and renders a passive audio attachment without changing the draft", async () => {
  const pending = deferred();
  const h = savedAnswerHarness(async (path, options) => {
    assert.equal(path, speechPath); assert.equal(options.method, "POST");
    return pending.promise;
  });
  const selected = { id: "9".repeat(32), name: "草稿已有附件.txt", mime_type: "text/plain", size: 17 };
  h.ui.state.attachments = [selected]; const attachments = h.ui.state.attachments;
  const voiceDraft = { text: "尚未合入的语音草稿" }; h.ui.state.voiceDraft = voiceDraft;
  const input = h.document.querySelector("#message-input");
  input.value = "正在写的新问题，生成旧答复语音不能覆盖它。"; await input.dispatch("input");
  h.ui.renderComposerAttachments();
  const draft = h.ui.captureDraft(), messages = JSON.stringify(h.ui.state.session.messages);
  const runId = h.ui.state.session.runId, status = h.ui.state.session.status;
  const fragments = assertSavedFinalFixture(h);

  const button = speechButton(h);
  assert.equal(fragments[0].contains(button), false);
  assert.equal(fragments.at(-1).contains(button), true, "only the final visible fragment owns the canonical answer action");
  assert.equal(posts(h).length, 0);
  await h.click(button);
  assert.equal(h.dialog.open, true);
  const content = h.document.querySelector("#dialog-content").textContent;
  assert.match(content, /完整.*答复/); assert.match(content, /语音服务/); assert.match(content, /保存.*音频附件/);
  assert.equal(posts(h).length, 0, "opening the confirmation must not synthesize or send a chat message");
  const confirm = h.document.querySelector("#dialog-submit");
  assert.equal(confirm.hidden, false); assert.equal(confirm.disabled, false);

  const saving = h.form.dispatch("submit"); await flush();
  const repeated = h.form.dispatch("submit"); await flush();
  assert.equal(posts(h).length, 1, "repeated confirmation must share the active generation");
  const request = posts(h)[0];
  assert.equal(request.path, speechPath); assert.equal(request.cache, "no-store");
  const body = typeof request.body === "string" ? JSON.parse(request.body) : request.body;
  assert.deepEqual(Object.keys(body).sort(), ["confirmed", "revision", "run_id"]);
  assert.equal(body.run_id, identity.run_id); assert.equal(body.revision, identity.revision); assert.equal(body.confirmed, true);
  assert.equal(JSON.stringify(body).includes(first), false); assert.equal(JSON.stringify(body).includes(last), false);
  assert.equal(request.signal.aborted, false);
  pending.resolve(receipt()); await Promise.all([saving, repeated]); await flush(); h.flushFrames();

  const audio = h.document.querySelectorAll("audio");
  assert.equal(audio.length, 1, "success must use the existing attachment renderer's audio controls");
  assert.ok(audio[0].closest(".attachment.audio")); assert.ok(audio[0].getAttribute("controls"));
  assert.equal(audio[0].getAttribute("src"), attachmentURL(speech));
  assert.equal(Boolean(audio[0].getAttribute("autoplay")), false);
  const download = audio[0].closest(".attachment").querySelector("a[download]");
  assert.ok(download); assert.equal(download.getAttribute("href"), attachmentURL(speech, true));
  assert.equal(download.getAttribute("download"), speech.name);
  assert.equal(h.document.playCalls, 0, "generating an attachment must never call play");
  h.render(); await flush();
  assert.equal(posts(h).length, 1); assert.equal(h.document.playCalls, 0);
  const after = h.ui.captureDraft();
  assert.equal(after.text, draft.text); assert.equal(after.revision, draft.revision); assert.equal(after.session, draft.session);
  assert.equal(h.ui.state.attachments === attachments, true); assert.equal(h.ui.state.attachments[0] === selected, true);
  assert.equal(h.ui.state.voiceDraft === voiceDraft, true);
  assert.equal(JSON.stringify(h.ui.state.session.messages), messages);
  assert.equal(h.ui.state.session.runId, runId); assert.equal(h.ui.state.session.status, status);
});

test("the message action requires the canonical saved version and an exact completed-run candidate in the main conversation", () => {
  const cases = [
    { name: "still-running", options: { completed: false } },
    { name: "another-result-message", options: { completion: { result_message_id: "other-message" } } },
    { name: "another-run", options: { completion: { run_id: "another-run" } } },
    { name: "tool-call-intermediate", options: { message: { tool_calls: [{ id: "other-call", name: "fs_read", arguments: "{}" }] } } },
    { name: "interrupted-partial", options: { message: { interrupted: true } } },
    { name: "explicit-runtime-excerpt", options: { message: { content_provenance: "runtime_excerpt" } } },
    { name: "non-main-session", change: (h) => { h.ui.state.mainSessionId = "another-main"; } },
    { name: "missing-main-identity", change: (h) => { h.ui.state.mainSessionId = null; } },
    { name: "canonical-source-deleted", change: (h) => { h.ui.state.session.messages = h.ui.state.session.messages.filter((message) => message.id !== identity.message_id); } },
    { name: "canonical-revision-missing", change: (h) => { h.ui.state.session.messages = h.ui.state.session.messages.map((message) => message.id === identity.message_id ? { ...message, evidence_revision: undefined } : message); } },
  ];
  for (const item of cases) {
    const h = savedAnswerHarness(async () => { throw new Error("an ineligible answer must not call a service"); }, item.options);
    item.change?.(h); h.render();
    assert.equal(speechButtons(h).length, 0, item.name);
    assert.equal(posts(h).length, 0, item.name);
  }
});

test("an unknown speech result never resubmits on repeated confirmation, render or reconnect", async () => {
  const h = savedAnswerHarness(async (path) => {
    assert.equal(path, speechPath); throw new ApiError("连接在生成回执前中断");
  });
  assertSavedFinalFixture(h); await h.click(speechButton(h));
  await h.form.dispatch("submit"); await flush();
  assert.equal(posts(h).length, 1);
  assert.match(h.document.querySelector("#dialog-content").textContent, /未知|未确认|无法确认/);
  const confirm = h.document.querySelector("#dialog-submit"); assert.ok(confirm.hidden || confirm.disabled);
  await h.form.dispatch("submit"); h.render(); await flush();
  assert.equal(posts(h).length, 1);
  h.ui.activateConnection(); h.render(); await flush();
  assert.equal(posts(h).length, 1); assert.equal(h.document.playCalls, 0);
});

// The browser changes `open` before its later close task. Hold that task so a
// delayed authorization response can arrive after the user's close intent.
function delayedClose(h) {
  const tasks = []; let delivered = 0;
  h.dialog.close = () => {
    if (!h.dialog.open) return;
    h.dialog.open = false; tasks.push(() => h.dialog.dispatch("close"));
  };
  return {
    get queued() { return tasks.length; }, get delivered() { return delivered; },
    cancel() {
      const event = { target: h.dialog, currentTarget: h.dialog, cancelable: true, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
      const pending = (h.dialog.events.cancel ?? []).map((callback) => callback(event));
      if (!event.defaultPrevented) h.dialog.close();
      return Promise.all(pending);
    },
    async deliver() { while (tasks.length) { delivered += 1; await tasks.shift()(); } },
  };
}

for (const entry of ["close button", "native cancel"]) {
  test(`speech ${entry} invalidates its request before a delayed close task and stale 401`, async () => {
    const pending = deferred();
    const h = savedAnswerHarness(async (path) => { assert.equal(path, speechPath); return pending.promise; });
    const owner = h.ui.activateConnection();
    assertSavedFinalFixture(h); await h.click(speechButton(h));
    const close = delayedClose(h);
    const saving = h.form.dispatch("submit"); await flush();
    assert.equal(posts(h).length, 1); const request = posts(h)[0];
    assert.equal(request.signal.aborted, false);
    if (entry === "close button") await h.click(h.document.querySelector('#dialog-actions [data-action="close-dialog"]'));
    else await close.cancel();
    assert.equal(h.dialog.open, false); assert.equal(close.queued, 1); assert.equal(close.delivered, 0);
    const abortedAtCloseIntent = request.signal.aborted;
    pending.reject(new ApiError("关闭之后才到达的旧 401", 401));
    await saving; await flush();
    try {
      assert.equal(close.delivered, 0, "the old request settles before any native close task is delivered");
      assert.equal(abortedAtCloseIntent, true, "close and cancel must synchronously invalidate their speech request");
      assert.equal(h.ui.state.auth.authenticated, true);
      assert.equal(owner.controller.signal.aborted, false, "a closed speech request cannot log out its still-current connection");
      assert.equal(posts(h).length, 1); assert.equal(h.document.querySelectorAll("audio").length, 0);
      assert.equal(h.document.playCalls, 0);
    } finally { await close.deliver(); }
  });
}

test("a current speech request still handles a valid 401 through the real owner-aware API path", async () => {
  const h = savedAnswerHarness(async () => { throw new ApiError("当前认证已过期", 401); });
  assertSavedFinalFixture(h); await h.click(speechButton(h));
  await h.form.dispatch("submit"); await flush();
  assert.equal(posts(h).length, 1); assert.equal(h.ui.state.auth.authenticated, false);
  assert.equal(h.document.querySelectorAll("audio").length, 0); assert.equal(h.document.playCalls, 0);
});

test("a later main run preserves the older exact final's action and does not grant it to an intermediate answer", async () => {
  const h = savedAnswerHarness(async () => receipt());
  h.emit("run.started", { run_id: "new-main-run" });
  h.emit("message.completed", { message: { id: "new-intermediate", session_id: "main", role: "assistant", content: "正在处理新问题。", evidence_revision: "d".repeat(32), tool_calls: [{ id: "new-call", name: "fs_read", arguments: "{}" }] } });
  h.render();
  assert.equal(h.ui.state.session.runId, "new-main-run"); assert.equal(h.ui.state.session.status, "running");
  const button = speechButton(h);
  assert.equal(button.closest("article").dataset.messageId, identity.message_id);
  await h.click(button); await h.form.dispatch("submit"); await flush();
  assert.equal(posts(h).length, 1); assert.equal(posts(h)[0].path, speechPath);
  assert.equal(h.ui.state.session.runId, "new-main-run"); assert.equal(h.ui.state.session.status, "running");
  assert.equal(h.document.playCalls, 0);
});

test("a source revision change suppresses its late 401 before authentication effects even without a render", async () => {
  const pending = deferred(), h = savedAnswerHarness(async () => pending.promise);
  const owner = h.ui.activateConnection();
  await h.click(speechButton(h)); const saving = h.form.dispatch("submit"); await flush();
  const request = posts(h)[0]; assert.equal(request.signal.aborted, false);
  h.emit("message.completed", { message: { ...final, content: "编辑后的完整答复。", evidence_revision: "d".repeat(32) } });
  // Deliberately no render or explicit cleanup before the competing response.
  pending.reject(new ApiError("旧版本的迟到 401", 401)); await saving; await flush();
  assert.equal(h.ui.state.auth.authenticated, true); assert.equal(owner.controller.signal.aborted, false);
  assert.equal(h.dialog.open, false);
  assert.equal(h.document.querySelectorAll("audio").length, 0); assert.equal(posts(h).length, 1);
  h.render(); assert.equal(speechButtons(h).length, 0);
});

for (const terminal of ["success", "401"]) {
  test(`an old owner's late ${terminal} cannot publish into a newer speech dialog`, async () => {
    const pending = deferred(), h = savedAnswerHarness(async () => pending.promise);
    const oldOwner = h.ui.activateConnection();
    await h.click(speechButton(h)); const saving = h.form.dispatch("submit"); await flush();
    const request = posts(h)[0], owner = h.ui.activateConnection();
    assert.equal(request.signal.aborted, true); assert.equal(oldOwner.controller.signal.aborted, true);
    await h.click(speechButton(h));
    const content = h.document.querySelector("#dialog-content").firstElementChild;
    assert.match(content.textContent, /未知|未确认|无法确认/);
    if (terminal === "success") pending.resolve(receipt());
    else pending.reject(new ApiError("旧连接的迟到 401", 401));
    await saving; await flush();
    assert.equal(h.ui.state.auth.authenticated, true); assert.equal(owner.controller.signal.aborted, false);
    assert.equal(h.dialog.open, true); assert.equal(h.document.querySelector("#dialog-content").firstElementChild === content, true);
    assert.equal(h.document.querySelectorAll("audio").length, 0); assert.equal(posts(h).length, 1);
    assert.equal(h.document.playCalls, 0);
  });
}

test("rendering a changed source cancels its old generation and an old message event cannot revive the action", async () => {
  const pending = deferred(), h = savedAnswerHarness(async () => pending.promise);
  await h.click(speechButton(h)); const saving = h.form.dispatch("submit"); await flush();
  const request = posts(h)[0];
  h.emit("message.completed", { message: { ...final, evidence_revision: "e".repeat(32), content: "新版本正文。" } }); h.render();
  assert.equal(request.signal.aborted, true); assert.equal(h.dialog.open, false); assert.equal(speechButtons(h).length, 0);
  pending.resolve(receipt()); await saving; await flush();
  assert.equal(h.document.querySelectorAll("audio").length, 0);
  h.emit("message.completed", { message: { ...final } }); h.render();
  assert.equal(speechButtons(h).length, 0, "restoring an old token without a new exact completion must not revive its candidate");
  assert.equal(posts(h).length, 1); assert.equal(h.document.playCalls, 0);
});

for (const entry of ["hidden", "pagehide", "another view"]) {
  test(`speech ${entry} cancels before a late response and preserves the completed main run and draft`, async () => {
    const pending = deferred(), h = savedAnswerHarness(async (path) => {
      if (path === "/uploads") return { items: [] };
      assert.equal(path, speechPath); return pending.promise;
    });
    const input = h.document.querySelector("#message-input"); input.value = "保留这段正在写的新问题。"; await input.dispatch("input");
    const before = h.ui.captureDraft(), messages = JSON.stringify(h.ui.state.session.messages);
    await h.click(speechButton(h)); const saving = h.form.dispatch("submit"); await flush();
    const request = posts(h)[0];
    if (entry === "hidden") {
      h.document.visibilityState = "hidden";
      for (const callback of h.document.events.visibilitychange ?? []) await callback({ target: h.document });
    } else if (entry === "pagehide") {
      for (const callback of h.windowEvents.pagehide ?? []) await callback({ target: h.document });
    } else await h.ui.switchView("uploads");
    assert.equal(request.signal.aborted, true); assert.equal(h.dialog.open, false);
    pending.reject(new ApiError("操作失效后才到达的 401", 401)); await saving; await flush();
    assert.equal(h.ui.state.auth.authenticated, true);
    assert.equal(h.ui.captureDraft().text, before.text); assert.equal(h.ui.captureDraft().revision, before.revision);
    assert.equal(JSON.stringify(h.ui.state.session.messages), messages);
    assert.equal(h.ui.state.session.runId, identity.run_id); assert.equal(h.ui.state.session.status, "completed");
    assert.equal(posts(h).length, 1); assert.equal(h.document.querySelectorAll("audio").length, 0); assert.equal(h.document.playCalls, 0);
  });
}

test("a saved result reopens without a request and explicit regeneration asks again before another POST", async () => {
  const h = savedAnswerHarness(async () => receipt());
  await h.click(speechButton(h)); await h.form.dispatch("submit"); await flush();
  assert.equal(posts(h).length, 1); assert.equal(h.document.querySelectorAll("audio").length, 1);
  await h.click(h.document.querySelector('#dialog-actions [data-action="close-dialog"]'));
  await h.click(speechButton(h));
  assert.equal(posts(h).length, 1); assert.equal(h.document.querySelectorAll("audio").length, 1);
  const again = h.dialog.querySelectorAll("button").find((button) => button.textContent === "重新生成");
  assert.ok(again); await h.click(again);
  assert.match(h.document.querySelector("#dialog-content").textContent, /再次生成.*重复计费/);
  assert.equal(posts(h).length, 1); assert.equal(h.document.querySelector("#dialog-submit").hidden, false);
  await h.form.dispatch("submit"); await flush();
  assert.equal(posts(h).length, 2); assert.equal(h.document.querySelectorAll("audio").length, 1); assert.equal(h.document.playCalls, 0);
});

test("mismatched identities and malformed attachment receipts remain unknown without publishing or resubmitting", async () => {
  const invalid = [
    { session_id: "other-main" }, { run_id: "other-run" }, { message_id: "other-answer" }, { revision: "d".repeat(32) },
    { attachment: { ...speech, source: "upload" } }, { attachment: { ...speech, id: "foreign/path" } },
    { attachment: { ...speech, size: -1 } }, { attachment: { ...speech, preview_url: "https://foreign.invalid/audio" } },
    { attachment: { ...speech, url: "https://foreign.invalid/download" } },
  ];
  for (const override of invalid) {
    const h = savedAnswerHarness(async () => ({ ...receipt(), ...override }));
    await h.click(speechButton(h)); await h.form.dispatch("submit"); await flush();
    assert.match(h.document.querySelector("#dialog-content").textContent, /未确认/);
    assert.equal(h.document.querySelectorAll("audio").length, 0); assert.equal(h.document.querySelector("#dialog-content").querySelectorAll("a[download]").length, 0);
    await h.form.dispatch("submit"); h.render(); await flush();
    assert.equal(posts(h).length, 1); assert.equal(h.document.playCalls, 0);
  }
});

test("a public attachment without an inline preview only offers download and never synthesizes after a media error", async () => {
  const file = { ...speech, name: "完整答复.pcm", mime_type: "application/octet-stream", preview_url: null, kind: "file" };
  const h = savedAnswerHarness(async () => ({ ...receipt(), attachment: file }));
  await h.click(speechButton(h)); await h.form.dispatch("submit"); await flush();
  assert.equal(h.document.querySelectorAll("audio, video").length, 0);
  const download = h.document.querySelector("#dialog-content").querySelector(".attachment.file a[download]");
  assert.ok(download); assert.equal(download.getAttribute("href"), attachmentURL(file, true));
  assert.equal(download.getAttribute("download"), file.name);
  await download.dispatch("error"); h.render(); await h.form.dispatch("submit"); await flush();
  assert.equal(posts(h).length, 1); assert.equal(h.document.playCalls, 0);
});

test("the real preview action produces an explicitly local demo WAV with controls and byte-identical download", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  const main = backend.state.sessions[0], runId = "preview-speech-run";
  const message = { ...final, session_id: main.id, evidence_revision: "preview-speech-revision" };
  backend.state.messages[main.id] = [message];
  backend.state.runs[runId] = { id: runId, session_id: main.id, status: "completed", result: message.content, result_message_id: message.id };
  backend.emit("run.started", { run_id: runId }, main.id);
  backend.emit("message.completed", { message, run_id: runId }, main.id);
  backend.emit("run.completed", { run_id: runId, result_message_id: message.id }, main.id);
  const h = harness({ interactive: true, handler: async (path, options) => {
    const response = await backend.fetch(`/api${path}`, { ...options, ...(options.body ? { body: JSON.stringify(options.body) } : {}) });
    const body = await response.json(); if (!response.ok) throw new ApiError(body.detail, response.status); return body;
  } });
  h.ui.state.mainSessionId = main.id; h.ui.state.activeSession = main;
  h.ui.state.status = await (await backend.fetch("/api/status")).json();
  const detail = await (await backend.fetch("/api/conversation")).json();
  h.ui.state.session = hydrateSession(detail); h.render();
  const before = JSON.stringify({ messages: backend.state.messages, runs: backend.state.runs });
  await h.click(speechButton(h)); assert.match(h.document.querySelector("#dialog-content").textContent, /仅演示/);
  await h.form.dispatch("submit"); await flush();
  assert.match(h.document.querySelector("#dialog-content").textContent, /演示.*示例提示音/);
  const file = backend.state.uploads[0]; assert.equal(file.source, "tts"); assert.match(file.name, /演示/);
  assert.equal(h.document.querySelectorAll("audio").length, 1); assert.equal(h.document.playCalls, 0);
  const downloaded = await backend.fetch(file.url), raw = new Uint8Array(await downloaded.arrayBuffer());
  assert.equal(new TextDecoder().decode(raw.slice(0, 4)), "RIFF"); assert.equal(raw.length, file.size);
  assert.deepEqual(raw, new Uint8Array(await backend.state.files.get(file.id).arrayBuffer()));
  assert.equal(JSON.stringify({ messages: backend.state.messages, runs: backend.state.runs }), before);
  assert.equal(posts(h).length, 1); assert.equal(backend.state.requests.filter((request) => request.method === "POST").length, 1);
  const restored = hydrateSession(await (await backend.fetch("/api/conversation")).json());
  assert.equal(restored.replyCandidates.filter((candidate) => candidate.completed).length, 1);
});
