import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";

const source = await readFile(new URL("../../web/app.mjs", import.meta.url), "utf8");
const render = source.slice(source.indexOf("function renderModelSettings("), source.indexOf("function renderPersonaSettings("));

test("Anthropic switches follow the selected protocol and preserve saved off values", () => {
  const controls = new Map();
  const element = (tag, props = {}, ...children) => ({
    tag, ...props, children,
    append(...items) { this.children.push(...items); },
  });
  const control = (tag, name, value) => {
    const node = { value, handlers: {}, addEventListener(type, listener) { this.handlers[type] = listener; } };
    controls.set(name, node);
    return element("label", {}, element(tag, node));
  };
  const context = {
    state: { modelRole: "main", settingsDraft: { models: { main: { anthropic_prompt_cache: false } } } },
    MODEL_ROLES: [["main", "主模型"]],
    element,
    field: (_label, name, value) => control("input", name, value),
    selectField: (_label, name, value) => control("select", name, value),
    switchField: (_label, name, checked) => control("input", name, checked),
    settingsSection: (_title, ...children) => element("section", {}, ...children),
    pretty: JSON.stringify,
    $: (tag, parent) => parent.children.find((node) => node.tag === tag),
  };
  runInNewContext(render, context, { timeout: 1000 });
  const content = context.renderModelSettings();
  const section = content.children.find((node) => node?.tag === "section");
  const options = section.children.find((node) => node.hidden === true);
  assert.equal(options.hidden, true);
  assert.equal(controls.get("models.main.anthropic_prompt_cache").value, false);
  assert.equal(controls.get("models.main.anthropic_adaptive_thinking").value, true);

  const protocol = section.children[0].children[0];
  protocol.value = "anthropic";
  protocol.handlers.change();
  assert.equal(options.hidden, false);
  protocol.value = "openai_responses";
  protocol.handlers.change();
  assert.equal(options.hidden, true);
});
