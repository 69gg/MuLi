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

const savedState = (backend) => structuredClone({ commitments: backend.state.commitments, ownerQuestions: backend.state.ownerQuestions, sessions: backend.state.sessions, members: backend.state.members, messages: backend.state.messages, runs: backend.state.runs, events: backend.state.events, notifications: backend.state.notifications, settings: backend.state.settings });
const directoryPath = (status = "active", cursor = null, limit = 1) => `/commitment-directory?${new URLSearchParams({ status, limit: String(limit), ...(cursor === null ? {} : { cursor }) })}`;
const ownerQuestionsPath = (id, params = {}) => `/commitments/${encodeURIComponent(id)}/inspection?${new URLSearchParams({ section: "owner_questions", ...params })}`;

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
  for (const path of ["/commitment-directory/unknown", `/commitments/${id}/inspection?section=overview`, "/unknown-task-route"]) {
    const result = await getJSON(backend, path);
    assert.equal(result.status, 501); assert.equal(result.body.preview, true); assert.equal(Object.hasOwn(result.body, "items"), false);
  }
  for (const path of ["/commitment-directory", `/commitments/${id}/unimplemented-control`]) {
    const result = await backend.fetch(`/api${path}`, { method: "POST", body: JSON.stringify({ action: "pause" }) });
    assert.equal(result.status, 501);
  }
  const invalidControl = await backend.fetch(`/api/commitments/${id}/control`, { method: "POST", body: JSON.stringify({ action: "pause" }) });
  assert.equal(invalidControl.status, 422, "已实现的控制入口必须明确拒绝缺失 epoch/request_id 的输入");
  const rejected = await invalidControl.json();
  assert.equal(rejected.preview, true); assert.equal(Object.hasOwn(rejected, "receipt"), false);
  assert.equal(backend.state.taskControlReceipts?.length ?? 0, 0);
  assert.deepEqual(savedState(backend), before);
});

test("preview owner questions use explicit candidate windows, exact metadata and status-scoped cursors", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  const taskId = backend.state.ownerQuestions[0].task_id, before = savedState(backend);
  const first = await getJSON(backend, ownerQuestionsPath(taskId, { limit: "1" }));
  assert.equal(first.status, 200); assert.equal(first.body.id, taskId); assert.equal(first.body.section, "owner_questions");
  assert.equal(first.body.question_status, "open"); assert.equal(first.body.authority, "coordination_data");
  assert.equal(first.body.source_completeness, "candidate_window_only"); assert.match(first.body.revision, /^[a-f0-9]{64}$/u);
  assert.equal(first.body.scanned, 1); assert.equal(first.body.omitted, 0); assert.equal(first.body.unavailable, 0); assert.equal(first.body.has_more, true);
  assert.equal(first.body.items[0].question_id, backend.state.ownerQuestions[0].id); assert.equal(first.body.items[0].binding, "paused");
  assert.equal(backend.state.requests.length, 1, "主动读一页不应预取后页");
  const empty = await getJSON(backend, ownerQuestionsPath(taskId, { cursor: first.body.next_cursor, revision: first.body.revision, limit: "1" }));
  assert.equal(empty.status, 200); assert.deepEqual(empty.body.items, []); assert.equal(empty.body.scanned, 1);
  assert.equal(empty.body.omitted, 1); assert.equal(empty.body.has_more, true); assert.ok(empty.body.next_cursor, "空候选窗口仍须保留后页");
  const all = []; let cursor = null;
  for (let index = 0; index < backend.state.ownerQuestions.length; index++) {
    const page = await getJSON(backend, ownerQuestionsPath(taskId, { question_status: "all", limit: "2", ...(cursor ? { cursor, revision: first.body.revision } : {}) }));
    assert.equal(page.status, 200); all.push(...page.body.items); cursor = page.body.next_cursor;
    if (!page.body.has_more) break;
  }
  assert.equal(cursor, null); assert.deepEqual(all.map((item) => item.question_id), backend.state.ownerQuestions.map((item) => item.id));
  assert.deepEqual(all.map((item) => item.status), ["open", "answered", "answered", "answered", "cancelled", "stale"]);
  assert.deepEqual(all.map((item) => item.delivery_state), ["not_dispatched", "not_dispatched", "queued", "claimed", "not_dispatched", "not_dispatched"]);
  for (const item of all) {
    assert.equal(item.record_id, item.question_id); assert.match(item.record_revision, /^[a-f0-9]{64}$/u);
    assert.equal(item.task_id, taskId); assert.ok(backend.state.members.some((worker) => worker.id === item.worker_id && worker.commitment_id === taskId));
    assert.equal(item.task_completion, "separate_evidence_required"); assert.equal(item.dispatch_allowed, false);
    assert.equal(Object.hasOwn(item, "message"), false); assert.equal(Object.hasOwn(item, "answer"), false);
  }
  for (const status of ["open", "answered", "cancelled", "stale"]) {
    const page = await getJSON(backend, ownerQuestionsPath(taskId, { question_status: status }));
    assert.equal(page.status, 200); assert.deepEqual(page.body.items.map((item) => item.question_id), all.filter((item) => item.status === status).map((item) => item.question_id));
  }
  assert.deepEqual((await getJSON(backend, ownerQuestionsPath(taskId, { limit: "1" }))).body, first.body);
  assert.deepEqual(savedState(backend), before); assert.ok(backend.state.requests.every((entry) => entry.method === "GET"));
});

test("preview owner-question records preserve original question, answer, author and actual demo delivery state", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  const taskId = backend.state.ownerQuestions[0].task_id;
  backend.state.commitments.find((item) => item.id === taskId).control.approved_epoch = 1;
  const before = savedState(backend), directory = await getJSON(backend, ownerQuestionsPath(taskId, { question_status: "answered" }));
  assert.equal(directory.status, 200); assert.equal(directory.body.items[0].dispatch_allowed, true);
  for (const metadata of directory.body.items) {
    const record = await getJSON(backend, ownerQuestionsPath(taskId, { record_id: metadata.record_id, record_revision: metadata.record_revision }));
    assert.equal(record.status, 200); assert.equal(record.body.record_revision, metadata.record_revision);
    assert.equal(record.body.authority, "coordination_data"); assert.equal(record.body.field, "content"); assert.equal(record.body.has_more, false);
    const value = JSON.parse(record.body.content), stored = backend.state.ownerQuestions.find((item) => item.id === metadata.record_id);
    assert.equal(value.id, metadata.question_id); assert.equal(value.worker_id, metadata.worker_id); assert.equal(value.message, stored.message);
    assert.deepEqual(value.author, stored.author); assert.deepEqual(value.attachment_ids, stored.attachment_ids); assert.deepEqual(value.answer, stored.answer);
    assert.equal(value.delivery?.state ?? "not_dispatched", metadata.delivery_state); assert.equal(value.notice.includes("本页模拟"), true);
    assert.equal(Object.keys(value).some((key) => key.startsWith("preview_")), false);
    assert.equal(record.body.total_bytes, Buffer.byteLength(record.body.content)); assert.equal(record.body.length_bytes, record.body.total_bytes);
    assert.equal(record.body.next_offset_bytes, record.body.total_bytes);
  }
  assert.deepEqual(savedState(backend), before); assert.equal(Object.keys(backend.state.runs).length, 0); assert.equal(backend.state.events.length, 0);
});

test("preview owner-question UTF-8 record pages keep revisions and never return partial characters", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  const item = backend.state.ownerQuestions[0]; item.message += "原问中段🙂。\n".repeat(30);
  const before = savedState(backend), first = await getJSON(backend, ownerQuestionsPath(item.task_id, { record_id: item.id, length_bytes: "113" }));
  assert.equal(first.status, 200); assert.equal(first.body.has_more, true);
  const pages = [first.body]; let offset = first.body.next_offset_bytes;
  for (let index = 0; index < first.body.total_bytes; index++) {
    const page = await getJSON(backend, ownerQuestionsPath(item.task_id, { record_id: item.id, record_revision: first.body.record_revision, offset_bytes: String(offset), length_bytes: "113" }));
    assert.equal(page.status, 200); assert.equal(page.body.offset_bytes, offset); assert.equal(page.body.record_revision, first.body.record_revision);
    assert.equal(page.body.content.includes("\ufffd"), false); assert.equal(page.body.length_bytes, Buffer.byteLength(page.body.content));
    pages.push(page.body); if (!page.body.has_more) break;
    assert.ok(page.body.next_offset_bytes > offset); offset = page.body.next_offset_bytes;
  }
  assert.equal(pages.at(-1).has_more, false);
  const content = pages.map((page) => page.content).join("");
  assert.equal(Buffer.byteLength(content), first.body.total_bytes); assert.equal(JSON.parse(content).message, item.message);
  const emojiOffset = Buffer.from(content).indexOf(Buffer.from("🙂")); assert.ok(emojiOffset >= 0);
  const middle = await getJSON(backend, ownerQuestionsPath(item.task_id, { record_id: item.id, record_revision: first.body.record_revision, offset_bytes: String(emojiOffset + 1) }));
  assert.equal(middle.status, 400); assert.equal(middle.body.detail.code, "invalid_offset");
  const tooShort = await getJSON(backend, ownerQuestionsPath(item.task_id, { record_id: item.id, record_revision: first.body.record_revision, offset_bytes: String(emojiOffset), length_bytes: "1" }));
  assert.equal(tooShort.status, 400); assert.equal(tooShort.body.detail.code, "output_budget");
  assert.deepEqual(savedState(backend), before);
});

test("preview owner questions reject mixed arguments, stale observations and unsupported inspection sections", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  const item = backend.state.ownerQuestions[0], path = (params) => ownerQuestionsPath(item.task_id, params);
  const before = savedState(backend), first = await getJSON(backend, path({ limit: "1" }));
  for (const params of [{ question_status: "unanswered" }, { status: "open" }, { agent_id: item.worker_id }, { query: "text" }, { record_revision: first.body.items[0].record_revision }, { offset_bytes: "1" }, { length_bytes: "20" }, { record_id: "incomplete" }, { record_id: item.id, question_status: "open" }, { record_id: item.id, cursor: first.body.next_cursor }, { record_id: item.id, offset_bytes: "1" }]) {
    const result = await getJSON(backend, path(params)); assert.equal(result.status, 400); assert.equal(result.body.detail.code, "invalid_arguments");
  }
  for (const params of [{ limit: "0" }, { limit: "101" }, { limit: "1.5" }, { offset_bytes: "-1" }, { offset_bytes: "" }, { length_bytes: "0" }, { cursor: "" }, { revision: "" }, { record_id: "" }, { record_revision: "" }]) assert.equal((await getJSON(backend, path(params))).status, 422);
  assert.equal((await getJSON(backend, `${path({})}&limit=1&limit=2`)).status, 422);
  for (const params of [{ cursor: "broken" }, { cursor: first.body.next_cursor, question_status: "all" }, { revision: "changed" }, { record_id: item.id, record_revision: "changed" }]) {
    const result = await getJSON(backend, path(params)); assert.equal(result.status, 409); assert.equal(result.body.detail.code, "evidence_changed");
  }
  for (const id of ["missing", backend.state.commitments.find((task) => task.id !== item.task_id).id]) {
    const result = await getJSON(backend, ownerQuestionsPath(id, { record_id: item.id })); assert.equal(result.status, 404); assert.equal(result.body.detail.code, "evidence_not_found");
  }
  for (const section of ["overview", "current_wait", "handoff", "reports", "unknown"]) {
    const result = await getJSON(backend, `/commitments/${item.task_id}/inspection?${new URLSearchParams({ section })}`);
    assert.equal(result.status, 501); assert.equal(result.body.preview, true); assert.equal(Object.hasOwn(result.body, "items"), false);
  }
  assert.equal((await getJSON(backend, `/commitments/${item.task_id}/inspection`)).status, 501);
  assert.equal((await backend.fetch(`/api${path({ record_id: item.id })}`, { method: "POST", body: JSON.stringify({ answer: "不能直接写问题" }) })).status, 501);
  assert.deepEqual(savedState(backend), before);
  item.message += "（示例改版）";
  for (const params of [{ revision: first.body.revision }, { cursor: first.body.next_cursor }, { record_id: item.id, record_revision: first.body.items[0].record_revision }]) {
    const result = await getJSON(backend, path(params)); assert.equal(result.status, 409); assert.equal(result.body.detail.code, "evidence_changed");
  }
});

test("preview owner-question unavailable windows and output budgets remain explicit and read-only", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  const item = backend.state.ownerQuestions[0]; item.preview_unavailable = true;
  const taskId = item.task_id, before = savedState(backend), page = await getJSON(backend, ownerQuestionsPath(taskId, { limit: "1" }));
  assert.equal(page.status, 200); assert.deepEqual(page.body.items, []); assert.equal(page.body.scanned, 1);
  assert.equal(page.body.omitted, 1); assert.equal(page.body.unavailable, 1); assert.equal(page.body.has_more, true); assert.ok(page.body.next_cursor);
  const record = await getJSON(backend, ownerQuestionsPath(taskId, { record_id: item.id }));
  assert.equal(record.status, 400); assert.equal(record.body.detail.code, "content_unavailable"); assert.deepEqual(savedState(backend), before);
  delete item.preview_unavailable;
  backend.state.settings.limits.max_output_chars = 32;
  for (const params of [{}, { record_id: item.id }]) {
    const result = await getJSON(backend, ownerQuestionsPath(taskId, params)); assert.equal(result.status, 400); assert.equal(result.body.detail.code, "output_budget");
  }
  assert.equal(Object.keys(backend.state.runs).length, 0); assert.equal(backend.state.events.length, 0);
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

test("the real app reads original preview questions and delivery evidence before cancelling an independent answer", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  const original = backend.state.ownerQuestions[0], claimed = backend.state.ownerQuestions.find((item) => item.delivery?.state === "claimed");
  original.message += "\n预览原问中段不能省略🙂。".repeat(700);
  const before = savedState(backend), pendingReads = [];
  const h = harness((path, options) => {
    const pending = (async () => {
      const result = await backend.fetch(`/api${path}`, options);
      assert.equal(result.status, 200, `真实 app 的 ${path} 必须由 preview backend 提供`);
      return result.json();
    })();
    pendingReads.push(pending); return pending;
  }, { team: true, composer: true });
  const settleReads = async () => { await Promise.all(pendingReads.splice(0)); await flush(); };
  const click = async (button) => { await clickMemoryButton(h, button); await settleReads(); };
  const main = backend.state.sessions[0], file = backend.state.uploads[0], attachments = [file], voice = { text: "仍未合并的普通语音草稿" };
  h.ui.state.connected = true; h.ui.state.loading = false;
  h.ui.state.status = { configured: true, preview: true, limits: backend.state.settings.limits };
  h.ui.state.mainSessionId = main.id; h.ui.state.activeSession = structuredClone(main);
  h.ui.state.attachments = attachments; h.ui.state.voiceDraft = voice; h.ui.renderComposerAttachments();
  const input = h.document.querySelector("#message-input"), draft = "这条普通聊天草稿仍需保留。";
  input.value = draft; await input.dispatch("input");
  h.ui.useTeamViews(); await h.ui.switchView("team"); await settleReads();
  await click(h.button("我的任务", h.other));
  await click(h.other.querySelector(`[data-task-identifier="${original.task_id}"]`));
  assert.ok(h.body().includes(backend.state.commitments.find((item) => item.id === original.task_id).brief.goal));
  assert.equal(backend.state.requests.some((entry) => entry.path.endsWith("/inspection")), false, "只有主动打开问题才读目录");
  await click(h.button("待回答的问题"));
  assert.ok(h.body().includes("原任务已暂停")); assert.ok(h.body().includes("本页遗漏"));
  let filter = h.dialog.querySelector('[aria-label="问题状态"]');
  filter.value = "all"; await filter.dispatch("change"); await settleReads();
  await click(h.dialog.querySelector(`[data-owner-question="${claimed.id}"]`));
  assert.ok(h.body().includes("原队友已领取答案，任务是否完成仍需核查"));
  assert.ok(h.body().includes(claimed.answer.owner.body)); assert.ok(h.body().includes("资料整理"));
  assert.equal(h.button("到主对话交回原答") === undefined, true);
  filter = h.dialog.querySelector('[aria-label="问题状态"]');
  filter.value = "open"; await filter.dispatch("change"); await settleReads();
  await click(h.dialog.querySelector(`[data-owner-question="${original.id}"]`));
  assert.equal(h.button("到主对话回答") === undefined, true, "半页 JSON 不应成为可回答的完整问题");
  assert.ok(h.button("继续读取问题"));
  for (let index = 0; index < 10 && h.button("继续读取问题"); index++) await click(h.button("继续读取问题"));
  assert.equal(h.button("继续读取问题") === undefined, true); assert.ok(h.body().includes(original.message));
  assert.ok(h.body().includes(original.author.run_id)); assert.ok(h.body().includes(file.id));
  await click(h.button("到主对话回答"));
  assert.equal(h.ui.state.view, "chat"); assert.equal(h.dialog.open, true);
  assert.equal(h.document.querySelector("#dialog-title").textContent, "回答原队友的问题");
  const answer = h.dialog.querySelector('[aria-label="给原队友的答案"]');
  assert.equal(answer.value, ""); answer.value = "取消前尚未提交的独立答案"; await answer.dispatch("input");
  const action = h.dialog.querySelector('[aria-label="答案处理方式"]');
  assert.deepEqual(action.children.map((option) => option.value), ["record_only"], "暂停任务只提供保存答案");
  assert.equal(input.value, draft); assert.equal(h.ui.state.attachments === attachments, true); assert.equal(h.ui.state.voiceDraft === voice, true);
  await click(h.document.querySelector("#dialog-close"));
  assert.equal(h.dialog.open, false); assert.equal(input.value, draft);
  assert.equal(h.ui.state.attachments === attachments, true); assert.equal(h.ui.state.attachments[0] === file, true); assert.equal(h.ui.state.voiceDraft === voice, true);
  assert.ok(h.document.querySelector("#composer-attachments").textContent.includes(file.name));
  assert.ok(backend.state.requests.every((entry) => entry.method === "GET")); assert.deepEqual(savedState(backend), before);
});
