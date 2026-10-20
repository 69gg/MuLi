import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { selectProactiveDestination, selectNotificationPreference, selectPreferenceDestination, preferenceStatusLabel, setPath, mergeSettingsExtras, settingsPatch } from "../../web/settings.mjs";

const source = readFileSync(new URL("../../web/app.mjs", import.meta.url), "utf8");
const render = source.slice(source.indexOf("function renderProactiveSettings("), source.indexOf("\nfunction deliveryLabel("));
const collect = source.slice(source.indexOf("function collectSettingsForm("), source.indexOf("\nfunction displaySettingsError("));

function harness(config) {
  const form = { controls: [] };
  const element = (tag, props, ...children) => ({ tag, props, children: children.filter(Boolean), append(...items) { this.children.push(...items.filter(Boolean)); } });
  const makeControl = (label, name, value, options = {}) => {
    const input = { name, value: String(value), type: options.type ?? "text", checked: Boolean(value), dataset: {}, events: {}, addEventListener(event, handler) { this.events[event] = handler; } };
    form.controls.push(input);
    return { ...element("label", { text: label }, element("input", options)), input };
  };
  const context = {
    state: { settingsDraft: { channels: structuredClone(config) }, channels: { channels: { onebot: { account_id: "bot" } }, proactive: { attention_enabled: true, model_configured: false } } },
    CHANNELS: [["onebot", "OneBot"], ["telegram", "Telegram"]],
    element, settingsSection: (label, ...children) => element("section", { text: label }, ...children),
    selectField: makeControl, field: makeControl,
    switchField: (label, name, checked) => makeControl(label, name, checked, { type: "checkbox" }),
    statusList: (rows) => element("status", { rows }),
    $: (selector, parent) => selector === "#settings-form" ? form : ["select", "input"].includes(selector) ? parent.input : form.controls.find((input) => input.name === selector.match(/^\[name="([^"]+)"\]$/)?.[1]),
    $$: () => form.controls,
    setPath, mergeSettingsExtras, selectProactiveDestination, selectNotificationPreference, selectPreferenceDestination, preferenceStatusLabel,
    renderOtherView() {},
    displaySettingsError(error) { throw error; },
  };
  runInNewContext(`${render}\n${collect}`, context, { timeout: 1000 });
  return { context, form };
}

test("real settings handler collects the form then enables a selected external default once", () => {
  const { context, form } = harness({ onebot: { owner_ids: ["first-owner-is-not-a-default"] }, proactive: { external_enabled: false, default_destination: { kind: "web" } } });
  context.renderProactiveSettings(context.state.settingsDraft.channels);
  const selected = form.controls.find((input) => input.name === "proactive-destination");
  selected.events.change({ target: { value: "onebot" } });
  const policy = context.state.settingsDraft.channels.proactive;
  assert.equal(policy.external_enabled, true);
  assert.deepEqual(policy.default_destination, { kind: "channel", route: { name: "onebot", account_id: "bot", kind: "private", target: "", sender_id: "" } });
  assert.equal("proactive-destination" in context.state.settingsDraft, false);
  assert.equal("proactive-pause" in context.state.settingsDraft, false);
});

test("real form preserves explicit addresses and pause without promising delivery", () => {
  const config = { proactive: { external_enabled: false, default_destination: { kind: "channel", route: { name: "onebot", account_id: "bot", kind: "private", target: "7", sender_id: "7" } } } };
  const { context, form } = harness(config);
  const rendered = context.renderProactiveSettings(context.state.settingsDraft.channels);
  const nodes = [];
  const visit = (node) => { nodes.push(node); for (const child of node.children ?? []) visit(child); };
  visit(rendered);
  assert.ok(nodes.some((node) => node.props?.text?.includes("排队不代表送达")));
  assert.ok(nodes.some((node) => node.props?.rows?.some((row) => row[0] === "主模型" && row[1] === "未配置")));
  form.controls.find((input) => input.name === "channels.proactive.default_destination.route.target").value = "8";
  form.controls.find((input) => input.name === "channels.proactive.default_destination.route.sender_id").value = "8";
  form.controls.find((input) => input.name === "proactive-pause").checked = true;
  context.collectSettingsForm();
  assert.equal(context.state.settingsDraft.channels.proactive.external_enabled, false);
  assert.equal(context.state.settingsDraft.channels.proactive.default_destination.route.target, "8");
  assert.equal(context.state.settingsDraft.channels.proactive.default_destination.route.sender_id, "8");
  assert.equal(config.proactive.default_destination.route.target, "7");
  const patch = settingsPatch({ channels: config }, context.state.settingsDraft);
  assert.equal(patch.channels.proactive.external_enabled, false);
  assert.equal(patch.channels.proactive.default_destination.route.target, "8");
});

test("selecting external then pausing before first save sends an explicit pause", () => {
  const original = { onebot: { account_id: "bot" }, proactive: { external_enabled: false, default_destination: { kind: "web" } } };
  const first = harness(original);
  first.context.renderProactiveSettings(first.context.state.settingsDraft.channels);
  first.form.controls.find((input) => input.name === "proactive-destination").events.change({ target: { value: "onebot" } });
  const second = harness(first.context.state.settingsDraft.channels);
  second.context.renderProactiveSettings(second.context.state.settingsDraft.channels);
  second.form.controls.find((input) => input.name === "channels.proactive.default_destination.route.target").value = "7";
  second.form.controls.find((input) => input.name === "channels.proactive.default_destination.route.sender_id").value = "7";
  second.form.controls.find((input) => input.name === "proactive-pause").checked = true;
  second.context.collectSettingsForm();
  const patch = settingsPatch({ channels: original }, second.context.state.settingsDraft);
  assert.equal(patch.channels.proactive.external_enabled, false);
  assert.equal(patch.channels.proactive.default_destination.route.target, "7");
});

test("explicit memory binding uses complete identity and destination without inferring an owner", () => {
  const original = { proactive: { external_enabled: false, default_destination: { kind: "web" }, memory_preference: null } };
  const first = harness(original);
  first.context.renderProactiveSettings(first.context.state.settingsDraft.channels);
  first.form.controls.find((input) => input.name === "proactive-memory-enabled").events.change({ target: { checked: true } });
  const second = harness(first.context.state.settingsDraft.channels);
  second.context.renderProactiveSettings(second.context.state.settingsDraft.channels);
  second.form.controls.find((input) => input.name === "channels.proactive.memory_preference.memory_id").value = "fact";
  second.form.controls.find((input) => input.name === "channels.proactive.memory_preference.version").value = "3";
  second.form.controls.find((input) => input.name === "proactive-memory-destination").events.change({ target: { value: "onebot" } });
  const selected = second.context.state.settingsDraft.channels.proactive.memory_preference;
  assert.equal(selected.destination.route.target, "");
  assert.equal(selected.destination.route.sender_id, "");
  const third = harness(second.context.state.settingsDraft.channels);
  third.context.renderProactiveSettings(third.context.state.settingsDraft.channels);
  third.form.controls.find((input) => input.name === "channels.proactive.memory_preference.destination.route.target").value = "8";
  third.form.controls.find((input) => input.name === "channels.proactive.memory_preference.destination.route.sender_id").value = "8";
  third.form.controls.find((input) => input.name === "proactive-pause").checked = true;
  third.context.collectSettingsForm();
  const patch = settingsPatch({ channels: original }, third.context.state.settingsDraft);
  assert.equal(patch.channels.proactive.external_enabled, false);
  assert.deepEqual(JSON.parse(JSON.stringify(patch.channels.proactive.memory_preference)), { memory_id: "fact", version: 3, destination: { kind: "channel", route: { name: "onebot", account_id: "bot", kind: "private", target: "8", sender_id: "8" } } });
  assert.equal("proactive-memory-enabled" in third.context.state.settingsDraft, false);
  assert.equal("proactive-memory-destination" in third.context.state.settingsDraft, false);
});

test("unchanged paused binding stays paused in a complete replacement and can be removed", () => {
  const original = { proactive: { external_enabled: false, default_destination: { kind: "web" }, memory_preference: { memory_id: "old", version: 1, destination: { kind: "channel", route: { name: "onebot", account_id: "bot", kind: "private", target: "7", sender_id: "7" } } } } };
  const { context, form } = harness(original);
  context.state.channels.proactive.memory_status = { valid: false, reason: "version_changed" };
  const rendered = context.renderProactiveSettings(context.state.settingsDraft.channels);
  assert.deepEqual(settingsPatch({ channels: original }, context.state.settingsDraft), {});
  assert.ok(rendered.children.some((node) => node.props?.rows?.some((row) => row[1] === "偏好版本已更正，使用普通默认")));
  form.controls.find((input) => input.name === "channels.proactive.memory_preference.version").value = "2";
  context.collectSettingsForm();
  const patch = settingsPatch({ channels: original }, context.state.settingsDraft);
  assert.equal(patch.channels.proactive.external_enabled, false);
  assert.equal(patch.channels.proactive.memory_preference.memory_id, "old");
  assert.equal(patch.channels.proactive.memory_preference.version, 2);
  form.controls.find((input) => input.name === "proactive-memory-enabled").events.change({ target: { checked: false } });
  assert.equal(settingsPatch({ channels: original }, context.state.settingsDraft).channels.proactive.memory_preference, null);
  assert.deepEqual(settingsPatch({ channels: { proactive: { memory_preference: null } } }, { channels: { proactive: { memory_preference: null } } }), {});
});
