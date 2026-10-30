import test from "node:test";
import assert from "node:assert/strict";
import { ApiError } from "../../web/api.mjs";
import { clickMemoryButton, deferred, flush, harness } from "./helpers/app-dom-harness.mjs";

// Run the real notification renderer, click handlers and owned requests without a
// browser, model, account or real database. Identity assertions stay boolean so
// an expected failure never recursively expands the mock DOM graph.
const notice = (id = "older-than-200", fields = {}) => ({ id, title: `保存的通知 ${id}`, source: "assistant", read: false, created_at: 100, revision: "a".repeat(64), ...fields });
const directory = (items = [notice()], fields = {}) => ({ items, has_more: false, next_cursor: null, scanned: items.length, json_bytes: 128, omitted: { oversized: 0, invalid: 0, status_unknown: 0 }, consistency: "bounded_live", stopped: fields.has_more ? "limit" : "end", ...fields });
const query = (path) => new URLSearchParams(path.split("?")[1]);
const directoryReads = (h) => h.requests.filter((entry) => entry.path.startsWith("/notification-directory?"));
const writes = (h) => h.requests.filter((entry) => ["POST", "PATCH", "DELETE"].includes(entry.method));

async function historyHarness(respond = async () => undefined, { open = true } = {}) {
  const h = harness(async (path, options) => {
    if (path === "/notifications") return { items: [notice("recent-window", { content: "只在最近汇总里的通知" })] };
    if (path === "/reminders" || path === "/approvals") return { items: [] };
    return respond(path, options);
  }, { composer: true });
  h.ui.state.connected = true; h.ui.state.loading = false;
  h.ui.state.status = { configured: true, model: "fixture-model" };
  h.ui.state.mainSessionId = "main"; h.ui.state.activeSession = { id: "main", mode: "auto", status: "idle" };
  h.ui.useNotificationViews();
  if (open) await h.ui.switchView("notifications");
  return h;
}

test("the inbox reads a stable history directory independently of the recent notification summary", async () => {
  const h = await historyHarness(async (path) => {
    if (path.startsWith("/notification-directory?")) return directory();
    throw new Error(`unexpected fixture read: ${path}`);
  });
  assert.equal(h.ui.state.viewError, "", "先证明真实通知页成功打开");
  assert.ok(h.other.textContent.includes("保存的通知 older-than-200"), "历史通知应可从独立目录找回");
  assert.equal(h.other.textContent.includes("只在最近汇总里的通知"), false);
  assert.equal(directoryReads(h).length, 1);
  assert.equal(query(directoryReads(h)[0].path).get("status"), "all");
  assert.equal(query(directoryReads(h)[0].path).get("limit"), "20");
  assert.equal(directoryReads(h)[0].cache, "no-store");
  assert.equal(h.ui.state.notifications[0].id, "recent-window", "历史目录不能覆盖最近汇总");
  assert.equal(writes(h).length, 0, "阅读目录不标已读，也不执行任何操作");
});

test("explicit history pages keep opaque cursors and allow empty scanned pages to continue", async () => {
  const cursor = "opaque+/=旧🙂";
  const h = await historyHarness(async (path) => {
    if (!path.startsWith("/notification-directory?")) throw new Error(`unexpected ${path}`);
    return query(path).has("cursor") ? directory([notice("oldest")]) : directory([], { scanned: 3, omitted: { oversized: 1, invalid: 1, status_unknown: 1 }, has_more: true, next_cursor: cursor, stopped: "candidate_budget" });
  });
  assert.equal(directoryReads(h).length, 1, "不能自动追赶空页的续页游标");
  assert.ok(h.other.textContent.includes("未能显示 3 条"));
  assert.equal(h.other.textContent.includes("通知已清空"), false);
  await clickMemoryButton(h, h.button("更早一页", h.other));
  assert.equal(query(directoryReads(h).at(-1).path).get("cursor"), cursor);
  assert.ok(h.other.textContent.includes("保存的通知 oldest"));
  assert.equal(h.button("更早一页", h.other).disabled, true);
  assert.equal(h.button("较新一页", h.other).disabled, false);
});

test("filter changes cancel the previous directory read and a late old 401 cannot log out the new filter", async () => {
  const old = deferred();
  const h = await historyHarness(async (path) => {
    if (!path.startsWith("/notification-directory?")) throw new Error(`unexpected ${path}`);
    if (query(path).has("cursor")) return old.promise;
    return query(path).get("status") === "unread" ? directory([notice("unread-page")], { status: "unread" }) : directory([notice()], { has_more: true, next_cursor: "old-page" });
  });
  assert.equal(h.button("更早一页", h.other).disabled, false, "先证明首个合法目录包允许续页");
  await clickMemoryButton(h, h.button("更早一页", h.other));
  assert.equal(directoryReads(h).length, 2, "先证明旧页请求确实仍在等待");
  const oldRequest = directoryReads(h).at(-1);
  const select = h.other.querySelector('[aria-label="通知筛选"]');
  assert.ok(select); select.value = "unread"; await select.dispatch("change"); await flush();
  assert.equal(oldRequest.signal.aborted, true);
  assert.ok(h.other.textContent.includes("保存的通知 unread-page"));
  old.reject(new ApiError("late old notification 401", 401)); await flush();
  assert.equal(h.ui.state.auth.authenticated, true);
  assert.ok(h.other.textContent.includes("保存的通知 unread-page"));
});

const record = (fields = {}) => ({ id: "older-than-200", title: "原提醒完整标题", content: "完整原文开始🙂\nFULL_BODY_MIDDLE_不能省略\n<script>只是通知正文</script>\n完整原文末尾", source: "assistant", read: false, created_at: 100, session_id: "origin-session", reminder_id: "exact-reminder-id", routing: { kind: "registered_channel", channel: "onebot" }, ...fields });
function contentPage(value = record(), offset = 0, { take = Infinity, revision = "a".repeat(64), ...fields } = {}) {
  const raw = Buffer.from(JSON.stringify(value), "utf8"); let end = Math.min(raw.length, offset + take);
  while (end < raw.length && (raw[end] & 0xc0) === 0x80) end -= 1;
  return { id: value.id, revision, format: "json", field: "content", content: raw.subarray(offset, end).toString("utf8"), offset_bytes: offset, length_bytes: end - offset, total_bytes: raw.length, next_offset_bytes: end, has_more: end < raw.length, ...fields };
}

async function openDetail(h, id = "older-than-200") {
  const button = h.other.querySelectorAll("[data-notification-id]").find((entry) => entry.dataset.notificationId === id && entry.textContent === "核对原文");
  assert.ok(button, "每条准确通知应有按需核查原文入口");
  await clickMemoryButton(h, button);
}

test("notification detail is lazy and parses only after every same-revision UTF-8 JSON page arrives", async () => {
  const value = record(), seen = [];
  const h = await historyHarness(async (path) => {
    if (path.startsWith("/notification-directory?")) return directory();
    const params = query(path); seen.push(params);
    assert.equal(path.split("?")[0], "/notifications/older-than-200");
    return contentPage(value, Number(params.get("offset_bytes") ?? 0), { take: params.get("offset_bytes") === "0" ? 80 : Infinity });
  });
  assert.equal(seen.length, 0);
  await openDetail(h);
  assert.equal(seen.length, 1); assert.equal(seen[0].get("revision"), "a".repeat(64));
  assert.equal(h.body().includes(value.content), false, "残缺JSON不当作完整通知正文");
  assert.equal(h.button("带着通知提问") === undefined, true);
  assert.ok(h.body().includes("尚未读完完整通知"));
  await clickMemoryButton(h, h.button("继续读取通知"));
  assert.equal(seen.length, 2); assert.equal(seen[1].get("revision"), seen[0].get("revision"));
  assert.ok(Number(seen[1].get("offset_bytes")) > 0);
  assert.ok(h.body().includes(value.content)); assert.ok(h.body().includes(value.reminder_id));
  assert.equal(h.dialog.querySelector(".notification-detail").childNodes.some((node) => node.textContent === "false"), false, "单个正文字段不能把可选标题条件作为false文本追加到原生DOM");
  assert.ok(h.body().includes("已读不代表已处理"));
  assert.ok(h.button("带着通知提问")); assert.equal(h.dialog.querySelector("script"), null);
  assert.equal(writes(h).length, 0);
});

test("complete notification text and primary actions precede the collapsed source metadata", async () => {
  const value = record();
  const h = await historyHarness(async (path) => path.startsWith("/notification-directory?") ? directory() : contentPage(value));
  await openDetail(h);
  const root = h.dialog.querySelector(".notification-detail"), metadata = root.querySelector("details.notification-origin");
  const body = root.children.find((node) => node.tagName === "PRE" && node.textContent === value.content);
  const question = h.button("带着通知提问"), session = h.button("查看原会话");
  const actions = root.children.find((node) => node.contains(question) && node.contains(session));
  assert.ok(body); assert.ok(actions); assert.ok(metadata);
  assert.equal(root.firstElementChild.tagName, "H3"); assert.equal(root.firstElementChild.textContent, value.title);
  assert.equal(metadata.open, false, "来源与完整记录默认折叠");
  assert.ok(root.children.indexOf(body) < root.children.indexOf(actions));
  assert.ok(root.children.indexOf(actions) < root.children.indexOf(metadata), "正文后即可进行主要操作，不需先滚过ID、hash与来源说明");
  assert.ok(metadata.textContent.includes(value.id)); assert.ok(metadata.textContent.includes("a".repeat(64)));
  assert.ok(metadata.textContent.includes(value.reminder_id)); assert.ok(metadata.textContent.includes(JSON.stringify(value)));
  assert.equal(writes(h).length, 0);
});

test("an updated byte page clears the incomplete observation and requires an explicit reread", async () => {
  const h = await historyHarness(async (path) => {
    if (path.startsWith("/notification-directory?")) return directory();
    if (query(path).get("offset_bytes") === "0") return contentPage(record(), 0, { take: 80 });
    throw new ApiError("evidence_changed", 409);
  });
  await openDetail(h); await clickMemoryButton(h, h.button("继续读取通知"));
  assert.ok(h.body().includes("通知已变化"));
  assert.equal(h.button("带着通知提问") === undefined, true);
  assert.equal(h.button("继续读取通知") === undefined, true);
  assert.ok(h.button("重新核查通知")); assert.equal(writes(h).length, 0);
});

test("only an exact complete notification can append a reference while preserving text, attachments and pending voice", async () => {
  const value = record({ source: undefined, reminder_id: undefined, session_id: undefined, routing: undefined });
  const h = await historyHarness(async (path) => path.startsWith("/notification-directory?") ? directory() : contentPage(value));
  const input = h.document.querySelector("#message-input"), files = [{ id: "draft-file", name: "保留的附件.txt" }], voice = { text: "未合并语音" };
  input.value = "原草稿\n请说明这次提醒"; await input.dispatch("input");
  h.ui.state.attachments = files; h.ui.state.voiceDraft = voice;
  await openDetail(h); assert.ok(h.body().includes("来源未记录"));
  await clickMemoryButton(h, h.button("带着通知提问"));
  assert.equal(h.ui.state.view, "chat"); assert.ok(input.value.startsWith("原草稿\n请说明这次提醒\n\n"));
  assert.ok(input.value.includes("notification_read")); assert.ok(input.value.includes(JSON.stringify({ id: value.id, revision: "a".repeat(64) })));
  assert.equal(h.ui.state.attachments === files, true); assert.equal(h.ui.state.voiceDraft === voice, true);
  assert.equal(writes(h).length, 0, "准备引用必须等主人主动发送");
  const original = input.value;
  await h.ui.switchView("notifications"); await openDetail(h); await clickMemoryButton(h, h.button("带着通知提问"));
  assert.equal(input.value, original, "重复核查同一版本不重复追加引用");
});

test("an ID mismatch, invalid byte count or mixed revision never becomes a trusted notification body", async () => {
  for (const wrong of [{ id: "some-other-id" }, { length_bytes: 1 }, { revision: "b".repeat(64) }]) {
    const h = await historyHarness(async (path) => path.startsWith("/notification-directory?") ? directory() : contentPage(record(), 0, wrong));
    await openDetail(h);
    assert.ok(h.dialog.querySelector('[role="alert"]'));
    assert.equal(h.body().includes(record().content), false);
    assert.equal(h.button("带着通知提问") === undefined, true); assert.equal(writes(h).length, 0);
  }
});

test("SSE keeps the selected old history page in place until an explicit refresh", async () => {
  let pageReads = 0;
  const h = await historyHarness(async (path) => {
    if (!path.startsWith("/notification-directory?")) throw new Error(`unexpected ${path}`);
    pageReads += 1;
    return query(path).has("cursor") ? directory([notice("old-page")]) : directory([notice("head-page")], { has_more: true, next_cursor: "older" });
  });
  assert.equal(h.button("更早一页", h.other).disabled, false);
  await clickMemoryButton(h, h.button("更早一页", h.other));
  assert.ok(h.other.textContent.includes("保存的通知 old-page"), "先证明已到旧页，再验证SSE是否顶掉");
  const oldRow = h.other.querySelector(".notification-row");
  h.ui.startGlobalEvents(); await flush();
  h.emitGlobal({ id: "1", type: "notification.created", data: { id: "new-head-notification" } }); await flush();
  assert.equal(pageReads, 2); assert.equal(h.other.querySelector(".notification-row") === oldRow, true);
  assert.ok(h.other.textContent.includes("通知有更新"));
  await clickMemoryButton(h, h.button("刷新通知", h.other));
  assert.equal(query(directoryReads(h).at(-1).path).has("cursor"), false);
  assert.ok(h.other.textContent.includes("保存的通知 head-page"));
  h.ui.stopEvents();
});

test("preparing a notification reference respects current draft edits and the real textarea limit", async () => {
  for (const oversized of [false, true]) {
    const h = await historyHarness(async (path) => path.startsWith("/notification-directory?") ? directory() : contentPage());
    const input = h.document.querySelector("#message-input"); input.value = "原草稿";
    await openDetail(h);
    if (oversized) {
      input.value = "字".repeat(input.maxLength - 1);
      const original = input.value;
      await clickMemoryButton(h, h.button("带着通知提问"));
      assert.equal(input.value, original); assert.equal(h.ui.state.view, "notifications");
    } else {
      const pending = h.button("带着通知提问").dispatch("click");
      input.value = "跳转时的新草稿";
      await pending; await flush();
      assert.equal(input.value, "跳转时的新草稿");
    }
    assert.equal(writes(h).length, 0);
  }
});

test("marking this page reads only its explicit unread IDs and reloads the same stable page", async () => {
  let items = [notice("old-unread"), notice("already-read", { read: true }), notice("unknown-state", { read: null })];
  const cursor = "exact-old-page+/=";
  const h = await historyHarness(async (path, options) => {
    if (path.startsWith("/notification-directory?")) return query(path).has("cursor") ? directory(items) : directory([notice("head")], { has_more: true, next_cursor: cursor });
    assert.equal(path, "/notifications/old-unread"); assert.equal(options.method, "PATCH");
    assert.equal(options.body.read, true);
    items = items.map((item) => item.id === "old-unread" ? { ...item, read: true, revision: "b".repeat(64) } : item);
    return { id: "old-unread", read: true };
  });
  await clickMemoryButton(h, h.button("更早一页", h.other));
  await clickMemoryButton(h, h.button("本页标为已读", h.other));
  assert.deepEqual(writes(h).map((entry) => entry.path), ["/notifications/old-unread"]);
  assert.equal(query(directoryReads(h).at(-1).path).get("cursor"), cursor);
  assert.deepEqual(Array.from(h.ui.notificationDirectorySlot.page.items, (item) => item.id), ["old-unread", "already-read", "unknown-state"]);
  assert.equal(h.ui.notificationDirectorySlot.page.items[0].revision, "b".repeat(64));
  assert.ok(h.other.textContent.includes("本页所选通知已标为已读"));
  assert.equal(h.button("本页标为已读", h.other).disabled, true);
  assert.equal(h.ui.state.notifications[0].id, "recent-window", "批量操作不能借最近汇总扩展范围");
});

test("an uncertain mark-read reply is never replayed and the following GET determines the displayed state", async () => {
  let marked = false;
  const h = await historyHarness(async (path, options) => {
    if (path.startsWith("/notification-directory?")) return directory([notice("uncertain", { read: marked, revision: (marked ? "b" : "a").repeat(64) })]);
    assert.equal(path, "/notifications/uncertain"); assert.equal(options.method, "PATCH");
    marked = true; throw new ApiError("simulated committed response was lost", 503);
  });
  await clickMemoryButton(h, h.button("本页标为已读", h.other));
  assert.equal(writes(h).length, 1); assert.equal(directoryReads(h).length, 2);
  assert.ok(h.other.textContent.includes("部分标读结果未确认"));
  assert.equal(h.other.textContent.includes("本页所选通知已标为已读"), false);
  assert.equal(h.ui.notificationDirectorySlot.page.items[0].read, true);
  assert.equal(h.button("本页标为已读", h.other).disabled, true);
});

for (const scope of ["filter", "connection"]) {
  test(`a late mark-read 401 cannot affect the new ${scope} or cause another PATCH`, async () => {
    const pending = deferred(); let directories = 0;
    const h = await historyHarness(async (path, options) => {
      if (path.startsWith("/notification-directory?")) return directory([notice(++directories === 1 ? "old-visible" : "new-visible")]);
      assert.equal(options.method, "PATCH"); return pending.promise;
    });
    await clickMemoryButton(h, h.button("本页标为已读", h.other));
    const old = writes(h)[0]; assert.ok(old);
    if (scope === "filter") {
      const select = h.other.querySelector('[aria-label="通知筛选"]'); select.value = "unread";
      await select.dispatch("change"); await flush();
    } else {
      h.ui.activateConnection(); h.ui.state.connected = true;
      await h.ui.switchView("notifications");
    }
    assert.equal(old.signal.aborted, true); assert.ok(h.other.textContent.includes("保存的通知 new-visible"));
    pending.reject(new ApiError("late private write 401", 401)); await flush();
    assert.equal(h.ui.state.auth.authenticated, true); assert.equal(writes(h).length, 1);
    assert.ok(h.other.textContent.includes("保存的通知 new-visible"));
    assert.equal(h.other.textContent.includes("部分标读结果未确认"), false);
  });
}

for (const scope of ["close", "navigation", "connection"]) {
  test(`a late detail 401 is ignored after ${scope} releases the observation`, async () => {
    const pending = deferred();
    const h = await historyHarness(async (path) => path.startsWith("/notification-directory?") ? directory() : pending.promise);
    await openDetail(h);
    const old = h.requests.find((entry) => entry.path.startsWith("/notifications/older-than-200?")); assert.ok(old);
    if (scope === "close") await clickMemoryButton(h, h.button("关闭"));
    else if (scope === "navigation") await h.ui.switchView("chat");
    else { h.ui.activateConnection(); h.ui.state.connected = true; await h.ui.switchView("notifications"); }
    assert.equal(old.signal.aborted, true);
    pending.reject(new ApiError("late detail 401", 401)); await flush();
    assert.equal(h.ui.state.auth.authenticated, true); assert.equal(h.ui.notificationDetailSlot, null);
    assert.equal(h.body().includes(record().content), false); assert.equal(writes(h).length, 0);
  });
}

test("a current private 401 remains effective for either directory or exact notification detail", async () => {
  for (const area of ["directory", "detail"]) {
    const h = await historyHarness(async (path) => {
      if (area === "detail" && path.startsWith("/notification-directory?")) return directory();
      throw new ApiError("current notification 401", 401);
    });
    if (area === "detail") await openDetail(h);
    assert.equal(h.ui.state.auth.authenticated, false);
    assert.equal(h.ui.notificationDirectorySlot, null); assert.equal(h.ui.notificationDetailSlot, null);
    assert.equal(writes(h).length, 0);
  }
});

test("an exact notification SSE update cancels an incomplete observation and rereads from zero without the old revision", async () => {
  const pending = deferred(); let reads = 0, restarted;
  const h = await historyHarness(async (path) => {
    if (path.startsWith("/notification-directory?")) return directory();
    if (++reads === 1) return pending.promise;
    restarted = query(path); return contentPage(record({ content: "更新后的准确原文🙂" }), 0, { revision: "b".repeat(64) });
  });
  h.ui.startGlobalEvents(); await flush(); await openDetail(h);
  const old = h.requests.find((entry) => entry.path.startsWith("/notifications/older-than-200?"));
  h.emitGlobal({ id: "2", type: "notification.updated", data: { id: "older-than-200" } }); await flush();
  assert.equal(old.signal.aborted, true); assert.ok(h.body().includes("通知有更新"));
  assert.equal(h.button("带着通知提问") === undefined, true);
  pending.reject(new ApiError("old observation 401", 401)); await flush();
  assert.equal(h.ui.state.auth.authenticated, true);
  await clickMemoryButton(h, h.button("重新核查通知"));
  assert.equal(restarted.get("offset_bytes"), "0"); assert.equal(restarted.has("revision"), false);
  assert.ok(h.body().includes("更新后的准确原文🙂")); assert.ok(h.button("带着通知提问"));
  assert.equal(writes(h).length, 0); h.ui.stopEvents();
});

test("malformed directory responses remain visible errors with no recent-window fallback or fabricated end", async () => {
  const invalid = [
    directory([], { scanned: 0, has_more: true, next_cursor: "no-progress" }),
    directory([notice()], { has_more: true, next_cursor: "next", stopped: "end" }),
    directory([notice(), notice()]),
    directory([notice("bad-source", { source: 123 })]),
    directory([notice("unknown", { read: "false" })]),
  ];
  for (const payload of invalid) {
    const h = await historyHarness(async () => payload);
    assert.ok(h.other.querySelector('[role="alert"]')); assert.equal(h.ui.notificationDirectorySlot.page, null);
    assert.equal(h.other.querySelectorAll(".notification-row").length, 0);
    assert.equal(h.other.textContent.includes("已到末尾"), false);
    assert.equal(h.other.textContent.includes("只在最近汇总里的通知"), false);
    assert.equal(directoryReads(h).length, 1);
  }
});

test("the exact saved raw JSON remains visible and unsafe parsed numbers never become source references", async () => {
  for (const number of ["9007199254740993", "1.0000000000000001", "1e-999"]) {
    const raw = `{"id":"older-than-200","title":"原数字证据","content":"请核对保存原文🙂","source":"topic_reminder","matched_input_id":"exact-original-input","matched_seq":${number},"created_at":1e999}`;
    const size = Buffer.byteLength(raw);
    const h = await historyHarness(async (path) => path.startsWith("/notification-directory?") ? directory() : contentPage(record(), 0, { content: raw, length_bytes: size, next_offset_bytes: size, total_bytes: size, has_more: false }));
    await openDetail(h);
    const full = h.dialog.querySelectorAll("details").find((entry) => entry.firstElementChild.textContent === "已保存的完整记录");
    assert.ok(full, "先证明准确ID与版本的完整原文已读回");
    assert.equal(full.querySelector("pre").textContent, raw, "完整记录必须保留已核查字节，不能parse后改写指数或整数");
    const sourceNumber = h.dialog.querySelectorAll(".status-list div").find((entry) => entry.querySelector("dt")?.textContent === "原输入序号");
    assert.ok(sourceNumber); assert.equal(sourceNumber.querySelector("dd").textContent, "数值请核对完整记录", "来源说明同样不能把已舍入数值当原序号显示");
    await clickMemoryButton(h, h.button("带着通知提问"));
    const input = h.document.querySelector("#message-input");
    assert.ok(input.value.includes("exact-original-input"));
    assert.ok(input.value.includes(JSON.stringify({ id: "older-than-200", revision: "a".repeat(64) })));
    assert.equal(input.value.includes("9007199254740992"), false, "不能把舍入后的序号冒充已保存来源定位");
    assert.equal(input.value.includes('"matched_seq"'), false, `来源数值 ${number} 交由准确id/revision读取原文核对`);
    assert.equal(writes(h).length, 0);
  }
});

const originMessage = { id: "exact-origin-message", seq: 1, revision: "origin-message-revision", role: "user", principal: "owner", source: "web", content: "会话里保存的原始输入🙂", content_provenance: "accepted", content_truncated: false };
originMessage.content_bytes = Buffer.byteLength(originMessage.content);
const originDirectory = () => ({ session_id: "origin-session", messages: [{ ...originMessage }], through_seq: 1, has_older: false, next_before_seq: null });
const originArea = (path) => path === "/sessions/origin-session" ? "record" : path.startsWith("/sessions/origin-session/messages?") ? "directory" : path.startsWith("/sessions/origin-session/messages/exact-origin-message/content?") ? "content" : null;

async function originJourney(area, respond) {
  const h = await historyHarness(async (path, options) => {
    if (path.startsWith("/notification-directory?")) return directory();
    if (path.startsWith("/notifications/older-than-200?")) return contentPage();
    const actual = originArea(path);
    if (actual === area) return respond(path, options);
    if (actual === "record") return { session: { id: "origin-session", title: "通知指向的原会话" }, messages: [] };
    if (actual === "directory") return originDirectory();
    throw new Error(`unexpected origin fixture read: ${path}`);
  });
  await openDetail(h);
  assert.ok(h.body().includes(record().content), "先证明通知完整原文已正常读回");
  await clickMemoryButton(h, h.button("查看原会话"));
  if (area !== "record") {
    assert.ok(h.button("查看正文归档"), "先证明真实会话记录入口已读回");
    await clickMemoryButton(h, h.button("查看正文归档"));
    if (area === "content") {
      assert.ok(h.body().includes(originMessage.content), "先证明真实正文目录已读回");
      await clickMemoryButton(h, h.button("查看正文"));
    }
  }
  const request = h.requests.find((entry) => originArea(entry.path) === area);
  assert.ok(request, `应已通过通知旅程发出实际 ${area} GET`);
  assert.equal(request.method ?? "GET", "GET"); assert.equal(request.cache, "no-store");
  return { h, request };
}

for (const area of ["record", "directory", "content"]) {
  for (const scope of ["close", "connection"]) {
    test(`notification origin ${area} ignores an old 401 after ${scope}`, async () => {
      const pending = deferred(), { h, request } = await originJourney(area, async () => pending.promise);
      if (scope === "close") {
        await clickMemoryButton(h, h.button("关闭"));
        const content = h.document.createElement("p"); content.textContent = "随后打开的新窗口";
        h.ui.openDialog({ title: "新的查看操作", content, readOnly: true });
      } else {
        h.ui.activateConnection(); h.ui.state.connected = true;
        await h.ui.switchView("notifications");
        assert.ok(h.other.textContent.includes("保存的通知 older-than-200"), "先证明新连接自己的目录已可读取");
      }
      pending.reject(new ApiError(`old ${area} 401`, 401)); await flush();
      assert.equal(h.ui.state.auth.authenticated, true, "旧原会话请求不能退出当前登录");
      assert.equal(request.signal.aborted, true); assert.equal(h.ui.historySlot, null);
      if (scope === "close") { assert.equal(h.dialog.open, true); assert.ok(h.body().includes("随后打开的新窗口")); }
      else { assert.equal(h.dialog.open, false); assert.ok(h.other.textContent.includes("保存的通知 older-than-200")); }
      assert.equal(writes(h).length, 0);
    });
  }
  test(`notification origin ${area} still honors a current 401`, async () => {
    const { h } = await originJourney(area, async () => { throw new ApiError(`current ${area} 401`, 401); });
    assert.equal(h.ui.state.auth.authenticated, false);
    assert.equal(h.ui.historySlot, null); assert.equal(h.dialog.open, false);
    assert.equal(writes(h).length, 0);
  });
}
