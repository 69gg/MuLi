import test from "node:test";
import assert from "node:assert/strict";
import { createPreviewBackend } from "../../web/preview.mjs";

async function read(backend, path) {
  const response = await backend.fetch(`/api${path}`);
  return { status: response.status, body: await response.json() };
}

const listPath = "/channels/deliveries?channel=onebot&owner_id=7&limit=20";
const bodyPath = "/channels/deliveries/preview-delivery/content?channel=onebot&owner_id=7&part=0&offset_bytes=0";

test("preview delivery records are explicit local simulations and do not send or create chat state", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  const before = structuredClone({ settings: backend.state.settings, records: backend.state.channelDeliveries, messages: backend.state.messages, runs: backend.state.runs, events: backend.state.events });
  const listing = await read(backend, listPath);
  assert.equal(listing.status, 200); assert.equal(listing.body.preview, true);
  assert.equal(listing.body.channel, "onebot"); assert.equal(listing.body.owner_id, "7");
  assert.equal(listing.body.account_id, "preview-bot"); assert.equal(listing.body.session_id, "preview-main");
  assert.deepEqual(Object.keys(listing.body.items[0]).sort(), ["attachment_ids", "confirmed_parts", "created_at", "id", "kind", "role", "state", "total_parts"]);
  const text = await read(backend, bodyPath);
  assert.equal(text.status, 200); assert.equal(text.body.delivery_id, "preview-delivery"); assert.equal(text.body.part, 0);
  assert.match(text.body.content, /本页模拟保存/); assert.equal(text.body.length_bytes, Buffer.byteLength(text.body.content));
  const attachment = await read(backend, bodyPath.replace("part=0", "part=1"));
  assert.equal(attachment.status, 200); assert.equal(attachment.body.content, "");
  assert.deepEqual(attachment.body.attachment_ids, listing.body.items[0].attachment_ids);
  assert.equal(attachment.body.length_bytes, 0); assert.equal(attachment.body.has_more, false);
  assert.deepEqual({ settings: backend.state.settings, records: backend.state.channelDeliveries, messages: backend.state.messages, runs: backend.state.runs, events: backend.state.events }, before);
  assert.ok(backend.state.requests.every((request) => request.method === "GET"));
  assert.equal((await read(backend, "/channels")).body.preview, true);
});

test("preview UTF-8 windows retain content revision across confirmation changes and reject changed text", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  const record = backend.state.channelDeliveries[0], original = "核查🙂\n".repeat(200);
  record.parts[0].text = original; backend.state.settings.limits.max_output_chars = 900;
  const first = await read(backend, bodyPath); assert.equal(first.status, 200); assert.equal(first.body.has_more, true);
  const revision = first.body.revision; let text = first.body.content, page = first.body;
  record.state = "sent"; record.confirmed_parts = record.parts.length;
  for (let index = 0; index < 30 && page.has_more; index++) {
    const result = await read(backend, `/channels/deliveries/preview-delivery/content?${new URLSearchParams({ channel: "onebot", owner_id: "7", part: "0", offset_bytes: String(page.next_offset_bytes), revision })}`);
    assert.equal(result.status, 200); assert.equal(result.body.revision, revision); assert.equal(result.body.state, "sent");
    assert.equal(result.body.offset_bytes, page.next_offset_bytes); assert.equal(result.body.length_bytes, Buffer.byteLength(result.body.content));
    assert.ok(result.body.next_offset_bytes > page.next_offset_bytes); text += result.body.content; page = result.body;
  }
  assert.equal(page.has_more, false); assert.equal(text, original);
  record.parts[0].text += "变化";
  assert.equal((await read(backend, `${bodyPath}&revision=${encodeURIComponent(revision)}`)).status, 409);
  assert.equal((await read(backend, bodyPath.replace("offset_bytes=0", "offset_bytes=1"))).status, 400);
  assert.ok(backend.state.requests.every((request) => request.method === "GET"));
});

test("preview continuation binds the exact active scope and keeps forbidden or missing reads explicit", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  const record = backend.state.channelDeliveries[0]; backend.state.channelDeliveries.push({ ...structuredClone(record), id: "preview-older" });
  const first = await read(backend, listPath.replace("limit=20", "limit=1"));
  assert.equal(first.status, 200); assert.equal(first.body.has_more, true);
  const cursorPath = `${listPath.replace("limit=20", "limit=1")}&cursor=${encodeURIComponent(first.body.next_cursor)}`;
  const next = await read(backend, cursorPath);
  assert.equal(next.status, 200); assert.equal(next.body.items[0].id, "preview-older"); assert.equal(next.body.has_more, false);
  assert.equal((await read(backend, `${listPath}&cursor=broken`)).status, 400);
  assert.equal((await read(backend, bodyPath.replace("preview-delivery", "missing"))).status, 404);
  backend.state.settings.channels.onebot.owner_ids.push("8");
  assert.equal((await read(backend, cursorPath.replace("owner_id=7", "owner_id=8"))).status, 400);
  assert.equal((await read(backend, bodyPath.replace("owner_id=7", "owner_id=8"))).status, 404);
  backend.state.settings.channels.onebot.account_id = "new-account";
  assert.equal((await read(backend, cursorPath)).status, 400); assert.equal((await read(backend, bodyPath)).status, 404);
  backend.state.settings.channels.onebot.enabled = false;
  assert.equal((await read(backend, listPath)).status, 403); assert.equal((await read(backend, bodyPath)).status, 403);
  assert.ok(backend.state.requests.every((request) => request.method === "GET"));
});
