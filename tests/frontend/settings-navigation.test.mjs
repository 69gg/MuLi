import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";
import { SETTINGS_GROUPS, setPath } from "../../web/settings.mjs";
import { parseJSONField } from "../../web/state.mjs";

const source = await readFile(new URL("../../web/app.mjs", import.meta.url), "utf8");
function section(start, end) { return source.slice(source.indexOf(start), source.indexOf(end)); }

// Only the settings shell/panel renderers and real navigation handler are
// evaluated. Other settings panels are small controls, not network substitutes.
function harness({ reduced = false } = {}) {
  let active = null;
  class Node {
    constructor(tag = "text", text = "") {
      this.tagName = tag; this.text = text; this.children = []; this.parent = null;
      this.className = ""; this.dataset = {}; this.attributes = {}; this.handlers = {};
      this.id = ""; this.value = ""; this.name = ""; this.type = ""; this.hidden = false;
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
    matches(selector) {
      if (selector === "[name]") return Boolean(this.name);
      if (selector === "[data-settings-tab]") return Boolean(this.dataset.settingsTab);
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
  const root = new Node("div"); root.id = "other-view"; document.append(root);
  const state = { view: "settings", settingsTab: "models", settingsDraft: { models: { main: { model: "original" } }, persona: { name: "沐璃" } }, settingsSaveStatus: "", viewLoading: false, viewError: "" };
  const requests = [];
  const owner = { ready: true };
  const context = {
    Node, document, state, connection: owner, SETTINGS_GROUPS, setPath, parseJSONField,
    $: (selector, node = document) => node.querySelector(selector),
    $$: (selector, node = document) => node.querySelectorAll(selector),
    connectionCurrent: (candidate) => candidate === owner,
    window: { matchMedia: () => ({ matches: reduced }) },
    request: (...args) => { requests.push(args); throw new Error("navigation must not request"); },
    showError: (error) => { throw error; },
    saveSettings() {},
  };
  const control = (name, value) => { const node = new Node("input"); node.name = name; node.value = value; return node; };
  Object.assign(context, {
    icon: () => new Node("span"),
    actionButton: (text) => { const node = new Node("button", text); return node; },
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
    section("function collectSettingsForm()", "function displaySettingsError("),
    section('document.addEventListener("click", async (event)', '\n$("#composer").addEventListener'),
    "globalThis.ui = { renderOtherView, switchView, switchSettingsTab };",
  ].join("\n"), context, { timeout: 1000 });
  context.ui.renderOtherView();
  const click = async (tab) => {
    const button = root.querySelectorAll("[data-settings-tab]").find((node) => node.dataset.settingsTab === tab);
    button.focus(); await document.handlers.click({ target: button }); return button;
  };
  return { root, state, requests, owner, ui: context.ui, click, active: () => active };
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
