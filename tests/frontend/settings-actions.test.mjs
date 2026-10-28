import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { changesListener, mergeSettingsExtras, setPath, settingsPatch } from "../../web/settings.mjs";
import { parseJSONField } from "../../web/state.mjs";

const source = readFileSync(new URL("../../web/app.mjs", import.meta.url), "utf8");
const start = source.indexOf("function collectSettingsForm(");
const end = source.indexOf("\nasync function serviceAction(", start);
assert.ok(start >= 0 && end > start);

const original = { server: { host: "127.0.0.1", port: 8765, password_set: true }, persona: { name: "沐璃" } };
const control = (name, value, type = "text") => ({ name, value: String(value), type, dataset: {} });

function harness(controls, respond) {
  const form = { controls };
  const submit = { disabled: false };
  const errorNode = { hidden: true, textContent: "" };
  const statusNode = { hidden: false, textContent: "已保存" };
  const calls = [], notices = [], renders = [];
  const state = { settings: structuredClone(original), settingsDraft: structuredClone(original), settingsSaveStatus: "已保存", management: { plans: [] }, activeSession: {}, mode: "auto" };
  const context = {
    state, structuredClone, settingsPatch, changesListener, setPath, mergeSettingsExtras, parseJSONField,
    $: (selector) => {
      if (selector === "#settings-form") return form;
      if (selector === 'button[type="submit"]') return submit;
      if (selector === "#settings-error") return errorNode;
      if (selector === "#settings-save-status") return statusNode;
      return controls.find((input) => input.name === selector.match(/^\[name="([^"]+)"\]$/)?.[1]);
    },
    $$: () => controls,
    request: async (path, options) => {
      calls.push({ path, options: options && JSON.parse(JSON.stringify(options)) });
      return respond(path, options);
    },
    toast: (message) => notices.push(message),
    showError: (error) => assert.fail(error.message),
    renderOtherView: () => renders.push("view"), renderChrome() {}, renderComposer() {},
    refreshSettingsResources: async () => renders.push("resources"),
    switchView: async () => renders.push("settings"),
  };
  runInNewContext(source.slice(start, end), context, { timeout: 1000 });
  const event = { preventDefault() {}, target: form };
  return { context, state, submit, errorNode, statusNode, calls, notices, renders, event };
}

test("embedding prefix separators survive collection, group switches and saving", async () => {
  const document = control("media.embedding.document_instruction", "  passage: \n", "textarea");
  const query = control("media.embedding.query_instruction", "query:\n", "textarea");
  document.dataset.format = query.dataset.format = "raw";
  const model = control("media.embedding.model", "  embedding-model  ");
  const applied = { ...structuredClone(original), media: { embedding: { document_instruction: document.value, query_instruction: query.value, model: "embedding-model" } } };
  const h = harness([document, query, model], async (_path, options) => options ? {} : applied);
  // Group navigation collects the outgoing form without saving it yet.
  h.context.collectSettingsForm();
  assert.equal(h.state.settingsDraft.media.embedding.document_instruction, document.value);
  assert.equal(h.state.settingsDraft.media.embedding.query_instruction, query.value);
  assert.equal(h.state.settingsDraft.media.embedding.model, "embedding-model");
  assert.equal(h.calls.length, 0);
  await h.context.saveSettings(h.event);
  assert.deepEqual(h.calls[0], { path: "/settings", options: { method: "PATCH", body: { media: applied.media } } });
  assert.equal(h.state.settingsDraft.media.embedding.document_instruction, document.value);
  assert.equal(h.state.settingsDraft.media.embedding.query_instruction, query.value);
});

test("clearing an optional model token limit PATCHes null without clearing other numeric settings", async () => {
  const applied = { ...structuredClone(original), models: { main: { max_output_tokens: null, timeout: 30 } } };
  const h = harness([control("models.main.max_output_tokens", "", "number"), control("models.main.timeout", "", "number")], async (_path, options) => options ? {} : applied);
  h.state.settings.models = { main: { max_output_tokens: 4096, timeout: 30 } };
  h.state.settingsDraft = structuredClone(h.state.settings);
  await h.context.saveSettings(h.event);
  assert.deepEqual(h.calls, [{ path: "/settings", options: { method: "PATCH", body: { models: { main: { max_output_tokens: null } } } } }, { path: "/settings", options: undefined }]);
  assert.equal(h.state.settingsDraft.models.main.timeout, 30);
});

test("listener save queues the complete patch without applying the target or redrawing newer input", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const port = control("server.port", 8766, "number");
  const h = harness([port, control("persona.name", "新称呼"), control("server.password", "", "password")], () => gate);
  const saved = h.state.settings, draft = h.state.settingsDraft;
  const saving = h.context.saveSettings(h.event);
  assert.equal(h.submit.disabled, true);
  assert.equal(h.state.settings === saved, true);
  assert.equal(h.state.settingsSaveStatus, "");
  port.value = "8767";
  release({ id: "listener-plan", action: "configure", status: "pending", changed_sections: ["persona", "server"] });
  await saving;
  assert.deepEqual(h.calls, [{ path: "/management/transactions", options: { method: "POST", body: { action: "configure", changes: { server: { port: 8766 }, persona: { name: "新称呼" } } } } }]);
  assert.equal(h.state.settings === saved, true);
  assert.equal(h.state.settings.server.port, 8765);
  assert.equal(h.state.settingsDraft === draft, true);
  assert.equal(port.value, "8767");
  assert.equal(h.state.settingsSaveStatus, "已排队，空闲后重启");
  assert.equal(h.statusNode.textContent, "已排队，空闲后重启");
  assert.equal(h.statusNode.hidden, false);
  assert.equal(h.state.management.plans[0].id, "listener-plan");
  assert.deepEqual(h.renders, []);
  assert.deepEqual(h.notices, ["已排队，空闲后重启"]);
  assert.equal(h.submit.disabled, false);
});

for (const status of [409, 0]) {
  test(`listener failure ${status} retains the draft and never retries through PATCH`, async () => {
    const h = harness([control("server.host", "localhost"), control("persona.name", "待提交称呼")], async () => { throw Object.assign(new Error("监听事务暂不可用"), { status }); });
    const saved = h.state.settings, draft = h.state.settingsDraft;
    await h.context.saveSettings(h.event);
    assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0].path, "/management/transactions");
    assert.equal(h.state.settings === saved, true);
    assert.equal(h.state.settingsDraft === draft, true);
    assert.equal(h.state.settingsDraft.server.host, "localhost");
    assert.equal(h.state.settingsDraft.persona.name, "待提交称呼");
    assert.equal(h.errorNode.textContent, "监听事务暂不可用");
    assert.equal(h.errorNode.hidden, false);
    assert.equal(h.statusNode.hidden, true);
    assert.equal(h.submit.disabled, false);
    assert.deepEqual(h.renders, []);
  });
}

test("an accepted helper response is only queued and never becomes saved settings", async () => {
  const h = harness([control("server.port", 8766, "number")], async () => ({ accepted: true }));
  await h.context.saveSettings(h.event);
  assert.equal(h.calls.length, 1);
  assert.equal(h.state.settings.server.port, 8765);
  assert.equal(h.state.settingsDraft.server.port, 8766);
  assert.equal(h.state.settingsSaveStatus, "已排队，空闲后重启");
  assert.deepEqual(h.state.management.plans, []);
  assert.deepEqual(h.renders, []);
});

test("unchanged listener controls keep ordinary configuration on the PATCH path", async () => {
  const applied = { ...structuredClone(original), persona: { name: "已应用称呼" } };
  const h = harness([control("server.host", "127.0.0.1"), control("server.port", 8765, "number"), control("server.password", "", "password"), control("persona.name", "已应用称呼")], async (path, options) => {
    assert.equal(path, "/settings");
    return options ? {} : applied;
  });
  await h.context.saveSettings(h.event);
  assert.deepEqual(h.calls, [{ path: "/settings", options: { method: "PATCH", body: { persona: { name: "已应用称呼" } } } }, { path: "/settings", options: undefined }]);
  assert.equal(h.state.settings === applied, true);
  assert.deepEqual(h.state.settingsDraft, applied);
  assert.equal(h.state.settingsSaveStatus, "已保存");
  assert.deepEqual(h.notices, ["设置已保存"]);
  assert.equal(h.submit.disabled, false);
});

test("unchanged settings perform no write", async () => {
  const h = harness([control("server.host", "127.0.0.1"), control("server.port", 8765, "number")], () => assert.fail("must not send"));
  await h.context.saveSettings(h.event);
  assert.deepEqual(h.calls, []);
  assert.deepEqual(h.notices, ["设置没有变化"]);
});

test("pairing refuses listener drafts without silently saving or dropping them", async () => {
  const h = harness([control("pairing-code", "pair-code"), control("server.port", 8766, "number"), control("persona.name", "待提交称呼")], () => assert.fail("must not send"));
  const button = { disabled: false };
  const draft = h.state.settingsDraft;
  await h.context.pairChiliarch(button);
  assert.deepEqual(h.calls, []);
  assert.equal(h.state.settingsDraft === draft, true);
  assert.equal(h.state.settingsDraft.server.port, 8766);
  assert.equal(h.state.settingsDraft.persona.name, "待提交称呼");
  assert.equal(h.errorNode.textContent, "请先保存监听变更，等待重启后再配对。");
  assert.equal(button.disabled, false);
  assert.deepEqual(h.renders, []);
});

test("pairing still saves ordinary edits before pairing when the listener is unchanged", async () => {
  const h = harness([control("pairing-code", "pair-code"), control("server.port", 8765, "number"), control("persona.name", "新称呼")], async () => ({}));
  const button = { disabled: false };
  await h.context.pairChiliarch(button);
  assert.deepEqual(h.calls, [{ path: "/settings", options: { method: "PATCH", body: { persona: { name: "新称呼" } } } }, { path: "/integrations/chiliarch/pair", options: { method: "POST", body: { code: "pair-code", name: "MuLi" } } }]);
  assert.equal(h.state.settingsDraft === null, true);
  assert.equal(button.disabled, false);
  assert.deepEqual(h.notices, ["配对已完成"]);
});
