import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { loadSettingsResources, capabilityLabel, managementLabel, managementActionLabel, managementPlanLabel, updateLabel, retryChannelDelivery } from "../../web/management.mjs";

test("management status loading only reads existing APIs and preserves their payloads", async () => {
  const calls = [];
  const payloads = { "/channels": { channels: { telegram: { active: false, enabled: true } }, deliveries: [{ id: "delivery", state: "unknown" }] }, "/management": { plans: [{ id: "plan", action: "restart", status: "pending" }], plugins: [], plugins_restart_required: true, update: null } };
  const result = await loadSettingsResources(async (path, options) => {
    calls.push({ path, options });
    return payloads[path] ?? { from: path };
  });
  assert.deepEqual(calls.map((call) => call.path).sort(), ["/status", "/service", "/management", "/channels", "/voice", "/capture"].sort());
  assert.ok(calls.every((call) => call.options === undefined));
  assert.deepEqual(result.values.channels.deliveries, payloads["/channels"].deliveries);
  assert.equal(result.values.management.plugins_restart_required, true);
  assert.deepEqual(result.errors, {});
});

test("an unavailable optional module does not hide other settings or invent an off state", async () => {
  const result = await loadSettingsResources(async (path) => {
    if (path === "/voice") throw new Error("语音接口暂不可用");
    return { from: path };
  });
  assert.equal(result.values.voice, null);
  assert.equal(result.errors.voice, "语音接口暂不可用");
  assert.deepEqual(result.values.capture, { from: "/capture" });
  assert.deepEqual(result.values.management, { from: "/management" });
  assert.equal(capabilityLabel(result.values.voice), "状态未知");
});

test("all unavailable status requests settle with errors and no false success", async () => {
  const result = await loadSettingsResources(async () => { throw new Error("离线"); });
  assert.equal(Object.keys(result.errors).length, 6);
  assert.ok(Object.values(result.values).every((value) => value === null));
});

test("capability labels distinguish disabled, detected, running and unknown modules", () => {
  assert.equal(capabilityLabel({ enabled: false, available: true }), "未启用");
  assert.equal(capabilityLabel({ enabled: true, running: true, available: true }), "运行中");
  assert.equal(capabilityLabel({ enabled: true, active: false }), "已启用");
  assert.equal(capabilityLabel({ enabled: true, available: false }), "暂不可用");
  assert.equal(capabilityLabel({ enabled: true, available: true }), "可用");
  assert.equal(capabilityLabel({}), "状态未知");
  assert.equal(managementLabel("unknown"), "结果未知");
  assert.equal(managementLabel("pending"), "待应用");
});

test("management action labels distinguish candidate updates, version switches and adoption", () => {
  assert.equal(managementActionLabel({ action: "switch", operation: "update" }), "版本更新");
  assert.equal(managementActionLabel({ action: "switch" }), "切换版本");
  assert.equal(managementActionLabel({ action: "adopt" }), "托管服务");
  assert.equal(managementActionLabel({ action: "configure" }), "配置变更");
  assert.equal(managementActionLabel({ action: "restart" }), "重启服务");
  assert.equal(managementActionLabel({ action: "update" }), "版本更新");
  assert.equal(managementActionLabel({ action: "future" }), "future");
});

test("candidate update plan labels follow both durable status and preparation phase", () => {
  const plan = { action: "switch", operation: "update" };
  for (const [status, phase, label] of [
    ["applying", "candidate_cleanup_required", "等待恢复"],
    ["applying", "candidate_preparing", "准备更新"],
    ["applying", "candidate_ready", "等待切换"],
    ["interrupted", "candidate_cleaned", "更新中断"],
    ["pending", "candidate_preparing", "待应用"],
    ["completed", "candidate_ready", "已完成"],
    ["failed", "candidate_cleanup_required", "失败"],
    ["interrupted", "candidate_preparing", "已中断"],
    ["applying", "prepared", "正在应用"],
  ]) assert.equal(managementPlanLabel({ ...plan, status, phase }), label);
});

test("noncandidate plans cannot borrow candidate preparation labels", () => {
  for (const action of ["switch", "configure", "restart", "update", "adopt"]) {
    for (const operation of [undefined, "switch", "update"]) {
      if (action === "switch" && operation === "update") continue;
      for (const phase of ["candidate_cleanup_required", "candidate_preparing", "candidate_ready", "candidate_cleaned"]) {
        assert.equal(managementPlanLabel({ action, operation, status: "applying", phase }), "正在应用");
        assert.equal(managementPlanLabel({ action, operation, status: "interrupted", phase }), "已中断");
      }
    }
  }
});

test("management plan list renders update recovery labels without changing its layout", () => {
  const source = readFileSync(new URL("../../web/app.mjs", import.meta.url), "utf8");
  const start = source.indexOf("function renderMaintenanceSettings()");
  const end = source.indexOf("function renderModelSettings()", start);
  assert.ok(start >= 0 && end > start);
  const element = (tag, props, ...children) => ({ tag, props, children: children.filter(Boolean), append(...items) { this.children.push(...items.filter(Boolean)); } });
  const context = {
    state: { settingsDraft: {}, status: {}, management: { plans: [
      { action: "switch", operation: "update", status: "applying", phase: "candidate_cleanup_required" },
      { action: "switch", status: "applying", phase: "candidate_cleanup_required" },
      { action: "adopt", status: "pending" },
    ] } },
    element, managementLabel, managementActionLabel, managementPlanLabel, updateLabel, formatFullDate: String,
    settingsSection: (title, ...children) => element("section", { title }, ...children),
    settingsResourceError: () => null, settingsJSON: () => null, statusList: () => null,
    switchField: () => null, field: () => null, actionButton: () => null,
  };
  runInNewContext(source.slice(start, end), context, { timeout: 1000 });
  const pending = [context.renderMaintenanceSettings()]; const headings = [];
  while (pending.length) {
    const node = pending.shift();
    if (node.props.class === "management-item-heading") headings.push(node.children.map((child) => child.props.text));
    pending.push(...node.children);
  }
  assert.deepEqual(headings, [["版本更新", "等待恢复"], ["切换版本", "正在应用"], ["托管服务", "待应用"]]);
});

test("a failed or missing update check never becomes an up-to-date claim", () => {
  assert.equal(updateLabel(null), "状态未知");
  assert.equal(updateLabel({ update: null }), "尚未检查");
  assert.equal(updateLabel({ update: { available: false, status: "unavailable" } }), "暂时无法检查");
  assert.equal(updateLabel({ update: { available: false } }), "状态未知");
  assert.equal(updateLabel({ update: { status: "checked", available: false } }), "已是最新");
  assert.equal(updateLabel({ update: { status: "checked", available: true } }), "有可用更新");
});

test("failed deliveries retry without approving unknown results", async () => {
  let captured;
  await retryChannelDelivery(async (path, options) => { captured = { path, options }; }, { id: "record/one", state: "failed" }, { allowUnknown: true });
  assert.deepEqual(captured, { path: "/channels/deliveries/record%2Fone/retry", options: { method: "POST", body: { allow_unknown: false } } });
});

test("blocked deliveries expose their reason and the existing explicit retry action", () => {
  const source = readFileSync(new URL("../../web/app.mjs", import.meta.url), "utf8");
  const start = source.indexOf("function deliveryLabel(value)");
  const end = source.indexOf("function renderInputSettings()", start);
  assert.ok(start >= 0 && end > start);
  const element = (tag, props, ...children) => ({ tag, props, children: children.filter(Boolean), append(...items) { this.children.push(...items.filter(Boolean)); } });
  const context = {
    state: { channels: { deliveries: [{ id: "blocked-origin", name: "onebot", state: "blocked", route: { target: "original-owner" }, last_error: "原回复渠道未启用" }, { id: "already-sent", name: "onebot", state: "sent" }] } },
    CHANNELS: [["onebot", "OneBot"]], element, formatFullDate: String,
    actionButton: (label, action, _style, _icon, data) => element("button", { text: label, action, ...data }),
  };
  runInNewContext(source.slice(start, end), context, { timeout: 1000 });
  const rendered = context.renderDeliveries();
  const nodes = [];
  const visit = (node) => { nodes.push(node); for (const child of node.children) visit(child); };
  visit(rendered);
  assert.ok(nodes.some((node) => node.props.text === "原路投递受阻"));
  assert.ok(nodes.some((node) => node.props.text === "原回复渠道未启用"));
  assert.ok(nodes.some((node) => node.props.text === "original-owner"));
  assert.ok(nodes.some((node) => node.tag === "button" && node.props.action === "retry-delivery" && node.props.id === "blocked-origin"));
  assert.ok(!nodes.some((node) => node.tag === "button" && node.props.id === "already-sent"));
});

test("blocked deliveries retry the same record without approving unknown results", async () => {
  const calls = [];
  const delivery = { id: "blocked/origin", state: "blocked" };
  const request = async (...args) => { calls.push(args); return { id: delivery.id, state: "pending" }; };
  assert.equal(calls.length, 0);
  const result = await retryChannelDelivery(request, delivery, { allowUnknown: true });
  assert.deepEqual(calls, [["/channels/deliveries/blocked%2Forigin/retry", { method: "POST", body: { allow_unknown: false } }]]);
  assert.equal(result.id, delivery.id);
});

test("unknown deliveries cannot retry until explicitly confirmed", async () => {
  const calls = [];
  const request = async (...args) => { calls.push(args); return { state: "pending" }; };
  await assert.rejects(retryChannelDelivery(request, { id: "uncertain", state: "unknown" }), /先确认/);
  assert.deepEqual(calls, []);
  const result = await retryChannelDelivery(request, { id: "uncertain", state: "unknown" }, { allowUnknown: true });
  assert.equal(calls[0][1].body.allow_unknown, true);
  assert.equal(result.state, "pending");
});

test("pending or completed deliveries never receive a retry request", async () => {
  for (const state of ["pending", "sending", "sent"]) await assert.rejects(retryChannelDelivery(() => assert.fail("must not send"), { id: "record", state }, { allowUnknown: true }), /不能重试/);
});
