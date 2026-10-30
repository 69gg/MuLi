import test from "node:test";
import assert from "node:assert/strict";
import { ApiError } from "../../web/api.mjs";
import { harness, clickMemoryButton, deferred, flush } from "./helpers/app-dom-harness.mjs";

test("registered tool settings provide a search entry in the real settings renderer", async () => {
  const h = harness(async (path) => {
    assert.equal(path, "/capabilities");
    return directory([{ id: "fixture-entry", name: "catalog_fixture_tool", description: "读取目录测试资料", risk: "read", requires_approval: false, enabled: true, origin: { kind: "unrecorded" } }]);
  });
  h.ui.state.view = "settings";
  h.ui.state.settings = { disabled_tools: [], workspace_roots: [] };
  h.ui.state.settingsDraft = structuredClone(h.ui.state.settings);
  h.ui.state.status = { tools: [{ name: "catalog_fixture_tool", description: "读取目录测试资料", risk: "read", enabled: true }] };
  h.ui.useSettingsViews();
  await clickMemoryButton(h, h.button("权限与工具", h.other));
  const toggle = h.other.querySelector('[name="tool-enabled:catalog_fixture_tool"]');
  assert.ok(toggle, "真实权限与工具页应先显示既有工具开关");
  assert.equal(toggle.checked, true);
  assert.ok(h.other.querySelector('input[aria-label="搜索工具"]'), "已注册工具应提供搜索工具入口");
});

const alpha = { id: "opaque:/alpha+entry", name: "registered_alpha", description: "查询天文资料 <img src=x onerror=alert(1)>", risk: "read", requires_approval: false, enabled: true, origin: { kind: "mcp", server: "真实资料库", remote_name: "remote.query" } };
const beta = { id: "opaque-beta", name: "mcp_looks_remote", description: "整理本地笔记", risk: "write", requires_approval: true, enabled: true, origin: { kind: "unrecorded" } };
const plugin = { id: "opaque-plugin", name: "registered_plugin", description: "生成星图", risk: "write", requires_approval: false, enabled: true, origin: { kind: "plugin", plugin_id: "real-plugin" } };
const mcpInterface = { id: "opaque-interface", name: "registered_interface", description: "查看 MCP 资源接口", risk: "read", requires_approval: false, enabled: true, origin: { kind: "mcp_interface" } };
const parameters = { type: "object", properties: { choice: { type: "string", enum: ["首选", "<img src=x onerror=alert(1)>"] }, nested: { $ref: "#/$defs/options" } }, required: ["choice"], additionalProperties: false, $defs: { options: { type: "object", properties: { values: { type: "array", items: { type: "integer" } } } } } };

function directory(items = [alpha, beta, plugin, mcpInterface], revision = "opaque-observation") {
  return { scope: "registered_tools", revision, complete: true, items: structuredClone(items) };
}

function detail(tool = alpha, revision = "opaque-observation") {
  return { scope: "registered_tools", revision, tool: { ...structuredClone(tool), parameters: structuredClone(parameters) } };
}

function defaultSettings() {
  return { disabled_tools: [], default_mode: "auto", workspace_roots: [],
    search: { provider: "searxng", base_url: "", api_key_env: "", max_results: 5, api_key: "" }, computer: { enabled: false, backend: "auto", python_path: "" },
    chiliarch: { enabled: false, base_url: "", agent_id: "" }, mcp: { mcpServers: {} }, plugins: { enabled: false, disabled: [] }, updates: { check_on_start: false } };
}

async function catalogHarness(respond, { composer = false } = {}) {
  const saved = defaultSettings();
  const status = () => ({ configured: true, tools: [alpha, beta, plugin, mcpInterface].map(({ name, description, risk }) => ({ name, description, risk, enabled: !saved.disabled_tools.includes(name) })) });
  const h = harness(async (path, options) => {
    if (respond) {
      const result = respond(path, options, saved);
      if (result !== undefined) return result;
    }
    if (path === "/capabilities") return directory([alpha, beta, plugin, mcpInterface].map((tool) => ({ ...tool, enabled: !saved.disabled_tools.includes(tool.name) })), `observation-${saved.disabled_tools.join(",")}`);
    if (path.startsWith("/capabilities/tools/")) {
      const url = new URL(path, "http://test.invalid"), id = decodeURIComponent(url.pathname.split("/").at(-1));
      return detail([alpha, beta, plugin, mcpInterface].find((tool) => tool.id === id), url.searchParams.get("revision"));
    }
    if (path === "/settings") {
      if (options.method === "PATCH") { Object.assign(saved, structuredClone(options.body)); return {}; }
      return structuredClone(saved);
    }
    if (path === "/status") return status();
    if (["/service", "/management", "/channels", "/voice", "/capture"].includes(path)) return {};
    assert.fail(`unexpected request: ${path}`);
  }, { composer });
  h.ui.state.view = "settings";
  h.ui.state.settings = structuredClone(saved);
  h.ui.state.settingsDraft = structuredClone(saved);
  h.ui.state.status = status();
  h.ui.useSettingsViews();
  await clickMemoryButton(h, h.button("权限与工具", h.other));
  return h;
}

const searchInput = (h) => h.other.querySelector('input[aria-label="搜索工具"]');
const sourceInput = (h) => h.other.querySelector('select[aria-label="工具来源"]');
const toggle = (h, tool = alpha) => h.other.querySelector(`[name="tool-enabled:${tool.name}"]`);
const detailButton = (h, tool = alpha) => h.other.querySelectorAll('[data-action="view-capability-tool"]').find((button) => button.dataset.capabilityId === tool.id);
const onlyReads = (h) => assert.ok(h.requests.every((request) => request.method === undefined));

test("the real catalog filters local text and real origins and displays the exact schema as text", async () => {
  const h = await catalogHarness();
  assert.equal(searchInput(h).name, ""); assert.equal(sourceInput(h).name, "");
  assert.match(h.other.textContent, /已保存：启用/); assert.match(h.other.textContent, /草稿：启用/);
  assert.match(h.other.textContent, /来源未记录 · 本地注册集合/); assert.doesNotMatch(h.other.textContent, /内置/);
  searchInput(h).value = "天文"; await searchInput(h).dispatch("input");
  assert.ok(toggle(h)); assert.equal(toggle(h, beta), null);
  searchInput(h).value = ""; await searchInput(h).dispatch("input");
  const source = sourceInput(h);
  source.value = source.children.find((option) => option.textContent === "插件 · real-plugin").value;
  await source.dispatch("change");
  assert.equal(toggle(h), null); assert.ok(toggle(h, plugin));
  source.value = ""; await source.dispatch("change");
  await clickMemoryButton(h, detailButton(h));
  const request = h.requests.at(-1), url = new URL(request.path, "http://test.invalid");
  assert.equal(decodeURIComponent(url.pathname.split("/").at(-1)), alpha.id);
  assert.equal(url.searchParams.get("revision"), "observation-");
  assert.equal(h.other.querySelector("pre").textContent, JSON.stringify(parameters, null, 2));
  assert.equal(h.other.querySelectorAll("img").length, 0);
  assert.ok(h.other.querySelectorAll("summary").some((summary) => summary.textContent === "完整参数 JSON"));
  onlyReads(h);
});

test("filtered-out checkbox drafts survive real configuration jumps and the original save PATCH", async () => {
  const h = await catalogHarness(undefined, { composer: true });
  toggle(h).checked = false; await toggle(h).dispatch("change");
  assert.match(h.other.textContent, /草稿：停用（尚未保存）/);
  searchInput(h).value = "本地笔记"; await searchInput(h).dispatch("input");
  assert.equal(toggle(h), null);
  await clickMemoryButton(h, h.button("MCP 与 Skills 设置", h.other));
  assert.equal(h.ui.state.settingsTab, "connections");
  await clickMemoryButton(h, h.button("权限与工具", h.other));
  await clickMemoryButton(h, h.button("本地插件设置", h.other));
  assert.equal(h.ui.state.settingsTab, "maintenance");
  await clickMemoryButton(h, h.button("权限与工具", h.other));
  assert.equal(toggle(h).checked, false);
  onlyReads(h);
  searchInput(h).value = "本地笔记"; await searchInput(h).dispatch("input");
  const before = h.requests.filter((request) => request.path === "/capabilities").length;
  await h.other.querySelector("#settings-form").dispatch("submit"); await flush();
  const writes = h.requests.filter((request) => request.method);
  assert.equal(writes.length, 1); assert.equal(writes[0].path, "/settings"); assert.equal(writes[0].method, "PATCH");
  assert.deepEqual(JSON.parse(JSON.stringify(writes[0].body)), { disabled_tools: [alpha.name] });
  assert.ok(h.requests.filter((request) => request.path === "/capabilities").length > before);
  assert.equal(toggle(h).checked, false); assert.match(h.other.textContent, /已保存：停用/);
  assert.doesNotMatch(h.other.textContent, /尚未保存/);
});

test("a header status refresh preserves edits made while its resources are pending", async () => {
  const pending = deferred(); let waiting = false;
  const h = await catalogHarness((path) => path === "/status" && waiting ? pending.promise : undefined);
  const roots = h.other.querySelector('[name="workspace_roots"]'); roots.value = "/before-refresh";
  waiting = true;
  const refresh = h.ui.handleAction("refresh-settings-status", h.other.querySelector('[data-action="refresh-settings-status"]'));
  roots.value = "/typed-while-refreshing";
  pending.resolve({ configured: true }); await refresh; await flush();
  assert.equal(h.other.querySelector('[name="workspace_roots"]').value, "/typed-while-refreshing");
  assert.deepEqual(JSON.parse(JSON.stringify(h.ui.state.settingsDraft.workspace_roots)), ["/typed-while-refreshing"]);
  onlyReads(h);
});

test("stale detail clears the old observation and requires an explicit refresh while retaining drafts", async () => {
  let stale = true;
  const h = await catalogHarness((path) => path.startsWith("/capabilities/tools/") && stale ? Promise.reject(new ApiError("stale", 409, { detail: { code: "capability_catalog_stale" } })) : undefined);
  toggle(h).checked = false;
  await clickMemoryButton(h, detailButton(h));
  assert.match(h.other.textContent, /目录已变化，请刷新目录/);
  assert.equal(h.other.querySelector("pre"), null); assert.equal(toggle(h), null);
  assert.equal(h.requests.filter((request) => request.path === "/capabilities").length, 1);
  assert.ok(h.ui.state.settingsDraft.disabled_tools.includes(alpha.name));
  stale = false;
  await clickMemoryButton(h, h.button("刷新目录", h.other));
  assert.equal(toggle(h).checked, false);
  await clickMemoryButton(h, detailButton(h));
  assert.ok(h.other.querySelector("pre"));
  onlyReads(h);
});

for (const [status, code, text] of [[413, "capability_payload_too_large", /超过当前显示限额/], [422, "capability_contract_sensitive", /包含敏感值/], [422, "capability_contract_invalid", /不是可完整显示的 JSON/]]) {
  test(`detail ${code} shows a complete-display error without a partial JSON contract`, async () => {
    const h = await catalogHarness((path) => path.startsWith("/capabilities/tools/") ? Promise.reject(new ApiError("private backend text", status, { detail: { code } })) : undefined);
    await clickMemoryButton(h, detailButton(h));
    assert.match(h.other.textContent, text); assert.equal(h.other.querySelector("pre"), null);
    assert.doesNotMatch(h.other.textContent, /private backend text/); onlyReads(h);
  });
}

test("an empty catalog and a local search with no match have distinct messages", async () => {
  const empty = await catalogHarness((path) => path === "/capabilities" ? directory([]) : undefined);
  assert.match(empty.other.textContent, /当前服务尚未注册工具/);
  const h = await catalogHarness(); searchInput(h).value = "不存在的用途"; await searchInput(h).dispatch("input");
  assert.match(h.other.textContent, /没有匹配的工具/); assert.doesNotMatch(h.other.textContent, /能力缺失|尚未注册工具/);
});

test("late directory authentication failures cannot log out a different settings tab", async () => {
  const pending = deferred();
  const h = await catalogHarness((path) => path === "/capabilities" ? pending.promise : undefined);
  const request = h.requests.at(-1);
  await clickMemoryButton(h, h.button("MCP 与 Skills 设置", h.other));
  assert.equal(request.signal.aborted, true);
  pending.reject(new ApiError("old unauthorized", 401)); await flush();
  assert.equal(h.ui.state.auth.authenticated, true); assert.equal(h.ui.state.settingsTab, "connections");
  assert.equal(h.other.querySelector("#capability-catalog-results"), null);
});

for (const change of ["close", "filter", "tab", "connection", "draft", "view"]) {
  test(`late detail authentication failures cannot revive or log out the UI after ${change}`, async () => {
    const pending = deferred();
    const h = await catalogHarness((path) => path.startsWith("/capabilities/tools/") ? pending.promise : undefined);
    const reading = h.ui.handleAction("view-capability-tool", detailButton(h));
    const request = h.requests.at(-1);
    if (change === "close") await clickMemoryButton(h, h.button("关闭参数", h.other));
    if (change === "filter") { searchInput(h).value = "本地笔记"; await searchInput(h).dispatch("input"); }
    if (change === "tab") await clickMemoryButton(h, h.button("MCP 与 Skills 设置", h.other));
    if (change === "connection") { h.ui.activateConnection(); h.ui.useSettingsViews(); await flush(); }
    if (change === "draft") { h.ui.state.settingsDraft = structuredClone(h.ui.state.settingsDraft); h.ui.useSettingsViews(); await flush(); }
    if (change === "view") { await h.ui.switchView("chat"); await h.ui.switchView("settings"); await flush(); }
    assert.equal(request.signal.aborted, true);
    pending.reject(new ApiError("old unauthorized", 401)); await reading; await flush();
    assert.equal(h.ui.state.auth.authenticated, true); assert.equal(h.other.querySelector("pre"), null);
    assert.doesNotMatch(h.other.textContent, /old unauthorized|正在读取完整参数/);
  });
}

test("a later selected opaque tool owns the detail even when an older response arrives last", async () => {
  const pending = deferred();
  const h = await catalogHarness((path) => path.startsWith(`/capabilities/tools/${encodeURIComponent(alpha.id)}?`) ? pending.promise : undefined);
  const old = h.ui.handleAction("view-capability-tool", detailButton(h));
  await clickMemoryButton(h, detailButton(h, beta));
  const shown = h.other.querySelector("#capability-catalog-detail").textContent;
  pending.resolve(detail(alpha, "observation-")); await old; await flush();
  assert.equal(h.other.querySelector("#capability-catalog-detail").textContent, shown);
  assert.match(shown, new RegExp(beta.name)); onlyReads(h);
});
