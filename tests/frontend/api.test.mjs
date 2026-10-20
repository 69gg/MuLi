import test from "node:test";
import assert from "node:assert/strict";
import { ApiError, createSSEParser, readEvents, request, uploadFile, transcribeAttachment } from "../../web/api.mjs";
import { applyEvent, createSessionState } from "../../web/state.mjs";

test("SSE parser survives every possible CRLF split and preserves Unicode", () => {
  const wire = ': heartbeat\r\nid: 8\r\nevent: message.delta\r\ndata: {"content":"沐璃，继续。"}\r\n\r\n';
  for (let split = 0; split <= wire.length; split += 1) {
    const events = [];
    const parser = createSSEParser((event) => events.push(event));
    parser.feed(wire.slice(0, split)); parser.feed(wire.slice(split));
    assert.deepEqual(events, [{ id: "8", type: "message.delta", data: { content: "沐璃，继续。" } }]);
  }
});

test("SSE supports multiline data, persisted IDs, envelope events and incomplete frames", () => {
  const events = [];
  const parser = createSSEParser((event) => events.push(event));
  parser.feed('id: 9\ndata: {"content":\ndata: "两行"}\n\n');
  parser.feed('data: {"id":10,"type":"run.completed","data":{}}\n\n');
  parser.feed('data: unfinished', true);
  assert.equal(events.length, 2);
  assert.deepEqual(events[0], { id: "9", type: "message", data: { content: "两行" } });
  assert.equal(events[1].id, 10);
  assert.equal(events[1].type, "run.completed");
});

test("raw SSE ids remain exact when JSON numeric ids would merge two deltas", () => {
  const events = [], positions = [];
  const parser = createSSEParser((event) => events.push(event), { onId: (id) => positions.push(id) });
  for (const id of ["9007199254740992", "9007199254740993"]) {
    const wire = `id: ${id}\ndata: {"id":${id},"type":"message.delta","data":{"content":"字"}}\n\n`;
    for (const character of wire) parser.feed(character);
  }
  assert.deepEqual(events.map((event) => event.id), positions);
  assert.deepEqual(positions, ["9007199254740992", "9007199254740993"]);
  const state = events.reduce((current, event) => applyEvent(current, event), createSessionState());
  assert.equal(state.messages[0].content, "字字");
  assert.equal(state.cursor, "9007199254740993");
});

test("explicit empty and id-only frames reset positions without creating business events", () => {
  const events = [], positions = [];
  const parser = createSSEParser((event) => events.push(event), { onId: (id) => positions.push(id) });
  parser.feed('id: 42\ndata: {"type":"ready"}\n\n');
  parser.feed('id:\ndata: {"id":99,"type":"reset"}\n\n');
  assert.equal(events[1].id, "");
  parser.feed('id: 8\n\nid\n\n');
  assert.equal(events.length, 2);
  assert.deepEqual(positions, ["42", "", "8", ""]);
  parser.feed('id: bad\0id\ndata: {"type":"after-reset"}\n\n');
  assert.equal(events[2].id, "");
  parser.feed('id: unfinished\n', true);
  assert.deepEqual(positions, ["42", "", "8", ""]);
});

test("readEvents decodes split UTF-8 bytes and resumes from the last event cursor", async (context) => {
  const bytes = new TextEncoder().encode('data: {"id":"42","type":"message.delta","data":{"content":"青瓷"}}\n\n');
  const chunks = [...bytes].map((byte) => new Uint8Array([byte]));
  let captured;
  context.mock.method(globalThis, "fetch", async (url, options) => {
    captured = { url, options };
    return new Response(new ReadableStream({ start(controller) { chunks.forEach((chunk) => controller.enqueue(chunk)); controller.close(); } }), { status: 200 });
  });
  const events = [];
  const signal = new AbortController().signal;
  await readEvents("/sessions/a/events", { after: "41", signal, onEvent: (event) => events.push(event) });
  assert.equal(captured.url, "/api/sessions/a/events?after=41");
  assert.equal(captured.options.headers["Last-Event-ID"], "41");
  assert.equal(captured.options.signal, signal);
  assert.equal(events[0].data.content, "青瓷");
});

test("all mutations declare JSON, including bodyless DELETE and stop requests", async (context) => {
  const captured = [];
  context.mock.method(globalThis, "fetch", async (url, options) => {
    captured.push({ url, options }); return new Response(null, { status: 204 });
  });
  await request("/sessions/test", { method: "DELETE" });
  await request("/sessions/test/stop", { method: "POST" });
  await request("/approvals/one", { method: "POST", body: { decision: "reject" } });
  for (const call of captured) assert.equal(call.options.headers["Content-Type"], "application/json");
  assert.equal(captured[2].options.body, '{"decision":"reject"}');
});

test("readEvents forwards exact ID-only frames and empty resets without inventing business events", async (context) => {
  const wire = 'id: 9007199254740993\n\nid:\n\nid: 9007199254740995\ndata: {"id":9007199254740995,"type":"ready"}\n\n';
  let captured;
  context.mock.method(globalThis, "fetch", async (url, options) => {
    captured = { url, options }; return new Response(wire);
  });
  const events = [], positions = [];
  await readEvents("/events?kind=global", { after: "", onEvent: (event) => events.push(event), onId: (id) => positions.push(id) });
  assert.equal(captured.url, "/api/events?kind=global");
  assert.equal("Last-Event-ID" in captured.options.headers, false);
  assert.deepEqual(positions, ["9007199254740993", "", "9007199254740995"]);
  assert.deepEqual(events.map((event) => event.id), ["9007199254740995"]);
});

test("API errors retain status without exposing raw HTML responses", async (context) => {
  context.mock.method(globalThis, "fetch", async () => new Response('{"detail":"会话仍在运行"}', { status: 409 }));
  await assert.rejects(request("/sessions/test"), (error) => error instanceof ApiError && error.status === 409 && error.message === "会话仍在运行");
});

test("file uploads preserve bytes and MIME without JSON serialization", async (context) => {
  const file = new Blob([new Uint8Array([0, 255, 13, 10, 128])], { type: "application/octet-stream" });
  Object.defineProperty(file, "name", { value: "沐璃 #1.bin" });
  let captured;
  context.mock.method(globalThis, "fetch", async (url, options) => {
    captured = { url, options }; return Response.json({ id: "local-file" });
  });
  const result = await uploadFile(file);
  assert.equal(captured.url, "/api/uploads");
  assert.equal(captured.options.body, file);
  assert.equal(captured.options.headers["Content-Type"], "application/octet-stream");
  assert.equal(decodeURIComponent(captured.options.headers["X-File-Name"]), file.name);
  assert.equal("rawBody" in captured.options, false);
  assert.equal(result.id, "local-file");
});

test("an unauthorized SSE is exposed before a stream starts", async (context) => {
  context.mock.method(globalThis, "fetch", async () => new Response(null, { status: 401 }));
  let opened = false;
  await assert.rejects(readEvents("/events", { onOpen: () => { opened = true; }, onEvent: () => assert.fail("an unauthorized stream must not deliver events") }), (error) => error.status === 401);
  assert.equal(opened, false);
});

test("web transcription posts only an attachment ID with cancellable draft semantics", async (context) => {
  let captured;
  context.mock.method(globalThis, "fetch", async (url, options) => {
    captured = { url, options }; return Response.json({ text: "可编辑", draft: true });
  });
  const controller = new AbortController();
  const result = await transcribeAttachment("fixture-id", { signal: controller.signal });
  assert.equal(captured.url, "/api/media/transcribe");
  assert.equal(captured.options.method, "POST");
  assert.deepEqual(JSON.parse(captured.options.body), { attachment_id: "fixture-id" });
  assert.equal(captured.options.signal, controller.signal);
  assert.equal(result.draft, true);
});
