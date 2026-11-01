import test from "node:test";
import assert from "node:assert/strict";
import { applyComposerPrompt, REPLACE_CONFIRMATION } from "../../web/composer-prompt.mjs";

const base = { session: "main", mainSession: "main", revision: 3, expectedRevision: 3, maxChars: 100 };

test("append keeps the existing draft and returns the previous text", () => {
  const result = applyComposerPrompt("参考任务", { ...base, text: "原草稿" });
  assert.deepEqual(result, { ok: true, mode: "append", text: "原草稿\n\n参考任务", previousText: "原草稿" });
  assert.equal(applyComposerPrompt("参考任务", { ...base, text: "" }).text, "参考任务");
});

test("an already included prompt is not appended twice", () => {
  const result = applyComposerPrompt("参考任务", { ...base, text: "先看参考任务再回答" });
  assert.equal(result.ok, true);
  assert.equal(result.unchanged, true);
  assert.equal(result.text, "先看参考任务再回答");
});

test("replace requires confirmation only when text would be lost", () => {
  const blocked = applyComposerPrompt("新要求", { ...base, mode: "replace", text: "原草稿" });
  assert.equal(blocked.ok, false);
  assert.equal(blocked.reason, "confirmation_required");
  assert.match(REPLACE_CONFIRMATION, /附件与语音保留/);
  const replaced = applyComposerPrompt("新要求", { ...base, mode: "replace", text: "原草稿", confirmed: true });
  assert.equal(replaced.ok, true);
  assert.equal(replaced.text, "新要求");
  assert.equal(replaced.previousText, "原草稿");
  assert.equal(applyComposerPrompt("新要求", { ...base, mode: "replace", text: "" }).ok, true);
});

test("draft identity, revision and length are checked before any text is composed", () => {
  assert.equal(applyComposerPrompt("x", { ...base, session: "other" }).reason, "not_main");
  assert.equal(applyComposerPrompt("x", { ...base, mainSession: "" }).reason, "not_main");
  assert.equal(applyComposerPrompt("x", { ...base, expectedSession: "older" }).reason, "stale");
  assert.equal(applyComposerPrompt("x", { ...base, revision: 4 }).reason, "stale");
  assert.equal(applyComposerPrompt("x", { ...base, text: "programmatic", expectedText: "原草稿" }).reason, "stale");
  assert.equal(applyComposerPrompt("x", { ...base, text: "a".repeat(100) }).reason, "too_long");
  assert.equal(applyComposerPrompt("", base).reason, "empty");
  assert.equal(applyComposerPrompt("x", { ...base, mode: "prepend" }).reason, "mode");
});
