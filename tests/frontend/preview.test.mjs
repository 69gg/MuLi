import test from "node:test";
import assert from "node:assert/strict";
import { createSSEParser } from "../../web/api.mjs";
import { createPreviewState, createPreviewBackend, installPreview, installPreviewNavigation } from "../../web/preview.mjs";

test("preview watch paging and exact cancellation stay local, including disabled processing", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  const first = await json(backend, "/memory/watches?limit=1");
  assert.equal(first.status, 200); assert.equal(first.body.items.length, 1); assert.equal(first.body.has_more, true);
  const selected = first.body.items[0], other = structuredClone(backend.state.memoryWatches[1]);
  assert.deepEqual(Object.keys(selected).sort(), ["id", "match_terms", "question", "reply_preference", "revision", "state"]);
  const next = await json(backend, `/memory/watches?limit=1&after_id=${first.body.next_after_id}`);
  assert.equal(next.body.items[0].id, other.id);
  assert.equal((await json(backend, "/memory/watches?limit=101")).status, 422);
  assert.equal((await json(backend, "/memory/watches/cancel", "POST", { id: selected.id, revision: 1 })).status, 422);
  backend.state.settings.memory.enabled = false;
  const disabled = await json(backend, "/memory/watches");
  assert.equal(disabled.body.processing.enabled, false); assert.equal(disabled.body.items.length, 3);
  const receipt = await json(backend, "/memory/watches/cancel", "POST", { id: selected.id });
  assert.deepEqual(receipt.body, { id: selected.id, state: "cancelled", revision: selected.revision + 1 });
  assert.deepEqual((await json(backend, "/memory/watches/cancel", "POST", { id: selected.id })).body, receipt.body);
  assert.deepEqual(backend.state.memoryWatches[1], other);
  const last = await json(backend, `/memory/watches?limit=1&after_id=${backend.state.memoryWatches.at(-1).id}`);
  assert.deepEqual(last.body.items, []); assert.equal(last.body.has_more, false); assert.equal(last.body.next_after_id, null);
  assert.deepEqual(Object.keys(backend.state.runs), []); assert.equal(backend.state.events.length, 0);
  backend.state.settings.disabled_tools = ["memory_watch_list", "memory_watch_cancel"];
  assert.equal((await json(backend, "/memory/watches")).status, 400);
  assert.equal((await json(backend, "/memory/watches/cancel", "POST", { id: other.id })).status, 400);
  assert.deepEqual(backend.state.memoryWatches[1], other);
});

test("preview watch pages keep whole long questions and exact cancellation across short pages", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  backend.state.memoryWatches.forEach((item, index) => { item.question = `问题${index}：` + "完整背景".repeat(250); item.state = "active"; item.revision = 1; });
  backend.state.settings.limits.max_output_chars = 2500;
  const original = structuredClone(backend.state.memoryWatches), received = [];
  let after = "";
  for (let index = 0; index < original.length; index++) {
    const response = await json(backend, `/memory/watches?after_id=${after}`);
    assert.equal(response.status, 200);
    const page = response.body;
    assert.equal(page.after_id, after); assert.ok(page.items.length > 0);
    if (index === 0) { assert.ok(page.items.length < original.length); assert.equal(page.has_more, true); }
    assert.deepEqual(page.items, original.slice(received.length, received.length + page.items.length));
    received.push(...page.items);
    if (!page.has_more) { assert.equal(page.next_after_id, null); break; }
    assert.equal(page.next_after_id, page.items.at(-1).id); after = page.next_after_id;
  }
  assert.deepEqual(received, original); assert.deepEqual(backend.state.memoryWatches, original);
  assert.equal(backend.state.requests.some(({ method }) => method === "POST"), false);
  const target = received.at(-1), stopped = await json(backend, "/memory/watches/cancel", "POST", { id: target.id });
  assert.equal(stopped.status, 200); assert.equal(stopped.body.id, target.id);
  assert.equal(stopped.body.state, "cancelled"); assert.equal(stopped.body.revision, target.revision + 1);
  for (const item of original.filter((item) => item.id !== target.id)) {
    assert.deepEqual(backend.state.memoryWatches.find((current) => current.id === item.id), item);
  }
  assert.equal(backend.state.requests.filter(({ method }) => method === "POST").length, 1);
});

test("preview does not replace an overbudget whole watch with a successful empty page", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  backend.state.memoryWatches = [{ ...backend.state.memoryWatches[0], question: "完整背景".repeat(300) }];
  backend.state.settings.limits.max_output_chars = 1000;
  const original = structuredClone(backend.state.memoryWatches);
  const response = await json(backend, "/memory/watches");
  assert.equal(response.status, 400); assert.equal(response.body.detail.code, "output_budget");
  assert.equal(Object.hasOwn(response.body, "items"), false);
  assert.deepEqual(backend.state.memoryWatches, original);
});

test("preview source discovery, UTF-8 reads and exact retries stay in local demo state", async (t) => {
  const scheduled = new Set();
  t.mock.method(globalThis, "setTimeout", (callback) => { scheduled.add(callback); return callback; });
  t.mock.method(globalThis, "clearTimeout", (callback) => scheduled.delete(callback));
  const backend = createPreviewBackend();
  t.after(() => backend.dispose());
  const read = async (path, options) => backend.fetch(`/api${path}`, options);
  const post = async (body) => read("/memory/sources/retry", { method: "POST", body: JSON.stringify(body) });
  const first = await (await read("/memory/sources?state=done&limit=1")).json();
  assert.deepEqual(first.items, []); assert.equal(first.examined, 1); assert.equal(first.has_more, true);
  const next = await (await read(`/memory/sources?state=done&limit=1&cursor=${encodeURIComponent(first.next_cursor)}`)).json();
  assert.equal(next.items.length, 1); assert.equal(next.has_more, true);
  assert.equal((await read(`/memory/sources?state=failed&cursor=${encodeURIComponent(first.next_cursor)}`)).status, 400);
  const item = next.items[0];
  assert.equal(Object.hasOwn(item, "content"), false);
  let text = "", offset = 0;
  for (let count = 0; count < 100; count++) {
    const result = await read(`/memory/source?${new URLSearchParams({ id: item.source_id, revision: item.revision, offset_bytes: String(offset), length_bytes: "8" })}`);
    assert.equal(result.status, 200);
    const page = await result.json();
    assert.equal(page.source_id, item.source_id); assert.equal(page.revision, item.revision);
    assert.equal(page.memory_id, undefined);
    assert.equal(new TextEncoder().encode(page.content).length, page.length_bytes);
    text += page.content;
    if (!page.has_more) break;
    assert.ok(page.next_offset_bytes > offset); offset = page.next_offset_bytes;
  }
  assert.equal(text, backend.state.memorySources.find((entry) => entry.source_id === item.source_id).content);
  const failed = backend.state.memorySources.find((entry) => entry.job.state === "failed");
  const request = { source_id: failed.source_id, revision: failed.revision };
  assert.equal((await post({ ...request, revision: "0".repeat(64) })).status, 409);
  assert.equal(failed.job.state, "failed"); assert.equal(scheduled.size, 0);
  const receipt = await (await post(request)).json();
  assert.equal(receipt.accepted, true); assert.equal(receipt.state, "queued");
  assert.equal((await (await post(request)).json()).reason, "already_pending");
  assert.equal(scheduled.size, 2);
  for (const callback of [...scheduled]) { scheduled.delete(callback); callback(); }
  assert.equal(failed.job.state, "done");
  assert.equal((await (await post(request)).json()).reason, "already_done");
  assert.equal(backend.state.memorySources.find((entry) => entry.source_id === item.source_id).job.state, "done");
});

test("preview keeps a question's saved original separate from upload references and manual sending", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  const uploads = structuredClone(backend.state.uploads), memories = structuredClone(backend.state.memories);
  const sources = structuredClone(backend.state.memorySources), main = backend.state.sessions[0];
  const catalog = await json(backend, "/memory/sources");
  const item = catalog.body.items[0];
  assert.match(item.source_id, /^attachment:[a-f0-9]{32}$/u);
  assert.equal(item.principal, "external:attachment"); assert.equal(item.role, "tool"); assert.equal(item.trusted_owner, false);
  assert.equal(backend.state.uploads.some((file) => `attachment:${file.id}` === item.source_id), false);
  const original = await json(backend, `/memory/source?${new URLSearchParams({ id: item.source_id, revision: item.revision })}`);
  assert.equal(original.status, 200); assert.equal(original.body.source_id, item.source_id); assert.equal(original.body.revision, item.revision);
  assert.equal(original.body.source, item.source); assert.equal(original.body.principal, item.principal);
  assert.equal(original.body.role, item.role); assert.equal(original.body.trusted_owner, false);
  assert.ok(original.body.content.length > 0);
  assert.deepEqual(backend.state.messages[main.id], []);
  assert.deepEqual(backend.state.uploads, uploads); assert.deepEqual(backend.state.memories, memories); assert.deepEqual(backend.state.memorySources, sources);
  assert.equal(backend.state.requests.some((entry) => entry.method === "POST"), false);

  const content = `这份说明建议先做什么？\n${JSON.stringify({ id: item.source_id, revision: item.revision })}`;
  const sent = await json(backend, `/sessions/${main.id}/messages`, "POST", { content, mode: "auto", attachment_ids: [] });
  assert.equal(sent.status, 200); assert.equal(sent.body.message.content, content);
  assert.deepEqual(sent.body.message.attachment_ids, []);
  assert.equal(backend.state.requests.filter((entry) => entry.method === "POST").length, 1);
  assert.deepEqual(backend.state.uploads, uploads);
});

test("preview compares its explicit demo pair and retains exact readable source coordinates", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  const sources = backend.state.memorySources.filter((item) => item.comparisonDemo);
  assert.equal(sources.length, 2);
  const [left, right] = sources;
  const pair = { left_id: left.source_id, left_revision: left.revision, right_id: right.source_id, right_revision: right.revision };
  const compare = (body) => backend.fetch("/api/memory/sources/compare", { method: "POST", body: JSON.stringify(body) });
  const response = await compare(pair); assert.equal(response.status, 200);
  const page = await response.json(); assert.equal(page.status, "changed"); assert.equal(page.has_more, false);
  for (const side of ["left", "right"]) {
    const change = page.items[0][side];
    const read = await backend.fetch(`/api/memory/source?${new URLSearchParams(change.read_arguments)}`);
    assert.equal((await read.json()).content, change.content);
  }
  assert.equal((await compare({ ...pair, right_revision: "0".repeat(64) })).status, 409);
  assert.equal((await compare({ ...pair, cursor: "unrelated" })).status, 400);
  assert.equal((await (await compare({ ...pair, right_id: left.source_id, right_revision: left.revision })).json()).status, "equal");
  assert.equal(backend.state.memorySources.length, 4);
});

async function json(backend, path, method = "GET", body) {
  const result = await backend.fetch(`/api${path}`, { method, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  return { status: result.status, body: await result.json() };
}

test("preview calendar schedule paging and cancellation preserve the exact kind", async (t) => {
  const backend = createPreviewBackend({ delay: 0 }); t.after(() => backend.dispose());
  const base = { id: "same-id", name: "合成定时委托", enabled: true, due_at: 10000 };
  backend.state.schedules = [{ ...base, kind: "calendar_workflow_schedule" }, { ...base, kind: "workflow_schedule" }];
  const first = await json(backend, "/workflow-schedules?limit=1");
  assert.equal(first.body.next_after, "same-id");
  assert.equal(first.body.next_after_kind, "calendar_workflow_schedule");
  const next = await json(backend, "/workflow-schedules?limit=1&after_id=same-id&after_kind=calendar_workflow_schedule");
  assert.deepEqual(next.body.items.map((item) => item.kind), ["workflow_schedule"]);
  assert.equal(next.body.next_after_kind, null);
  assert.equal((await json(backend, "/workflow-schedules/same-id/cancel?kind=calendar_workflow_schedule", "POST")).status, 200);
  assert.equal(backend.state.schedules[0].enabled, false);
  assert.equal(backend.state.schedules[1].enabled, true);
  assert.equal((await json(backend, "/workflow-schedules/same-id/cancel", "POST")).body.kind, "workflow_schedule");
  assert.equal((await json(backend, "/workflow-schedules/same-id/cancel?kind=unknown", "POST")).status, 422);
  assert.equal((await json(backend, "/workflow-schedules?after_kind=unknown")).status, 422);
  backend.state.schedules = [{ ...base }];
  assert.equal((await json(backend, "/workflow-schedules/same-id/cancel?kind=calendar_workflow_schedule", "POST")).status, 404);
  assert.equal(backend.state.schedules[0].enabled, true);
});

test("preview activation never claims that a real channel receiver was verified", async (t) => {
  const backend = createPreviewBackend({ delay: 0 }); t.after(() => backend.dispose());
  backend.state.settings.channels.telegram.enabled = true;
  const { body } = await json(backend, "/channels");
  assert.equal(body.channels.telegram.enabled, true);
  for (const name of ["telegram", "weixin"]) assert.deepEqual(body.channels[name].receive_health, { state: "idle", code: null, occurrence_id: null });
  for (const name of ["onebot", "qq"]) assert.equal(Object.hasOwn(body.channels[name], "receive_health"), false);
});

test("preview local voice setup confirms, enrolls and verifies without submitting a message", async (t) => {
  const backend = createPreviewBackend({ delay: 0 }); t.after(() => backend.dispose());
  const before = structuredClone(backend.state.messages);
  assert.equal((await json(backend, "/voice/setup", "POST", { operation: "enroll", confirmed: false })).status, 400);
  assert.equal((await json(backend, "/voice/setup", "POST", { operation: "verify", confirmed: true })).status, 409);
  const enrollment = await json(backend, "/voice/setup", "POST", { operation: "enroll", confirmed: true, seconds: 5 });
  assert.equal(enrollment.body.preview, true); assert.equal(enrollment.body.enrolled, true);
  const check = await json(backend, "/voice/setup", "POST", { operation: "verify", confirmed: true, seconds: 5 });
  assert.equal(check.body.state, "completed"); assert.equal(check.body.preview, true);
  assert.equal(check.body.wake_detected, true); assert.equal(check.body.speaker_verified, true);
  const status = await json(backend, "/voice");
  assert.equal(status.body.preview, true); assert.equal(status.body.available, false);
  assert.equal(status.body.enabled, false); assert.equal(status.body.speaker.enrolled, true);
  assert.equal(status.body.health.state, "idle");
  assert.equal((await json(backend, "/voice/setup/cancel", "POST", { id: enrollment.body.id })).status, 409);
  assert.equal((await json(backend, "/voice/setup/cancel", "POST", { id: check.body.id })).body.id, check.body.id);
  assert.deepEqual(backend.state.messages, before);
});

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

test("preview extension reload queues saved in-memory settings and exposes completion on a later read", async (t) => {
  const scheduled = new Set(), calls = [];
  t.mock.method(globalThis, "setTimeout", (callback) => { scheduled.add(callback); return callback; });
  t.mock.method(globalThis, "clearTimeout", (callback) => scheduled.delete(callback));
  const target = { location: { href: "http://preview.local/preview.html", origin: "http://preview.local" }, fetch: async (...args) => { calls.push(args); throw new Error("preview must stay local"); } };
  const preview = installPreview({ target }), client = { fetch: target.fetch };
  t.after(() => preview.dispose());
  const initial = (await json(client, "/status")).body.extensions;
  assert.equal(initial.phase, "loaded"); assert.equal(initial.preview, true);
  assert.equal(initial.skills.count, 2); assert.equal(initial.mcp[0].status, "connected");
  assert.deepEqual((await json(client, "/extensions")).body, initial);
  assert.equal(scheduled.size, 0, "status reads do not start a reload or polling");
  await json(client, "/settings", "PATCH", { mcp: { mcpServers: { [initial.mcp[0].name]: { enabled: false } } } });
  const saved = structuredClone(preview.state.settings);
  const queued = await json(client, "/extensions/reload", "POST", {});
  assert.equal(queued.status, 200); assert.equal(queued.body.phase, "queued");
  assert.equal(queued.body.generation, initial.generation);
  assert.deepEqual(queued.body.mcp, initial.mcp);
  assert.equal((await json(client, "/status")).body.extensions.phase, "queued");
  assert.equal(scheduled.size, 1);
  await json(client, "/extensions/reload", "POST", {});
  assert.equal(scheduled.size, 1, "repeated queued requests share the pending demo work");
  const complete = scheduled.values().next().value; scheduled.delete(complete); complete();
  const loaded = (await json(client, "/status")).body;
  assert.equal(loaded.extensions.phase, "loaded"); assert.equal(loaded.extensions.generation, initial.generation + 1);
  assert.equal(loaded.extensions.mcp[0].status, "disabled"); assert.deepEqual(loaded.mcp, loaded.extensions.mcp);
  assert.deepEqual((await json(client, "/extensions")).body, loaded.extensions);
  assert.deepEqual(preview.state.settings, saved); assert.equal(queued.body.phase, "queued");
  assert.equal(scheduled.size, 0); assert.equal(calls.length, 0);
  await json(client, "/extensions/reload", "POST", {});
  const late = scheduled.values().next().value, pending = preview.state.extensions;
  preview.dispose(); late();
  assert.equal(scheduled.size, 0); assert.equal(preview.state.extensions === pending, true);
  assert.equal((await json(preview, "/extensions")).status, 410);
  assert.equal(calls.length, 0);
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
