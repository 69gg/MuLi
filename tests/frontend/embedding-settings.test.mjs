import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";

const source = readFileSync(new URL("../../web/app.mjs", import.meta.url), "utf8");
const render = source.slice(source.indexOf("function renderMediaSettings("), source.indexOf("\nfunction renderToolSettings("));

function harness(embedding = {}) {
  const controls = new Map();
  const element = (tag, props = {}, ...children) => ({ tag, props, children: children.filter(Boolean), append(...items) { this.children.push(...items.filter(Boolean)); } });
  const field = (label, name, value, options = {}) => {
    const control = { label, name, value, options };
    controls.set(name, control);
    return control;
  };
  const context = {
    state: { settingsDraft: { media: { embedding } } },
    element, field, pretty: (value) => JSON.stringify(value, null, 2),
    switchField: field, settingsSection: () => null, settingsJSON: () => null,
    $: (selector, parent) => {
      assert.equal(selector, "details.advanced-settings");
      return parent.children.find((node) => node.tag === "details" && node.props.class === "advanced-settings");
    },
  };
  runInNewContext(render, context, { timeout: 1000 });
  context.renderMediaSettings();
  return controls;
}

test("embedding purposes expose separate editable prefixes and query JSON", () => {
  const options = { document_instruction: "passage: ", query_instruction: "query: ", extra_body: { task: "document" }, query_extra_body: { task: "query" } };
  const controls = harness(options);
  assert.equal(controls.get("media.embedding.document_instruction").value, "passage: ");
  assert.equal(controls.get("media.embedding.query_instruction").value, "query: ");
  assert.equal(controls.get("media.embedding.document_instruction").options.format, "raw");
  assert.equal(controls.get("media.embedding.query_instruction").options.format, "raw");
  const query = controls.get("media.embedding.query_extra_body");
  assert.equal(query.options.format, "json");
  assert.equal(query.options.json, true);
  assert.deepEqual(JSON.parse(query.value), { task: "query" });
  assert.deepEqual(JSON.parse(controls.get("media.embedding.extra_body").value), { task: "document" });
  for (const role of ["reranker", "asr", "tts"]) {
    assert.equal(controls.has(`media.${role}.query_extra_body`), false);
    assert.equal(controls.has(`media.${role}.document_instruction`), false);
  }
});

test("unconfigured embedding leaves prefixes empty and query options optional", () => {
  const controls = harness();
  assert.equal(controls.get("media.embedding.document_instruction").value, "");
  assert.equal(controls.get("media.embedding.query_instruction").value, "");
  assert.deepEqual(JSON.parse(controls.get("media.embedding.query_extra_body").value), {});
});
