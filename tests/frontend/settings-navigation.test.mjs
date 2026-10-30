import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";
import { SETTINGS_GROUPS, setPath } from "../../web/settings.mjs";
import { parseJSONField } from "../../web/state.mjs";
import { loadSettingsResources } from "../../web/management.mjs";

const source = await readFile(new URL("../../web/app.mjs", import.meta.url), "utf8");
function section(start, end) { return source.slice(source.indexOf(start), source.indexOf(end)); }

// Only the settings shell/panel renderers and real navigation handler are
// evaluated. Other settings panels are small controls, not network substitutes.
function harness({ reduced = false, respond } = {}) {
  let active = null;
  class Node {
    constructor(tag = "text", text = "") {
      this.tagName = tag; this.text = text; this.children = []; this.parent = null;
      this.className = ""; this.dataset = {}; this.attributes = {}; this.handlers = {};
      this.id = ""; this.value = ""; this.name = ""; this.type = ""; this.hidden = false; this.disabled = false;
      this.scrollTop = 0; this.scrollWidth = this.clientWidth = 200; this.replacements = 0; this.animations = [];
      this.classList = {
        contains: (name) => this.className.split(" ").includes(name),
        toggle: (name, enabled) => {
          const names = new Set(this.className.split(" ").filter(Boolean));
          if (enabled) names.add(name); else names.delete(name);
          this.className = [...names].join(" ");
        },
      };
    }
    set textContent(value) { this.text = String(value); }
    get textContent() { return this.text + this.children.map((child) => child.textContent).join(""); }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    getAttribute(key) { return this.attributes[key]; }
    addEventListener(type, handler) { this.handlers[type] = handler; }
    append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
    replaceChildren(...children) { for (const child of this.children) child.parent = null; this.children = []; this.replacements += 1; this.append(...children); }
    focus() { active = this; }
    closest(selector) { return this.matches(selector) ? this : this.parent?.closest(selector); }
    contains(node) { return node === this || this.children.some((child) => child.contains(node)); }
    matches(selector) {
      if (selector === "[name]") return Boolean(this.name);
      if (selector === "[data-settings-tab]") return Boolean(this.dataset.settingsTab);
      const attribute = /^\[(name|data-action)="([^"]+)"\]$/.exec(selector);
      if (attribute) return (attribute[1] === "name" ? this.name : this.dataset.action) === attribute[2];
      if (selector.startsWith(".")) return selector.slice(1).split(".").every((name) => this.classList.contains(name));
      if (selector.startsWith("#")) return this.id === selector.slice(1);
      return this.tagName === selector;
    }
    querySelectorAll(selector) {
      const tokens = selector.split(" ");
      const descend = (node) => node.children.flatMap((child) => [child, ...descend(child)]);
      if (tokens.length === 1) return descend(this).filter((node) => node.matches(selector));
      return this.querySelectorAll(tokens[0]).flatMap((node) => node.querySelectorAll(tokens.slice(1).join(" ")));
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
    getAnimations() { return this.animations; }
    animate(frames, options) { this.animations.push({ frames, options, cancel() {} }); }
  }
  const document = new Node("document");
  document.createElement = (tag) => new Node(tag);
  document.createTextNode = (text) => new Node("text", text);
  document.body = document;
  Object.defineProperty(document, "activeElement", { get: () => active ?? document.body });
  const root = new Node("div"); root.id = "other-view"; document.append(root);
  const state = { view: "settings", settingsTab: "models", settingsDraft: { models: { main: { model: "original" } }, persona: { name: "沐璃" }, mcp: { mcpServers: {} } }, settingsSaveStatus: "", settingsStatusErrors: {}, viewLoading: false, viewError: "", status: { extensions: { generation: 1, phase: "loaded", skills: { enabled: true, count: 2 }, mcp: [] } } };
  const requests = [];
  const notices = [], logins = [];
  const owner = { ready: true, controller: new AbortController() };
  const request = async (path, options) => {
    requests.push([path, options]);
    if (!respond) throw new Error("navigation must not request");
    return respond(path, options);
  };
  const context = {
    Node, document, state, connection: owner, SETTINGS_GROUPS, setPath, parseJSONField, loadSettingsResources,
    AbortController, extensionReload: null, viewRequestVersion: 0,
    $: (selector, node = document) => node.querySelector(selector),
    $$: (selector, node = document) => node.querySelectorAll(selector),
    connectionCurrent: (candidate) => candidate === context.connection && !candidate.controller.signal.aborted,
    window: { matchMedia: () => ({ matches: reduced }) },
    request, apiRequest: request,
    toast: (...args) => notices.push(args), showLogin: () => logins.push(true), renderChrome() {},
    showError: (error) => { throw error; },
    saveSettings() {},
    releaseAttachmentCollection() {},
  };
  const control = (name, value) => { const node = new Node("input"); node.name = name; node.value = value; return node; };
  Object.assign(context, {
    icon: () => new Node("span"),
    actionButton: (text, action) => { const node = new Node("button", text); node.type = "button"; node.dataset.action = action; return node; },
    pretty: (value) => JSON.stringify(value, null, 2),
    field: (_label, name, value, options = {}) => { const node = control(name, value); node.tagName = options.type === "textarea" ? "textarea" : "input"; node.type = options.type ?? "text"; node.dataset.format = options.format; return node; },
    switchField: (_label, name, value) => { const node = control(name, ""); node.type = "checkbox"; node.checked = Boolean(value); return node; },
    renderConfigurationOverview: () => new Node("section", "配置概览"),
    renderModelSettings: () => control("models.main.model", state.settingsDraft.models.main.model),
    renderPersonaSettings: () => control("persona.name", state.settingsDraft.persona.name),
  });
  runInNewContext([
    section("function element(", "function icon("),
    section("function viewHeading(", "function emptyState("),
    section("function emptyState(", "async function switchView("),
    section("async function switchView(", "function renderOtherView("),
    section("function renderOtherView(", "function renderThemeControl("),
    section("function renderSettings()", "function renderConfigurationOverview("),
    section("function settingsSection(", "function renderChannelSettings("),
    section("function renderConnectionSettings()", "function renderSystemSettings()"),
    section("function collectSettingsForm()", "function displaySettingsError("),
    section("async function ownedRequest(", "function showLogin()"),
    section("async function handleAction(", 'document.addEventListener("click", async (event)'),
    section('document.addEventListener("click", async (event)', '\n$("#composer").addEventListener'),
    "globalThis.ui = { renderOtherView, switchView, switchSettingsTab, handleAction };",
  ].join("\n"), context, { timeout: 1000 });
  context.ui.renderOtherView();
  const click = async (tab) => {
    const button = root.querySelectorAll("[data-settings-tab]").find((node) => node.dataset.settingsTab === tab);
    button.focus(); await document.handlers.click({ target: button }); return button;
  };
  return { root, state, requests, notices, logins, owner, context, Node, ui: context.ui, click, active: () => active };
}

test("settings group clicks retain heading/nav identity, focus and independent collected drafts without requests", async () => {
  const h = harness(), shell = h.root.children[0], nav = h.root.querySelector(".settings-nav"), heading = h.root.querySelector(".view-heading");
  const models = nav.children.find((node) => node.dataset.settingsTab === "models");
  const oldForm = h.root.querySelector("#settings-form");
  h.root.querySelector("input").value = "edited-main";
  const persona = await h.click("persona");
  assert.equal(h.root.children[0] === shell, true); assert.equal(h.root.querySelector(".view-heading") === heading, true);
  assert.equal(h.root.querySelector(".settings-nav") === nav, true); assert.equal(h.active() === persona, true);
  assert.equal(oldForm.parent === null, true, "inactive forms, including secret inputs, are discarded rather than cached");
  assert.equal(h.state.settingsDraft.models.main.model, "edited-main");
  assert.equal(persona.getAttribute("aria-current"), "page"); assert.equal(models.getAttribute("aria-current"), "false");
  h.root.querySelector("input").value = "新的称呼";
  await h.click("models");
  assert.equal(h.root.querySelector("input").value, "edited-main");
  assert.equal(h.state.settingsDraft.persona.name, "新的称呼");
  assert.equal(h.root.replacements, 1); assert.equal(h.requests.length, 0);
});

test("same-tab and unknown-tab actions do not remount or animate the current panel", async () => {
  const h = harness(), panel = h.root.querySelector(".settings-content"), form = h.root.querySelector("#settings-form");
  await h.click("models"); h.ui.switchSettingsTab("not-a-group");
  assert.equal(h.root.querySelector("#settings-form") === form, true);
  assert.equal(panel.replacements, 0); assert.equal(panel.animations.length, 0); assert.equal(h.requests.length, 0);
});

test("reselecting the settings management page keeps the shell and performs no reload", async () => {
  const h = harness(), shell = h.root.children[0];
  h.root.querySelector("input").value = "unsaved";
  await h.ui.switchView("settings", h.owner);
  assert.equal(h.root.children[0] === shell, true); assert.equal(h.requests.length, 0);
  assert.equal(h.root.querySelector("input").value, "unsaved");
});

test("local panel motion is optional and respects reduced motion", async () => {
  const normal = harness(); await normal.click("persona");
  assert.equal(normal.root.querySelector(".settings-content").animations.length, 1);
  const reduced = harness({ reduced: true }); await reduced.click("persona");
  assert.equal(reduced.root.querySelector(".settings-content").animations.length, 0);
});

test("status and saved-draft refreshes retain the shell without replaying motion; clearing the draft resets it", () => {
  const h = harness(), shell = h.root.children[0], nav = h.root.querySelector(".settings-nav"), panel = h.root.querySelector(".settings-content");
  const oldForm = h.root.querySelector("#settings-form");
  h.state.settingsSaveStatus = "已保存";
  h.state.settingsDraft = { models: { main: { model: "saved-main" } }, persona: { name: "沐璃" } };
  h.ui.renderOtherView();
  assert.equal(h.root.children[0] === shell, true); assert.equal(h.root.querySelector(".settings-nav") === nav, true);
  assert.equal(h.root.querySelector("input").value, "saved-main"); assert.equal(oldForm.parent === null, true);
  assert.equal(panel.animations.length, 0); assert.equal(h.requests.length, 0);
  const draft = h.state.settingsDraft;
  h.state.settingsDraft = null; h.ui.renderOtherView();
  assert.equal(h.root.querySelector(".settings-nav"), null);
  h.state.settingsDraft = draft; h.ui.renderOtherView();
  assert.equal(h.root.children[0] === shell, false);
});

function deferred() {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

function reloadButton(h) {
  const buttons = h.root.querySelectorAll('[data-action="reload-extensions"]');
  assert.equal(buttons.length, 1, "connections expose one explicit MCP/Skills reload action");
  return buttons[0];
}

test("extension reload queues once and status refresh retains the live form, edits and focus", async () => {
  const post = deferred(), status = deferred();
  const h = harness({ respond: (path, options) => {
    if (path === "/extensions/reload") { assert.equal(options.method, "POST"); assert.deepEqual(JSON.parse(JSON.stringify(options.body)), {}); return post.promise; }
    if (path === "/status") return status.promise;
    return {};
  } });
  await h.click("connections");
  const button = reloadButton(h), shell = h.root.children[0], form = h.root.querySelector("#settings-form");
  const input = h.root.querySelector('[name="mcp"]');
  assert.match(h.root.textContent, /已加载/); assert.match(h.root.textContent, /2/);
  input.value = '{"mcpServers":{"unsaved":{"enabled":false}}}'; input.focus();
  const reload = h.ui.handleAction("reload-extensions", button);
  assert.equal(button.disabled, true);
  await h.ui.handleAction("reload-extensions", button);
  assert.equal(h.requests.length, 1);
  input.value = '{"mcpServers":{"newer":{"enabled":false}}}';
  post.resolve({ generation: 1, phase: "queued", skills: { enabled: true, count: 2 }, mcp: [] });
  await reload;
  assert.equal(button.disabled, false); assert.match(h.root.textContent, /排队/);
  assert.equal(h.root.children[0] === shell, true); assert.equal(h.root.querySelector("#settings-form") === form, true);
  assert.equal(h.root.querySelector('[name="mcp"]') === input, true); assert.equal(h.active() === input, true);
  assert.equal(h.notices.some(([text]) => /成功|已完成|已重载/.test(text)), false);
  const refresh = h.ui.handleAction("refresh-settings-status", h.root.querySelector('[data-action="refresh-settings-status"]'));
  input.value = '{"mcpServers":{"latest":{"enabled":false}}}'; input.focus();
  status.resolve({ extensions: { generation: 2, phase: "loaded", skills: { enabled: true, count: 3 }, mcp: [] } });
  await refresh;
  assert.equal(h.root.querySelector("#settings-form") === form, true); assert.equal(h.root.querySelector('[name="mcp"]') === input, true);
  assert.equal(input.value, '{"mcpServers":{"latest":{"enabled":false}}}'); assert.equal(h.active() === input, true);
  assert.match(h.root.textContent, /已加载/); assert.match(h.root.textContent, /3/);
  assert.equal(h.requests.filter(([, options]) => options?.method === "POST").length, 1);
  assert.equal(h.requests.some(([path]) => path === "/settings"), false);
});

for (const [phase, message] of [["failed", /失败.*保留|保留.*失败/], ["restart_required", /需要重启/]]) {
  test(`extension reload reports ${phase} without replacing the settings form`, async () => {
    const h = harness({ respond: () => ({ generation: 1, phase, error: { code: phase }, skills: { enabled: true, count: 2 }, mcp: [] }) });
    await h.click("connections");
    const form = h.root.querySelector("#settings-form"), input = h.root.querySelector('[name="mcp"]');
    input.value = '{"keep":"draft"}'; input.focus();
    await h.ui.handleAction("reload-extensions", reloadButton(h));
    assert.match(h.root.textContent, message);
    assert.equal(h.root.querySelector("#settings-form") === form, true); assert.equal(h.active() === input, true);
    assert.equal(input.value, '{"keep":"draft"}'); assert.equal(h.requests.length, 1);
  });
}

test("an unconfirmed reload is not repeated and keeps the last observed extension status", async () => {
  const h = harness({ respond: async () => { throw Object.assign(new Error("offline"), { status: 0 }); } });
  await h.click("connections");
  const button = reloadButton(h), observed = h.state.status.extensions;
  await h.ui.handleAction("reload-extensions", button);
  assert.equal(h.requests.length, 1); assert.equal(button.disabled, false);
  assert.equal(h.state.status.extensions === observed, true);
  assert.match(h.root.textContent, /未确认|无法确认/);
  assert.equal(h.notices.some(([text]) => /成功|已完成|已重载/.test(text)), false);
});

test("changing settings groups cannot submit a second reload while the first request is pending", async () => {
  const post = deferred();
  const h = harness({ respond: () => post.promise });
  await h.click("connections");
  const reload = h.ui.handleAction("reload-extensions", reloadButton(h));
  await h.click("persona"); await h.click("connections");
  const currentButton = reloadButton(h);
  assert.equal(currentButton.disabled, true);
  await h.ui.handleAction("reload-extensions", currentButton);
  assert.equal(h.requests.length, 1);
  post.resolve({ generation: 1, phase: "queued", skills: { enabled: true, count: 2 }, mcp: [] });
  await reload;
  assert.equal(currentButton.disabled, false); assert.match(h.root.textContent, /排队/);
});

test("returning to settings on the same connection unlocks the current controls after an old reload settles", async () => {
  const post = deferred();
  const h = harness({ respond: () => post.promise });
  await h.click("connections");
  h.root.querySelector('[name="mcp"]').value = '{"mcpServers":{"unsaved":{"enabled":false}}}';
  const reload = h.ui.handleAction("reload-extensions", reloadButton(h));
  h.context.collectSettingsForm();
  h.state.view = "chat"; h.context.viewRequestVersion += 1;
  h.root.replaceChildren(new h.Node("p", "another page"));
  const observed = { generation: 2, phase: "loaded", skills: { enabled: true, count: 3 }, mcp: [] };
  h.state.status = { extensions: observed };
  h.state.view = "settings"; h.context.viewRequestVersion += 1;
  h.ui.renderOtherView();
  const button = reloadButton(h), shell = h.root.children[0], form = h.root.querySelector("#settings-form");
  const input = h.root.querySelector('[name="mcp"]');
  assert.equal(JSON.parse(input.value).mcpServers.unsaved.enabled, false);
  assert.equal(button.disabled, true);
  await h.ui.handleAction("reload-extensions", button);
  assert.equal(h.requests.length, 1);
  input.value = '{"mcpServers":{"latest":{"enabled":false}}}'; input.focus();
  post.resolve({ generation: 1, phase: "queued", skills: { enabled: true, count: 2 }, mcp: [] });
  await reload;
  assert.equal(button.disabled, false);
  assert.equal(h.root.querySelector('[data-action="refresh-settings-status"]').disabled, false);
  assert.equal(h.root.children[0] === shell, true); assert.equal(h.root.querySelector("#settings-form") === form, true);
  assert.equal(h.root.querySelector('[name="mcp"]') === input, true); assert.equal(h.active() === input, true);
  assert.equal(input.value, '{"mcpServers":{"latest":{"enabled":false}}}');
  assert.equal(h.state.status.extensions === observed, true);
  assert.match(h.root.textContent, /已加载/); assert.doesNotMatch(h.root.textContent, /排队/);
  assert.equal(h.requests.length, 1); assert.deepEqual(h.notices, []);
});

test("an old settings refresh cannot publish its settled resource batch after leaving and returning", async () => {
  const service = deferred();
  const h = harness({ respond: (path) => {
    if (path === "/status") return { extensions: { generation: 1, phase: "queued", skills: { enabled: true, count: 2 }, mcp: [] } };
    if (path === "/service") return service.promise;
    if (path === "/management") throw new Error("old management unavailable");
    return {};
  } });
  await h.click("connections");
  h.root.querySelector('[name="mcp"]').value = '{"mcpServers":{"unsaved":{"enabled":false}}}';
  const refresh = h.ui.handleAction("refresh-settings-status", h.root.querySelector('[data-action="refresh-settings-status"]'));
  assert.deepEqual(h.requests.map(([path]) => path), ["/status", "/service", "/management", "/channels", "/voice", "/capture"]);
  h.context.collectSettingsForm();
  h.state.view = "chat"; h.context.viewRequestVersion += 1;
  h.root.replaceChildren(new h.Node("p", "another page"));
  const observed = { extensions: { generation: 2, phase: "loaded", skills: { enabled: true, count: 3 }, mcp: [] } };
  const errors = { capture: "current capture unavailable" }, currentService = { active: true };
  h.state.status = observed; h.state.settingsStatusErrors = errors; h.state.service = currentService;
  h.state.view = "settings"; h.context.viewRequestVersion += 1;
  h.ui.renderOtherView();
  const shell = h.root.children[0], form = h.root.querySelector("#settings-form"), input = h.root.querySelector('[name="mcp"]');
  assert.equal(JSON.parse(input.value).mcpServers.unsaved.enabled, false);
  input.value = '{"mcpServers":{"latest":{"enabled":false}}}'; input.focus();
  service.resolve({ active: false });
  await refresh;
  assert.equal(h.state.status === observed, true, "the old batch must not overwrite the current loaded status");
  assert.equal(h.state.settingsStatusErrors === errors, true); assert.equal(h.state.service === currentService, true);
  assert.equal(h.root.children[0] === shell, true); assert.equal(h.root.querySelector("#settings-form") === form, true);
  assert.equal(h.root.querySelector('[name="mcp"]') === input, true); assert.equal(h.active() === input, true);
  assert.equal(input.value, '{"mcpServers":{"latest":{"enabled":false}}}');
  assert.match(h.root.textContent, /已加载/); assert.doesNotMatch(h.root.textContent, /排队/);
  assert.equal(reloadButton(h).disabled, false);
  assert.equal(h.root.querySelector('[data-action="refresh-settings-status"]').disabled, false);
  assert.equal(h.requests.length, 6); assert.deepEqual(h.notices, []);
});

test("leaving settings and replacing the connection ignores a late reload error without another request", async () => {
  const post = deferred();
  const h = harness({ respond: () => post.promise });
  await h.click("connections");
  const reload = h.ui.handleAction("reload-extensions", reloadButton(h));
  const observed = h.state.status;
  h.state.view = "chat"; h.context.viewRequestVersion += 1;
  h.context.connection = { ready: true, controller: new AbortController() };
  const otherPage = new h.Node("p", "another page"); h.root.replaceChildren(otherPage); otherPage.focus();
  post.reject(Object.assign(new Error("old login expired"), { status: 401 }));
  await reload;
  assert.equal(h.root.children[0] === otherPage, true); assert.equal(h.active() === otherPage, true);
  assert.equal(h.state.status === observed, true); assert.equal(h.logins.length, 0);
  assert.equal(h.requests.length, 1); assert.deepEqual(h.notices, []);
});
