import test from "node:test";
import assert from "node:assert/strict";
import { createSSEParser } from "../../web/api.mjs";
import { createPreviewState, createPreviewBackend, installPreview, installPreviewNavigation } from "../../web/preview.mjs";

async function json(backend, path, method = "GET", body) {
  const result = await backend.fetch(`/api${path}`, { method, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  return { status: result.status, body: await result.json() };
}

/** Consume the real demo SSE endpoint, including its normal abort cleanup. */
async function demo(t) {
  const backend = createPreviewBackend({ delay: 0 });
  const controller = new AbortController();
  const response = await backend.fetch("/api/events", { signal: controller.signal });
  assert.equal(response.headers.get("Content-Type"), "text/event-stream");
  const reader = response.body.getReader(), events = [], waits = new Set();
  const parser = createSSEParser((event) => {
    events.push(event);
    for (const entry of [...waits]) if (entry.predicate(event)) { waits.delete(entry); clearTimeout(entry.timer); entry.resolve(event); }
  });
  const reading = (async () => {
    const decoder = new TextDecoder();
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      parser.feed(decoder.decode(value, { stream: true }));
    }
    reader.releaseLock();
  })();
  t.after(async () => { controller.abort(); backend.dispose(); for (const entry of waits) clearTimeout(entry.timer); await reading; });
  const until = (predicate) => {
    const found = events.find(predicate); if (found) return Promise.resolve(found);
    return new Promise((resolve, reject) => {
      const entry = { predicate, resolve, timer: setTimeout(() => { waits.delete(entry); reject(new Error("预览没有产生预期的 SSE 事件")); }, 1000) };
      waits.add(entry);
    });
  };
  const send = async (mode = "auto", content = "请整理一份项目简报") => {
    const result = await json(backend, "/sessions/preview-main/messages", "POST", { content, mode, attachment_ids: [] });
    assert.equal(result.status, 200); return result.body;
  };
  return { backend, events, until, send };
}

for (const decision of ["approve", "reject"]) {
  test(`preview ${decision} resolves the live approval once and streams an accurate final answer`, async (t) => {
    const { backend, until, events, send } = await demo(t);
    const started = await send(decision === "approve" ? "ask" : "auto");
    const requested = await until((event) => event.type === "approval.requested" && event.data.run_id === started.run_id);
    assert.equal(requested.data.tool, "fs_write");
    const live = await json(backend, "/approvals");
    assert.equal(live.body.items.length, 1);
    assert.equal(live.body.items[0].id, requested.data.id);
    const accepted = await json(backend, `/approvals/${requested.data.id}`, "POST", { decision });
    assert.deepEqual(accepted.body, { resolved: true });
    assert.equal((await json(backend, `/approvals/${requested.data.id}`, "POST", { decision })).status, 409);
    await until((event) => event.type === "run.completed" && event.data.run_id === started.run_id);
    const writes = events.filter((event) => event.type === "tool.started" && event.data.run_id === started.run_id && event.data.name === "fs_write");
    assert.equal(writes.length, decision === "approve" ? 1 : 0);
    const receipt = events.find((event) => event.type === "tool.completed" && event.data.run_id === started.run_id && event.data.name === "fs_write");
    assert.equal(receipt.data.status, decision === "approve" ? "completed" : "not_executed");
    const final = events.find((event) => event.type === "message.completed" && event.data.run_id === started.run_id && event.data.message.role === "assistant");
    assert.ok(final.data.message.content.includes(decision === "approve" ? "整理好了" : "没有执行"));
    assert.equal(events.filter((event) => event.type === "message.completed" && event.data.message?.role === "assistant").length, 1);
    assert.equal(events.filter((event) => event.type === "message.delta").map((event) => event.data.content).join(""), final.data.message.content);
    assert.deepEqual((await json(backend, "/approvals")).body.items, []);
  });
}

test("preview yolo uses the same tool stream without fabricating a pending approval", async (t) => {
  const { backend, until, events, send } = await demo(t);
  const started = await send("yolo", "请记住：先给结论，再给依据。");
  await until((event) => event.type === "run.completed" && event.data.run_id === started.run_id);
  assert.equal(events.some((event) => event.type === "approval.requested"), false);
  assert.equal(events.filter((event) => event.type === "tool.completed" && event.data.name === "memory_save").length, 1);
  const recalled = (await json(backend, "/memories?query=先给结论")).body.items;
  const saved = recalled.filter((item) => item.content === "先给结论，再给依据。");
  assert.equal(saved.length, 1);
  assert.equal(saved[0].source, "演示对话");
  const remembered = events.filter((event) => event.type === "memory.updated" && event.data.memory_ids.includes(saved[0].id));
  assert.equal(remembered.length, 1);
  assert.equal(backend.state.memories.find((item) => item.id === saved[0].id).content, "先给结论，再给依据。");
  const bootstrap = (await json(backend, "/conversation")).body;
  assert.equal(bootstrap.live.status, "idle");
  assert.equal(bootstrap.messages.length, 2);
  assert.equal(backend.state.settings.models.main.max_output_tokens, null);
  assert.equal(backend.state.settings.models.worker.model, "");
});

test("preview stop fences both a live tool callback and a not-yet-started run", async (t) => {
  const { backend, until, events, send } = await demo(t);
  const first = await send("yolo");
  await until((event) => event.type === "tool.started" && event.data.run_id === first.run_id);
  assert.equal((await json(backend, "/sessions/preview-main/stop", "POST")).status, 200);
  const second = await send("yolo");
  await json(backend, "/sessions/preview-main/stop", "POST");
  const third = await send("yolo", "记住：这是停止之后的新请求。");
  await until((event) => event.type === "run.completed" && event.data.run_id === third.run_id);
  for (const run of [first, second]) {
    assert.equal(events.some((event) => event.type === "run.cancelled" && event.data.run_id === run.run_id), true);
    assert.equal(events.some((event) => event.type === "run.completed" && event.data.run_id === run.run_id), false);
    assert.equal(events.some((event) => event.type === "tool.started" && event.data.run_id === run.run_id && event.data.name === "fs_write"), false);
  }
  assert.equal(events.some((event) => event.type === "run.started" && event.data.run_id === second.run_id), false);
});

test("preview queue capacity rejects before persisting a user message or scheduling a run", async (t) => {
  const { backend, until, send } = await demo(t);
  backend.state.settings.limits.max_pending_runs = 2;
  const first = await send("ask");
  await until((event) => event.type === "approval.requested" && event.data.run_id === first.run_id);
  const second = await send("ask", "这是排队的第二条请求");
  assert.equal(second.queued, true);
  const counts = { messages: backend.state.messages["preview-main"].length, runs: Object.keys(backend.state.runs).length };
  const rejected = await json(backend, "/sessions/preview-main/messages", "POST", { content: "不应被持久的第三条请求", mode: "ask" });
  assert.equal(rejected.status, 429);
  assert.match(rejected.body.detail, /队列已满/u);
  assert.equal(backend.state.messages["preview-main"].length, counts.messages);
  assert.equal(Object.keys(backend.state.runs).length, counts.runs);
});

test("preview uploads enforce both total bytes and count without partial metadata or blob writes", async (t) => {
  const state = createPreviewState(), backend = createPreviewBackend({ state });
  t.after(() => backend.dispose());
  const initialBytes = [...state.files.values()].reduce((total, blob) => total + blob.size, 0);
  state.previewLimits.max_upload_bytes = initialBytes + 4;
  state.previewLimits.max_uploads = state.files.size + 1;
  const upload = (text) => backend.fetch("/api/uploads", { method: "POST", body: new Blob([text], { type: "text/plain" }), headers: { "X-File-Name": encodeURIComponent("临时资料.txt") } });
  assert.equal((await upload("12345")).status, 413);
  assert.equal(state.files.size, 1); assert.equal(state.uploads.length, 1);
  const saved = await (await upload("1234")).json();
  assert.equal(saved.name, "临时资料.txt");
  assert.equal(state.files.size, 2); assert.equal(state.uploads.length, 2);
  assert.equal((await upload("")).status, 429);
  assert.equal(state.files.size, 2); assert.equal(state.uploads.length, 2);
  assert.equal(await (await backend.fetch(`/api/uploads/${saved.id}/content`)).text(), "1234");
});

test("preview blocks external fetch and actual anchor navigation while preserving local buttons and owned blobs", async (t) => {
  const calls = [], target = { location: { href: "http://preview.local/preview.html", origin: "http://preview.local" }, fetch: async (...args) => { calls.push(args); return new Response("static"); } };
  const originalFetch = target.fetch;
  const preview = installPreview({ target });
  t.after(() => preview.dispose());
  for (const [url, options] of [["https://example.com/model", { method: "POST" }], ["/api/not-a-real-operation", { method: "POST" }], ["/api/unknown", {}]]) {
    const result = await target.fetch(url, options);
    assert.equal(result.status, 501);
  }
  assert.equal(calls.length, 0);
  assert.equal((await target.fetch("/styles.css")).status, 200);
  assert.equal(calls.length, 1);
  const handlers = new Map(), root = { addEventListener(name, fn) { handlers.set(name, fn); }, removeEventListener(name, fn) { if (handlers.get(name) === fn) handlers.delete(name); } };
  let blocked = 0;
  const remove = installPreviewNavigation({ root, pageURL: target.location.href, isLocalBlob: (value) => value === "blob:http://preview.local/owned", onBlocked: () => { blocked++; } });
  const click = (value, kind = "click") => {
    let prevented = false;
    const anchor = value === null ? null : { getAttribute: () => value };
    handlers.get(kind)({ target: { closest: () => anchor }, preventDefault() { prevented = true; }, stopPropagation() {} });
    return prevented;
  };
  assert.equal(click("https://example.com/article"), true);
  assert.equal(click("https://example.com/article", "auxclick"), true);
  assert.equal(click("/api/settings"), true);
  assert.equal(click("blob:http://preview.local/unowned"), true);
  assert.equal(click("#message-input"), false);
  assert.equal(click("blob:http://preview.local/owned"), false);
  assert.equal(click(null), false);
  assert.equal(blocked, 4);
  remove(); assert.equal(handlers.size, 0);
  preview.dispose(); assert.equal(target.fetch === originalFetch, true);
});
