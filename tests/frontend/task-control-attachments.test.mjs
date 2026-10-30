import test from "node:test";
import assert from "node:assert/strict";
import { ApiError } from "../../web/api.mjs";
import { createPreviewBackend } from "../../web/preview.mjs";
import { clickMemoryButton, deferred, flush, harness } from "./helpers/app-dom-harness.mjs";

const taskId = "original-task";
const oldFile = { id: "a".repeat(32), name: "原始要求.md", mime_type: "text/markdown", size: 12 };
const firstFile = { id: "b".repeat(32), name: "补充资料一.md", mime_type: "text/markdown", size: 24 };
const secondFile = { id: "c".repeat(32), name: "补充资料二.txt", mime_type: "text/plain", size: 36 };
const fullTask = () => ({
  id: taskId, title: "原队友的受控任务", status: "in_progress", revision: 4,
  brief: { goal: "保留完整原目标", constraints: ["保留原限制"], evidence_refs: [{ kind: "attachment", id: oldFile.id }] },
  control: { epoch: 1, approved_epoch: 0, replan_request_id: null },
  scope: { member_count: 1, active_workflow_count: 0, can_pause: true, blocked_reason: null },
});

async function taskHarness(control = async () => { throw new Error("unexpected control request"); }, { upload } = {}) {
  const h = harness(async (path, options) => {
    if (path.startsWith("/team?")) return { members: [{ id: "original-worker", name: "原队友", status: "idle", commitment_id: taskId }], messages: [], total: 1, limit: 50, offset: 0, active_only: false, has_more: false };
    if (path === `/commitments/${taskId}`) return fullTask();
    assert.equal(path, `/commitments/${taskId}/control`);
    return control(options.body);
  }, { team: true, composer: true, ...(upload ? { upload } : {}) });
  h.ui.state.connected = true; h.ui.state.loading = false;
  h.ui.state.mainSessionId = "main"; h.ui.state.activeSession = { id: "main", mode: "auto", status: "idle" };
  h.ui.state.status = { configured: true, model: "fixture-model" };
  h.ui.state.uploads = [oldFile, firstFile, secondFile];
  h.ui.useTeamViews(); await h.ui.switchView("team");
  assert.ok(h.other.textContent.includes("原队友"), "先到达现有真实团队入口");
  return h;
}

async function openTask(h) {
  await clickMemoryButton(h, h.other.querySelector('[data-action="view-task-brief"]'));
  assert.ok(h.body().includes("保留完整原目标"), "真实任务说明须已读回");
  assert.ok(h.body().includes("保留原限制"));
}

const controls = (h) => h.requests.filter((entry) => entry.path === `/commitments/${taskId}/control` && entry.method === "POST");
const received = (body, revision = 5) => ({ receipt: { id: "receipt", action: body.action, request_id: body.request_id }, current: { ...fullTask(), revision, control: { epoch: 2, approved_epoch: 0, replan_request_id: "committed-original" } } });
const localFile = () => Object.assign(new Blob(["完整新资料🙂"], { type: "text/markdown" }), { name: "新资料.md" });
const uploadedFile = { id: "d".repeat(32), name: "新资料.md", mime_type: "text/markdown", size: 24 };

async function editTask(h) { await openTask(h); await clickMemoryButton(h, h.button("补充要求并交沐璃")); }
async function choose(h, id) {
  const select = h.dialog.querySelector('[aria-label="选择已有资料"]'); assert.ok(select);
  select.value = id; await select.dispatch("change"); await flush();
}
async function upload(h, file = localFile()) {
  const input = h.dialog.querySelector('input[type="file"][name="additional_attachments"]'); assert.ok(input);
  let opened = false; input.click = () => { opened = true; };
  await clickMemoryButton(h, h.button("添加资料")); assert.equal(opened, true, "真实资料按钮应打开独立文件选择器");
  input.files = [file]; await input.dispatch("change"); await flush(); return file;
}

async function replaceSurface(h, boundary) {
  if (boundary === "close") { h.dialog.close(); await flush(); }
  else if (boundary === "leave") { await h.ui.switchView("chat"); await h.ui.switchView("team"); }
  else {
    if (boundary === "logout") h.ui.showLogin();
    if (boundary === "pagehide") h.windowEvents.pagehide();
    h.ui.activateConnection(); h.ui.state.connected = true; h.ui.state.auth.authenticated = true; await flush();
  }
}

test("the real paused task supplement offers its own attachment entry without touching the chat draft", async () => {
  const h = await taskHarness();
  const input = h.document.querySelector("#message-input"); input.value = "主聊天未发送草稿";
  h.ui.state.attachments = [oldFile];
  await openTask(h);
  await clickMemoryButton(h, h.button("补充要求并交沐璃"));
  assert.ok(h.dialog.querySelector('textarea[name="additional_requirements"]'), "既有文字补充入口应已成功打开");
  assert.ok(h.button("交沐璃重新规划"));
  assert.equal(input.value, "主聊天未发送草稿");
  assert.equal(h.ui.state.attachments[0] === oldFile, true);
  assert.ok(h.button("添加资料"), "补充要求旁应提供任务独立的资料入口");
  assert.ok(h.dialog.querySelector('input[type="file"][name="additional_attachments"]'), "资料须先经原附件上传设施保存");
});

test("the real unknown retry preserves the saved ordered attachment list in its original control body", async () => {
  const pending = { action: "request_replan", expected_epoch: 1, request_id: "original-evidence-request", additional_requirements: "按原要求核对两份资料", additional_attachment_ids: [secondFile.id, firstFile.id] };
  const h = await taskHarness(async (body) => ({ receipt: { id: "receipt", action: body.action, request_id: body.request_id }, current: { ...fullTask(), revision: 5, control: { epoch: 2, approved_epoch: 0, replan_request_id: "committed-original" } } }));
  h.taskStorage.setItem(`muli.task-control:${taskId}`, JSON.stringify(pending));
  await openTask(h);
  assert.ok(h.body().includes("提交结果待确认"), "先到达既有未知提交恢复状态");
  await clickMemoryButton(h, h.button("重试原操作"));
  const controls = h.requests.filter((entry) => entry.path === `/commitments/${taskId}/control` && entry.method === "POST");
  assert.equal(controls.length, 1, "只允许用户主动核对原请求一次");
  assert.deepEqual(JSON.parse(JSON.stringify(controls[0].body)), pending, "原 request_id、epoch、文字与附件顺序都须原样核对");
});

test("real task upload and existing selection submit only their ordered task references and preserve the main composer", async () => {
  const pending = deferred(), uploads = [];
  const h = await taskHarness(async (body) => received(body), { upload: (file, options) => { uploads.push({ file, ...options }); return pending.promise; } });
  const composer = h.document.querySelector("#message-input"); composer.value = "原主聊天草稿🙂"; h.ui.state.attachments = [oldFile];
  await editTask(h); await choose(h, secondFile.id);
  const requirement = h.dialog.querySelector("textarea"); requirement.value = "保留原目标，并核对补充资料"; await requirement.dispatch("input");
  const retainedSubmit = h.button("交沐璃重新规划");
  const file = await upload(h);
  assert.equal(uploads.length, 1); assert.equal(uploads[0].file === file, true);
  assert.equal(uploads[0].signal.aborted, false); assert.equal(h.ui.state.uploading, 0);
  assert.equal(h.button("交沐璃重新规划").disabled, true); assert.ok(h.body().includes("正在上传资料"));
  await clickMemoryButton(h, retainedSubmit); await clickMemoryButton(h, h.button("交沐璃重新规划"));
  assert.equal(controls(h).length, 0, "上传期间的新旧提交按钮都不能提交");
  pending.resolve(uploadedFile); await flush();
  assert.equal(h.button("交沐璃重新规划").disabled, false);
  assert.deepEqual([...h.ui.taskControlSlot.model.state.attachmentIds], [secondFile.id, uploadedFile.id]);
  assert.ok(h.body().includes(uploadedFile.name)); assert.ok(h.ui.state.uploads.some((entry) => entry.id === uploadedFile.id));
  await clickMemoryButton(h, h.button("交沐璃重新规划"));
  assert.equal(controls(h).length, 1);
  assert.equal(controls(h)[0].body.additional_requirements, "保留原目标，并核对补充资料");
  assert.deepEqual([...controls(h)[0].body.additional_attachment_ids], [secondFile.id, uploadedFile.id]);
  assert.equal(composer.value, "原主聊天草稿🙂"); assert.equal(h.ui.state.attachments[0] === oldFile, true);
});

test("failed task uploads can be retried or removed before an unchanged empty-attachment request is submitted", async () => {
  const file = localFile(); let attempts = 0;
  const h = await taskHarness(async (body) => received(body), { upload: async (value) => { assert.equal(value === file, true); if (++attempts !== 2) throw new Error("本次上传失败"); return uploadedFile; } });
  await editTask(h); await upload(h, file);
  assert.ok(h.body().includes("本次上传失败")); assert.equal(h.button("交沐璃重新规划").disabled, true);
  await clickMemoryButton(h, h.button("重试上传"));
  assert.equal(attempts, 2); assert.deepEqual([...h.ui.taskControlSlot.model.state.attachmentIds], [uploadedFile.id]);
  assert.equal(h.ui.taskControlSlot.failedUploads.length, 0);
  await clickMemoryButton(h, h.dialog.querySelectorAll("button").find((button) => button.getAttribute("aria-label") === "移除资料 新资料.md"));
  assert.equal(h.button("交沐璃重新规划").disabled, false);
  await upload(h, file); assert.equal(attempts, 3);
  await clickMemoryButton(h, h.dialog.querySelectorAll("button").find((button) => button.getAttribute("aria-label") === "移除失败资料 新资料.md"));
  assert.equal(h.button("重试上传") === undefined, true); assert.equal(h.button("交沐璃重新规划").disabled, false);
  await clickMemoryButton(h, h.button("交沐璃重新规划"));
  assert.equal(controls(h).length, 1); assert.equal(Object.hasOwn(controls(h)[0].body, "additional_attachment_ids"), false);
});

test("a lost task response keeps the selected list frozen through close and explicit original retry", async () => {
  let attempt = 0;
  const h = await taskHarness(async (body) => { if (++attempt === 1) throw new Error("response lost"); return received(body); });
  await editTask(h); await choose(h, secondFile.id); await choose(h, firstFile.id);
  const input = h.dialog.querySelector("textarea"); input.value = "原补充要求🙂"; await input.dispatch("input");
  await clickMemoryButton(h, h.button("交沐璃重新规划"));
  const model = h.ui.taskControlSlot.model, body = model.state.submission;
  assert.equal(model.state.phase, "unknown"); assert.equal(Object.isFrozen(body.additional_attachment_ids), true);
  model.addAttachment(oldFile.id); model.removeAttachment(secondFile.id); model.setDraft("不能替换");
  assert.deepEqual([...model.state.attachmentIds], [secondFile.id, firstFile.id]); assert.equal(model.state.draft, "原补充要求🙂");
  assert.equal(h.dialog.querySelector("textarea").disabled, true); assert.equal(h.dialog.querySelector('[aria-label="选择已有资料"]').disabled, true);
  h.dialog.close(); await flush(); h.ui.state.uploads = [];
  await openTask(h);
  assert.ok(h.body().includes(secondFile.id)); assert.ok(h.body().includes(firstFile.id));
  assert.equal(controls(h).length, 1, "重开不得自动重放");
  await clickMemoryButton(h, h.button("重试原操作"));
  assert.equal(controls(h).length, 2); assert.equal(controls(h)[1].body === body, true);
  assert.deepEqual([...controls(h)[1].body.additional_attachment_ids], [secondFile.id, firstFile.id]);
  assert.equal(h.savedTaskControls.size, 0);
});

for (const boundary of ["close", "leave", "reconnect", "logout", "pagehide"]) {
  test(`late task upload success and 401 cannot replace the current attachment list after ${boundary}`, async () => {
    for (const denied of [false, true]) {
      const pending = deferred(); let signal;
      const h = await taskHarness(undefined, { upload: (_file, options) => { signal = options.signal; return pending.promise; } });
      await editTask(h); await upload(h);
      const previous = h.ui.taskControlSlot;
      await replaceSurface(h, boundary); assert.equal(signal.aborted, true);
      await editTask(h); await choose(h, firstFile.id);
      const current = h.ui.taskControlSlot; assert.equal(current === previous, false);
      if (denied) pending.reject(new ApiError("旧主人的拒绝", 401)); else pending.resolve(uploadedFile);
      await flush();
      assert.equal(h.ui.taskControlSlot === current, true); assert.equal(h.ui.state.auth.authenticated, true);
      assert.deepEqual([...current.model.state.attachmentIds], [firstFile.id]);
      assert.equal(h.ui.state.uploads.some((entry) => entry.id === uploadedFile.id), false);
      assert.equal(h.body().includes("旧主人的拒绝"), false);
    }
  });
}

for (const boundary of ["close", "reconnect", "pagehide"]) {
  test(`a pending task write becomes unknown and its late receipt cannot overwrite a reopened dialog after ${boundary}`, async () => {
    const pending = deferred();
    const h = await taskHarness(() => pending.promise);
    await editTask(h); await choose(h, secondFile.id); await choose(h, firstFile.id);
    await clickMemoryButton(h, h.button("交沐璃重新规划"));
    const model = h.ui.taskControlSlot.model, body = model.state.submission;
    assert.equal(model.state.phase, "submitting");
    await replaceSurface(h, boundary); await openTask(h);
    const reopened = h.ui.taskControlSlot;
    assert.equal(reopened.model === model, true); assert.equal(model.state.phase, "unknown");
    assert.equal(controls(h).length, 1); assert.equal(model.state.submission === body, true);
    pending.resolve(received(body, 99)); await flush();
    assert.equal(h.ui.taskControlSlot === reopened, true); assert.equal(model.state.phase, "unknown");
    assert.equal(model.state.current.revision, 4); assert.deepEqual([...model.state.attachmentIds], [secondFile.id, firstFile.id]);
    assert.equal(model.state.submission === body, true); assert.ok(h.button("重试原操作"));
  });
}

test("a late prior owner's control 401 cannot clear a new owner's visible task editor", async () => {
  const pending = deferred(), h = await taskHarness(() => pending.promise);
  await editTask(h); await choose(h, secondFile.id); await clickMemoryButton(h, h.button("交沐璃重新规划"));
  await replaceSurface(h, "logout"); await editTask(h); await choose(h, firstFile.id);
  const current = h.ui.taskControlSlot;
  pending.reject(new ApiError("旧主人已退出", 401)); await flush();
  assert.equal(h.ui.state.auth.authenticated, true); assert.equal(h.ui.taskControlSlot === current, true);
  assert.deepEqual([...current.model.state.attachmentIds], [firstFile.id]); assert.equal(current.model.state.phase, "ready");
  assert.equal(h.savedTaskControls.size, 0);
});

test("preview task controls retain original and appended attachment refs across reopening without model runs", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  const task = backend.state.commitments.find((item) => item.control), initial = structuredClone(task.brief);
  const path = `/api/commitments/${task.id}/control`, file = localFile();
  const saved = await backend.fetch("/api/uploads", { method: "POST", body: file, headers: { "X-File-Name": encodeURIComponent(file.name), "Content-Type": file.type } });
  assert.equal(saved.status, 200); const uploaded = await saved.json();
  const body = { action: "request_replan", expected_epoch: task.control.epoch, request_id: "preview-original-request", additional_requirements: "补充核对新版本", additional_attachment_ids: [uploaded.id] };
  const post = (value) => backend.fetch(path, { method: "POST", body: JSON.stringify(value) });
  const result = await post(body); assert.equal(result.status, 200); const receipt = await result.json();
  const read = await (await backend.fetch(`/api/commitments/${task.id}`)).json();
  assert.equal(read.brief.goal, initial.goal); assert.deepEqual(read.brief.constraints, [...initial.constraints, body.additional_requirements]);
  assert.deepEqual(read.brief.evidence_refs, [...initial.evidence_refs, { kind: "attachment", id: uploaded.id }]);
  assert.equal(read.control.paused, true); assert.equal(read.control.replan_request_id, receipt.receipt.id);
  backend.state.files.delete(uploaded.id);
  const duplicate = await post(body); assert.equal(duplicate.status, 200);
  assert.deepEqual((await duplicate.json()).receipt, receipt.receipt); assert.equal(task.revision, read.revision);
  assert.equal((await post({ ...body, additional_attachment_ids: [backend.state.uploads.find((entry) => entry.id !== uploaded.id).id] })).status, 409);
  const reopened = createPreviewBackend({ state: backend.state }); t.after(() => reopened.dispose());
  const reloaded = await (await reopened.fetch(`/api/commitments/${task.id}`)).json(); assert.deepEqual(reloaded.brief, read.brief);
  assert.equal(Object.keys(backend.state.runs).length, 0); assert.deepEqual(backend.state.messages[backend.state.sessions[0].id], []);
});

test("the real preview DOM uploads evidence, preserves the saved brief and checks the same lost response request", async (t) => {
  const backend = createPreviewBackend(); t.after(() => backend.dispose());
  const task = backend.state.commitments.find((item) => item.control), original = structuredClone(task.brief), main = backend.state.sessions[0];
  let loseResponse = true;
  const read = async (path, options = {}) => {
    const response = await backend.fetch(`/api${path}`, options), value = await response.json();
    if (!response.ok) throw new ApiError(typeof value.detail === "string" ? value.detail : value.detail.message, response.status, value);
    if (path === `/commitments/${task.id}/control` && loseResponse) { loseResponse = false; throw new Error("committed response lost"); }
    return value;
  };
  const h = harness(read, { team: true, composer: true, upload: (file, options) => read("/uploads", { ...options, method: "POST", body: file, headers: { "X-File-Name": encodeURIComponent(file.name), "Content-Type": file.type } }) });
  h.ui.state.connected = true; h.ui.state.loading = false; h.ui.state.mainSessionId = main.id; h.ui.state.activeSession = main;
  h.ui.state.status = { configured: true }; h.ui.state.uploads = structuredClone(backend.state.uploads);
  const composer = h.document.querySelector("#message-input"); composer.value = "不要覆盖主草稿"; h.ui.state.attachments = [oldFile];
  h.ui.useTeamViews(); await h.ui.switchView("team");
  await clickMemoryButton(h, h.other.querySelector('[data-action="view-task-brief"]'));
  await clickMemoryButton(h, h.button("补充要求并交沐璃")); await upload(h);
  const ids = [...h.ui.taskControlSlot.model.state.attachmentIds]; assert.equal(ids.length, 1);
  const input = h.dialog.querySelector("textarea"); input.value = "按补充资料核对版本"; await input.dispatch("input");
  await clickMemoryButton(h, h.button("交沐璃重新规划"));
  const body = h.ui.taskControlSlot.model.state.submission;
  assert.equal(h.ui.taskControlSlot.model.state.phase, "unknown");
  assert.equal(task.brief.goal, original.goal); assert.deepEqual(task.brief.constraints, [...original.constraints, input.value]);
  assert.deepEqual(task.brief.evidence_refs, [...original.evidence_refs, { kind: "attachment", id: ids[0] }]);
  assert.equal(backend.state.taskControlReceipts.length, 1);
  h.dialog.close(); await flush();
  await clickMemoryButton(h, h.other.querySelector('[data-action="view-task-brief"]'));
  assert.ok(h.body().includes("按补充资料核对版本")); assert.ok(h.body().includes("新资料.md"));
  assert.equal(h.requests.filter((entry) => entry.method === "POST").length, 1);
  await clickMemoryButton(h, h.button("重试原操作"));
  const posts = h.requests.filter((entry) => entry.method === "POST");
  assert.equal(posts.length, 2); assert.equal(posts[1].body === body, true);
  assert.equal(backend.state.taskControlReceipts.length, 1); assert.equal(h.savedTaskControls.size, 0);
  assert.equal(composer.value, "不要覆盖主草稿"); assert.equal(h.ui.state.attachments[0] === oldFile, true);
  assert.equal(Object.keys(backend.state.runs).length, 0); assert.deepEqual(backend.state.messages[main.id], []);
});
