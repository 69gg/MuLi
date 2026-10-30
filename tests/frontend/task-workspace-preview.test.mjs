import test from "node:test";
import assert from "node:assert/strict";
import { createPreviewBackend } from "../../web/preview.mjs";
import { clickMemoryButton, flush, harness } from "./helpers/app-dom-harness.mjs";

async function getJSON(backend, path) {
  const response = await backend.fetch(`/api${path}`);
  return { status: response.status, body: await response.json() };
}

test("preview task directory serves the real bounded page contract", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  const result = await getJSON(backend, "/commitment-directory?status=active&limit=20");
  assert.equal(result.status, 200);
  assert.equal(result.body.consistency, "bounded_live");
  assert.ok(Array.isArray(result.body.items));
});

const savedState = (backend) => structuredClone({ commitments: backend.state.commitments, sessions: backend.state.sessions, members: backend.state.members, messages: backend.state.messages, runs: backend.state.runs, events: backend.state.events, notifications: backend.state.notifications, settings: backend.state.settings });
const directoryPath = (status = "active", cursor = null, limit = 1) => `/commitment-directory?${new URLSearchParams({ status, limit: String(limit), ...(cursor === null ? {} : { cursor }) })}`;

test("preview task paging is explicit, status-scoped and complete without creating execution state", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  const before = savedState(backend);
  const active = backend.state.commitments.filter((item) => !["done", "cancelled"].includes(item.status)).map((item) => item.id).sort().reverse();
  const first = await getJSON(backend, directoryPath());
  assert.equal(first.status, 200);
  assert.deepEqual(Object.keys(first.body).sort(), ["consistency", "has_more", "items", "json_bytes", "next_cursor", "omitted", "omitted_total", "scanned", "stopped"]);
  assert.deepEqual(first.body.items.map((item) => item.id), active.slice(0, 1));
  assert.equal(first.body.has_more, true); assert.equal(first.body.stopped, "limit");
  assert.equal(first.body.scanned, 1); assert.equal(first.body.omitted_total, 0);
  assert.deepEqual(first.body.omitted, { oversized: 0, invalid: 0, missing: 0 });
  assert.equal(first.body.json_bytes, Buffer.byteLength(JSON.stringify(backend.state.commitments.find((item) => item.id === active[0]))));
  assert.equal(backend.state.requests.length, 1, "一页读取不能预先追踪后续游标");
  const second = await getJSON(backend, directoryPath("active", first.body.next_cursor));
  assert.equal(second.status, 200); assert.deepEqual(second.body.items.map((item) => item.id), active.slice(1));
  assert.equal(second.body.has_more, false); assert.equal(second.body.next_cursor, null); assert.equal(second.body.stopped, "end");
  const repeated = await getJSON(backend, directoryPath()); assert.deepEqual(repeated.body, first.body);
  for (const status of ["open", "in_progress", "blocked", "done", "cancelled", "all"]) {
    const page = await getJSON(backend, directoryPath(status, null, 20));
    assert.equal(page.status, 200);
    const expected = backend.state.commitments.filter((item) => status === "all" || item.status === status).map((item) => item.id).sort().reverse();
    assert.deepEqual(page.body.items.map((item) => item.id), expected);
    assert.equal(page.body.has_more, false); assert.equal(page.body.next_cursor, null);
  }
  assert.deepEqual(savedState(backend), before);
  assert.ok(backend.state.requests.every((entry) => entry.method === "GET"));
});

test("preview legacy commitment lists retain items-only metadata and never leak full briefs or plans", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  const item = backend.state.commitments.find((entry) => entry.control);
  item.control = { ...item.control, replan_request_id: "private-replan-id", plan: { epoch: 1, text: "完整计划中段只属于详情" } };
  const before = savedState(backend);
  const legacy = await getJSON(backend, "/commitments?status=all");
  assert.equal(legacy.status, 200); assert.deepEqual(Object.keys(legacy.body), ["items"]);
  assert.deepEqual(legacy.body.items.map((entry) => entry.id), [...backend.state.commitments].sort((left, right) => right.updated_at - left.updated_at).map((entry) => entry.id));
  const directory = await getJSON(backend, directoryPath("all", null, 20));
  const legacyById = new Map(legacy.body.items.map((entry) => [entry.id, entry]));
  for (const metadata of directory.body.items) {
    assert.deepEqual(metadata, legacyById.get(metadata.id));
    assert.equal(metadata.has_brief, true);
    for (const privateField of ["brief", "scope", "plan"]) assert.equal(Object.hasOwn(metadata, privateField), false);
    assert.equal(Object.hasOwn(metadata.control ?? {}, "plan"), false);
    assert.equal(JSON.stringify(metadata).includes(backend.state.commitments.find((entry) => entry.id === metadata.id).brief.goal), false);
  }
  const controlled = legacyById.get(item.id);
  assert.deepEqual(controlled.control, { epoch: 1, approved_epoch: 0, paused: true, replan_requested: true, has_plan: true });
  assert.equal(JSON.stringify(legacy.body).includes(item.control.plan.text), false);
  assert.equal(JSON.stringify(legacy.body).includes(item.control.replan_request_id), false);
  const active = await getJSON(backend, "/commitments");
  assert.equal(active.body.items.some((entry) => ["done", "cancelled"].includes(entry.status)), false);
  assert.deepEqual(savedState(backend), before);
});

test("preview exact task details preserve full goal and middle requirements independently of teammates", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  const item = backend.state.commitments.find((entry) => entry.status === "open");
  item.revision = 19;
  item.brief = { goal: "完整目标首段🙂\n" + "完整目标中段不得省略。".repeat(80) + "\n完整目标末段。", constraints: ["完整要求首项", "FULL_REQUIREMENTS_MIDDLE_不能省略。".repeat(80), "完整要求末项"], evidence_refs: [{ kind: "attachment", id: backend.state.uploads[0].id }, { kind: "message", id: "preview-evidence", session_id: "preview-main", seq: 12 }] };
  const waiting = backend.state.commitments.find((entry) => entry.control);
  waiting.control.plan = { epoch: 1, text: "详情里的完整计划正文与中段。" };
  const before = savedState(backend);
  const result = await getJSON(backend, `/commitments/${encodeURIComponent(item.id)}`);
  assert.equal(result.status, 200); assert.equal(result.body.id, item.id); assert.equal(result.body.revision, item.revision);
  assert.equal(result.body.title, item.title); assert.deepEqual(result.body.brief, item.brief);
  assert.deepEqual(result.body.control, { epoch: 0, approved_epoch: 0, replan_request_id: null, plan: null, paused: false, has_plan: false, plan_epoch: null });
  assert.deepEqual(result.body.scope, { member_count: 0, active_workflow_count: 0, can_pause: true, blocked_reason: null });
  assert.equal(backend.state.members.some((member) => member.commitment_id === item.id), false);
  const controlled = await getJSON(backend, `/commitments/${waiting.id}`);
  assert.deepEqual(controlled.body.brief, waiting.brief); assert.deepEqual(controlled.body.control.plan, waiting.control.plan);
  assert.equal(controlled.body.control.paused, true); assert.equal(controlled.body.control.has_plan, true); assert.equal(controlled.body.control.plan_epoch, 1);
  assert.deepEqual(savedState(backend), before);
  assert.deepEqual(backend.state.messages, before.messages); assert.deepEqual(backend.state.runs, before.runs); assert.deepEqual(backend.state.events, before.events);
  assert.ok(backend.state.requests.every((entry) => entry.method === "GET"));
});

test("preview task parameters reject invalid limits and cursors including cross-status continuation", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  const before = savedState(backend), first = await getJSON(backend, directoryPath());
  assert.equal(first.status, 200); assert.ok(first.body.next_cursor);
  for (const value of ["", "0", "-1", "101", "1.5", "word", "Infinity", "9007199254740992"]) {
    assert.equal((await getJSON(backend, `/commitment-directory?${new URLSearchParams({ limit: value })}`)).status, 422);
  }
  assert.equal((await getJSON(backend, "/commitment-directory?cursor=")).status, 422);
  for (const cursor of ["broken", "null", "[]", JSON.stringify(["preview-commitments", "active", ""]), JSON.stringify(["memory-sources", "active", "task"]), JSON.stringify(["preview-commitments", "active", 1])]) {
    assert.equal((await getJSON(backend, directoryPath("active", cursor))).status, 400);
  }
  assert.equal((await getJSON(backend, directoryPath("done", first.body.next_cursor))).status, 400);
  for (const route of ["/commitment-directory", "/commitments"]) {
    assert.equal((await getJSON(backend, `${route}?status=unknown`)).status, 400);
    assert.equal((await getJSON(backend, `${route}?status=`)).status, 400);
  }
  assert.deepEqual(savedState(backend), before);
});

test("preview missing tasks and unimplemented routes stay explicit without fallback success", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  const before = savedState(backend), id = backend.state.commitments[0].id;
  assert.equal((await getJSON(backend, "/commitments/missing")).status, 404);
  for (const path of ["/commitment-directory/unknown", `/commitments/${id}/inspection`, "/unknown-task-route"]) {
    const result = await getJSON(backend, path);
    assert.equal(result.status, 501); assert.equal(result.body.preview, true); assert.equal(Object.hasOwn(result.body, "items"), false);
  }
  for (const path of ["/commitment-directory", `/commitments/${id}/control`]) {
    const result = await backend.fetch(`/api${path}`, { method: "POST", body: JSON.stringify({ action: "pause" }) });
    assert.equal(result.status, 501);
  }
  assert.deepEqual(savedState(backend), before);
});

test("the real app uses preview task pages and exact read-only details to preserve an editable question draft", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  const selected = { ...structuredClone(backend.state.commitments.find((entry) => entry.status === "open")), id: "preview-000-unassigned-goal" };
  const completed = structuredClone(backend.state.commitments.find((entry) => entry.status === "done"));
  backend.state.commitments = [...Array.from({ length: 20 }, (_, index) => ({ ...structuredClone(selected), id: `preview-100-${String(index).padStart(2, "0")}`, title: `本页模拟目标 ${index + 1}` })), selected, completed];
  const before = savedState(backend);
  const h = harness(async (path, options) => {
    const result = await backend.fetch(`/api${path}`, options);
    assert.equal(result.status, 200, `真实 app 的 ${path} 必须由 preview backend 提供`);
    return result.json();
  }, { team: true, composer: true });
  const main = backend.state.sessions[0];
  h.ui.state.connected = true; h.ui.state.loading = false; h.ui.state.status = { configured: true, preview: true, model: backend.state.settings.models.main.model };
  h.ui.state.mainSessionId = main.id; h.ui.state.activeSession = structuredClone(main);
  h.ui.useTeamViews(); await h.ui.switchView("team");
  assert.equal(h.ui.state.viewError, ""); assert.ok(h.other.textContent.includes(backend.state.members[0].name));
  assert.equal(backend.state.requests.length, 1, "打开队友页不能自动读取任务");
  await clickMemoryButton(h, h.button("我的任务", h.other));
  assert.equal(h.ui.state.viewError, ""); assert.equal(h.ui.taskDirectorySlot.page.items.length, 20);
  assert.equal(h.other.querySelector(`[data-task-identifier="${selected.id}"]`) === null, true);
  assert.equal(h.button("继续翻页", h.other).disabled, false);
  let select = h.other.querySelector('[aria-label="账本状态"]');
  select.value = "done"; await select.dispatch("change"); await flush();
  assert.ok(h.other.textContent.includes(completed.title)); assert.equal(h.ui.taskDirectorySlot.page.items.length, 1);
  assert.equal(h.button("继续翻页", h.other).disabled, true);
  select = h.other.querySelector('[aria-label="账本状态"]');
  select.value = "active"; await select.dispatch("change"); await flush();
  await clickMemoryButton(h, h.button("继续翻页", h.other));
  assert.equal(h.ui.taskDirectorySlot.page.items.length, 1); assert.ok(h.other.textContent.includes(selected.title));
  assert.equal(h.button("上一页", h.other).disabled, false); assert.equal(h.button("继续翻页", h.other).disabled, true);
  const file = backend.state.uploads[0], attachments = [file], voice = { text: "还没合并的语音草稿" };
  h.ui.state.attachments = attachments; h.ui.state.voiceDraft = voice; h.ui.renderComposerAttachments();
  const input = h.document.querySelector("#message-input"), question = "请先核对这个旧目标，再回答我的问题。";
  input.value = question; await input.dispatch("input");
  await clickMemoryButton(h, h.other.querySelector(`[data-task-identifier="${selected.id}"]`));
  assert.equal(h.dialog.open, true); assert.ok(h.body().includes(selected.brief.goal));
  assert.ok(h.body().includes(selected.brief.constraints[1])); assert.ok(h.body().includes(`附件 ${file.id}`));
  assert.ok(h.body().includes(`页面观察修订 ${selected.revision}`));
  assert.equal(h.dialog.querySelector('[aria-label="任务控制"]') === null, true);
  assert.equal(h.ui.taskControlSlot === null, true); assert.equal(h.ui.taskControls.size, 0);
  await clickMemoryButton(h, h.button("带着任务提问"));
  assert.equal(h.ui.state.view, "chat"); assert.equal(h.dialog.open, false);
  assert.ok(input.value.startsWith(`${question}\n\n`)); assert.ok(input.value.includes("commitment_read"));
  assert.ok(input.value.includes("不是继续或批准操作的授权"));
  assert.deepEqual(JSON.parse(input.value.split("\n").at(-1)), { identifier: selected.id, observed_revision: selected.revision });
  assert.equal(input.value.includes(selected.brief.goal), false); assert.equal(input.value.includes(selected.brief.constraints[1]), false);
  assert.equal(h.ui.state.attachments === attachments, true); assert.equal(h.ui.state.attachments[0] === file, true); assert.equal(h.ui.state.voiceDraft === voice, true);
  assert.equal(h.document.activeElement === input, true); assert.ok(h.document.querySelector("#composer-attachments").textContent.includes(file.name));
  const reads = h.requests.filter((entry) => entry.path.startsWith("/commitment-directory?"));
  assert.equal(reads.length, 4); assert.equal(new URLSearchParams(reads.at(-1).path.split("?")[1]).has("cursor"), true);
  assert.equal(h.requests.length, 6); assert.ok(h.requests.every((entry) => (entry.method ?? "GET") === "GET"));
  assert.deepEqual(savedState(backend), before);
});
