import test from "node:test";
import assert from "node:assert/strict";
import { ApiError } from "../../web/api.mjs";
import { VoiceInput } from "../../web/voice-input.mjs";
import { createPreviewBackend } from "../../web/preview.mjs";
import { clickMemoryButton, deferred, flush, harness } from "./helpers/app-dom-harness.mjs";

const saved = { id: "7".repeat(32), name: "已经保存的会议录音.webm", mime_type: "audio/webm", size: 81 };
const note = { id: "8".repeat(32), name: "仍然可带入对话的笔记.txt", mime_type: "text/plain", size: 12 };
const limits = { enabled: true, configured: true, available: true, max_file_bytes: 1024, max_seconds: 60, max_text_chars: 64000, timeout: 30 };

function savedAudioHarness(transcribe, { handler, voiceOptions = {}, microphone, upload } = {}) {
  const calls = { microphones: 0, uploads: 0, transcriptions: [] };
  const h = harness(async (path, options) => {
    if (handler) return handler(path, options);
    if (path === "/uploads") return { items: [saved, note] };
    if (path === "/sessions") return { items: [{ id: "main", mode: "auto", status: "idle" }] };
    if (path === "/sessions/main/messages" && options.method === "POST") return { run_id: "explicitly-sent-run" };
    throw new Error(`unexpected request: ${path}`);
  }, {
    composer: true,
    voiceControls: true,
    upload: async (file, options) => { calls.uploads += 1; if (upload) return upload(file, options); throw new Error("saved audio must never be uploaded again"); },
    transcribe: async (id, options) => {
      calls.transcriptions.push({ id, ...options });
      return transcribe(id, options);
    },
    createVoiceInput: (callbacks) => new VoiceInput({
      ...callbacks,
      secure: false,
      devices: { getUserMedia() { calls.microphones += 1; if (microphone) return microphone(); throw new Error("saved audio must never request a microphone"); } },
      Recorder: undefined,
      setTimer: () => 0, clearTimer() {},
      ...voiceOptions,
    }),
  });
  h.ui.state.connected = true;
  h.ui.state.status = { configured: true, model: "fixture-model", web_voice: { ...limits } };
  h.ui.state.mainSessionId = "main";
  h.ui.state.activeSession = { id: "main", mode: "auto", status: "idle" };
  h.ui.useUploadViews();
  const cancel = h.document.createElement("button"); cancel.dataset.action = "voice-cancel"; cancel.textContent = "取消";
  h.document.querySelector("#voice-progress").append(cancel);
  return { ...h, calls };
}

function audioAction(h) {
  const row = h.other.querySelectorAll(".upload-row").find((entry) => entry.textContent.includes(saved.name));
  assert.ok(row, "the real attachment page must show the registered audio");
  const button = h.button("转写为草稿", row);
  assert.ok(button, "the saved audio row must expose 转写为草稿 through the real attachment DOM");
  return button;
}

const messages = (h) => h.requests.filter((request) => request.path.endsWith("/messages") && request.method === "POST");

async function beginSaved(h) {
  await h.ui.switchView("uploads");
  await clickMemoryButton(h, audioAction(h));
  const confirming = h.form.dispatch("submit"); await flush();
  assert.equal(h.calls.transcriptions.length, 1);
  assert.equal(h.document.querySelector("#voice-progress").hidden, false);
  return { confirming, request: h.calls.transcriptions[0] };
}

function microphoneFixture() {
  const track = { stops: 0, stop() { this.stops += 1; } }, stream = { getTracks: () => [track] };
  class Recorder {
    static isTypeSupported(type) { return type === "audio/webm;codecs=opus"; }
    constructor(_stream, options) { this.mimeType = options.mimeType; this.state = "inactive"; }
    start() { this.state = "recording"; }
    stop() {
      this.state = "inactive";
      queueMicrotask(() => { this.ondataavailable({ data: new Blob(["fake microphone bytes"], { type: this.mimeType }) }); this.onstop(); });
    }
  }
  return { track, microphone: () => Promise.resolve(stream), upload: async () => ({ ...saved }), voiceOptions: { secure: true, Recorder } };
}

async function beginAudioSource(h, source) {
  if (source === "saved audio") return beginSaved(h);
  await h.ui.switchView("chat");
  assert.equal(h.document.querySelector("#voice-button").disabled, false, "the fixture must render its connected, configured composer before recording");
  await clickMemoryButton(h, h.document.querySelector("#voice-button"));
  assert.equal(h.calls.microphones, 1);
  const confirming = clickMemoryButton(h, h.document.querySelector("#voice-stop")); await flush();
  assert.equal(h.calls.uploads, 1); assert.equal(h.calls.transcriptions.length, 1);
  return { confirming, request: h.calls.transcriptions[0] };
}

async function chatShortcut(h, modifier) {
  let prevented = false;
  const event = { key: "k", [modifier]: true, target: h.document.querySelector("#message-input"), preventDefault() { prevented = true; } };
  for (const listener of h.document.events.keydown) await listener(event);
  await flush();
  assert.equal(prevented, true, "the actual Ctrl/Cmd+K handler must receive the shortcut");
}

test("saved audio confirms ASR once without microphone access or another upload and only an explicit send enters chat", async () => {
  const pending = deferred();
  const h = savedAudioHarness(() => pending.promise);
  const input = h.document.querySelector("#message-input");
  input.value = "正在编辑的原草稿"; await input.dispatch("input");
  h.ui.state.attachments = [note]; const selected = h.ui.state.attachments;
  h.ui.renderComposerAttachments();
  await h.ui.switchView("uploads");
  const row = h.other.querySelectorAll(".upload-row").find((entry) => entry.textContent.includes(note.name));
  assert.equal(h.button("转写为草稿", row), undefined, "text attachments retain their original actions");
  assert.ok(h.button("带入对话", row)); assert.ok(row.querySelector("a[download]"));
  await clickMemoryButton(h, audioAction(h));
  assert.equal(h.dialog.open, true); assert.ok(h.body().includes(saved.name));
  assert.match(h.body(), /已配置.*ASR|ASR.*服务/);
  assert.match(h.body(), /草稿/); assert.match(h.body(), /发送/);
  assert.equal(h.calls.transcriptions.length, 0, "opening the confirmation is read-only");
  const confirming = h.form.dispatch("submit"); await flush();
  const repeated = h.form.dispatch("submit"); await flush();
  assert.equal(h.calls.transcriptions.length, 1);
  assert.equal(h.calls.transcriptions[0].id, saved.id);
  assert.equal(h.ui.state.view, "chat", "ASR progress and cancel must be visible in the original main conversation");
  assert.equal(h.ui.state.activeSession.id, h.ui.state.mainSessionId);
  assert.equal(messages(h).length, 0);
  assert.equal(input.value, "正在编辑的原草稿");
  assert.equal(h.ui.state.attachments === selected, true);
  pending.resolve({ text: "录音转写文字", draft: true });
  await Promise.all([confirming, repeated]); await flush();
  assert.equal(input.value, "正在编辑的原草稿\n录音转写文字");
  assert.equal(h.ui.state.attachments === selected, true);
  assert.equal(h.ui.state.voiceDraft, null);
  assert.equal(messages(h).length, 0, "a transcript remains an editable draft until the user sends it");
  assert.equal(h.calls.microphones, 0); assert.equal(h.calls.uploads, 0);
  assert.equal(h.ui.state.uploads.some((entry) => entry.id === saved.id), true);
  await h.document.querySelector("#composer").dispatch("submit"); await flush();
  assert.equal(messages(h).length, 1);
  assert.equal(messages(h)[0].body.content, "正在编辑的原草稿\n录音转写文字");
  assert.deepEqual(Array.from(messages(h)[0].body.attachment_ids), [note.id]);
});

test("a saved-audio request is revoked by reconnect before its late 401 can replace the new login or pending draft", async () => {
  const pending = deferred();
  const h = savedAudioHarness(() => pending.promise);
  const input = h.document.querySelector("#message-input");
  input.value = "原连接的文字"; await input.dispatch("input");
  await h.ui.switchView("uploads");
  await clickMemoryButton(h, audioAction(h));
  const confirming = h.form.dispatch("submit"); await flush();
  assert.equal(h.calls.transcriptions.length, 1);
  const old = h.calls.transcriptions[0];
  assert.equal(old.signal.aborted, false);
  const owner = h.ui.activateConnection(); h.ui.state.connected = true;
  input.value = "新登录继续写的文字"; await input.dispatch("input");
  const waiting = { text: "新登录尚待处理的语音草稿" }; h.ui.state.voiceDraft = waiting;
  h.document.querySelector("#voice-draft-text").value = waiting.text;
  const selected = [note]; h.ui.state.attachments = selected;
  assert.equal(old.signal.aborted, true, "reconnect must revoke the actual saved-audio HTTP attempt");
  pending.reject(new ApiError("旧连接延迟返回的认证失败", 401));
  await confirming; await flush();
  assert.equal(h.ui.state.auth.authenticated, true);
  assert.equal(owner.controller.signal.aborted, false);
  assert.equal(input.value, "新登录继续写的文字");
  assert.equal(h.ui.state.voiceDraft === waiting, true);
  assert.equal(h.document.querySelector("#voice-draft-text").value, waiting.text);
  assert.equal(h.ui.state.attachments === selected, true);
  assert.equal(h.calls.transcriptions.length, 1, "revoked work must never replay automatically");
  assert.equal(h.calls.microphones, 0); assert.equal(h.calls.uploads, 0); assert.equal(messages(h).length, 0);
});

for (const source of ["saved audio", "microphone"]) for (const outcome of ["text", "401"]) {
  test(`same-chat shortcut keeps ${source} ${outcome} current`, async () => {
    const pending = deferred(), mic = source === "microphone" ? microphoneFixture() : {};
    const h = savedAudioHarness(() => pending.promise, mic);
    const input = h.document.querySelector("#message-input"); input.value = "当前主对话草稿"; await input.dispatch("input");
    const { confirming, request } = await beginAudioSource(h, source);
    await chatShortcut(h, "ctrlKey"); await chatShortcut(h, "metaKey");
    assert.equal(h.ui.state.view, "chat"); assert.equal(h.ui.state.activeSession.id, "main");
    assert.equal(request.signal.aborted, false); assert.equal(h.document.querySelector("#voice-progress").hidden, false);
    if (outcome === "text") pending.resolve({ text: "快捷键之后仍有效的转写", attachment_id: saved.id, draft: true });
    else pending.reject(new ApiError("当前音频请求的认证已过期", 401));
    await confirming; await flush();
    if (outcome === "text") {
      assert.equal(input.value, "当前主对话草稿\n快捷键之后仍有效的转写"); assert.equal(h.ui.state.voiceDraft, null);
      assert.equal(h.ui.state.auth.authenticated, true);
    } else {
      assert.equal(h.ui.state.auth.authenticated, false, "same-chat navigation cannot swallow the still-current ASR 401");
      assert.equal(h.document.querySelector("#auth-gate").hidden, false); assert.equal(input.value, "当前主对话草稿");
    }
    assert.equal(h.calls.transcriptions.length, 1); assert.equal(h.calls.microphones, source === "microphone" ? 1 : 0);
    assert.equal(h.calls.uploads, source === "microphone" ? 1 : 0); assert.equal(messages(h).length, 0);
    if (mic.track) assert.ok(mic.track.stops > 0);
  });
}

for (const source of ["saved audio", "microphone"]) for (const boundary of ["real navigation", "changed main identity"]) for (const outcome of ["text", "401"]) {
  test(`voice ownership still fences ${source} ${outcome} after ${boundary}`, async () => {
    const pending = deferred(), h = savedAudioHarness(() => pending.promise, source === "microphone" ? microphoneFixture() : {});
    const input = h.document.querySelector("#message-input"); input.value = "本轮初始主对话草稿"; await input.dispatch("input");
    const { confirming, request } = await beginAudioSource(h, source);
    if (boundary === "real navigation") {
      await h.ui.switchView("uploads"); await chatShortcut(h, "ctrlKey");
      assert.equal(request.signal.aborted, true, "leaving chat must revoke ASR even after a same-chat shortcut returns");
    } else {
      h.ui.state.mainSessionId = "replacement-main"; h.ui.state.activeSession = { id: "replacement-main", mode: "auto", status: "idle" };
      await chatShortcut(h, "metaKey");
    }
    input.value = "下一轮主对话文字"; await input.dispatch("input");
    const waiting = { text: "下一轮已有转写" }; h.ui.state.voiceDraft = waiting;
    h.document.querySelector("#voice-draft-text").value = "下一轮已编辑转写";
    if (outcome === "text") pending.resolve({ text: "过期转写", attachment_id: saved.id, draft: true });
    else pending.reject(new ApiError("失效音频请求的旧401", 401));
    await confirming; await flush();
    assert.equal(input.value, "下一轮主对话文字"); assert.equal(h.ui.state.voiceDraft === waiting, true);
    assert.equal(h.document.querySelector("#voice-draft-text").value, "下一轮已编辑转写");
    assert.equal(h.ui.state.auth.authenticated, true); assert.equal(h.calls.transcriptions.length, 1); assert.equal(messages(h).length, 0);
  });
}

for (const close of ["close button", "native cancel", "another dialog", "refreshed attachments"]) {
  test(`saved-audio ${close} consumes no ASR request and revokes an old confirmation`, async () => {
    const h = savedAudioHarness(async () => ({ text: "不应产生的文字", draft: true }));
    const input = h.document.querySelector("#message-input"); input.value = "原输入"; await input.dispatch("input");
    await h.ui.switchView("uploads"); await clickMemoryButton(h, audioAction(h));
    const oldSubmit = h.ui.submit;
    if (close === "close button") await clickMemoryButton(h, h.document.querySelector("#dialog-close"));
    if (close === "native cancel") await h.dialog.dispatch("cancel");
    if (close === "another dialog") h.ui.openDialog({ title: "另一份记录", content: h.document.createElement("p"), readOnly: true });
    if (close === "refreshed attachments") await h.ui.switchView("uploads");
    await oldSubmit(); await flush();
    assert.equal(h.calls.transcriptions.length, 0); assert.equal(h.calls.microphones, 0); assert.equal(h.calls.uploads, 0);
    assert.equal(input.value, "原输入"); assert.equal(h.ui.state.view, "uploads"); assert.equal(messages(h).length, 0);
    if (close === "another dialog") assert.equal(h.document.querySelector("#dialog-title").textContent, "另一份记录");
  });
}

for (const cause of ["cancel", "navigate", "hide", "pagehide", "new login"]) for (const outcome of ["success", "401"]) {
  test(`saved audio ${cause} revokes a late ${outcome} while preserving the next draft`, async () => {
    const pending = deferred(), h = savedAudioHarness(() => pending.promise);
    const { confirming, request } = await beginSaved(h);
    if (cause === "cancel") await clickMemoryButton(h, h.document.querySelector('[data-action="voice-cancel"]'));
    if (cause === "navigate") await h.ui.switchView("uploads");
    if (cause === "hide") {
      h.document.visibilityState = "hidden";
      for (const callback of h.document.events.visibilitychange) await callback();
    }
    if (cause === "pagehide") h.windowEvents.pagehide();
    if (cause === "new login") h.ui.showLogin();
    assert.equal(request.signal.aborted, true);
    if (["pagehide", "new login"].includes(cause)) { h.ui.activateConnection(); h.ui.state.connected = true; h.ui.state.auth.authenticated = true; }
    const input = h.document.querySelector("#message-input"); input.value = "下一轮输入"; await input.dispatch("input");
    const waiting = { text: "下一轮待处理语音" }, selected = [note];
    h.ui.state.voiceDraft = waiting; h.ui.state.attachments = selected;
    h.document.querySelector("#voice-draft-text").value = "下一轮已经编辑的转写";
    if (outcome === "success") pending.resolve({ text: "不能落入新输入的旧结果", draft: true });
    else pending.reject(new ApiError("撤销后才到达的旧认证错误", 401));
    await confirming; await flush();
    assert.equal(input.value, "下一轮输入"); assert.equal(h.ui.state.voiceDraft === waiting, true);
    assert.equal(h.document.querySelector("#voice-draft-text").value, "下一轮已经编辑的转写");
    assert.equal(h.ui.state.attachments === selected, true); assert.equal(h.ui.state.auth.authenticated, true);
    assert.equal(h.calls.transcriptions.length, 1); assert.equal(h.calls.uploads, 0); assert.equal(h.calls.microphones, 0); assert.equal(messages(h).length, 0);
  });
}

test("a failed saved-audio transcription can be actively retried with the same saved ID and no upload", async () => {
  let attempts = 0;
  const h = savedAudioHarness(async () => {
    if (++attempts === 1) throw new ApiError("ASR 暂时不可用", 503);
    return { text: "主动重试后识别的文字", attachment_id: saved.id, draft: true };
  });
  const input = h.document.querySelector("#message-input"); input.value = "保留原文字"; await input.dispatch("input");
  await h.ui.switchView("uploads"); await clickMemoryButton(h, audioAction(h));
  assert.match(h.body(), /可能.*计费/);
  await h.form.dispatch("submit"); await flush();
  assert.equal(input.value, "保留原文字"); assert.equal(h.ui.state.voiceDraft, null);
  assert.equal(h.ui.state.uploads.some((entry) => entry.id === saved.id), true);
  await h.ui.switchView("uploads"); await clickMemoryButton(h, audioAction(h));
  assert.equal(h.calls.transcriptions.length, 1, "reopening never retries before a new explicit confirmation");
  await h.form.dispatch("submit"); await flush();
  assert.deepEqual(h.calls.transcriptions.map((entry) => entry.id), [saved.id, saved.id]);
  assert.equal(input.value, "保留原文字\n主动重试后识别的文字");
  assert.equal(h.calls.uploads, 0); assert.equal(h.calls.microphones, 0); assert.equal(messages(h).length, 0);
});

for (const change of ["edit", "send"]) {
  test(`a saved-audio result becomes pending when the user chooses to ${change} the original draft`, async () => {
    const pending = deferred(), h = savedAudioHarness(() => pending.promise);
    const input = h.document.querySelector("#message-input"); input.value = "原草稿"; await input.dispatch("input");
    h.ui.state.attachments = [note]; const selected = h.ui.state.attachments;
    const { confirming } = await beginSaved(h);
    if (change === "edit") { input.value = "转写中编辑的新文字"; await input.dispatch("input"); }
    else await h.document.querySelector("#composer").dispatch("submit");
    const current = input.value;
    pending.resolve({ text: "需要单独核对的录音文字", attachment_id: saved.id, draft: true });
    await confirming; await flush();
    assert.equal(input.value, current); assert.equal(h.ui.state.voiceDraft.text, "需要单独核对的录音文字");
    assert.equal(h.document.querySelector("#voice-draft-text").value, "需要单独核对的录音文字");
    assert.equal(h.document.querySelector("#voice-draft").hidden, false);
    if (change === "edit") assert.equal(h.ui.state.attachments === selected, true);
    assert.equal(messages(h).length, change === "send" ? 1 : 0);
    h.document.querySelector("#voice-draft-text").value = "主人修改过的待处理转写";
    await h.ui.switchView("uploads"); const button = audioAction(h); assert.equal(button.disabled, true);
    await clickMemoryButton(h, button);
    assert.equal(h.dialog.open, false); assert.equal(h.calls.transcriptions.length, 1);
    assert.equal(h.document.querySelector("#voice-draft-text").value, "主人修改过的待处理转写");
  });
}

for (const reason of ["disabled", "unconfigured", "oversized", "empty", "pending voice", "archive session", "missing main"]) {
  test(`saved-audio eligibility rejects ${reason} in both the row and the delegated action`, async () => {
    const h = savedAudioHarness(async () => ({ text: "不应转写", draft: true }));
    if (reason === "disabled") h.ui.state.status.web_voice = { ...limits, enabled: false, available: false };
    if (reason === "unconfigured") h.ui.state.status.web_voice = { ...limits, configured: false, available: false };
    if (reason === "oversized") h.ui.state.status.web_voice.max_file_bytes = saved.size - 1;
    if (reason === "empty") h.ui.state.uploads = [{ ...saved, size: 0 }, note];
    if (reason === "pending voice") { h.ui.state.voiceDraft = { text: "尚待核对" }; h.document.querySelector("#voice-draft-text").value = "已编辑，不能丢"; }
    if (reason === "archive session") h.ui.state.activeSession = { id: "archive", mode: "auto" };
    if (reason === "missing main") h.ui.state.mainSessionId = null;
    await h.ui.switchView("uploads");
    if (reason === "empty") { h.ui.state.uploads = [{ ...saved, size: 0 }, note]; h.other.replaceChildren(h.ui.renderUploads()); }
    const button = audioAction(h); assert.equal(button.disabled, true);
    await clickMemoryButton(h, button);
    assert.equal(h.dialog.open, false); assert.equal(h.calls.transcriptions.length, 0); assert.equal(h.calls.uploads, 0); assert.equal(h.calls.microphones, 0);
    if (reason === "pending voice") assert.equal(h.document.querySelector("#voice-draft-text").value, "已编辑，不能丢");
  });
}

for (const change of ["new typed text", "replacement main", "another navigation"]) {
  test(`saved-audio confirmation yields to ${change} while returning to chat`, async () => {
    const h = savedAudioHarness(async () => ({ text: "不应转写", draft: true }));
    await h.ui.switchView("uploads"); await clickMemoryButton(h, audioAction(h));
    const input = h.document.querySelector("#message-input"), focus = input.focus.bind(input); let armed = true;
    input.focus = () => {
      focus(); if (!armed) return; armed = false;
      queueMicrotask(() => {
        if (change === "new typed text") { input.value = "确认后新编辑的内容"; void input.dispatch("input"); }
        if (change === "replacement main") h.ui.state.mainSessionId = "new-main";
        if (change === "another navigation") { void h.ui.switchView("uploads"); void h.ui.switchView("chat"); }
      });
    };
    await h.form.dispatch("submit"); await flush();
    assert.equal(h.calls.transcriptions.length, 0); assert.equal(messages(h).length, 0);
    if (change === "new typed text") assert.equal(input.value, "确认后新编辑的内容");
  });
}

test("a current saved-audio 401 still asks for login through the real voice callback", async () => {
  const h = savedAudioHarness(async () => { throw new ApiError("当前登录已过期", 401); });
  await h.ui.switchView("uploads"); await clickMemoryButton(h, audioAction(h));
  await h.form.dispatch("submit"); await flush();
  assert.equal(h.calls.transcriptions.length, 1); assert.equal(h.ui.state.auth.authenticated, false);
  assert.equal(h.document.querySelector("#auth-gate").hidden, false); assert.equal(messages(h).length, 0);
});

for (const change of ["pending voice", "ASR disabled", "archive session"]) {
  test(`an open saved-audio confirmation rechecks ${change} before dispatching ASR`, async () => {
    const h = savedAudioHarness(async () => ({ text: "不应识别", draft: true }));
    const input = h.document.querySelector("#message-input"); input.value = "原主对话文字"; await input.dispatch("input");
    await h.ui.switchView("uploads"); await clickMemoryButton(h, audioAction(h));
    const waiting = { text: "确认等待时到达的待处理语音" };
    if (change === "pending voice") { h.ui.state.voiceDraft = waiting; h.document.querySelector("#voice-draft-text").value = "已编辑的待处理文字"; }
    if (change === "ASR disabled") h.ui.state.status.web_voice.available = false;
    if (change === "archive session") h.ui.state.activeSession = { id: "archive", mode: "auto" };
    await h.form.dispatch("submit"); await flush();
    assert.equal(h.calls.transcriptions.length, 0); assert.equal(h.ui.state.view, "uploads"); assert.equal(input.value, "原主对话文字");
    if (change === "pending voice") { assert.equal(h.ui.state.voiceDraft === waiting, true); assert.equal(h.document.querySelector("#voice-draft-text").value, "已编辑的待处理文字"); }
  });
}

test("a delayed native confirmation close only releases its qualification and cannot cancel the handed-off ASR", async () => {
  const pending = deferred(), h = savedAudioHarness(() => pending.promise), closeTasks = [];
  h.dialog.close = () => {
    if (!h.dialog.open) return;
    h.dialog.open = false; closeTasks.push(() => h.dialog.dispatch("close"));
  };
  const { confirming, request } = await beginSaved(h);
  assert.equal(closeTasks.length, 1); assert.equal(request.signal.aborted, false);
  await closeTasks.shift()(); await flush();
  assert.equal(request.signal.aborted, false); assert.equal(h.document.querySelector("#voice-progress").hidden, false);
  pending.resolve({ text: "确认框关闭后正常识别", attachment_id: saved.id, draft: true });
  await confirming; await flush();
  assert.equal(h.document.querySelector("#message-input").value, "确认框关闭后正常识别");
  assert.equal(h.calls.transcriptions.length, 1); assert.equal(messages(h).length, 0);
});

test("the actual preview attachment DOM uses only the configured in-memory ASR simulation", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  backend.state.settings.web_voice.enabled = true;
  backend.state.uploads.push({ ...saved }); backend.state.files.set(saved.id, new Blob(["small fixture audio"], { type: saved.mime_type }));
  const read = async (path, options = {}) => {
    const response = await backend.fetch(`/api${path}`, options), payload = await response.json();
    if (!response.ok) throw new ApiError(payload.detail, response.status, payload);
    return payload;
  };
  const h = savedAudioHarness((id, options) => read("/media/transcribe", { ...options, method: "POST", body: { attachment_id: id } }), { handler: read });
  h.ui.state.status = await read("/status"); h.ui.state.mainSessionId = "preview-main"; h.ui.state.activeSession = backend.state.sessions[0];
  const before = JSON.stringify({ messages: backend.state.messages, runs: backend.state.runs });
  await h.ui.switchView("uploads"); await clickMemoryButton(h, audioAction(h));
  assert.match(h.body(), /本页内存模拟/); assert.match(h.body(), /不连接 ASR/);
  await h.form.dispatch("submit"); await flush();
  assert.match(h.document.querySelector("#message-input").value, /演示转写/);
  assert.equal(backend.state.requests.filter((request) => request.path === "/media/transcribe").length, 1);
  assert.equal(backend.state.requests.some((request) => request.method === "POST" && request.path !== "/media/transcribe"), false);
  assert.equal(JSON.stringify({ messages: backend.state.messages, runs: backend.state.runs }), before);
  assert.equal(h.calls.uploads, 0); assert.equal(h.calls.microphones, 0);
});

test("preview saved-audio ASR keeps the existing policy, exact attachment and text budgets", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  const read = (path, body) => backend.fetch(`/api${path}`, body === undefined ? {} : { method: "POST", body });
  const transcribe = (body = { attachment_id: saved.id }) => read("/media/transcribe", body);
  assert.equal((await read("/status").then((response) => response.json())).web_voice.available, false);
  assert.equal((await transcribe()).status, 403);
  backend.state.settings.web_voice.enabled = true;
  assert.equal((await transcribe({})).status, 422);
  assert.equal((await transcribe()).status, 404);
  backend.state.uploads.push({ ...saved }); backend.state.files.set(saved.id, new Blob(["small audio"], { type: saved.mime_type }));
  const text = backend.state.uploads.find((entry) => entry.mime_type === "text/markdown");
  assert.equal((await transcribe({ attachment_id: text.id })).status, 415);
  backend.state.settings.web_voice.max_file_bytes = saved.size - 1;
  assert.equal((await transcribe()).status, 413);
  backend.state.settings.web_voice.max_file_bytes = saved.size;
  backend.state.settings.web_voice.max_text_chars = 1;
  assert.equal((await transcribe()).status, 413);
  backend.state.settings.web_voice.max_text_chars = limits.max_text_chars;
  backend.state.settings.media.asr.model = "";
  assert.equal((await transcribe()).status, 409);
  backend.state.settings.media.asr.model = "preview-asr";
  const response = await transcribe(); assert.equal(response.status, 200);
  const result = await response.json(); assert.equal(result.attachment_id, saved.id); assert.equal(result.draft, true); assert.equal(result.preview, true);
  assert.equal(backend.state.messages["preview-main"].length, 0); assert.equal(Object.keys(backend.state.runs).length, 0);
});
