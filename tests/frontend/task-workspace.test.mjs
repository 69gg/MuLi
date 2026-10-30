import test from "node:test";
import assert from "node:assert/strict";
import { ApiError } from "../../web/api.mjs";
import { createTaskControl } from "../../web/task-control.mjs";
import { clickMemoryButton, deferred, flush, harness } from "./helpers/app-dom-harness.mjs";

test("the real team page provides an explicit 我的任务 tab after loading teammates", async () => {
  const h = harness(async (path) => {
    assert.equal(path, "/team?limit=50&offset=0&active_only=false");
    return { members: [{ id: "teammate", name: "既有队友", status: "idle" }], messages: [], total: 1, limit: 50, offset: 0, active_only: false, has_more: false };
  }, { team: true });
  h.ui.useTeamViews();
  await h.ui.switchView("team");
  assert.equal(h.ui.state.viewError, "");
  assert.ok(h.other.textContent.includes("既有队友"), "真实团队 renderer 必须已成功显示队友，再核查缺失入口");
  assert.equal(h.requests.length, 1, "打开团队不能自动读取任务目录");
  assert.ok(h.button("我的任务", h.other), "真实团队页应提供主人主动打开账本目录的页签");
});

const task = (id = "unassigned-task", fields = {}) => ({ id, title: `保存的目标 ${id}`, status: "in_progress", revision: 2, created_at: 100, updated_at: 200, has_brief: true, ...fields });
const directory = (items = [task()], fields = {}) => ({ items, has_more: false, next_cursor: null, scanned: items.length, json_bytes: 128, omitted: { oversized: 0, invalid: 0, missing: 0 }, omitted_total: 0, stopped: "end", consistency: "bounded_live", ...fields });
const detail = (item = task(), fields = {}) => ({ ...item, revision: 7, brief: { goal: "完整目标🙂<script>仅为任务正文</script>", constraints: ["先核对第一项", "FULL_REQUIREMENTS_MIDDLE_不能省略中段", "完整要求末尾"], evidence_refs: [{ kind: "message", id: "message-reference", session_id: "evidence-session", seq: 12 }, { kind: "attachment", id: "evidence-file" }] }, control: { epoch: 0, approved_epoch: 0, replan_request_id: null }, scope: { member_count: 2, active_workflow_count: 0, can_pause: true, blocked_reason: null }, ...fields });
const teamPage = (members = []) => ({ members, messages: [], total: members.length, limit: 50, offset: 0, active_only: false, has_more: false });
const query = (path) => new URLSearchParams(path.split("?")[1]);
const reads = (h) => h.requests.filter((entry) => entry.path.startsWith("/commitment-directory?"));
const posts = (h) => h.requests.filter((entry) => entry.method === "POST");

async function workspaceHarness(respond, { members = [], open = true } = {}) {
  const h = harness(async (path, options) => {
    if (path.startsWith("/team?")) return teamPage(members);
    return respond(path, options);
  }, { team: true, composer: true });
  h.ui.state.connected = true; h.ui.state.loading = false;
  h.ui.state.status = { configured: true, model: "fixture-model" };
  h.ui.state.mainSessionId = "main"; h.ui.state.activeSession = { id: "main", mode: "auto", status: "idle" };
  h.ui.useTeamViews();
  await h.ui.switchView("team");
  if (open) await clickMemoryButton(h, h.button("我的任务", h.other));
  return h;
}

async function openDetail(h, id = "unassigned-task") {
  const button = h.other.querySelectorAll("[data-task-identifier]").find((entry) => entry.dataset.taskIdentifier === id);
  assert.ok(button, "真实目录条目必须提供完整说明入口");
  await clickMemoryButton(h, button);
}

async function filter(h, status) {
  const select = h.other.querySelector('[aria-label="账本状态"]');
  assert.ok(select); select.value = status; select.focus(); await select.dispatch("change"); await flush();
}

test("the task directory independently reads one bounded page and the complete read-only brief", async () => {
  const item = task("older-than-100"), full = detail(item);
  const h = await workspaceHarness(async (path) => {
    if (path.startsWith("/commitment-directory?")) { assert.equal(query(path).get("status"), "active"); assert.equal(query(path).get("limit"), "20"); assert.equal(query(path).has("cursor"), false); return directory([item]); }
    assert.equal(path, `/commitments/${item.id}`); return full;
  });
  assert.equal(h.ui.state.team.members.length, 0); assert.equal(reads(h).length, 1);
  assert.ok(h.other.textContent.includes(item.title)); assert.ok(h.other.textContent.includes("账本状态：进行中"));
  for (const misleading of ["运行中", "最近更新", "更早一页", "较新一页"]) assert.equal(h.other.textContent.includes(misleading), false);
  assert.equal(h.button("上一页", h.other).disabled, true); assert.equal(h.button("继续翻页", h.other).disabled, true);
  await openDetail(h, item.id);
  assert.ok(h.body().includes(full.brief.goal)); assert.ok(h.body().includes(full.brief.constraints[1]));
  assert.ok(h.body().includes("会话 evidence-session · 消息 12")); assert.ok(h.body().includes("附件 evidence-file"));
  assert.ok(h.body().includes("页面观察修订 7")); assert.equal(h.dialog.querySelectorAll("script").length, 0);
  assert.equal(h.ui.taskControlSlot === null, true); assert.equal(h.ui.taskControls.size, 0); assert.equal(h.savedTaskControls.size, 0);
  for (const forbidden of ["暂停任务树", "补充要求并交沐璃", "重试原操作", "交沐璃重新规划"]) assert.equal(h.button(forbidden) === undefined, true);
  assert.equal(h.dialog.querySelector('[aria-label="任务控制"]') === null, true);
  assert.equal(h.requests.length, 3); assert.equal(posts(h).length, 0);
  assert.ok(h.requests.filter((entry) => !entry.path.startsWith("/team?")).every((entry) => entry.cache === "no-store" && entry.signal));
  const trigger = h.other.querySelector('[data-task-identifier="older-than-100"]');
  h.dialog.close(); await flush(); assert.equal(h.document.activeElement === trigger, true);
});

test("two empty bounded pages preserve omissions and exact cursors until an unassigned old goal is reached", async () => {
  const first = "empty+/=%一", second = "empty+/=%二", found = task("old-unassigned");
  const pages = [
    directory([], { has_more: true, next_cursor: first, stopped: "candidate_budget", omitted: { oversized: 1, invalid: 1, missing: 0 }, omitted_total: 2 }),
    directory([], { has_more: true, next_cursor: second, stopped: "raw_budget", omitted: { oversized: 0, invalid: 1, missing: 1 }, omitted_total: 4 }),
    directory([found], { omitted_total: 4 }),
  ];
  const h = await workspaceHarness(async (path) => {
    assert.ok(path.startsWith("/commitment-directory?"));
    return pages[query(path).get("cursor") === first ? 1 : query(path).get("cursor") === second ? 2 : 0];
  });
  assert.equal(reads(h).length, 1); assert.equal(h.ui.taskDirectorySlot.page === pages[0], true);
  assert.ok(h.other.textContent.includes("部分记录未能列出")); assert.ok(h.other.textContent.includes("仍有后页"));
  assert.equal(h.other.textContent.includes("当前筛选下没有任务"), false);
  const retainedNext = h.button("继续翻页", h.other);
  await clickMemoryButton(h, retainedNext);
  assert.equal(reads(h).length, 2); assert.equal(query(reads(h).at(-1).path).get("cursor"), first);
  assert.equal(h.ui.taskDirectorySlot.page === pages[1], true); assert.equal(h.button("继续翻页", h.other).disabled, false);
  await clickMemoryButton(h, retainedNext); assert.equal(reads(h).length, 2, "已移除的旧按钮不能再读取");
  await clickMemoryButton(h, h.button("继续翻页", h.other));
  assert.equal(reads(h).length, 3); assert.equal(query(reads(h).at(-1).path).get("cursor"), second);
  assert.ok(h.other.textContent.includes(found.title)); assert.ok(h.other.textContent.includes("第 3 页"));
  assert.equal(h.button("继续翻页", h.other).disabled, true);
  await clickMemoryButton(h, h.button("上一页", h.other));
  assert.equal(reads(h).length, 4); assert.equal(query(reads(h).at(-1).path).get("cursor"), first);
  assert.equal(h.ui.taskDirectorySlot.page === pages[1], true); assert.ok(h.other.textContent.includes("第 2 页"));
  assert.equal(h.document.activeElement === h.other.querySelector(".task-directory-position"), true);
});

test("a legitimate empty final page remains on that page without an implicit fallback read", async () => {
  const h = await workspaceHarness(async (path) => query(path).has("cursor") ? directory([]) : directory([task()], { has_more: true, next_cursor: "last-page", stopped: "limit" }));
  await clickMemoryButton(h, h.button("继续翻页", h.other));
  assert.equal(reads(h).length, 2); assert.ok(h.other.textContent.includes("第 2 页"));
  assert.equal(h.button("上一页", h.other).disabled, false); assert.equal(h.button("继续翻页", h.other).disabled, true);
});

test("all ledger status filters reset the cursor and leave the teammate pager independent", async () => {
  const h = await workspaceHarness(async () => directory([task()], { has_more: true, next_cursor: "next", stopped: "limit" }));
  const savedTeam = h.ui.state.team;
  await clickMemoryButton(h, h.button("继续翻页", h.other));
  for (const status of ["all", "open", "in_progress", "blocked", "done", "cancelled", "active"]) {
    await filter(h, status);
    assert.equal(query(reads(h).at(-1).path).get("status"), status); assert.equal(query(reads(h).at(-1).path).has("cursor"), false);
    assert.ok(h.other.textContent.includes("第 1 页")); assert.equal(h.ui.state.team === savedTeam, true);
  }
  const count = reads(h).length;
  await clickMemoryButton(h, h.button("队友", h.other));
  assert.equal(reads(h).length, count); assert.equal(h.ui.taskDirectorySlot === null, true);
  assert.equal(query(h.requests.at(-1).path).get("offset"), "0"); assert.equal(query(h.requests.at(-1).path).get("active_only"), "false");
});

test("a directory failure is distinct from no tasks and retries the same explicit page", async () => {
  let attempts = 0;
  const h = await workspaceHarness(async (path) => {
    if (!query(path).has("cursor")) return directory([task()], { has_more: true, next_cursor: "exact-retry+/=", stopped: "limit" });
    if (++attempts === 1) throw new ApiError("READ_FAILED", 503);
    return directory([task("retry-result")]);
  });
  await clickMemoryButton(h, h.button("继续翻页", h.other));
  assert.ok(h.other.textContent.includes("READ_FAILED")); assert.equal(h.other.textContent.includes("当前筛选下没有任务"), false);
  assert.equal(reads(h).length, 2); const failed = reads(h).at(-1).path;
  await clickMemoryButton(h, h.button("重试读取", h.other));
  assert.equal(reads(h).at(-1).path, failed); assert.ok(h.other.textContent.includes("保存的目标 retry-result"));
});

for (const [name, malformed] of [
  ["missing reader envelope", { items: [], has_more: false, next_cursor: null }],
  ["absent continuation", directory([], { has_more: true })],
  ["duplicate task identity", directory([task(), task()])],
  ["unknown ledger status", directory([task("unknown", { status: "running" })])],
  ["invalid omission count", directory([], { omitted: { oversized: -1, invalid: 0, missing: 0 } })],
]) {
  test(`a malformed directory (${name}) reports failure without automatic reads`, async () => {
    const h = await workspaceHarness(async () => malformed);
    assert.ok(h.other.querySelector('[role="alert"]')); assert.equal(reads(h).length, 1);
    assert.equal(h.button("继续翻页", h.other).disabled, true);
    assert.equal(h.other.textContent.includes("当前筛选下没有任务"), false);
  });
}

const directoryLeaves = [
  ["a new status filter", async (h) => filter(h, "all")],
  ["the teammates tab", async (h) => clickMemoryButton(h, h.button("队友", h.other))],
  ["another view", async (h) => h.ui.switchView("chat")],
  ["a replacement connection", async (h) => { h.ui.activateConnection(); h.ui.state.connected = true; await h.ui.switchView("team"); }],
  ["a newly authenticated owner after logout", async (h) => { h.ui.showLogin(); h.ui.activateConnection(); h.ui.state.auth.authenticated = true; h.ui.state.connected = true; await h.ui.switchView("team"); }],
  ["pagehide", async (h) => h.windowEvents.pagehide()],
];

for (const [name, leave] of directoryLeaves) {
  for (const unauthorized of [false, true]) {
    test(`a late directory ${unauthorized ? "401" : "page"} cannot affect ${name}`, async () => {
      const pending = deferred(); let attempt = 0;
      const h = await workspaceHarness(async (path) => {
        assert.ok(path.startsWith("/commitment-directory?"));
        return ++attempt === 1 ? pending.promise : directory([task("fresh-directory")]);
      }, { open: false });
      const opening = clickMemoryButton(h, h.button("我的任务", h.other)); await flush();
      const oldSlot = h.ui.taskDirectorySlot, signal = reads(h)[0].signal;
      assert.ok(oldSlot); await leave(h); assert.equal(signal.aborted, true);
      if (unauthorized) pending.reject(new ApiError("STALE_PRIVATE_401", 401));
      else pending.resolve(directory([task("LATE_PRIVATE_DIRECTORY")]));
      await opening; await flush();
      assert.equal(h.ui.state.auth.authenticated, true); assert.equal(h.other.textContent.includes("LATE_PRIVATE_DIRECTORY"), false);
      assert.equal(h.ui.taskDirectorySlot === oldSlot, name === "a new status filter");
      assert.equal(posts(h).length, 0);
    });
  }
}

test("a current directory 401 still clears authenticated private state", async () => {
  const h = await workspaceHarness(async () => { throw new ApiError("CURRENT_401", 401); });
  assert.equal(h.ui.state.auth.authenticated, false); assert.equal(h.ui.taskDirectorySlot === null, true);
  assert.equal(h.dialog.open, false);
});

test("reconnection preserves the filter and requires an explicit refresh before reading a new directory", async () => {
  const h = await workspaceHarness(async () => directory([task()]));
  await filter(h, "done"); const count = reads(h).length, previous = h.ui.taskDirectorySlot;
  h.ui.activateConnection(); h.ui.state.connected = true; h.ui.revalidateVisibleState();
  assert.equal(h.ui.taskDirectorySlot === previous, false); assert.equal(reads(h).length, count);
  assert.ok(h.other.textContent.includes("请刷新列表读取任务"));
  assert.equal(h.other.querySelector('[aria-label="账本状态"]').value, "done");
  assert.equal(h.button("上一页", h.other).disabled, true); assert.equal(h.button("继续翻页", h.other).disabled, true);
  await clickMemoryButton(h, h.button("刷新列表", h.other));
  assert.equal(reads(h).length, count + 1); assert.equal(query(reads(h).at(-1).path).get("status"), "done");
  assert.equal(query(reads(h).at(-1).path).has("cursor"), false); assert.equal(posts(h).length, 0);
});

const detailLeaves = [
  ["closing its dialog", async (h) => { h.dialog.close(); await flush(); }],
  ["another task detail", async (h) => openDetail(h, "second-task")],
  ["a new status filter", async (h) => filter(h, "all")],
  ["the teammates tab", async (h) => clickMemoryButton(h, h.button("队友", h.other))],
  ["another view", async (h) => h.ui.switchView("chat")],
  ["a replacement connection", async (h) => { h.ui.activateConnection(); h.ui.state.connected = true; await h.ui.switchView("team"); await openDetail(h, "second-task"); }],
  ["a new login", async (h) => { h.ui.showLogin(); h.ui.activateConnection(); h.ui.state.auth.authenticated = true; h.ui.state.connected = true; await h.ui.switchView("team"); await openDetail(h, "second-task"); }],
  ["pagehide", async (h) => h.windowEvents.pagehide()],
];

for (const [name, leave] of detailLeaves) {
  for (const unauthorized of [false, true]) {
    test(`a late task detail ${unauthorized ? "401" : "body"} cannot affect ${name}`, async () => {
      const pending = deferred(), second = task("second-task");
      const h = await workspaceHarness(async (path) => {
        if (path.startsWith("/commitment-directory?")) return directory([task(), second]);
        if (path === "/commitments/second-task") return detail(second, { brief: { goal: "FRESH_TASK_BODY", constraints: [], evidence_refs: [] } });
        assert.equal(path, "/commitments/unassigned-task"); return pending.promise;
      });
      await openDetail(h);
      const oldSlot = h.ui.taskDetailSlot, signal = h.requests.at(-1).signal;
      assert.ok(oldSlot); assert.equal(h.dialog.open, true);
      await leave(h); assert.equal(signal.aborted, true);
      if (unauthorized) pending.reject(new ApiError("LATE_TASK_401", 401));
      else pending.resolve(detail(task(), { brief: { goal: "LATE_PRIVATE_TASK_BODY", constraints: [], evidence_refs: [] } }));
      await flush();
      assert.equal(h.ui.state.auth.authenticated, true); assert.equal(h.body().includes("LATE_PRIVATE_TASK_BODY"), false);
      assert.equal(h.ui.taskDetailSlot === oldSlot, false); assert.equal(posts(h).length, 0);
      if (["another task detail", "a replacement connection", "a new login"].includes(name)) assert.ok(h.body().includes("FRESH_TASK_BODY"));
    });
  }
}

test("a current detail 401 still opens login and releases both private task slots", async () => {
  const h = await workspaceHarness(async (path) => {
    if (path.startsWith("/commitment-directory?")) return directory();
    throw new ApiError("CURRENT_DETAIL_401", 401);
  });
  await openDetail(h);
  assert.equal(h.ui.state.auth.authenticated, false); assert.equal(h.ui.taskDetailSlot === null, true);
  assert.equal(h.ui.taskDirectorySlot === null, true); assert.equal(h.dialog.open, false);
});

test("SSE marks saved tasks stale without reading pages or accepting the known older revision", async () => {
  const item = task(); let version = 7;
  const h = await workspaceHarness(async (path) => path.startsWith("/commitment-directory?") ? directory([item]) : detail(item, { revision: version }));
  await openDetail(h); h.ui.startGlobalEvents(); await flush();
  const oldQuestion = h.button("带着任务提问"), count = h.requests.length;
  h.emitGlobal({ type: "team.updated", data: {} });
  h.emitGlobal({ type: "commitment.updated", data: { id: item.id, revision: 8 } });
  await flush();
  assert.equal(h.requests.length, count); assert.ok(h.other.textContent.includes("任务记录有更新"));
  assert.ok(h.body().includes("任务说明有更新")); assert.equal(h.button("带着任务提问") === undefined, true);
  await clickMemoryButton(h, oldQuestion); assert.equal(h.document.querySelector("#message-input").value, "");
  await clickMemoryButton(h, h.button("刷新说明"));
  assert.equal(h.button("带着任务提问") === undefined, true, "刷新仍返回已知旧修订时不能解除过期提示");
  assert.ok(h.body().includes("页面观察修订 7"));
  version = 8; await clickMemoryButton(h, h.button("刷新说明"));
  assert.ok(h.body().includes("页面观察修订 8")); assert.ok(h.button("带着任务提问"));
  assert.equal(reads(h).length, 1); assert.equal(posts(h).length, 0); h.ui.stopEvents();
});

test("an SSE update aborts a pending detail and keeps its late 401 out of authentication", async () => {
  const pending = deferred();
  const h = await workspaceHarness(async (path) => path.startsWith("/commitment-directory?") ? directory() : pending.promise);
  await openDetail(h); h.ui.startGlobalEvents(); await flush();
  const signal = h.requests.at(-1).signal;
  h.emitGlobal({ type: "commitment.updated", data: { id: "unassigned-task", revision: 8 } });
  assert.equal(signal.aborted, true);
  pending.reject(new ApiError("OLD_REVISION_401", 401)); await flush();
  assert.equal(h.ui.state.auth.authenticated, true); assert.ok(h.body().includes("任务说明有更新"));
  assert.equal(h.button("带着任务提问") === undefined, true); h.ui.stopEvents();
});

for (const [name, fields] of [
  ["an old record without a brief", { brief: undefined }],
  ["an absent revision", { revision: undefined }],
  ["a zero revision", { revision: 0 }],
  ["a string revision", { revision: "7" }],
  ["an unsafe integer revision", { revision: Number.MAX_SAFE_INTEGER + 1 }],
]) {
  test(`${name} remains read-only without inventing an accurate task reference`, async () => {
    const h = await workspaceHarness(async (path) => path.startsWith("/commitment-directory?") ? directory() : detail(task(), fields));
    await openDetail(h);
    assert.ok(h.button("刷新说明")); assert.equal(h.button("带着任务提问") === undefined, true);
    assert.ok(h.body().includes(fields.brief === undefined && name.includes("without a brief") ? "尚未登记完整说明" : "修订无法准确核对"));
    assert.equal(h.ui.taskControls.size, 0); assert.equal(posts(h).length, 0);
  });
}

test("mismatched or incomplete task details never become a usable full brief", async () => {
  for (const value of [detail(task("another-task")), detail(task(), { brief: { goal: "缺失完整要求" } })]) {
    const h = await workspaceHarness(async (path) => path.startsWith("/commitment-directory?") ? directory() : value);
    await openDetail(h);
    assert.ok(h.body().includes("任务说明响应不完整")); assert.equal(h.button("带着任务提问") === undefined, true);
    assert.equal(h.body().includes(value.brief.goal), false); assert.equal(posts(h).length, 0);
  }
});

for (const action of ["pause", "request_replan"]) {
  test(`directory reading cannot borrow a restored unknown ${action} submission or retained team button`, async () => {
    const item = task(), pending = { action, expected_epoch: 0, request_id: `original-${action}`, ...(action === "request_replan" ? { additional_requirements: "原未知提交里的要求" } : {}) };
    let full = detail(item);
    const h = await workspaceHarness(async (path, options) => {
      if (path.startsWith("/commitment-directory?")) return directory([item]);
      if (path === `/commitments/${item.id}`) return full;
      assert.equal(path, `/commitments/${item.id}/control`); assert.equal(options.method, "POST");
      assert.deepEqual(JSON.parse(JSON.stringify(options.body)), pending);
      full = detail(item, { revision: 8, control: { epoch: 1, approved_epoch: 0, replan_request_id: action === "request_replan" ? "accepted-replan" : null } });
      return { receipt: { id: "receipt", action, request_id: pending.request_id }, current: full };
    }, { members: [{ id: "member", name: "原队友", status: "idle", commitment_id: item.id }], open: false });
    const key = `muli.task-control:${item.id}`;
    h.taskStorage.setItem(key, JSON.stringify(pending));
    await clickMemoryButton(h, h.other.querySelector('[data-action="view-task-brief"]'));
    const original = h.ui.taskControlSlot.model, submission = original.state.submission;
    const retainedRetry = h.button("重试原操作"); assert.ok(retainedRetry);
    assert.equal(h.ui.taskControls.get(item.id) === original, true);
    h.dialog.close(); await flush();
    const saved = h.savedTaskControls.get(key);
    await clickMemoryButton(h, h.button("我的任务", h.other)); await openDetail(h);
    assert.equal(h.ui.taskControlSlot === null, true); assert.equal(h.ui.taskControls.get(item.id) === original, true);
    assert.equal(original.state.submission === submission, true); assert.equal(h.savedTaskControls.get(key), saved);
    assert.equal(h.button("重试原操作") === undefined, true); assert.equal(h.dialog.querySelector("textarea") === null, true);
    await clickMemoryButton(h, retainedRetry);
    assert.equal(posts(h).length, 0); assert.equal(original.state.submission === submission, true);
    h.dialog.close(); await flush();
    assert.equal(h.ui.taskControls.get(item.id) === original, true); assert.equal(h.savedTaskControls.get(key), saved);
    await clickMemoryButton(h, h.button("队友", h.other));
    await clickMemoryButton(h, h.other.querySelector('[data-action="view-task-brief"]'));
    await clickMemoryButton(h, h.button("重试原操作"));
    assert.equal(posts(h).length, 1); assert.equal(posts(h)[0].body.request_id, pending.request_id);
    assert.equal(original.state.submission, null); assert.equal(h.savedTaskControls.has(key), false);
  });
}

test("the original visible team pause and replan controls retain their exact write path", async () => {
  const item = task(); let full = detail(item), sequence = 0;
  const model = createTaskControl(item.id, { createRequestId: () => `manual-control-${++sequence}` });
  const h = await workspaceHarness(async (path, options) => {
    if (path === `/commitments/${item.id}`) return full;
    assert.equal(path, `/commitments/${item.id}/control`); assert.equal(options.method, "POST");
    const epoch = options.body.expected_epoch + 1;
    full = detail(item, { revision: full.revision + 1, control: { epoch, approved_epoch: 0, replan_request_id: options.body.action === "request_replan" ? "manual-replan" : null } });
    return { receipt: { id: `receipt-${epoch}`, action: options.body.action, request_id: options.body.request_id }, current: full };
  }, { members: [{ id: "member", name: "可控队友", status: "idle", commitment_id: item.id }], open: false });
  h.ui.taskControls.set(item.id, model);
  await clickMemoryButton(h, h.other.querySelector('[data-action="view-task-brief"]'));
  await clickMemoryButton(h, h.button("暂停任务树"));
  assert.equal(posts(h).length, 1); assert.equal(posts(h)[0].body.action, "pause");
  await clickMemoryButton(h, h.button("补充要求并交沐璃"));
  const input = h.dialog.querySelector("textarea"), requirements = "原团队补充的完整要求"; input.value = requirements; await input.dispatch("input");
  await clickMemoryButton(h, h.button("交沐璃重新规划"));
  assert.equal(posts(h).length, 2); assert.equal(posts(h)[1].body.action, "request_replan");
  assert.equal(posts(h)[1].body.additional_requirements, requirements); assert.equal(reads(h).length, 0);
});

test("logout clears real enumerable saved pending keys while directory-only close preserves them", async () => {
  const item = task(), pending = { action: "pause", expected_epoch: 0, request_id: "saved-pause" };
  const h = await workspaceHarness(async (path) => path.startsWith("/commitment-directory?") ? directory([item]) : detail(item));
  const key = `muli.task-control:${item.id}`;
  h.taskStorage.setItem(key, JSON.stringify(pending));
  assert.ok(Object.keys(h.taskStorage).includes(key));
  await openDetail(h); h.dialog.close(); await flush();
  assert.equal(h.savedTaskControls.has(key), true);
  h.ui.showLogin();
  assert.equal(h.savedTaskControls.has(key), false); assert.equal(Object.keys(h.taskStorage).includes(key), false);
});

for (const running of [false, true]) {
  test(`task references preserve text, attachments and voice then use manual chat send (${running ? "running" : "idle"})`, async () => {
    const item = task("exact+/=任务ID"), full = detail(item), file = { id: "2".repeat(32), name: "既有草稿附件.txt", mime_type: "text/plain", size: 12 };
    const h = await workspaceHarness(async (path, options) => {
      if (path.startsWith("/commitment-directory?")) return directory([item]);
      if (path === `/commitments/${encodeURIComponent(item.id)}`) return full;
      if (path === "/sessions") return { items: [{ id: "main", status: running ? "running" : "idle" }] };
      assert.equal(path, "/sessions/main/messages"); assert.equal(options.method, "POST");
      return { run_id: running ? "existing-run" : "new-run", queued: running, message: { id: "accepted-task-question", session_id: "main", role: "user", principal: "owner", content: options.body.content, attachment_ids: [...options.body.attachment_ids], attachments: [file], created_at: 2, delivery_status: running ? "pending" : "delivered" } };
    });
    h.ui.state.activeSession.status = running ? "running" : "idle";
    h.ui.state.session.status = running ? "running" : "idle"; h.ui.state.session.runId = running ? "existing-run" : null;
    h.ui.state.attachments = [file]; const attachments = h.ui.state.attachments;
    const voice = { text: "尚未合并的语音草稿" }; h.ui.state.voiceDraft = voice;
    const input = h.document.querySelector("#message-input"), question = "先帮我核对这个保存的目标🙂。";
    input.value = question; await input.dispatch("input"); h.ui.renderComposerAttachments();
    const captured = h.ui.captureDraft();
    await openDetail(h, item.id);
    const useTask = h.button("带着任务提问"); assert.ok(useTask);
    await clickMemoryButton(h, useTask);
    const prepared = input.value, lines = prepared.split("\n");
    assert.equal(h.ui.state.view, "chat"); assert.equal(h.dialog.open, false);
    assert.equal(h.ui.taskDetailSlot === null, true); assert.equal(h.ui.taskDirectorySlot === null, true);
    assert.equal(h.document.activeElement === input, true);
    assert.equal(lines.length, 4); assert.deepEqual(lines.slice(0, 2), [question, ""]);
    assert.ok(lines[2].includes("页面观察修订 7")); assert.ok(lines[2].includes("commitment_read"));
    assert.ok(lines[2].includes("读取当前说明")); assert.ok(lines[2].includes("不是继续或批准操作的授权"));
    assert.deepEqual(JSON.parse(lines[3]), { identifier: item.id, observed_revision: full.revision });
    for (const privateText of [full.brief.goal, ...full.brief.constraints, "evidence-session", "evidence-file", item.title]) assert.equal(prepared.includes(privateText), false);
    assert.ok(h.ui.captureDraft().revision > captured.revision);
    assert.equal(h.ui.state.attachments === attachments, true); assert.equal(h.ui.state.attachments[0] === file, true);
    assert.equal(h.ui.state.voiceDraft === voice, true); assert.ok(h.document.querySelector("#composer-attachments").textContent.includes(file.name));
    assert.equal(h.requests.length, 3); assert.equal(posts(h).length, 0, "准备可编辑草稿不发送消息或操作任务");
    await clickMemoryButton(h, useTask); assert.equal(input.value, prepared); assert.equal(h.requests.length, 3);
    input.value += "\n只回答我后来补充的问题。"; await input.dispatch("input");
    const edited = input.value; await h.document.querySelector("#composer").dispatch("submit"); await flush();
    assert.equal(posts(h).length, 1); assert.equal(posts(h)[0].path, "/sessions/main/messages");
    assert.equal(posts(h)[0].body.content, edited); assert.equal(posts(h)[0].body.mode, "auto");
    assert.deepEqual([...posts(h)[0].body.attachment_ids], [file.id]);
    assert.equal(h.ui.state.session.messages.some((message) => message.id === "accepted-task-question" && message.content === edited), true);
    assert.equal(h.ui.state.session.runId, running ? "existing-run" : "new-run"); assert.equal(h.ui.state.voiceDraft === voice, true);
  });
}

async function taskQuestionHarness() {
  const item = task(), h = await workspaceHarness(async (path) => path.startsWith("/commitment-directory?") ? directory([item]) : detail(item));
  const input = h.document.querySelector("#message-input"), question = "保留主人已写好的问题。";
  input.value = question; await input.dispatch("input"); await openDetail(h);
  return { ...h, input, question, item };
}

for (const [name, change, expected] of [
  ["a newly typed draft", (h) => { h.input.value = "导航期间的新文字"; void h.input.dispatch("input"); }, "导航期间的新文字"],
  ["programmatic text with an unchanged revision", (h) => { h.input.value = "没有触发 input 的新草稿"; }, "没有触发 input 的新草稿"],
  ["a replacement connection", (h) => { h.ui.activateConnection(); h.ui.state.connected = true; }, null],
  ["a replacement main identity", (h) => { h.ui.state.mainSessionId = "replacement-main"; }, null],
  ["a newer navigation also ending in chat", (h) => { void h.ui.switchView("team"); void h.ui.switchView("chat"); }, null],
]) {
  test(`preparing a task reference yields to ${name}`, async () => {
    const h = await taskQuestionHarness(), focus = h.input.focus.bind(h.input);
    let armed = true;
    h.input.focus = () => { focus(); if (armed) { armed = false; queueMicrotask(() => change(h)); } };
    await clickMemoryButton(h, h.button("带着任务提问"));
    assert.equal(h.input.value, expected ?? h.question); assert.equal(h.ui.state.auth.authenticated, true);
    assert.equal(h.ui.state.view, "chat"); assert.equal(posts(h).length, 0);
  });
}

test("preparing a task reference retains attachments and voice that arrive during navigation", async () => {
  const h = await taskQuestionHarness(), focus = h.input.focus.bind(h.input);
  const attachments = [{ id: "4".repeat(32) }], voice = { text: "导航期间到达的语音" };
  let armed = true;
  h.input.focus = () => { focus(); if (armed) { armed = false; queueMicrotask(() => { h.ui.state.attachments = attachments; h.ui.state.voiceDraft = voice; }); } };
  await clickMemoryButton(h, h.button("带着任务提问"));
  assert.ok(h.input.value.includes(h.item.id)); assert.equal(h.ui.state.attachments === attachments, true);
  assert.equal(h.ui.state.voiceDraft === voice, true); assert.equal(posts(h).length, 0);
});

test("a late voice transcript cannot overwrite a task reference appended to the captured draft", async () => {
  const h = await taskQuestionHarness(), captured = h.ui.captureDraft();
  await clickMemoryButton(h, h.button("带着任务提问"));
  const prepared = h.input.value;
  h.publishVoiceDraft({ context: captured, text: "迟到的语音正文" });
  assert.equal(h.input.value, prepared); assert.equal(h.ui.state.voiceDraft.text, "迟到的语音正文");
  assert.equal(h.document.querySelector("#voice-draft-text").value, "迟到的语音正文"); assert.equal(posts(h).length, 0);
});

test("task references deduplicate editable text and respect the exact existing maxlength", async () => {
  const h = await taskQuestionHarness();
  await clickMemoryButton(h, h.button("带着任务提问"));
  const prepared = h.input.value, revision = h.ui.captureDraft().revision;
  await h.ui.switchView("team"); await openDetail(h); await clickMemoryButton(h, h.button("带着任务提问"));
  assert.equal(h.input.value, prepared); assert.equal(h.ui.captureDraft().revision, revision);
  h.input.value = h.question; await h.input.dispatch("input"); h.input.maxLength = prepared.length;
  await h.ui.switchView("team"); await openDetail(h); await clickMemoryButton(h, h.button("带着任务提问"));
  assert.equal(h.input.value, prepared);
  h.input.value = h.question; await h.input.dispatch("input"); h.input.maxLength = prepared.length - 1;
  const captured = h.ui.captureDraft();
  await h.ui.switchView("team"); await openDetail(h); await clickMemoryButton(h, h.button("带着任务提问"));
  assert.equal(h.input.value, h.question); assert.equal(h.ui.captureDraft().revision, captured.revision);
  assert.equal(h.dialog.open, true); assert.equal(h.ui.state.view, "team"); assert.equal(posts(h).length, 0);
});

test("a task reference is disabled without the exact existing main conversation", async () => {
  for (const alter of [(h) => { h.ui.state.mainSessionId = null; }, (h) => { h.ui.state.activeSession.id = "another-session"; }]) {
    const h = await taskQuestionHarness(); alter(h);
    await clickMemoryButton(h, h.button("刷新说明"));
    const button = h.button("带着任务提问"); assert.ok(button); assert.equal(button.disabled, true);
    await clickMemoryButton(h, button);
    assert.equal(h.input.value, h.question); assert.equal(h.ui.state.view, "team"); assert.equal(posts(h).length, 0);
  }
});
