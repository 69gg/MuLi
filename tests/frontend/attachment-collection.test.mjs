import test from "node:test";
import assert from "node:assert/strict";
import { ApiError } from "../../web/api.mjs";
import { createPreviewBackend } from "../../web/preview.mjs";
import { clickMemoryButton, deferred, flush, harness } from "./helpers/app-dom-harness.mjs";

function collectionFixture(identifier = "1".repeat(32), filename = "收藏笔记.docx") {
  const first = "外部资料预览🙂<script>这里只是正文</script>";
  const last = "完整资料还包含这段末尾，预览不能声称已经显示全文。";
  const sourceRevision = "a".repeat(64), documentRevision = "b".repeat(64), registeredRevision = "c".repeat(64);
  const identity = {
    attachment_id: identifier, source_revision: sourceRevision, format: "docx", scope: "docx_main_body_text", document_revision: documentRevision,
    source_id: `attachment:${identifier}`, registered_revision: registeredRevision,
    source: `attachment:${identifier}:document:v1:docx:docx_main_body_text:${sourceRevision}:${documentRevision}`,
    session_id: "", role: "tool", principal: "external:attachment", trusted_owner: false,
  };
  const file = { id: identifier, name: filename, mime_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", size: 4096 };
  const page = { field: "content", content: first, offset_bytes: 0, length_bytes: Buffer.byteLength(first), next_offset_bytes: Buffer.byteLength(first), total_bytes: Buffer.byteLength(first + last), has_more: true };
  const preview = { ...identity, filename, read_only: true, processing_notice: "本次只核对资料；确认时使用当时设置，后台整理可能向已配置服务发送正文。", ...page };
  const receipt = { ...identity, state: "queued", created: true };
  const source = { id: identity.source_id, source_id: identity.source_id, source: identity.source, session_id: "", role: "tool", principal: "external:attachment", trusted_owner: false, digest: registeredRevision, revision: registeredRevision, created_at: 1, truncated: true, ...page };
  return { file, preview, receipt, source, first, last, previewPath: `/uploads/${identifier}/memory-preview`, collectionPath: `/uploads/${identifier}/memory-collection` };
}

async function openUploads(h) {
  h.ui.useUploadViews();
  await h.ui.switchView("uploads");
  assert.equal(h.ui.state.view, "uploads");
}

function collectionButton(h, file) {
  const row = h.other.querySelectorAll(".upload-row").find((item) => item.textContent.includes(file.name));
  assert.ok(row, "the real attachment list must show the selected saved file");
  const button = h.button("收藏", row);
  assert.ok(button, "the saved attachment row must offer 收藏 without sending a chat message");
  return button;
}

function posts(h) { return h.requests.filter((entry) => entry.method === "POST"); }

test("attachment collection prepares read-only and confirms once while preserving the running chat draft", async () => {
  const item = collectionFixture(), pending = deferred();
  const h = harness(async (path, options) => {
    if (path === "/uploads") return { items: [item.file] };
    if (path === item.previewPath) return item.preview;
    if (path === item.collectionPath && options.method === "POST") return pending.promise;
    throw new Error(`unexpected request: ${path}`);
  }, { composer: true });
  h.ui.state.connected = true; h.ui.state.status = { configured: true, model: "fixture-model" };
  h.ui.state.mainSessionId = "current-main";
  h.ui.state.activeSession = { id: "current-main", mode: "auto", status: "running" };
  h.ui.state.session.status = "running"; h.ui.state.session.runId = "existing-run";
  const selected = { id: "9".repeat(32), name: "已选草稿附件.txt", mime_type: "text/plain", size: 17 };
  h.ui.state.attachments = [selected]; const attachments = h.ui.state.attachments;
  const voice = { text: "尚未合入的语音草稿" }; h.ui.state.voiceDraft = voice;
  const input = h.document.querySelector("#message-input");
  input.value = "正在写的问题，收藏资料不能覆盖它。"; await input.dispatch("input");
  h.ui.renderComposerAttachments();
  const draft = h.ui.captureDraft(), session = h.ui.state.session, active = h.ui.state.activeSession;

  await openUploads(h);
  assert.ok(h.button("带入对话", h.other), "the original attachment action remains available");
  assert.ok(h.other.querySelector("a[download]"), "the original download remains available");
  await clickMemoryButton(h, collectionButton(h, item.file));
  assert.equal(h.dialog.open, true); assert.ok(h.body().includes(item.file.name));
  assert.ok(h.body().includes("主文档正文")); assert.ok(h.body().includes(item.first));
  assert.equal(h.body().includes(item.last), false); assert.match(h.body(), /还有后文|尚有后文/);
  assert.equal(h.dialog.querySelector("script"), null, "preview markup must remain literal text");
  assert.equal(posts(h).length, 0, "preparation must only read");
  const preparation = h.requests.find((entry) => entry.path === item.previewPath);
  assert.equal(preparation.cache, "no-store"); assert.equal(preparation.signal.aborted, false);
  const confirm = h.button("确认收藏"); assert.ok(confirm); assert.equal(confirm.disabled, false);

  const saving = h.form.dispatch("submit"); await flush();
  const repeated = h.form.dispatch("submit"); await flush();
  assert.equal(posts(h).length, 1, "an in-flight confirmation cannot be submitted twice");
  const request = posts(h)[0]; assert.equal(request.path, item.collectionPath);
  assert.deepEqual(Object.keys(request.body).sort(), ["document_revision", "registered_revision", "source_revision"]);
  assert.equal(request.body.source_revision, item.preview.source_revision);
  assert.equal(request.body.document_revision, item.preview.document_revision);
  assert.equal(request.body.registered_revision, item.preview.registered_revision);
  assert.notEqual(request.body.registered_revision, request.body.source_revision);
  assert.notEqual(request.body.registered_revision, request.body.document_revision);
  pending.resolve(item.receipt); await Promise.all([saving, repeated]); await flush();
  assert.match(h.body(), /已排队|等待整理/); assert.equal(h.body().includes("整理完成"), false);
  assert.equal(h.ui.state.view, "uploads"); assert.equal(posts(h).length, 1);
  const after = h.ui.captureDraft();
  assert.equal(after.text, draft.text); assert.equal(after.revision, draft.revision); assert.equal(after.session, draft.session);
  assert.equal(h.ui.state.session === session, true); assert.equal(h.ui.state.activeSession === active, true);
  assert.equal(h.ui.state.session.status, "running"); assert.equal(h.ui.state.session.runId, "existing-run");
  assert.equal(h.ui.state.attachments === attachments, true); assert.equal(h.ui.state.attachments[0] === selected, true);
  assert.equal(h.ui.state.voiceDraft === voice, true);
  assert.ok(h.document.querySelector("#composer-attachments").textContent.includes(selected.name));
  assert.equal(h.ui.state.session.messages.length, 0, "collection must not create a chat message");
});

test("an unknown attachment collection only reconciles the exact source through GET without resubmitting", async () => {
  const item = collectionFixture(); let observed = { ...item.source, source: item.source.source + ":foreign" };
  const h = harness(async (path, options) => {
    if (path === "/uploads") return { items: [item.file] };
    if (path === item.previewPath) return item.preview;
    if (path === item.collectionPath && options.method === "POST") throw new ApiError("连接在回执前中断");
    if (path.startsWith("/memory/source?")) {
      assert.equal(options.method ?? "GET", "GET"); assert.equal(options.cache, "no-store");
      const query = new URLSearchParams(path.split("?")[1]);
      assert.equal(query.get("id"), item.preview.source_id);
      assert.equal(query.get("revision"), item.preview.registered_revision);
      return observed;
    }
    throw new Error(`unexpected request: ${path}`);
  });
  await openUploads(h); await clickMemoryButton(h, collectionButton(h, item.file));
  await h.form.dispatch("submit"); await flush();
  assert.equal(posts(h).length, 1); assert.match(h.body(), /未知|未确认|无法确认/);
  const confirm = h.document.querySelector("#dialog-submit"); assert.ok(confirm.hidden || confirm.disabled);
  await h.form.dispatch("submit"); await flush(); assert.equal(posts(h).length, 1);

  for (const mismatch of [
    { source: item.source.source + ":foreign" },
    { revision: item.preview.source_revision, digest: item.preview.source_revision },
    { principal: "owner", trusted_owner: true },
    { total_bytes: item.source.total_bytes + 1 },
  ]) {
    observed = { ...item.source, ...mismatch };
    const reconcile = h.button("核对结果"); assert.ok(reconcile);
    await clickMemoryButton(h, reconcile);
    assert.equal(posts(h).length, 1); assert.equal(h.body().includes("已登记"), false);
    assert.match(h.body(), /未知|未确认|无法确认/);
  }
  observed = item.source;
  await clickMemoryButton(h, h.button("核对结果"));
  assert.match(h.body(), /已登记/);
  for (const claim of ["本次收藏成功", "本次提交成功", "整理完成"]) assert.equal(h.body().includes(claim), false);
  assert.equal(posts(h).length, 1); assert.ok(h.button("核原文"));
  const sourceReads = h.requests.filter((entry) => entry.path.startsWith("/memory/source?"));
  assert.equal(sourceReads.length, 5, "each explicit reconciliation reads one bounded identity page");
  assert.equal(h.requests.filter((entry) => entry.path === item.previewPath).length, 1, "reconciliation must not silently prepare or reconfirm");
  await h.form.dispatch("submit"); await flush(); assert.equal(posts(h).length, 1);
});

test("attachment collection isolates stale 401 responses while a current 401 still logs out", async () => {
  for (const phase of ["prepare", "submit"]) for (const stale of [true, false]) {
    const first = collectionFixture(), second = collectionFixture("2".repeat(32), "新连接的收藏资料.docx"), pending = deferred();
    const h = harness(async (path, options) => {
      if (path === "/uploads") return { items: [first.file, second.file] };
      if (path === first.previewPath) return phase === "prepare" ? pending.promise : first.preview;
      if (path === second.previewPath) return second.preview;
      if (path === first.collectionPath && options.method === "POST") return pending.promise;
      throw new Error(`unexpected request: ${path}`);
    });
    await openUploads(h);
    const opening = clickMemoryButton(h, collectionButton(h, first.file)); await flush();
    let operation = opening;
    if (phase === "submit") { await opening; operation = h.form.dispatch("submit"); await flush(); }
    const oldRequest = h.requests.at(-1);
    assert.equal(oldRequest.path, phase === "prepare" ? first.previewPath : first.collectionPath);
    assert.equal(oldRequest.signal.aborted, false);
    let body = null, root = null;
    if (stale) {
      h.ui.activateConnection();
      await openUploads(h); await clickMemoryButton(h, collectionButton(h, second.file));
      body = h.body(); root = h.dialog.querySelector("#dialog-content").firstElementChild;
      assert.ok(body.includes(second.file.name)); assert.equal(oldRequest.signal.aborted, true);
      assert.equal(h.ui.state.auth.authenticated, true); assert.equal(h.dialog.open, true);
    }
    pending.reject(new ApiError("密码已失效", 401)); await operation; await flush();
    assert.equal(h.ui.state.auth.authenticated, stale, `${phase}: only a current connection's 401 may log out`);
    if (stale) {
      assert.equal(h.dialog.open, true); assert.equal(h.body(), body);
      assert.equal(h.dialog.querySelector("#dialog-content").firstElementChild === root, true);
      assert.equal(h.button("确认收藏").disabled, false, "the old POST cannot clear or disable the new dialog");
    } else assert.equal(h.dialog.open, false);
    assert.equal(posts(h).length, phase === "submit" ? 1 : 0);
  }
});

test("closing or replacing a collection preview blocks its same-owner late 401 before authentication changes", async () => {
  for (const replace of ["close and reopen", "another dialog"]) {
    const first = collectionFixture(), second = collectionFixture("2".repeat(32), "当前资料.txt"), pending = deferred();
    const h = harness(async (path) => {
      if (path === "/uploads") return { items: [first.file, second.file] };
      if (path === first.previewPath) return pending.promise;
      if (path === second.previewPath) return second.preview;
      throw new Error(`unexpected request: ${path}`);
    });
    await openUploads(h);
    const opening = clickMemoryButton(h, collectionButton(h, first.file)); await flush();
    const old = h.requests.at(-1);
    if (replace === "close and reopen") {
      await clickMemoryButton(h, h.document.querySelector("#dialog-close"));
      await clickMemoryButton(h, collectionButton(h, second.file));
    } else {
      const content = h.document.createElement("p"); content.textContent = "当前的另一个对话框";
      h.ui.openDialog({ title: "当前对话框", content, readOnly: true });
    }
    const body = h.body(), root = h.dialog.querySelector("#dialog-content").firstElementChild;
    assert.equal(old.signal.aborted, true);
    pending.reject(new ApiError("旧准备的密码错误", 401)); await opening; await flush();
    assert.equal(h.ui.state.auth.authenticated, true); assert.equal(h.dialog.open, true);
    assert.equal(h.body(), body); assert.equal(h.dialog.querySelector("#dialog-content").firstElementChild === root, true);
    assert.equal(posts(h).length, 0);
  }
});

test("reopening an in-flight collection keeps unknown GET-only until a new explicit prepare and confirmation", async () => {
  const item = collectionFixture(), pending = deferred(); let count = 0, unavailable = 404;
  const h = harness(async (path, options) => {
    if (path === "/uploads") return { items: [item.file] };
    if (path === item.previewPath) return item.preview;
    if (path === item.collectionPath && options.method === "POST") return ++count === 1 ? pending.promise : item.receipt;
    if (path.startsWith("/memory/source?")) throw new ApiError("本次读取暂不可用", unavailable, { detail: { code: unavailable === 400 ? "output_budget" : "evidence_changed" } });
    throw new Error(`unexpected request: ${path}`);
  });
  await openUploads(h); await clickMemoryButton(h, collectionButton(h, item.file));
  const saving = h.form.dispatch("submit"); await flush(); const original = posts(h)[0];
  await clickMemoryButton(h, h.document.querySelector("#dialog-close"));
  assert.equal(original.signal.aborted, true); assert.equal(h.dialog.open, false);
  await clickMemoryButton(h, collectionButton(h, item.file));
  assert.match(h.body(), /未确认/); assert.equal(posts(h).length, 1);
  assert.equal(h.requests.filter((entry) => entry.path === item.previewPath).length, 1);
  pending.resolve(item.receipt); await saving; await flush();
  assert.match(h.body(), /未确认/); assert.equal(h.body().includes("已登记"), false);
  for (const status of [404, 409, 400]) {
    unavailable = status; await clickMemoryButton(h, h.button("核对结果"));
    assert.match(h.body(), /未确认/); assert.equal(h.body().includes("已登记"), false);
    await h.form.dispatch("submit"); await flush(); assert.equal(posts(h).length, 1);
  }
  await clickMemoryButton(h, h.button("重新准备"));
  assert.equal(h.requests.filter((entry) => entry.path === item.previewPath).length, 2);
  assert.equal(posts(h).length, 1); assert.equal(h.button("确认收藏").disabled, false);
  await h.form.dispatch("submit"); await flush();
  assert.equal(posts(h).length, 2, "a second POST needs both a fresh prepare and a separate explicit confirmation");
  assert.match(h.body(), /已排队|等待整理/);
});

test("invalid preparation and incomplete collection receipts never enable or reuse an old confirmation", async () => {
  const item = collectionFixture();
  for (const override of [{ read_only: false }, { principal: "owner", trusted_owner: true }, { document_revision: null }, { next_offset_bytes: 0 }]) {
    const h = harness(async (path) => {
      if (path === "/uploads") return { items: [item.file] };
      assert.equal(path, item.previewPath); return { ...item.preview, ...override };
    });
    await openUploads(h); await clickMemoryButton(h, collectionButton(h, item.file));
    assert.match(h.body(), /无法完整核查|不一致/);
    const confirm = h.document.querySelector("#dialog-submit"); assert.ok(confirm.hidden || confirm.disabled);
    await h.form.dispatch("submit"); await flush(); assert.equal(posts(h).length, 0);
  }
  for (const override of [{ registered_revision: "d".repeat(64) }, { created: "true" }, { state: ["queued"] }]) {
    const h = harness(async (path, options) => {
      if (path === "/uploads") return { items: [item.file] };
      if (path === item.previewPath) return item.preview;
      assert.equal(path, item.collectionPath); assert.equal(options.method, "POST"); return { ...item.receipt, ...override };
    });
    await openUploads(h); await clickMemoryButton(h, collectionButton(h, item.file));
    await h.form.dispatch("submit"); await flush();
    assert.match(h.body(), /未确认/); assert.equal(h.body().includes("已登记"), false);
    await h.form.dispatch("submit"); await flush(); assert.equal(posts(h).length, 1);
  }
});

test("the real collection dialog reconciles a lost local-preview receipt and opens the saved original", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  const text = "演示资料里有可完整找回的正文🙂。\n".repeat(8) + "这里是必须在原文保留的最后一句。";
  const uploaded = await backend.fetch("/api/uploads", { method: "POST", body: new Blob([text], { type: "text/plain" }), headers: { "X-File-Name": encodeURIComponent("收藏闭环演示.txt") } });
  assert.equal(uploaded.status, 200); const file = await uploaded.json();
  const messages = structuredClone(backend.state.messages), originalCount = backend.state.memorySources.length;
  const h = harness(async (path, options) => {
    const result = await backend.fetch(`/api${path}`, { ...options, ...(options.body ? { body: JSON.stringify(options.body) } : {}) });
    const value = await result.json();
    if (!result.ok) throw new ApiError("演示请求失败", result.status, value);
    if (options.method === "POST") throw new ApiError("演示：登记后未收到回执");
    return value;
  });
  await openUploads(h); await clickMemoryButton(h, collectionButton(h, file));
  assert.ok(h.body().includes("完整 UTF-8 文本")); assert.match(h.body(), /还有后文/);
  await h.form.dispatch("submit"); await flush();
  assert.match(h.body(), /未确认/); assert.equal(backend.state.memorySources.length, originalCount + 1);
  await clickMemoryButton(h, h.button("核对结果"));
  assert.match(h.body(), /准确资料当前已登记/); assert.equal(posts(h).length, 1);
  await clickMemoryButton(h, h.button("核原文"));
  assert.equal(h.document.querySelector("#dialog-title").textContent, "核查来源原文");
  assert.ok(h.body().includes(text)); assert.match(h.body(), /已到末尾/);
  const saved = backend.state.memorySources.find((item) => item.attachment_id === file.id);
  assert.equal(h.ui.slot.page.revision, saved.registered_revision); assert.equal(h.ui.slot.page.source, saved.source);
  assert.equal(h.ui.slot.page.principal, "external:attachment"); assert.equal(h.ui.slot.page.trusted_owner, false);
  assert.equal(posts(h).length, 1); assert.deepEqual(backend.state.messages, messages);
});

// A browser changes `open` before delivering its later close task. Keep this
// timing local to these cases; the shared harness and older cases stay intact.
function delayedCollectionClose(h) {
  const tasks = []; let delivered = 0;
  h.dialog.close = () => {
    if (!h.dialog.open) return;
    h.dialog.open = false;
    tasks.push(() => h.dialog.dispatch("close"));
  };
  return {
    get queued() { return tasks.length; },
    get delivered() { return delivered; },
    cancel() {
      const event = { type: "cancel", target: h.dialog, currentTarget: h.dialog, cancelable: true, bubbles: false, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
      // Native cancel listeners run before the browser's default close. Honor
      // preventDefault rather than invoking the collection cleanup directly.
      const listeners = (h.dialog.events.cancel ?? []).map((listener) => listener(event));
      if (!event.defaultPrevented) h.dialog.close();
      return Promise.all(listeners);
    },
    async deliver() { while (tasks.length) { delivered++; await tasks.shift()(); } },
  };
}

for (const phase of ["prepare", "submit"]) for (const entry of ["close button", "native cancel"]) {
  test(`collection ${phase} ${entry} invalidates late 401 before the browser close task`, async () => {
    const item = collectionFixture(), pending = deferred();
    const h = harness(async (path, options) => {
      if (path === "/uploads") return { items: [item.file] };
      if (path === item.previewPath) return phase === "prepare" ? pending.promise : item.preview;
      if (path === item.collectionPath && options.method === "POST") return pending.promise;
      throw new Error(`unexpected request: ${path}`);
    });
    const owner = h.ui.activateConnection(), close = delayedCollectionClose(h);
    await openUploads(h);
    const opening = clickMemoryButton(h, collectionButton(h, item.file)); await flush();
    let operation = opening;
    if (phase === "submit") { await opening; operation = h.form.dispatch("submit"); await flush(); }
    const request = h.requests.at(-1);
    assert.equal(request.path, phase === "prepare" ? item.previewPath : item.collectionPath);
    assert.equal(request.signal.aborted, false); assert.equal(h.dialog.open, true);
    let closing;
    if (entry === "close button") {
      const button = h.document.querySelector('#dialog-actions [data-action="close-dialog"]');
      assert.ok(button); assert.equal(button.disabled, false);
      await button.dispatch("click");
      // Enter the real delegated click handler, without clickMemoryButton's
      // final flush before delivering the competing authorization response.
      closing = Promise.all((h.document.events.click ?? []).map((listener) => listener({ target: button, preventDefault() {} })));
    } else closing = close.cancel();
    assert.equal(h.dialog.open, false); assert.equal(close.queued, 1); assert.equal(close.delivered, 0);
    const abortedAtCloseIntent = request.signal.aborted;
    pending.reject(new ApiError("关闭意图之后才到达的旧 401", 401));
    await Promise.all([operation, closing]); await flush();
    try {
      assert.equal(close.delivered, 0, "the stale response must settle before any close task is delivered");
      assert.equal(h.ui.state.auth.authenticated, true, "closing this dialog must not log out its current connection");
      assert.equal(owner.controller.signal.aborted, false, "the same connection must remain usable");
      assert.equal(abortedAtCloseIntent, true, "the request must be invalidated synchronously with the close intent");
      assert.equal(posts(h).length, phase === "submit" ? 1 : 0);
    } finally { await close.deliver(); }
    assert.equal(h.dialog.open, false);
    if (phase === "submit") {
      const prepares = h.requests.filter((request) => request.path === item.previewPath).length;
      await clickMemoryButton(h, collectionButton(h, item.file));
      assert.match(h.body(), /未确认/); assert.equal(h.body().includes("已登记"), false);
      assert.equal(h.requests.filter((request) => request.path === item.previewPath).length, prepares);
      await h.form.dispatch("submit"); await flush(); assert.equal(posts(h).length, 1);
    }
  });
}
