import test from "node:test";
import assert from "node:assert/strict";
import { createPreviewBackend } from "../../web/preview.mjs";
import { clickMemoryButton, deferred, flush, harness } from "./helpers/app-dom-harness.mjs";

const sourceId = "3".repeat(32), revision = "a".repeat(64);
const quote = "先讲直观例子🙂，再推公式。";
const first = "这是之前保存的数学题讲解约定。\n", last = `${quote}\n请保留原来的表述。`;
const split = Buffer.byteLength(first), total = split + Buffer.byteLength(last);
const source = {
  source_id: sourceId, source: "web", session_id: "main", role: "user", principal: "owner", trusted_owner: true,
  revision, created_at: 149, total_bytes: total, job: { state: "done", attempts: 1, last_error: "" },
};
const currentPreference = {
  id: "7".repeat(32), content: quote, quote, kind: "preference", layer: "preference", source_id: sourceId,
  trusted_owner: true, version: 1, status: "active",
};

test("an owner source read through the existing catalog can explicitly become a preference without changing the working draft", async (t) => {
  let recorded = null;
  const sourceReads = [], confirmations = [];
  const h = harness(async (path, options = {}) => {
    if (path === "/memories" || path.startsWith("/memories?")) return { items: [] };
    if (path === "/memories/graph") return { nodes: [], edges: [] };
    if (path.startsWith("/memory/sources?")) return { items: [source], examined: 1, omitted: 0, has_more: false, next_cursor: null, consistency: "live", order: "newest_registered_first" };
    if (path.startsWith("/memory/source?")) {
      assert.equal(options.method ?? "GET", "GET"); assert.equal(options.cache, "no-store");
      const params = new URLSearchParams(path.split("?")[1]), offset = Number(params.get("offset_bytes"));
      assert.equal(params.get("id"), sourceId); assert.equal(params.get("revision"), revision);
      assert.ok(offset === 0 || offset === split, "the original source is read at its exact UTF-8 byte boundaries");
      sourceReads.push(offset);
      const content = offset === 0 ? first : last, size = Buffer.byteLength(content);
      return { ...source, id: sourceId, digest: revision, field: "content", content, offset_bytes: offset, length_bytes: size, next_offset_bytes: offset + size, has_more: offset + size < total };
    }
    if (path === "/memory/preferences/from-source") {
      assert.equal(options.method, "POST");
      const body = typeof options.body === "string" ? JSON.parse(options.body) : options.body;
      assert.deepEqual(Object.keys(body).sort(), ["action", "quote", "revision", "source_id"]);
      assert.equal(body.source_id, sourceId); assert.equal(body.revision, revision);
      assert.ok(["inspect", "confirm"].includes(body.action)); assert.ok((first + last).includes(body.quote) && body.quote.trim());
      if (body.action === "confirm") {
        assert.equal(body.quote, quote, "the explicit confirmation keeps the chosen original wording");
        confirmations.push(body); recorded = structuredClone(currentPreference);
      }
      return { state: recorded ? "present" : "absent", source_id: sourceId, revision, quote: body.quote, item: recorded };
    }
    if (path === `/memories/${currentPreference.id}`) { assert.ok(recorded); return recorded; }
    throw new Error(`unexpected request: ${path}`);
  }, { composer: true });
  t.after(() => h.windowEvents.pagehide());
  h.ui.state.connected = true; h.ui.state.view = "chat";
  h.ui.state.status = { configured: true, store_instance_id: "f".repeat(32), web_voice: { available: false } };
  h.ui.state.mainSessionId = "main"; h.ui.state.activeSession = { id: "main", role: "main", mode: "auto" };
  const input = h.document.querySelector("#message-input"); input.value = "正在准备另一项工作，别改这段草稿。"; await input.dispatch("input");
  const selected = [{ id: "8".repeat(32), name: "先选的说明.pdf", mime_type: "application/pdf", size: 8 }, { id: "9".repeat(32), name: "后选的要求.txt", mime_type: "text/plain", size: 9 }];
  h.ui.state.attachments = selected;
  const voiceDraft = { text: "尚待主人决定追加的转写" }; h.ui.state.voiceDraft = voiceDraft; h.document.querySelector("#voice-draft-text").value = voiceDraft.text;
  h.ui.renderComposerAttachments(); const draft = h.ui.captureDraft();

  h.ui.useMemoryViews(); await h.ui.switchView("memories");
  assert.equal(h.ui.state.viewError, "", "the existing memory list must load before opening its source tab");
  await clickMemoryButton(h, h.other.querySelector('[data-memory-tab="sources"]'));
  assert.ok(h.other.textContent.includes(sourceId));
  await clickMemoryButton(h, h.button("核原文", h.other));
  assert.equal(h.dialog.open, true); assert.ok(h.body().includes(first)); assert.ok(h.body().includes("主人来源"));
  await clickMemoryButton(h, h.button("下一段"));
  assert.ok(h.body().includes(last)); assert.deepEqual(sourceReads, [0, split]);
  assert.equal(h.requests.some((request) => request.method === "POST"), false, "catalog navigation and original paging remain read-only");

  const remember = h.button("记作偏好");
  assert.ok(remember, "the successfully verified owner source needs the actual 记作偏好 action");
  await clickMemoryButton(h, remember);
  const originalQuote = h.form.querySelector('textarea[name="quote"]'); assert.ok(originalQuote, "the confirmation must let the owner choose original wording");
  originalQuote.value = quote; await originalQuote.dispatch("input"); await flush();
  assert.equal(confirmations.length, 0, "opening and editing a preference does not confirm it");
  const confirm = h.document.querySelector("#dialog-submit"); assert.equal(confirm.textContent, "确认记住"); assert.equal(confirm.hidden, false); assert.equal(confirm.disabled, false);
  await h.form.dispatch("submit"); await flush();
  assert.equal(confirmations.length, 1); assert.ok(h.body().includes(quote)); assert.ok(h.body().includes(currentPreference.id));
  assert.equal(h.body().includes("本次新建成功"), false, "a current fact is not proof this request created it");
  await clickMemoryButton(h, h.button("核原文"));
  assert.ok(h.body().includes(first)); assert.deepEqual(sourceReads, [0, split, 0]); assert.equal(confirmations.length, 1);

  assert.equal(input.value, draft.text); assert.equal(h.ui.captureDraft().revision, draft.revision);
  assert.equal(h.ui.state.attachments === selected, true); assert.deepEqual(Array.from(h.ui.state.attachments, (file) => file.id), selected.map((file) => file.id));
  assert.equal(h.ui.state.voiceDraft === voiceDraft, true); assert.equal(h.document.querySelector("#voice-draft-text").value, voiceDraft.text);
  assert.equal(h.ui.state.activeSession.id, "main"); assert.equal(h.ui.state.session.messages.length, 0);
  assert.equal(h.requests.some((request) => request.path.includes("/messages") || request.path === "/uploads" || request.path === "/media/transcribe"), false);
});

test("a verified owner source page above the API quote limit stays editable without posting until shortened", async (t) => {
  const quoteLimit = 12000, maxOutputChars = 16002;
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  backend.state.settings.limits.max_output_chars = maxOutputChars;
  const main = backend.state.sessions[0];
  const owner = backend.state.memorySources.find((item) => item.trusted_owner && item.principal === "owner");
  const prefix = "前".repeat(maxOutputChars / Buffer.byteLength("前"));
  const oversized = `${quote}${"x".repeat(quoteLimit + 1 - Array.from(quote).length)}`;
  owner.content = prefix + oversized; owner.total_bytes = Buffer.byteLength(owner.content);
  assert.equal(Buffer.byteLength(prefix), maxOutputChars);
  assert.equal(Array.from(oversized).length, quoteLimit + 1);
  const pages = [];
  const h = harness(async (path, options = {}) => {
    const response = await backend.fetch(`/api${path}`, { ...options, ...(options.body ? { body: JSON.stringify(options.body) } : {}) });
    const value = await response.json(); if (!response.ok) throw Object.assign(new Error(JSON.stringify(value.detail)), { status: response.status });
    if (path.startsWith("/memory/source?")) {
      assert.equal(options.method ?? "GET", "GET"); assert.equal(options.cache, "no-store");
      const params = new URLSearchParams(path.split("?")[1]);
      assert.equal(params.get("id"), owner.source_id); assert.equal(params.get("revision"), owner.revision);
      assert.ok(Array.from(JSON.stringify(value)).length <= maxOutputChars, "the complete original page fits the configured output budget");
      pages.push(value);
    }
    return value;
  }, { composer: true });
  t.after(() => h.windowEvents.pagehide());
  h.ui.state.status = await (await backend.fetch("/api/status")).json(); h.ui.state.connected = true;
  h.ui.state.mainSessionId = main.id; h.ui.state.activeSession = main;
  h.ui.useMemoryViews(); h.other.replaceChildren(h.ui.renderMemories());
  await clickMemoryButton(h, h.other.querySelector('[data-memory-tab="sources"]'));
  const row = h.other.querySelectorAll(".memory-row").find((item) => item.textContent.includes(owner.source_id));
  assert.ok(row, "the owner original must be reached through the actual source catalog");
  await clickMemoryButton(h, h.button("核原文", row));
  assert.equal(h.dialog.open, true); assert.equal(pages.length, 1); assert.equal(pages[0].content, prefix); assert.equal(pages[0].has_more, true);
  await clickMemoryButton(h, h.button("下一段"));
  assert.equal(pages.length, 2); assert.equal(pages[1].content, oversized); assert.equal(pages[1].has_more, false);
  assert.deepEqual(pages.map((page) => page.offset_bytes), [0, Buffer.byteLength(prefix)]);
  for (const page of pages) {
    assert.equal(page.id, owner.source_id); assert.equal(page.source_id, owner.source_id);
    assert.equal(page.revision, owner.revision); assert.equal(page.digest, owner.revision);
    assert.equal(page.session_id, main.id); assert.equal(page.role, "user"); assert.equal(page.principal, "owner"); assert.equal(page.trusted_owner, true);
    assert.equal(page.length_bytes, Buffer.byteLength(page.content)); assert.equal(page.next_offset_bytes, page.offset_bytes + page.length_bytes);
    assert.equal(page.total_bytes, owner.total_bytes);
  }
  assert.equal(h.requests.some((request) => request.method === "POST"), false);
  const remember = h.button("记作偏好"); assert.ok(remember, "the verified current-main owner original must expose preference confirmation");
  assert.equal(remember.disabled, false); await clickMemoryButton(h, remember);
  const input = h.form.querySelector('textarea[name="quote"]'); assert.ok(input); assert.equal(input.value, oversized);
  const confirm = h.document.querySelector("#dialog-submit"); assert.equal(confirm.hidden, false); assert.equal(confirm.textContent, "确认记住");
  const initiallyDisabled = confirm.disabled;
  await input.dispatch("input"); const editedDisabled = confirm.disabled;
  await h.form.dispatch("submit"); await flush();
  assert.deepEqual({ initiallyDisabled, editedDisabled, posts: h.requests.filter((request) => request.method === "POST").length },
    { initiallyDisabled: true, editedDisabled: true, posts: 0 }, "an oversized original quote must be blocked before a request can freeze its tuple");
  assert.equal(h.body().includes("结果尚未确认"), false);
  const shorter = h.form.querySelector('textarea[name="quote"]'); assert.ok(shorter, "rejecting an oversized quote keeps the original selection editable");
  const unicodeBoundary = Array.from(oversized).slice(0, quoteLimit).join("");
  assert.equal(Array.from(unicodeBoundary).length, quoteLimit); assert.ok(unicodeBoundary.length > quoteLimit);
  assert.ok(shorter.maxLength >= unicodeBoundary.length, "the HTML UTF-16 limit must allow a valid quote containing a non-BMP character");
  shorter.value = unicodeBoundary; await shorter.dispatch("input"); await flush();
  assert.equal(confirm.disabled, false, "a quote at the API character boundary remains selectable with its original non-BMP character");
  assert.equal(h.requests.some((request) => request.method === "POST"), false);
  shorter.value = quote; await shorter.dispatch("input"); await flush();
  assert.equal(confirm.disabled, false); await h.form.dispatch("submit"); await flush();
  const confirmations = h.requests.filter((request) => request.path === "/memory/preferences/from-source");
  assert.equal(confirmations.length, 1);
  assert.deepEqual(structuredClone(confirmations[0].body), { action: "confirm", source_id: owner.source_id, revision: owner.revision, quote });
  assert.ok(h.body().includes("对应条目当前存在"));
  assert.equal(backend.state.memories.filter((item) => item.source_id === owner.source_id).length, 1);
  assert.equal(backend.state.messages[main.id].length, 0);
});

function receipt(body, item = currentPreference) {
  return { state: item ? "present" : "absent", source_id: body.source_id, revision: body.revision, quote: body.quote, item: item && structuredClone(item) };
}

function ownerSourceHarness(t, respond, { catalog = source, original = {} } = {}) {
  const calls = [];
  const h = harness(async (path, options = {}) => {
    if (path.startsWith("/memory/sources?")) return { items: [catalog], examined: 1, omitted: 0, has_more: false, next_cursor: null, consistency: "live", order: "newest_registered_first" };
    if (path.startsWith("/memory/source?")) return { ...catalog, id: catalog.source_id, digest: catalog.revision, field: "content", content: first + last, offset_bytes: 0, length_bytes: total, next_offset_bytes: total, has_more: false, ...original };
    assert.equal(path, "/memory/preferences/from-source"); assert.equal(options.method, "POST"); assert.equal(options.cache, "no-store");
    const body = options.body; assert.deepEqual(Object.keys(body).sort(), ["action", "quote", "revision", "source_id"]);
    calls.push(structuredClone(body)); return respond(body, options, calls.length);
  }, { composer: true });
  t.after(() => h.windowEvents.pagehide());
  h.ui.state.connected = true; h.ui.state.status = { configured: true, store_instance_id: "f".repeat(32) };
  h.ui.state.mainSessionId = "main"; h.ui.state.activeSession = { id: "main", role: "main", mode: "auto" };
  h.ui.useMemoryViews(); h.other.replaceChildren(h.ui.renderMemories());
  return { ...h, calls };
}

async function openPreference(h, { catalog = true } = {}) {
  if (catalog) await clickMemoryButton(h, h.other.querySelector('[data-memory-tab="sources"]'));
  await clickMemoryButton(h, h.button("核原文", h.other));
  assert.ok(h.body().includes(first + last), "the existing original reader must have completed");
  await clickMemoryButton(h, h.button("记作偏好"));
  const input = h.form.querySelector('textarea[name="quote"]');
  if (input) { input.value = quote; await input.dispatch("input"); }
  return input;
}

test("an interrupted confirmation reopens with the frozen original tuple and only inspects the actual corrected record", async (t) => {
  const pending = deferred();
  const corrected = { ...currentPreference, content: "以后先给结论。", quote: "先讲直观例子🙂，再推公式。", kind: "fact", layer: "semantic", status: "superseded", version: 4 };
  const h = ownerSourceHarness(t, (body) => body.action === "confirm" ? pending.promise : receipt(body, corrected));
  const input = await openPreference(h);
  const submitting = h.form.dispatch("submit"); await flush();
  await h.form.dispatch("submit"); assert.equal(h.calls.length, 1);
  assert.equal(h.document.querySelector("#dialog-submit").hidden, true);
  h.dialog.close(); await flush();
  await openPreference(h, { catalog: false });
  assert.equal(h.form.querySelector('textarea[name="quote"]'), null);
  assert.ok(h.body().includes(quote)); assert.ok(h.button("核对结果"));
  input.value = "旧输入不能改写新的确认内容"; await input.dispatch("input");
  pending.reject(Object.assign(new Error("旧请求认证已过期"), { status: 401 })); await submitting; await flush();
  assert.equal(h.ui.state.auth.authenticated, true); assert.equal(h.dialog.open, true);
  assert.equal(h.calls.length, 1, "closing or reopening does not replay the write");
  await clickMemoryButton(h, h.button("核对结果"));
  assert.deepEqual(h.calls, [{ action: "confirm", source_id: sourceId, revision, quote }, { action: "inspect", source_id: sourceId, revision, quote }]);
  for (const value of [corrected.content, "事实", "语义事实", "已替代", "4", corrected.id]) assert.ok(h.body().includes(value), value);
  assert.equal(h.document.querySelector("#dialog-submit").hidden, true); assert.equal(h.calls.filter((call) => call.action === "confirm").length, 1);
});

test("a late confirmation cannot replace a new connection's source dialog or log it out", async (t) => {
  const pending = deferred();
  const h = ownerSourceHarness(t, (body) => body.action === "confirm" ? pending.promise : receipt(body));
  await openPreference(h); const submitting = h.form.dispatch("submit"); await flush();
  h.ui.activateConnection(); h.ui.state.connected = true; h.ui.state.auth.authenticated = true;
  await openPreference(h, { catalog: false });
  const before = h.body(); assert.ok(before.includes("结果尚未确认"));
  pending.reject(Object.assign(new Error("旧连接401"), { status: 401 })); await submitting; await flush();
  assert.equal(h.ui.state.auth.authenticated, true); assert.equal(h.dialog.open, true); assert.equal(h.body(), before);
  assert.equal(h.calls.length, 1); await clickMemoryButton(h, h.button("核对结果"));
  assert.equal(h.calls[1].action, "inspect"); assert.ok(h.body().includes(currentPreference.id));
});

test("a late success cannot replace a different source dialog", async (t) => {
  const pending = deferred();
  const catalog = { ...source };
  const h = ownerSourceHarness(t, () => pending.promise, { catalog });
  await openPreference(h); const submitting = h.form.dispatch("submit"); await flush();
  h.dialog.close(); await flush();
  catalog.source_id = "6".repeat(32); catalog.revision = "c".repeat(64);
  await clickMemoryButton(h, h.button("刷新来源", h.other)); await openPreference(h, { catalog: false });
  const replacement = h.document.querySelector("#dialog-content").firstElementChild, before = h.body();
  assert.ok(before.includes(catalog.source_id)); assert.equal(h.document.querySelector("#dialog-submit").hidden, false);
  pending.resolve(receipt(h.calls[0])); await submitting; await flush();
  assert.equal(h.dialog.open, true); assert.equal(h.document.querySelector("#dialog-content").firstElementChild === replacement, true);
  assert.equal(h.body(), before); assert.equal(h.calls.length, 1);
});

test("an incomplete or mismatched receipt stays unknown and a failed inspection never becomes absence", async (t) => {
  const h = ownerSourceHarness(t, (body, _options, count) => {
    if (count === 1) { const incomplete = receipt(body); delete incomplete.item.status; return incomplete; }
    if (count === 2) throw new Error("暂时离线");
    if (count === 3) return { ...receipt(body), quote: `${quote}另一句话` };
    return receipt(body, null);
  });
  await openPreference(h); await h.form.dispatch("submit"); await flush();
  assert.ok(h.body().includes("结果尚未确认")); assert.equal(h.body().includes("当前存在"), false);
  await clickMemoryButton(h, h.button("核对结果"));
  assert.ok(h.body().includes("暂时离线")); assert.equal(h.body().includes("没有找到"), false);
  await clickMemoryButton(h, h.button("核对结果"));
  assert.ok(h.body().includes("结果尚未确认")); assert.equal(h.body().includes("当前存在"), false);
  await clickMemoryButton(h, h.button("核对结果"));
  assert.ok(h.body().includes("当前没有找到对应条目")); assert.equal(h.document.querySelector("#dialog-submit").hidden, true);
  assert.deepEqual(h.calls.map((call) => call.action), ["confirm", "inspect", "inspect", "inspect"]);
});

for (const [label, catalog, original] of [
  ["external sources", { ...source, role: "tool", principal: "external:attachment", trusted_owner: false }, {}],
  ["another main session", { ...source, session_id: "old-main" }, {}],
  ["an original whose actual identity differs from its catalog", source, { principal: "external:forged", trusted_owner: false }],
]) {
  test(`the existing original reader does not offer owner preference confirmation for ${label}`, async (t) => {
    const h = ownerSourceHarness(t, () => assert.fail("unqualified original must never be posted"), { catalog, original });
    await clickMemoryButton(h, h.other.querySelector('[data-memory-tab="sources"]'));
    await clickMemoryButton(h, h.button("核原文", h.other));
    assert.ok(h.body().includes(first + last)); assert.equal(h.button("记作偏好"), undefined); assert.equal(h.calls.length, 0);
    await h.form.dispatch("submit"); assert.equal(h.calls.length, 0);
  });
}

test("the actual preview source dialog confirms in memory and keeps existing corrections on later inspection", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  const h = harness(async (path, options = {}) => {
    const response = await backend.fetch(`/api${path}`, { ...options, ...(options.body ? { body: JSON.stringify(options.body) } : {}) });
    const value = await response.json(); if (!response.ok) throw Object.assign(new Error(JSON.stringify(value.detail)), { status: response.status });
    return value;
  }, { composer: true });
  t.after(() => h.windowEvents.pagehide());
  h.ui.state.status = await (await backend.fetch("/api/status")).json(); h.ui.state.connected = true;
  const main = backend.state.sessions[0]; h.ui.state.mainSessionId = main.id; h.ui.state.activeSession = main;
  h.ui.useMemoryViews(); h.other.replaceChildren(h.ui.renderMemories());
  await clickMemoryButton(h, h.other.querySelector('[data-memory-tab="sources"]'));
  const owner = backend.state.memorySources.find((item) => item.trusted_owner && item.principal === "owner");
  const row = h.other.querySelectorAll(".memory-row").find((item) => item.textContent.includes(owner.source_id));
  await clickMemoryButton(h, h.button("核原文", row)); await clickMemoryButton(h, h.button("记作偏好"));
  assert.ok(h.body().includes("本页内存模拟"));
  const input = h.form.querySelector('textarea[name="quote"]'); input.value = owner.content.split("\n")[0]; await input.dispatch("input");
  const count = backend.state.memories.length; await h.form.dispatch("submit"); await flush();
  assert.equal(backend.state.memories.length, count + 1); assert.ok(h.body().includes("对应条目当前存在"));
  const item = backend.state.memories.find((memory) => memory.source_id === owner.source_id);
  item.content = "演示中已更正的当前事实"; item.kind = "fact"; item.layer = "semantic"; item.status = "superseded"; item.version = 3;
  await clickMemoryButton(h, h.button("核对结果"));
  assert.ok(h.body().includes(item.content)); assert.ok(h.body().includes("已替代")); assert.equal(item.version, 3);
  assert.equal(backend.state.memories.length, count + 1); assert.equal(backend.state.messages[main.id].length, 0);
  assert.deepEqual(backend.state.requests.filter((request) => request.path === "/memory/preferences/from-source").map((request) => request.method), ["POST", "POST"]);
});
