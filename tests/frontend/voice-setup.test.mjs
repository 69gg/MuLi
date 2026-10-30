import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";
import { VoiceSetup, voiceSetupLabel } from "../../web/voice-setup.mjs";

const completed = { operation: "verify", state: "completed", wake_detected: true, speaker_configured: true, speaker_verified: false };

test("finite setup sends explicit confirmation and keeps independent acoustic results", async () => {
  const calls = [], changes = [];
  const setup = new VoiceSetup({ active: () => true, request: async (...args) => { calls.push(args); return completed; }, onChange: (value) => changes.push(value.phase) });
  await setup.run("verify");
  assert.match(calls[0][1].body.id, /^[0-9a-f-]{36}$/u);
  assert.deepEqual(calls[0][1].body, { id: setup.identifier, operation: "verify", seconds: 5, confirmed: true });
  assert.equal(calls[0][0], "/voice/setup");
  assert.deepEqual(changes, ["recording", "completed"]);
  assert.equal(voiceSetupLabel(setup.status()), "唤醒命中 · 声纹未通过");
});

test("leaving the view aborts and discards a late successful response", async () => {
  let finish, options, active = true;
  const setup = new VoiceSetup({ active: () => active, request: async (_path, args) => { options = args; return new Promise((resolve) => { finish = resolve; }); } });
  const pending = setup.run("enroll");
  active = false; setup.release();
  assert.equal(options.signal.aborted, true);
  finish({ operation: "enroll", state: "completed", enrolled: true });
  assert.equal(await pending, undefined);
  assert.equal(setup.status().phase, "idle"); assert.equal(setup.status().result, null);
});

test("busy operations cannot queue another recording and cancelled observer cannot resurrect", async () => {
  let finish;
  const calls = [];
  const setup = new VoiceSetup({ active: () => true, request: async (path) => { calls.push(path); return path.endsWith("/cancel") ? { state: "cancelled" } : new Promise((resolve) => { finish = resolve; }); } });
  const pending = setup.run("enroll");
  await assert.rejects(setup.run("verify"), /等待/u);
  await setup.stop();
  assert.equal(setup.status().phase, "cancelled");
  finish({ operation: "enroll", state: "completed", enrolled: true }); await pending;
  assert.equal(setup.status().phase, "cancelled"); assert.equal(calls.length, 2);
});

test("unconfirmed completion and unknown cancellation remain failures", async () => {
  let mode = "record";
  const setup = new VoiceSetup({ active: () => true, request: async () => mode === "record" ? { state: "comparing" } : { state: "unknown" } });
  await setup.run("verify"); assert.equal(setup.status().phase, "error");
  setup.controller = new AbortController(); mode = "cancel";
  await setup.stop(); assert.equal(setup.status().phase, "error");
  assert.match(voiceSetupLabel(setup.status()), /未确认/u);
});

test("a failed recording or cancellation never reports success", async () => {
  const setup = new VoiceSetup({ active: () => true, request: async () => { throw new Error("offline"); } });
  await setup.run("verify"); assert.equal(setup.status().phase, "error");
  assert.equal(voiceSetupLabel(setup.status()), "offline");
  setup.controller = new AbortController(); await setup.stop();
  assert.equal(setup.status().phase, "error");
});

test("inactive controls never send a recording request", async () => {
  const setup = new VoiceSetup({ active: () => false, request: async () => assert.fail("inactive") });
  await setup.run("verify"); await setup.stop();
  assert.equal(setup.status().phase, "idle");
});

test("leaving while cancellation is pending fences a late cancellation error", async () => {
  let fail, finish;
  const setup = new VoiceSetup({ active: () => true, request: async (path) => path.endsWith("/cancel") ? new Promise((_resolve, reject) => { fail = reject; }) : new Promise((resolve) => { finish = resolve; }) });
  const pending = setup.run("enroll");
  const stopping = setup.stop(); setup.release();
  fail(new Error("late error")); await stopping;
  assert.equal(setup.status().phase, "idle"); assert.equal(setup.status().error, "");
  finish(completed); await pending;
});

test("hidden page stops both voice entry flows while visible page only reconnects", async () => {
  const source = await readFile(new URL("../../web/app.mjs", import.meta.url), "utf8");
  const start = source.indexOf('document.addEventListener("visibilitychange"');
  const end = source.indexOf("\n});", start) + "\n});".length;
  const events = [], calls = [];
  const document = { visibilityState: "visible", addEventListener: (name, listener) => events.push([name, listener]) };
  runInNewContext(source.slice(start, end), { document, closeChannelDeliveryDialog() {}, releaseAnswerSpeech() {}, voiceInput: { cancel: () => calls.push("draft") }, state: { voiceSetupController: { release: () => calls.push("setup") } }, recoverConnection: () => calls.push("recover") });
  assert.equal(events[0][0], "visibilitychange");
  events[0][1](); assert.deepEqual(calls, ["recover"]);
  calls.length = 0; document.visibilityState = "hidden";
  events[0][1](); assert.deepEqual(calls, ["draft", "setup", "recover"]);
});

test("a delayed cancel stays bound to its original recording after reentering", async () => {
  const calls = [];
  const setup = new VoiceSetup({ active: () => true, request: (path, options) => new Promise((resolve) => calls.push({ path, options, resolve })) });
  const first = setup.run("enroll"), stopping = setup.stop();
  const firstId = calls[0].options.body.id;
  assert.equal(calls[1].options.body.id, firstId);
  setup.release();
  const second = setup.run("verify"), secondId = calls[2].options.body.id;
  assert.notEqual(secondId, firstId);
  calls[1].resolve({ state: "cancelled", id: firstId }); await stopping;
  assert.equal(setup.status().phase, "recording");
  calls[0].resolve({ state: "cancelled", id: firstId }); await first;
  calls[2].resolve({ ...completed, id: secondId }); await second;
  assert.equal(setup.status().result.id, secondId);
});

test("the app uses connection-owned requests for setup and cancellation", async () => {
  const source = await readFile(new URL("../../web/app.mjs", import.meta.url), "utf8");
  const start = source.indexOf("const voiceSetup = new VoiceSetup({"), end = source.indexOf("const voiceInput = ", start);
  const state = { connected: true, view: "settings", settingsTab: "inputs" }, calls = [];
  runInNewContext(source.slice(start, end), { state, VoiceSetup, renderVoiceSetupControls() {}, request: () => assert.fail("unowned request"), ownedRequest: async (...args) => { calls.push(args); return completed; } });
  await state.voiceSetupController.run("verify");
  assert.equal(calls[0][0], "/voice/setup");
});

test("voice summary distinguishes actual background history from a successful preflight", async () => {
  const source = await readFile(new URL("../../web/app.mjs", import.meta.url), "utf8");
  const start = source.indexOf("function voiceStatusSummary("), end = source.indexOf("function renderVoiceSetupControls(", start);
  const render = runInNewContext(source.slice(start, end) + "; voiceStatusSummary;", { statusList: (rows) => rows, capabilityLabel: () => "预检可用" });
  const label = (status) => render({ enabled: true, available: true, wake: { available: true }, ...status }).find(([name]) => name === "后台监听")[1];
  assert.equal(label({}), "尚未验证");
  assert.equal(label({ health: { state: "idle" } }), "尚未验证");
  assert.equal(label({ health: { state: "ready" } }), "最近正常");
  assert.equal(label({ health: { state: "failed" } }), "故障");
  assert.equal(label({ enabled: false, health: { state: "ready" } }), "已关闭");
});
