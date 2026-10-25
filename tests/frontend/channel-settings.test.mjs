import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";

const source = readFileSync(new URL("../../web/app.mjs", import.meta.url), "utf8");
const channels = source.slice(source.indexOf("function renderChannelSettings("), source.indexOf("\nfunction renderProactiveSettings("));
const routes = source.slice(source.indexOf("function proactiveRouteFields("), source.indexOf("\nfunction deliveryLabel("));

function harness(enabled) {
  const controls = new Map();
  const element = (tag, props, ...children) => ({ tag, props, children: children.filter(Boolean), append(...items) { this.children.push(...items.filter(Boolean)); } });
  const field = (label, name, value, options = {}) => {
    const input = { value, required: options.required ?? false, events: {}, addEventListener(event, handler) { this.events[event] = handler; } };
    const control = { label, name, input, options };
    controls.set(name, control);
    return control;
  };
  const context = {
    state: { settingsDraft: { channels: { onebot: { enabled, owner_ids: ["12345678"], allowed_groups: ["legacy-group"] }, telegram: {} } }, channels: null },
    CHANNELS: [["onebot", "OneBot"], ["telegram", "Telegram"]],
    element, field, switchField: (label, name, value) => field(label, name, value),
    selectField: (label, name, value, choices) => field(label, name, value, { choices }),
    $: (selector, parent) => parent.input,
    capabilityLabel: () => "未启用", settingsResourceError: () => null,
    settingsJSON: () => null, statusList: () => null, settingsSection: () => null,
    renderProactiveSettings: () => null, renderDeliveries: () => null,
  };
  runInNewContext(channels + "\n" + routes, context, { timeout: 1000 });
  return { context, controls };
}

test("OneBot activation requires its owner's QQ field without blocking disabled channels", () => {
  const { context, controls } = harness(false);
  context.renderChannelSettings();
  const owner = controls.get("channels.onebot.owner_ids");
  assert.equal(owner.label, "主人 QQ 号");
  assert.equal(owner.input.required, false);
  const toggle = controls.get("channels.onebot.enabled").input;
  toggle.events.change({ target: { checked: true } });
  assert.equal(owner.input.required, true);
  toggle.events.change({ target: { checked: false } });
  assert.equal(owner.input.required, false);
  assert.equal(controls.has("channels.onebot.allowed_groups"), false);
  assert.equal(controls.has("channels.telegram.allowed_groups"), true);
  const active = harness(true);
  active.context.renderChannelSettings();
  assert.equal(active.controls.get("channels.onebot.owner_ids").input.required, true);
});

test("notification targets offer private chat only for OneBot and retain Telegram group choices", () => {
  const { context, controls } = harness(false);
  for (const name of ["onebot", "telegram"]) {
    context.proactiveRouteFields({ kind: "channel", route: { name, kind: "private", target: "12345678", sender_id: "12345678" } }, name);
  }
  assert.deepEqual(JSON.parse(JSON.stringify(controls.get("onebot.kind").options.choices)), [["private", "私聊"]]);
  assert.deepEqual(JSON.parse(JSON.stringify(controls.get("telegram.kind").options.choices)), [["private", "私聊"], ["group", "已允许的群组"]]);
});
