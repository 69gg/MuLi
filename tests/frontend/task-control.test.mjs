import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";
import { createTaskControl, restoreTaskControlRequest, taskControlView } from "../../web/task-control.mjs";

function current(revision = 1, control = {}, scope = {}) {
  return { id: "task", revision, control: { epoch: 0, approved_epoch: 0, replan_request_id: null, ...control }, scope: { member_count: 3, active_workflow_count: 0, can_pause: true, blocked_reason: null, ...scope } };
}
function detail(revision = 1, control = {}, scope = {}) {
  return { ...current(revision, control, scope), title: "核查", brief: { goal: "完整目标", constraints: ["只读"], evidence_refs: [] } };
}
function ready(value = detail()) {
  let ids = 0;
  const model = createTaskControl("task", { createRequestId: () => `request-${++ids}` });
  model.acceptDetail(value);
  return model;
}
function result(body, value) { return { receipt: { id: "receipt", request_id: body.request_id, action: body.action }, current: value }; }

test("2.2 records without control remain readable and have no writable capability", () => {
  const model = ready({ id: "task", revision: 2, brief: { goal: "旧任务" } });
  assert.equal(model.state.detail.brief.goal, "旧任务");
  assert.equal(taskControlView(model.state).capable, false);
  assert.throws(() => model.begin("pause"), /刷新/);
  assert.equal(model.acceptDetail({ ...detail(), id: "different" }), false);
});

test("same epoch approval is ordered by revision across GET and write responses", () => {
  const model = ready(detail(2, { epoch: 1 }));
  model.acceptCurrent(current(4, { epoch: 1, approved_epoch: 1 }));
  assert.equal(model.acceptDetail(detail(3, { epoch: 1 })), false);
  assert.equal(taskControlView(model.state).paused, false);
  assert.equal(model.state.current.revision, 4);
  assert.equal(taskControlView(model.state).fresh, false);
  assert.equal(model.acceptDetail(detail(4, { epoch: 1, approved_epoch: 1 })), true);
  assert.equal(taskControlView(model.state).fresh, true);
});

test("compact write projections preserve plan text and never relabel old brief revisions", () => {
  const plan = { epoch: 1, text: "先核查再继续" };
  const model = ready(detail(4, { epoch: 1, approved_epoch: 1, plan, has_plan: true, plan_epoch: 1 }));
  const body = model.begin("pause");
  assert.deepEqual(body, { action: "pause", expected_epoch: 1, request_id: "request-1" });
  assert.equal(taskControlView(model.state).paused, false, "no optimistic pause");
  model.complete(result(body, current(5, { epoch: 2, approved_epoch: 1, has_plan: true, plan_epoch: 1 })));
  assert.deepEqual(taskControlView(model.state).plan, plan);
  assert.equal(model.state.detail.revision, 4);
  assert.equal(model.state.current.revision, 5);
  assert.equal(taskControlView(model.state).canReplan, false, "read full updated brief first");
});

test("pure pause and replan intent are distinguished only by committed control", () => {
  const model = ready();
  const pause = model.begin("pause");
  model.complete(result(pause, current(2, { epoch: 1 })));
  assert.equal(taskControlView(model.state).label, "已暂停后续操作");
  model.acceptDetail(detail(2, { epoch: 1 }));
  model.edit();
  const replan = model.begin("request_replan");
  assert.equal(replan.additional_requirements, "", "empty supplement is an explicit request");
  assert.equal(taskControlView(model.state).label, "已暂停后续操作");
  model.complete(result(replan, current(3, { epoch: 2, replan_request_id: "owner-replan" })));
  assert.equal(taskControlView(model.state).label, "已交沐璃 · 仍暂停");
});

test("live detail updates preserve editing and unsent requirements", () => {
  const model = ready(detail(2, { epoch: 1 }));
  model.edit(); model.setDraft("新增要求\n保留中段");
  model.acceptDetail({ ...detail(3, { epoch: 1 }), brief: { goal: "完整目标", constraints: ["只读", "新要求"] } });
  assert.equal(model.state.editing, true);
  assert.equal(model.state.draft, "新增要求\n保留中段");
  assert.equal(model.state.conflict, true);
  model.cancelEdit();
  assert.equal(taskControlView(model.state).paused, true);
  assert.equal(model.state.draft, "");
});

test("unknown submission keeps an immutable body and stable ID across edit attempts and restore", () => {
  const model = ready(detail(2, { epoch: 1 }));
  model.edit(); model.setDraft("只读，不写入");
  const first = model.begin("request_replan");
  model.fail(new Error("offline"));
  model.setDraft("不能替换原请求"); model.cancelEdit();
  assert.equal(model.state.draft, "只读，不写入");
  assert.equal(taskControlView(model.state).unknown, true);
  const reloaded = createTaskControl("task", { pending: JSON.parse(JSON.stringify(model.state.submission)) });
  reloaded.acceptDetail(detail(3, { epoch: 2, replan_request_id: "committed-but-response-lost" }));
  assert.deepEqual(reloaded.begin("request_replan"), first);
  assert.equal(taskControlView(reloaded.state).busy, true);
});

test("rejected admission preserves the committed pause and editable requirements without an unknown retry", () => {
  for (const [status, code, message] of [[503, "model_unconfigured", "尚未配置主模型"], [429, "queue_full", "待处理任务已满"]]) {
    const model = ready(detail(2, { epoch: 1 }));
    model.edit(); model.setDraft("仍须只读");
    const first = model.begin("request_replan");
    model.fail({ status, message, details: { detail: { code, message } } });
    assert.equal(model.state.phase, "ready");
    assert.equal(model.state.submission, null);
    assert.equal(model.state.error, message);
    assert.equal(model.state.errorCode, code);
    assert.equal(model.state.editing, true);
    assert.equal(model.state.draft, "仍须只读");
    assert.equal(taskControlView(model.state).paused, true);
    assert.equal(taskControlView(model.state).canReplan, true);
    assert.notEqual(model.begin("request_replan").request_id, first.request_id, "a new explicit attempt uses a new ID after definite rejection");
  }
});

test("unrecognized server failures retain the original request instead of assuming no commit", () => {
  for (const [status, code] of [[500, "internal_error"], [503, "service_unavailable"], [503, undefined]]) {
    const model = ready();
    const body = model.begin("pause");
    model.fail({ status, message: "服务暂不可用", details: { detail: { code } } });
    assert.equal(taskControlView(model.state).unknown, true);
    assert.equal(model.state.submission, body);
    assert.equal(model.begin("pause"), body);
  }
});

test("late original receipt cannot roll back newer state or erase the approved plan", () => {
  const model = ready(detail(2, { epoch: 1 }));
  const body = model.begin("request_replan");
  model.fail(new Error("response lost"));
  const plan = { epoch: 3, text: "更新后的计划" };
  model.acceptDetail(detail(8, { epoch: 3, approved_epoch: 3, plan, plan_epoch: 3 }));
  model.begin("request_replan");
  model.complete(result(body, current(3, { epoch: 2, replan_request_id: "older-intent" })));
  assert.equal(model.state.current.revision, 8);
  assert.equal(taskControlView(model.state).paused, false);
  assert.deepEqual(taskControlView(model.state).plan, plan);
  assert.equal(model.state.submission, null);
});

test("409 applies only the current projection, preserves draft, and requires a new explicit submission", () => {
  const model = ready(detail(2, { epoch: 1 }));
  model.edit(); model.setDraft("待核对的补充");
  const body = model.begin("request_replan");
  model.fail({ status: 409, message: "任务要求已有更新", details: { detail: { code: "epoch_conflict", current: current(5, { epoch: 3 }) } } });
  assert.equal(model.state.submission, null);
  assert.equal(model.state.draft, "待核对的补充");
  assert.equal(model.state.editing, true);
  assert.equal(model.state.conflict, true);
  assert.equal(taskControlView(model.state).canReplan, false);
  model.acceptDetail(detail(5, { epoch: 3 }));
  const retry = model.begin("request_replan");
  assert.notEqual(retry.request_id, body.request_id);
  assert.equal(retry.expected_epoch, 3);
});

test("workflow conflict never invents a paused state or a partial success", () => {
  const model = ready();
  model.begin("pause");
  model.fail({ status: 409, message: "workflow", details: { detail: { code: "workflow_active", current: current(1, {}, { can_pause: false, blocked_reason: "workflow_active", active_workflow_count: 1 }) } } });
  const view = taskControlView(model.state);
  assert.equal(view.paused, false);
  assert.equal(view.workflowBlocked, true);
  assert.equal(view.canPause, false);
  assert.equal(model.state.current.control.epoch, 0);
  assert.match(model.state.error, /先停止工作流/);
});

test("401 discards detail, pending body, and unsubmitted requirements", () => {
  const model = ready(detail(2, { epoch: 1 }));
  model.edit(); model.setDraft("私人要求"); const body = model.begin("request_replan");
  model.fail({ status: 401 });
  assert.equal(model.state.current, null);
  assert.equal(model.state.detail, null);
  assert.equal(model.state.submission, null);
  assert.equal(model.state.draft, "");
  assert.throws(() => model.complete(result(body, current(3, { epoch: 2, replan_request_id: "late" }))), /待确认/);
  assert.equal(model.state.current, null, "a late response cannot restore cleared private detail");
});

test("malformed or mismatched receipts remain unknown instead of claiming success", () => {
  const model = ready();
  model.begin("pause");
  assert.throws(() => model.complete({ receipt: { action: "pause", request_id: "other" }, current: current(2, { epoch: 1 }) }), /不匹配/);
  model.fail(new Error("invalid receipt"));
  assert.equal(taskControlView(model.state).unknown, true);
  assert.equal(taskControlView(model.state).paused, false);
  for (const invalid of [null, {}, { action: "resume", expected_epoch: 0, request_id: "x" }, { action: "pause", expected_epoch: true, request_id: "x" }]) assert.equal(restoreTaskControlRequest(invalid), null);
});

// Exercise the actual application renderer. The tiny DOM double preserves the
// browser's variadic replaceChildren behavior: an array is text, not child nodes.
class DOMElement {
  constructor(tagName) { this.tagName = tagName.toUpperCase(); this.children = []; this.dataset = {}; this.classList = { toggle() {} }; }
  append(...children) { this.children.push(...children.map((child) => child instanceof DOMElement ? child : String(child))); }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  contains(node) { return this === node || this.children.some((child) => child instanceof DOMElement && child.contains(node)); }
}
class DOMButton extends DOMElement {}
function domElement(tag, attributes = {}, ...children) {
  const node = tag === "button" ? new DOMButton(tag) : new DOMElement(tag);
  Object.assign(node, attributes);
  if (attributes.text !== undefined) node.textContent = attributes.text;
  node.append(...children.flat(Infinity));
  return node;
}
const app = await readFile(new URL("../../web/app.mjs", import.meta.url), "utf8");
const rendererStart = app.indexOf("function renderTaskBriefDialog(");
const rendererEnd = app.indexOf("\nasync function refreshTaskBriefDialog(", rendererStart);
assert.ok(rendererStart >= 0 && rendererEnd > rendererStart);
const renderDialog = runInNewContext(`(${app.slice(rendererStart, rendererEnd)})`, {
  taskControlView, taskDialogCurrent: () => true, document: { activeElement: null }, element: domElement,
  $: () => null, $$: () => [],
});
function dialogSlot(model) {
  const slot = { model, loading: false, readError: "", renderedDetail: model.state.detail };
  for (const key of ["content", "brief", "readStatus", "panel", "status", "scope", "note", "error", "editor", "input", "actions", "plan"]) slot[key] = domElement("div");
  return slot;
}

test("the live task control renderer inserts button nodes in every actionable state", () => {
  const states = [ready(), ready(detail(2, { epoch: 1 })), ready(detail(2, { epoch: 1 }))];
  states[2].edit();
  for (const model of states) {
    const slot = dialogSlot(model);
    renderDialog(slot);
    assert.ok(slot.actions.children.length >= 2);
    assert.ok(slot.actions.children.every((node) => node instanceof DOMButton), "replaceChildren must receive the actual button nodes, not an array coerced to text");
    assert.ok(slot.actions.children.every((node) => node.type === "button"));
  }
});

test("a newly paused epoch labels the unchanged previous plan as historical", () => {
  const plan = { epoch: 1, text: "先检查再汇报" };
  const model = ready(detail(4, { epoch: 1, approved_epoch: 1, plan, plan_epoch: 1 }));
  const slot = dialogSlot(model);
  renderDialog(slot);
  assert.equal(slot.plan.children[0].children[0].textContent, "已确认计划");
  const body = model.begin("pause");
  model.complete(result(body, current(5, { epoch: 2, approved_epoch: 1, plan_epoch: 1 })));
  renderDialog(slot);
  assert.equal(slot.status.textContent, "已暂停后续操作");
  assert.equal(slot.plan.children[0].children[0].textContent, "此前计划");
  assert.equal(slot.plan.children[0].children[1].textContent, plan.text);
});
