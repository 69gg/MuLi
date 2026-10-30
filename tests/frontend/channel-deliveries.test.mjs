import test from "node:test";
import assert from "node:assert/strict";
import { ApiError } from "../../web/api.mjs";
import { harness, clickMemoryButton, deferred, flush } from "./helpers/app-dom-harness.mjs";

const scope = { source: "local", coverage: "saved_by_muli", consistency: "live", channel: "onebot", account_id: "bot", owner_id: "7", session_id: "main" };
const item = { id: "exact-delivery-id", kind: "delivery", role: "assistant", state: "unknown", confirmed_parts: 1, total_parts: 2, created_at: 1, attachment_ids: ["exact-attachment-id"] };
const content = "<img src=x onerror=alert(1)>实际发送正文😀";

test("message channel settings open the exact saved delivery and render its text without sending", async () => {
  const h = harness(async (path) => {
    if (path === "/channels/deliveries?channel=onebot&owner_id=7&limit=20") return { ...scope, items: [item], omitted: 0, next_cursor: null, has_more: false };
    if (path === "/channels/deliveries/exact-delivery-id/content?channel=onebot&owner_id=7&part=0&offset_bytes=0") return { ...scope, delivery_id: item.id, part: 0, state: item.state, confirmed_parts: item.confirmed_parts, total_parts: item.total_parts, created_at: item.created_at, attachment_ids: [], revision: "exact-part-revision", field: "content", content, offset_bytes: 0, total_bytes: Buffer.byteLength(content), length_bytes: Buffer.byteLength(content), next_offset_bytes: Buffer.byteLength(content), has_more: false };
    assert.fail(`unexpected request: ${path}`);
  });
  h.ui.state.view = "settings";
  h.ui.state.settings = { channels: { onebot: { enabled: true, owner_ids: ["7"] } } };
  h.ui.state.settingsDraft = structuredClone(h.ui.state.settings);
  h.ui.state.mainSessionId = "main";
  h.ui.state.channels = { channels: { onebot: { enabled: true, active: true, account_id: "bot", owner_count: 1 } }, outbox: { unknown: 1 }, deliveries: [{ id: item.id, name: "onebot", target: "7", state: "unknown", created_at: 1 }] };
  h.ui.useSettingsViews();
  await clickMemoryButton(h, h.button("消息渠道", h.other));
  assert.equal(h.ui.state.settingsTab, "channels");
  assert.ok(h.other.querySelectorAll("summary").some((node) => node.textContent === "最近发送记录"));
  const entry = h.button("查看发送内容", h.other);
  assert.ok(entry, "消息渠道的最近发送记录应提供查看发送内容入口");
  await clickMemoryButton(h, entry);
  await clickMemoryButton(h, h.button("读取记录"));
  const selected = h.dialog.querySelectorAll("[data-channel-delivery-id]").find((node) => node.dataset.channelDeliveryId === item.id);
  await clickMemoryButton(h, selected);
  assert.equal(h.dialog.querySelector("pre").textContent, content);
  assert.equal(h.dialog.querySelectorAll("img").length, 0);
  assert.equal(h.requests.length, 2);
  assert.ok(h.requests.every((request) => request.method === undefined));
});

function directory(items = [item], fields = {}) {
  return { ...scope, items, omitted: 0, next_cursor: null, has_more: false, ...fields };
}

function contentPage(text = content, fields = {}) {
  const offset = fields.offset_bytes ?? 0, total = fields.total_bytes ?? offset + Buffer.byteLength(text);
  return { ...scope, delivery_id: item.id, part: 0, state: item.state, confirmed_parts: item.confirmed_parts, total_parts: item.total_parts, created_at: item.created_at, attachment_ids: [], revision: "exact-part-revision", field: "content", content: text, offset_bytes: offset, total_bytes: total, length_bytes: Buffer.byteLength(text), next_offset_bytes: offset + Buffer.byteLength(text), has_more: offset + Buffer.byteLength(text) < total, ...fields };
}

async function deliveryHarness(handler, { owners = ["7"], draftOwners = owners } = {}) {
  const h = harness(handler);
  h.ui.state.view = "settings";
  h.ui.state.settings = { channels: { onebot: { enabled: true, owner_ids: owners } } };
  h.ui.state.settingsDraft = { channels: { onebot: { enabled: true, owner_ids: draftOwners } } };
  h.ui.state.mainSessionId = "main";
  h.ui.state.channels = { channels: { onebot: { enabled: true, active: true, account_id: "bot", owner_count: owners.length } }, outbox: { unknown: 1 }, deliveries: [{ id: item.id, name: "onebot", target: "7", state: "unknown", created_at: 1 }] };
  h.ui.useSettingsViews();
  await clickMemoryButton(h, h.button("消息渠道", h.other));
  return h;
}

async function readDirectory(h) {
  await clickMemoryButton(h, h.button("查看发送内容", h.other));
  await clickMemoryButton(h, h.button("读取记录"));
}

async function readItem(h, id = item.id) {
  const target = h.dialog.querySelectorAll("[data-channel-delivery-id]").find((node) => node.dataset.channelDeliveryId === id);
  await clickMemoryButton(h, target);
}

function assertOnlyReads(h) {
  assert.ok(h.requests.every((request) => request.method === undefined && request.path.startsWith("/channels/deliveries") && !request.path.includes("/retry")));
}

test("delivery scope uses applied owners while preserving settings and chat drafts", async () => {
  const h = await deliveryHarness(async (path) => path.includes("/content?") ? contentPage() : directory(), { draftOwners: ["UNSAVED_OWNER"] });
  const input = h.document.querySelector("#message-input"); input.value = "UNSENT_CHAT";
  h.ui.state.attachments = [{ id: "unsent-file" }];
  const draft = JSON.stringify(h.ui.state.settingsDraft), attachments = h.ui.state.attachments;
  await clickMemoryButton(h, h.button("查看发送内容", h.other));
  assert.deepEqual(h.dialog.querySelector('[name="delivery-owner"]').children.map((option) => option.value), ["7"]);
  assert.equal(h.requests.length, 0);
  await clickMemoryButton(h, h.button("读取记录")); await readItem(h);
  await clickMemoryButton(h, h.button("返回记录"));
  assert.ok(h.requests.every((request) => request.path.includes("owner_id=7")));
  assert.equal(JSON.stringify(h.ui.state.settingsDraft), draft); assert.equal(input.value, "UNSENT_CHAT");
  assert.equal(h.ui.state.attachments === attachments, true);
  assert.ok(h.button("重试发送", h.other), "既有未知投递的重试入口保持可见");
  assertOnlyReads(h);
});

test("empty delivery pages preserve omitted counts and opaque continuation without retaining older rows", async () => {
  const cursor = 'opaque:[900719925474099312345,"+/="]';
  const h = await deliveryHarness(async (path) => new URL(path, "http://test.invalid").searchParams.has("cursor") ? directory([{ ...item, id: "older-exact-id" }]) : directory([], { omitted: 2, next_cursor: cursor, has_more: true }));
  await readDirectory(h);
  assert.ok(h.body().includes("2 条候选记录")); assert.ok(h.body().includes("仍可手动读取下一页"));
  assert.equal(h.button("下一页").disabled, false);
  await clickMemoryButton(h, h.button("下一页"));
  assert.equal(new URL(h.requests.at(-1).path, "http://test.invalid").searchParams.get("cursor"), cursor);
  assert.equal(h.ui.channelDeliverySlot.page.items.length, 1);
  assert.deepEqual(h.dialog.querySelectorAll("[data-channel-delivery-id]").map((node) => node.dataset.channelDeliveryId), ["older-exact-id"]);
  await clickMemoryButton(h, h.button("上一页"));
  assert.equal(h.dialog.querySelectorAll("[data-channel-delivery-id]").length, 0);
  assert.equal(h.body().includes("older-exact-id"), false); assert.equal(h.requests.at(-1).path, h.requests[0].path);
  assertOnlyReads(h);
});

test("UTF-8 windows replace text and retain the exact part revision while switching part restarts at zero", async () => {
  const first = "首段😀", second = "<script>后文</script>", offset = Buffer.byteLength(first), total = offset + Buffer.byteLength(second);
  const h = await deliveryHarness(async (path) => {
    if (!path.includes("/content?")) return directory();
    const query = new URL(path, "http://test.invalid").searchParams;
    if (query.get("part") === "1") return contentPage("", { part: 1, attachment_ids: ["exact-attachment-id"], revision: "attachment-revision" });
    return query.get("offset_bytes") === String(offset) ? contentPage(second, { offset_bytes: offset, total_bytes: total }) : contentPage(first, { total_bytes: total });
  });
  await readDirectory(h); await readItem(h);
  const slot = h.ui.channelDeliverySlot;
  assert.equal(slot.page === null, true); assert.equal(h.dialog.querySelector("pre").textContent, first);
  assert.ok(h.dialog.querySelector('[aria-label="发送分段"]').textContent.includes("分段 1/2 · 平台已确认"));
  h.document.querySelector("#dialog-content").scrollTop = 6000;
  await clickMemoryButton(h, h.button("下一窗口"));
  assert.equal(h.document.querySelector("#dialog-content").scrollTop, 0);
  assert.equal(h.requests.at(-1).path, `/channels/deliveries/${item.id}/content?channel=onebot&owner_id=7&part=0&offset_bytes=${offset}&revision=exact-part-revision`);
  assert.equal(h.dialog.querySelectorAll("pre").length, 1); assert.equal(h.dialog.querySelector("pre").textContent, second);
  assert.equal(h.body().includes(first), false); assert.equal(h.dialog.querySelectorAll("script").length, 0);
  await clickMemoryButton(h, h.button("上一窗口"));
  assert.ok(h.requests.at(-1).path.endsWith("offset_bytes=0&revision=exact-part-revision"));
  await clickMemoryButton(h, h.button("下一分段"));
  assert.equal(h.requests.at(-1).path, `/channels/deliveries/${item.id}/content?channel=onebot&owner_id=7&part=1&offset_bytes=0`);
  assert.equal(h.dialog.querySelector("pre") === null, true); assert.ok(h.body().includes("附件引用：exact-attachment-id"));
  assert.ok(h.body().includes("当前页面未载入此附件的元信息")); assert.equal(h.body().includes("已删除"), false);
  assert.ok(h.dialog.querySelector('[aria-label="发送分段"]').textContent.includes("分段 2/2 · 结果未知"));
  const back = h.button("返回记录"); back.focus();
  await clickMemoryButton(h, back);
  assert.equal(slot.selected === null, true); assert.equal(slot.bodyPage === null, true);
  assert.equal(h.document.activeElement.dataset.channelDeliveryId, item.id); assertOnlyReads(h);
});

test("confirmation checkpoints distinguish the unknown part and never autoplay known attachments", async () => {
  const three = { ...item, total_parts: 3 };
  const h = await deliveryHarness(async (path) => {
    if (!path.includes("/content?")) return directory([three]);
    const part = Number(new URL(path, "http://test.invalid").searchParams.get("part"));
    return part === 1 ? contentPage("", { part, total_parts: 3, attachment_ids: ["exact-attachment-id"] }) : contentPage("文字", { part, total_parts: 3 });
  });
  h.ui.state.uploads = [{ id: "exact-attachment-id", name: "已知声音.wav", size: 12, mime_type: "audio/wav", kind: "audio" }];
  await readDirectory(h); await readItem(h);
  assert.ok(h.dialog.querySelector('[aria-label="发送分段"]').textContent.includes("平台已确认"));
  await clickMemoryButton(h, h.button("下一分段"));
  assert.ok(h.dialog.querySelector('[aria-label="发送分段"]').textContent.includes("结果未知"));
  assert.equal(h.dialog.querySelectorAll("audio,video,img").length, 0);
  assert.ok(h.dialog.querySelector('a[download]').getAttribute("href").includes("/uploads/exact-attachment-id/download"));
  await clickMemoryButton(h, h.button("下一分段"));
  assert.ok(h.dialog.querySelector('[aria-label="发送分段"]').textContent.includes("尚未确认"));
  assert.ok(h.body().includes("平台已确认不表示已读或已播放")); assertOnlyReads(h);
});

test("directory responses reject wrong scopes, duplicate identities and malformed pagination", async () => {
  for (const response of [directory([item], { channel: "telegram" }), directory([item], { owner_id: "8" }), directory([item], { account_id: "other" }), directory([item], { session_id: "other" }), directory([item, item]), directory([], { has_more: true }), directory([item], { omitted: -1 }), directory([item], { next_cursor: "unexpected" })]) {
    const h = await deliveryHarness(async () => response); await readDirectory(h);
    assert.equal(h.ui.channelDeliverySlot.page === null, true); assert.ok(h.dialog.querySelector('[role="alert"]'));
    assert.equal(h.dialog.querySelectorAll("[data-channel-delivery-id]").length, 0);
    assert.equal(h.button("下一页") === undefined, true); assert.equal(h.requests.length, 1);
  }
});

test("body responses reject another delivery, part, scope and invalid UTF-8 byte windows", async () => {
  for (const fields of [{ delivery_id: "another-id" }, { part: 1 }, { channel: "telegram" }, { owner_id: "8" }, { account_id: "another-account" }, { session_id: "another-main" }, { field: "payload" }, { offset_bytes: 1 }, { length_bytes: 1 }, { next_offset_bytes: 1 }, { total_bytes: 1 }, { has_more: true }, { content: "", length_bytes: 0, next_offset_bytes: 0, total_bytes: 1, has_more: true }, { confirmed_parts: 3 }]) {
    const h = await deliveryHarness(async (path) => path.includes("/content?") ? contentPage(content, fields) : directory());
    await readDirectory(h); await readItem(h);
    assert.equal(h.ui.channelDeliverySlot.bodyPage === null, true); assert.equal(h.dialog.querySelector("pre") === null, true);
    assert.ok(h.body().includes("分段正文响应不一致")); assertOnlyReads(h);
  }
});

test("continuation rejects a changed revision or total without retaining the earlier text", async () => {
  for (const fields of [{ revision: "other-revision" }, { total_bytes: 99 }]) {
    let reads = 0;
    const h = await deliveryHarness(async (path) => !path.includes("/content?") ? directory() : reads++ === 0 ? contentPage("first", { total_bytes: 10 }) : contentPage("after", { offset_bytes: 5, total_bytes: 10, ...fields }));
    await readDirectory(h); await readItem(h); await clickMemoryButton(h, h.button("下一窗口"));
    assert.equal(h.dialog.querySelector("pre") === null, true); assert.equal(h.body().includes("first"), false);
    assert.ok(h.dialog.querySelector('[role="alert"]')); assert.equal(h.requests.length, 3);
  }
});

test("403, 404 and 409 clear retained text and require a fresh selection", async () => {
  for (const status of [403, 404, 409]) {
    let reads = 0;
    const h = await deliveryHarness(async (path) => {
      if (!path.includes("/content?")) return directory();
      if (reads++ === 0) return contentPage("SECRET", { total_bytes: 12 });
      throw new ApiError("explicit read failure", status);
    });
    await readDirectory(h); await readItem(h); await clickMemoryButton(h, h.button("下一窗口"));
    assert.equal(h.dialog.querySelector("pre") === null, true); assert.equal(h.body().includes("SECRET"), false);
    assert.ok(h.dialog.querySelector('[role="alert"]')); assert.equal(h.button("重试读取") === undefined, true);
    assert.equal(h.body().includes("已到本分段末尾"), false);
    assert.equal(h.ui.channelDeliverySlot.revision === null, true); assertOnlyReads(h);
  }
});

test("network failures retry the exact byte offset and revision, while directory failures discard old rows", async () => {
  let reads = 0, failDirectory = false;
  const h = await deliveryHarness(async (path) => {
    if (!path.includes("/content?")) { if (failDirectory) throw new ApiError("目录暂时离线"); return directory(); }
    if (reads++ === 0) return contentPage("first", { total_bytes: 10 });
    if (reads === 2) throw new ApiError("正文暂时离线");
    return contentPage("after", { offset_bytes: 5, total_bytes: 10 });
  });
  await readDirectory(h); await readItem(h); await clickMemoryButton(h, h.button("下一窗口"));
  const failedPath = h.requests.at(-1).path;
  assert.equal(h.dialog.querySelector("pre") === null, true); assert.ok(h.body().includes("正文暂时离线"));
  await clickMemoryButton(h, h.button("重试读取"));
  assert.equal(h.requests.at(-1).path, failedPath); assert.equal(h.dialog.querySelector("pre").textContent, "after");
  failDirectory = true; await clickMemoryButton(h, h.button("返回记录"));
  assert.equal(h.ui.channelDeliverySlot.page === null, true); assert.equal(h.body().includes(item.id), false);
  assert.ok(h.body().includes("目录暂时离线")); assert.equal(h.body().includes("本次查询已到末页"), false);
  const failedDirectory = h.requests.at(-1).path; failDirectory = false; await clickMemoryButton(h, h.button("重试读取"));
  assert.equal(h.requests.at(-1).path, failedDirectory); assertOnlyReads(h);
});

test("switching owners rejects an old 401 even when its request ignores abort", async () => {
  const old = deferred();
  const h = await deliveryHarness(async (path) => {
    const owner = new URL(path, "http://test.invalid").searchParams.get("owner_id");
    if (!path.includes("/content?")) return directory([{ ...item, id: owner === "7" ? item.id : "owner-8-id" }], { owner_id: owner });
    return owner === "7" ? old.promise : contentPage("OWNER_8_TEXT", { owner_id: "8", delivery_id: "owner-8-id" });
  }, { owners: ["7", "8"] });
  await readDirectory(h); await readItem(h);
  const oldSignal = h.requests.at(-1).signal, owner = h.dialog.querySelector('[name="delivery-owner"]'); owner.value = "8";
  await owner.dispatch("change"); assert.equal(oldSignal.aborted, true);
  assert.equal(h.ui.channelDeliverySlot.page === null, true); assert.equal(h.ui.channelDeliverySlot.bodyPage === null, true);
  await clickMemoryButton(h, h.button("读取记录")); await readItem(h, "owner-8-id");
  old.reject(new ApiError("old unauthorized", 401)); await flush();
  assert.equal(h.ui.state.auth.authenticated, true); assert.equal(h.dialog.open, true);
  assert.equal(h.dialog.querySelector("pre").textContent, "OWNER_8_TEXT");
  assert.equal(h.ui.channelDeliverySlot.scope.ownerId, "8"); assertOnlyReads(h);
});

test("close and cancel synchronously release data and suppress a late response in a replacement dialog", async () => {
  for (const close of ["button", "cancel"]) {
    const gate = deferred(), h = await deliveryHarness(async () => gate.promise); await readDirectory(h);
    const slot = h.ui.channelDeliverySlot, signal = h.requests[0].signal;
    const closing = close === "button" ? h.ui.handleAction("close-dialog", h.document.querySelector("#dialog-close")) : h.dialog.dispatch("cancel");
    assert.equal(signal.aborted, true); assert.equal(h.ui.channelDeliverySlot === null, true);
    assert.equal(slot.scope === null, true); assert.equal(slot.page === null, true); assert.equal(slot.bodyPage === null, true);
    await closing;
    const replacement = h.document.createElement("p"); replacement.textContent = "REPLACEMENT";
    h.ui.openDialog({ title: "另一个窗口", content: replacement, readOnly: true });
    gate.reject(new ApiError("late unauthorized", 401)); await flush();
    assert.equal(h.ui.state.auth.authenticated, true); assert.equal(h.body(), "REPLACEMENT"); assert.equal(h.dialog.open, true);
  }
});

test("applied binding, status account and main-session changes invalidate the old scope", async () => {
  const changes = [
    (h) => { h.ui.state.settings.channels.onebot.owner_ids = ["8"]; },
    (h) => { h.ui.state.settings.channels.onebot.enabled = false; },
    (h) => { h.ui.state.settings.channels.onebot.base_url = "http://new.invalid"; },
    (h) => { h.ui.state.channels.channels.onebot.account_id = "other-account"; },
    (h) => { h.ui.state.mainSessionId = "other-main"; },
  ];
  for (const change of changes) {
    const gate = deferred(), h = await deliveryHarness(async () => gate.promise); await readDirectory(h);
    change(h); h.ui.useSettingsViews();
    assert.equal(h.requests[0].signal.aborted, true); assert.equal(h.ui.channelDeliverySlot === null, true); assert.equal(h.dialog.open, false);
    gate.resolve(directory()); await flush();
    assert.equal(h.dialog.querySelector("pre") === null, true); assert.equal(h.body().includes(item.id), false);
  }
});

test("leaving the tab, view, page or visibility closes a pending delivery read", async () => {
  for (const leave of ["tab", "view", "page", "hidden"]) {
    const gate = deferred(), h = await deliveryHarness(async () => gate.promise); await readDirectory(h);
    if (leave === "tab") await clickMemoryButton(h, h.button("概览", h.other));
    else if (leave === "view") await h.ui.switchView("chat");
    else if (leave === "page") h.windowEvents.pagehide();
    else { h.document.visibilityState = "hidden"; for (const callback of h.document.events.visibilitychange) callback(); }
    assert.equal(h.requests[0].signal.aborted, true); assert.equal(h.ui.channelDeliverySlot === null, true); assert.equal(h.dialog.open, false);
    gate.resolve(directory()); await flush(); assert.equal(h.body().includes(item.id), false);
  }
});

test("channel events only mark manual refresh and preserve the current body, focus and drafts", async () => {
  const h = await deliveryHarness(async (path) => path.includes("/content?") ? contentPage() : directory());
  h.ui.startGlobalEvents(); await readDirectory(h); await readItem(h);
  const input = h.document.querySelector("#message-input"); input.value = "KEEP_DRAFT";
  const settings = JSON.stringify(h.ui.state.settingsDraft), body = h.dialog.querySelector("pre"), slot = h.ui.channelDeliverySlot;
  const target = h.button("下一分段"); target.focus(); const before = h.requests.length;
  h.emitGlobal({ id: "channel-event", type: "channel.updated", data: { name: "onebot" } }); await flush();
  assert.equal(h.requests.length, before); assert.equal(h.ui.channelDeliverySlot === slot, true);
  assert.equal(h.dialog.querySelector("pre") === body, true); assert.equal(h.document.activeElement === target, true);
  assert.equal(slot.dirtyNotice.hidden, false); assert.equal(input.value, "KEEP_DRAFT"); assert.equal(JSON.stringify(h.ui.state.settingsDraft), settings);
  h.ui.stopEvents(); assertOnlyReads(h);
});
