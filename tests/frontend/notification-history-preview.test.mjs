import test from "node:test";
import assert from "node:assert/strict";
import { createPreviewBackend, createPreviewState } from "../../web/preview.mjs";

async function read(backend, path, options) {
  const response = await backend.fetch(`/api${path}`, options);
  return { status: response.status, body: await response.json() };
}

const directoryPath = "/notification-directory?status=all&limit=20";
const queryPath = (id, fields = {}) => `/notifications/${encodeURIComponent(id)}?${new URLSearchParams({ offset_bytes: "0", ...fields })}`;
const notice = (id, fields = {}) => ({ id, title: `本页模拟 ${id}`, source: "preview", content: "本页内存保存的通知原文🙂，没有真实执行或外发。", read: false, created_at: 100, ...fields });

test("preview notification history is an explicit memory simulation with exact metadata and readonly full JSON", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  const before = structuredClone({ notifications: backend.state.notifications, messages: backend.state.messages, runs: backend.state.runs, events: backend.state.events });
  const listing = await read(backend, directoryPath);
  assert.equal(listing.status, 200); assert.equal(listing.body.preview, true);
  assert.equal(listing.body.consistency, "bounded_live"); assert.equal(listing.body.items.length, 20);
  assert.equal(listing.body.has_more, true);
  const first = listing.body.items[0];
  assert.deepEqual(Object.keys(first).sort(), ["created_at", "id", "read", "revision", "source", "title"]);
  const detail = await read(backend, queryPath(first.id, { revision: first.revision }));
  assert.equal(detail.status, 200); assert.equal(detail.body.preview, true);
  assert.equal(detail.body.format, "json"); assert.equal(detail.body.has_more, false);
  assert.equal(detail.body.length_bytes, Buffer.byteLength(detail.body.content));
  assert.deepEqual(JSON.parse(detail.body.content), before.notifications.find((item) => item.id === first.id));
  assert.deepEqual({ notifications: backend.state.notifications, messages: backend.state.messages, runs: backend.state.runs, events: backend.state.events }, before);
  assert.ok(backend.state.requests.every((request) => request.method === "GET"));
});

test("preview history reaches records outside the recent 200 and seek cursors survive a read change or newer insert", async (t) => {
  const state = createPreviewState();
  state.notifications = Array.from({ length: 205 }, (_, index) => notice(`saved-${String(index).padStart(3, "0")}`, { created_at: 1000 - Math.floor(index / 2) }));
  const backend = createPreviewBackend({ state }); t.after(() => backend.dispose());
  assert.equal((await read(backend, "/notifications")).body.items.length, 200);
  let result = await read(backend, directoryPath), ids = result.body.items.map((item) => item.id);
  const firstCursor = result.body.next_cursor, firstIds = [...ids];
  const marked = await read(backend, `/notifications/${encodeURIComponent(firstIds[0])}`, { method: "PATCH", body: JSON.stringify({ read: true }) });
  assert.equal(marked.status, 200);
  state.notifications.unshift(notice("new-head", { created_at: 2000 }));
  for (let index = 0; index < 15 && result.body.has_more; index++) {
    result = await read(backend, `${directoryPath}&cursor=${encodeURIComponent(result.body.next_cursor)}`);
    assert.equal(result.status, 200); ids.push(...result.body.items.map((item) => item.id));
  }
  assert.equal(result.body.has_more, false); assert.equal(ids.length, 205); assert.equal(new Set(ids).size, 205);
  assert.ok(ids.includes("saved-204")); assert.equal(ids.includes("new-head"), false);
  const repeated = await read(backend, `${directoryPath}&cursor=${encodeURIComponent(firstCursor)}`);
  assert.deepEqual(repeated.body.items.map((item) => item.id), ids.slice(20, 40));
  const refreshed = await read(backend, directoryPath);
  assert.equal(refreshed.body.items[0].id, "new-head");
});

test("preview unread scan can return an empty page with omissions and a real continuation", async (t) => {
  const state = createPreviewState(); state.settings.limits.max_search_results = 2;
  state.notifications = [notice("invalid", { title: 123, created_at: 400 }), notice("too-large", { content: "字".repeat(23000), created_at: 300 }), notice("unknown-read", { read: "false", created_at: 200 }), notice("old-unread", { created_at: undefined })];
  const backend = createPreviewBackend({ state }); t.after(() => backend.dispose());
  const first = await read(backend, "/notification-directory?status=unread&limit=20");
  assert.equal(first.status, 200); assert.equal(first.body.items.length, 0);
  assert.equal(first.body.scanned, 2); assert.deepEqual(first.body.omitted, { invalid: 1, oversized: 1, status_unknown: 0 });
  assert.equal(first.body.has_more, true); assert.equal(first.body.stopped, "candidate_budget");
  const second = await read(backend, `/notification-directory?status=unread&limit=20&cursor=${encodeURIComponent(first.body.next_cursor)}`);
  assert.equal(second.status, 200); assert.equal(second.body.items[0].id, "old-unread");
  assert.equal(second.body.items[0].created_at, null); assert.equal(second.body.omitted.status_unknown, 1);
  assert.equal(second.body.has_more, false);
  assert.equal((await read(backend, queryPath("too-large"))).body.detail.code, "notification_record_oversized");
});

test("preview detail preserves UTF-8 byte windows and makes read mutations invalidate the exact raw revision", async (t) => {
  const state = createPreviewState(), item = notice("utf8-record", { content: "原文🙂中段\n".repeat(400) });
  state.notifications = [item]; state.settings.limits.max_output_chars = 900;
  const backend = createPreviewBackend({ state }); t.after(() => backend.dispose());
  const first = await read(backend, queryPath(item.id));
  assert.equal(first.status, 200); assert.equal(first.body.has_more, true);
  let page = first.body, content = page.content;
  const revision = page.revision;
  for (let index = 0; index < 100 && page.has_more; index++) {
    const next = await read(backend, queryPath(item.id, { offset_bytes: String(page.next_offset_bytes), revision }));
    assert.equal(next.status, 200); assert.equal(next.body.offset_bytes, page.next_offset_bytes);
    assert.equal(next.body.revision, revision); assert.equal(next.body.length_bytes, Buffer.byteLength(next.body.content));
    assert.ok(next.body.next_offset_bytes > page.next_offset_bytes);
    page = next.body; content += page.content;
  }
  assert.equal(page.has_more, false); assert.equal(content, JSON.stringify(item));
  await read(backend, `/notifications/${item.id}`, { method: "PATCH", body: JSON.stringify({ read: true }) });
  const changed = await read(backend, queryPath(item.id, { revision }));
  assert.equal(changed.status, 409); assert.equal(changed.body.detail.code, "evidence_changed");
  assert.equal((await read(backend, queryPath(item.id))).status, 200);
});

test("preview binds cursor scope and filter and rejects invalid offsets or missing records explicitly", async (t) => {
  const backend = createPreviewBackend(), other = createPreviewBackend();
  t.after(() => { backend.dispose(); other.dispose(); });
  const listing = await read(backend, directoryPath), cursor = encodeURIComponent(listing.body.next_cursor);
  assert.equal((await read(backend, `/notification-directory?status=unread&limit=20&cursor=${cursor}`)).body.detail.code, "notification_cursor_invalid");
  assert.equal((await read(other, `${directoryPath}&cursor=${cursor}`)).body.detail.code, "notification_cursor_invalid");
  assert.equal((await read(backend, `${directoryPath}&cursor=broken`)).status, 400);
  assert.equal((await read(backend, queryPath("missing"))).status, 404);
  const item = backend.state.notifications[0]; item.content = "🙂";
  const full = await read(backend, queryPath(item.id));
  const inside = Buffer.from(JSON.stringify(item)).indexOf(Buffer.from("🙂")) + 1;
  assert.equal((await read(backend, queryPath(item.id, { offset_bytes: String(inside), revision: full.body.revision }))).body.detail.code, "invalid_offset");
  assert.equal((await read(backend, queryPath(item.id, { offset_bytes: "1" }))).status, 422);
  assert.equal((await read(backend, `${directoryPath}&status=unread`)).status, 422);
});

test("preview insufficient output budget stays an error instead of an empty or complete-looking response", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  backend.state.settings.limits.max_output_chars = 10;
  for (const path of [directoryPath, queryPath(backend.state.notifications[0].id)]) {
    const result = await read(backend, path);
    assert.equal(result.status, 400); assert.equal(result.body.detail.code, "output_budget");
    assert.equal(Object.hasOwn(result.body, "items"), false); assert.equal(Object.hasOwn(result.body, "content"), false);
  }
});
