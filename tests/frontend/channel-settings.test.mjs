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
    state: { settingsDraft: { channels: { onebot: { enabled, owner_ids: ["12345678"], allowed_groups: ["legacy-group"] }, telegram: { enabled }, qq: { enabled }, weixin: { enabled } } }, channels: null },
    CHANNELS: [["onebot", "OneBot"], ["telegram", "Telegram"], ["qq", "QQ 官方"], ["weixin", "微信 iLink"]],
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

test("bot activation requires private owners while preserving Weixin settings", () => {
  const { context, controls } = harness(false);
  context.renderChannelSettings();
  for (const name of ["onebot", "telegram", "qq"]) {
    const owner = controls.get(`channels.${name}.owner_ids`);
    assert.equal(owner.label, name === "onebot" ? "主人 QQ 号" : "主人用户 ID");
    assert.equal(owner.input.required, false);
    const toggle = controls.get(`channels.${name}.enabled`).input;
    toggle.events.change({ target: { checked: true } });
    assert.equal(owner.input.required, true);
    toggle.events.change({ target: { checked: false } });
    assert.equal(owner.input.required, false);
    assert.equal(controls.has(`channels.${name}.allowed_groups`), false);
  }
  assert.equal(controls.has("channels.weixin.allowed_groups"), true);
  assert.equal(controls.get("channels.weixin.owner_ids").label, "你的用户 ID");
  assert.equal(controls.get("channels.weixin.owner_ids").input.required, false);
  const active = harness(true);
  active.context.renderChannelSettings();
  for (const name of ["onebot", "telegram", "qq"]) {
    assert.equal(active.controls.get(`channels.${name}.owner_ids`).input.required, true);
  }
  assert.equal(active.controls.get("channels.weixin.owner_ids").input.required, false);
});

test("notification targets offer bot private chats and preserve Weixin choices", () => {
  const { context, controls } = harness(false);
  for (const name of ["onebot", "telegram", "qq", "weixin"]) {
    context.proactiveRouteFields({ kind: "channel", route: { name, kind: "private", target: "12345678", sender_id: "12345678" } }, name);
  }
  for (const name of ["onebot", "telegram", "qq"]) {
    assert.deepEqual(JSON.parse(JSON.stringify(controls.get(`${name}.kind`).options.choices)), [["private", "私聊"]]);
  }
  assert.deepEqual(JSON.parse(JSON.stringify(controls.get("weixin.kind").options.choices)), [["private", "私聊"], ["group", "已允许的群组"]]);
});

test("background receive status distinguishes actual history from activation and webhook silence", () => {
  const { context } = harness(true);
  const rows = [];
  context.statusList = (items) => { rows.push(...items); return null; };
  context.state.channels = { channels: { onebot: { enabled: true, active: true }, telegram: { enabled: true, active: true, receive_health: { state: "idle" } }, weixin: { enabled: true, active: true, receive_health: { state: "failed" } } } };
  context.renderChannelSettings();
  assert.deepEqual(rows.filter(([name]) => name === "后台接收").map(([, value]) => value), ["尚未验证", "故障"]);
  rows.length = 0;
  context.state.channels.channels.telegram.receive_health.state = "ready";
  context.state.channels.channels.weixin.enabled = false;
  context.renderChannelSettings();
  assert.deepEqual(rows.filter(([name]) => name === "后台接收").map(([, value]) => value), ["最近正常", "已关闭"]);
});
