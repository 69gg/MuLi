import test from "node:test";
import assert from "node:assert/strict";
import { COMPOSER_DRAFT_KEY, composerIdentity, createComposerDraft } from "../../web/composer-draft.mjs";

const identity = { store: "a".repeat(32), main: "main" };
const file = { id: "1".repeat(32), name: "已保存资料.pdf" };
function fixture() {
  const values = new Map(), faults = { read: false, write: false, remove: false };
  const storage = {
    getItem(key) { if (faults.read) throw new Error("blocked"); return values.get(key) ?? null; },
    setItem(key, value) { if (faults.write) throw new Error("quota"); values.set(key, value); },
    removeItem(key) { if (faults.remove) throw new Error("blocked"); values.delete(key); },
  };
  return { values, faults, draft: createComposerDraft({ storage: () => storage, maxTextChars: 100 }) };
}

test("one bounded snapshot keeps exact text and ordered IDs without copying attachment capabilities", () => {
  const h = fixture(), text = "原文字🙂\n准确引用 {\"id\":\"task\",\"revision\":7}  ";
  const files = [{ ...file, url: "https://untrusted.invalid/", mime_type: "text/html", blob: "never saved" }, { id: "2".repeat(32), name: "第二份.txt" }];
  assert.equal(h.draft.save(identity, text, files), true);
  const raw = JSON.parse(h.values.get(COMPOSER_DRAFT_KEY));
  assert.deepEqual(Object.keys(raw).sort(), ["attachments", "main", "schema", "store", "text"]);
  assert.deepEqual(raw.attachments, files.map(({ id, name }) => ({ id, name })));
  assert.deepEqual(h.draft.read(identity).record, { text, attachments: raw.attachments });
  assert.equal(h.draft.consume(), true); assert.equal(h.draft.read(identity).record, null);
});

test("a failed snapshot consumption leaves the exact old record and reports failure", () => {
  const h = fixture(); h.draft.save(identity, "不能重放的原提交", [file]);
  const before = h.values.get(COMPOSER_DRAFT_KEY); h.faults.remove = true;
  assert.equal(h.draft.consume(), false); assert.equal(h.values.get(COMPOSER_DRAFT_KEY), before);
  h.faults.remove = false; assert.equal(h.draft.consume(), true); assert.equal(h.values.has(COMPOSER_DRAFT_KEY), false);
});

test("storage errors stay bounded and do not escape into login or editing", () => {
  const h = fixture(); h.faults.write = true;
  assert.equal(h.draft.save(identity, "仍在内存编辑", []), false);
  h.faults.read = true;
  assert.equal(h.draft.read(identity).unavailable, true); assert.equal(h.draft.consume(), false);
});

for (const bad of ["{", "x".repeat(100000), JSON.stringify({ schema: 2 }), JSON.stringify({ schema: 1, ...identity, text: "x".repeat(101), attachments: [] }), JSON.stringify({ schema: 1, ...identity, text: "原文", attachments: [file, file] }), JSON.stringify({ schema: 1, ...identity, text: "原文", attachments: [{ ...file, id: "not-an-id" }] }), JSON.stringify({ schema: 1, ...identity, text: "原文", attachments: [{ ...file, name: "中".repeat(81) }] })]) {
  test(`invalid stored record is rejected as one whole snapshot (${bad.length} chars)`, () => {
    const h = fixture(); h.values.set(COMPOSER_DRAFT_KEY, bad); h.values.set("muli.task-control:keep", "unrelated");
    assert.equal(h.draft.read(identity).record, null); assert.equal(h.values.has(COMPOSER_DRAFT_KEY), false);
    assert.equal(h.values.get("muli.task-control:keep"), "unrelated");
  });
}

test("wrong store or main never restores and invalid identity cannot enable persistence", () => {
  for (const other of [{ ...identity, store: "b".repeat(32) }, { ...identity, main: "another-main" }]) {
    const h = fixture(); h.draft.save(identity, "只属于原身份", [file]); assert.equal(h.draft.read(other).record, null);
  }
  assert.equal(composerIdentity(undefined, "main"), null); assert.equal(composerIdentity("bad-store", "main"), null);
  assert.equal(composerIdentity(identity.store, ""), null);
});
