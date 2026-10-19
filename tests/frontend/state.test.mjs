import test from "node:test";
import assert from "node:assert/strict";
import { applyEvent, createSessionState, hydrateSession, isRunning, toolStatusLabel, safeURL, parseJSONField, formatTimestamp, toDateTimeLocal, fromDateTimeLocal, messagePresentation } from "../../web/state.mjs";

test("duplicate SSE replay never duplicates streamed tokens or tool calls", () => {
  let state = applyEvent(createSessionState(), { id: 1, type: "run.started", data: { run_id: "r1" } });
  const delta = { id: 2, type: "message.delta", data: { content: "你好" } };
  state = applyEvent(state, delta);
  const repeated = applyEvent(state, delta);
  assert.equal(repeated, state);
  assert.equal(state.messages[0].content, "你好");
  state = applyEvent(state, { id: 3, type: "tool.started", data: { call_id: "t1", name: "read_file", arguments: { path: "README.md" } } });
  state = applyEvent(state, { id: 4, type: "tool.completed", data: { call_id: "t1", name: "read_file", result: "内容" } });
  assert.equal(state.tools.length, 1);
  assert.equal(state.tools[0].status, "completed");
  assert.deepEqual(state.tools[0].arguments, { path: "README.md" });
});

test("persisted messages and historical deltas hydrate without duplicate assistant text", () => {
  const state = hydrateSession({
    messages: [{ id: "m1", role: "assistant", content: "完整回复" }],
    events: [{ id: 1, type: "run.started", data: { run_id: "r1" } }, { id: 2, type: "message.delta", data: { content: "完整" } }, { id: 3, type: "message.completed", data: { message: { id: "m1", role: "assistant", content: "完整回复" } } }, { id: 4, type: "run.completed", data: {} }],
  });
  assert.equal(state.messages.length, 1);
  assert.equal(state.messages[0].content, "完整回复");
  assert.equal(state.status, "completed");
  assert.equal(state.cursor, "4");
});

test("session reload restores only the unfinished reply and resumes without repeated tokens", () => {
  const events = [
    { id: 1, type: "run.started", data: { id: "run-one" } },
    { id: 2, type: "message.delta", data: { content: "已保存的回复" } },
    { id: 3, type: "message.completed", data: { message: { role: "assistant", id: "saved" } } },
    { id: 4, type: "reasoning.delta", data: { content: "正在核验" } },
    { id: 5, type: "message.delta", data: { content: "新的部分" } },
    { id: 5, type: "message.delta", data: { content: "新的部分" } },
  ];
  let state = hydrateSession({ messages: [{ id: "saved", role: "assistant", content: "已保存的回复" }], events });
  assert.deepEqual(state.messages.map((message) => message.content), ["已保存的回复", "新的部分"]);
  assert.equal(state.messages[1].streaming, true);
  assert.equal(state.reasoning, "正在核验");
  state = applyEvent(state, { id: 6, type: "message.delta", data: { content: "，继续" } });
  assert.equal(state.messages[1].content, "新的部分，继续");
  const interrupted = hydrateSession({ messages: [], events: [...events, { id: 6, type: "run.cancelled", data: {} }] });
  assert.equal(interrupted.messages[0].content, "新的部分");
  assert.equal(interrupted.messages[0].interrupted, true);
  assert.equal(interrupted.messages[0].streaming, false);
});

test("server acknowledgement replaces optimistic user messages", () => {
  const initial = createSessionState([{ id: "pending:one", role: "user", content: "检查项目", pending: true }]);
  const state = applyEvent(initial, { type: "message.completed", data: { message: { id: "server-one", role: "user", content: "检查项目" } } });
  assert.deepEqual(state.messages, [{ id: "server-one", role: "user", content: "检查项目" }]);
  assert.equal(initial.messages[0].pending, true);
});

test("attachment-only acknowledgement replaces its optimistic message after the server supplies a prompt", () => {
  const initial = createSessionState([{ id: "pending:attachment", role: "user", content: "", attachments: [{ id: "file-one", name: "file.txt" }], pending: true }]);
  const state = applyEvent(initial, { type: "message.queued", data: { message: { id: "receipt", role: "user", content: "请查看附件", attachment_ids: ["file-one"], delivery_status: "pending" } } });
  assert.equal(state.messages.length, 1);
  assert.equal(state.messages[0].id, "receipt");
  assert.deepEqual(state.queued, ["receipt"]);
});

test("queued user messages never erase the running assistant reply and replay keeps pending IDs", () => {
  const events = [
    { id: 1, type: "run.started", data: { run_id: "main-run" } },
    { id: 2, type: "message.delta", data: { content: "正在读取" } },
    { id: 3, type: "message.completed", data: { message: { id: "supplement", role: "user", content: "也检查测试" }, queued: true } },
    { id: 4, type: "conversation.queued", data: { message_id: "supplement", run_id: "main-run" } },
  ];
  let state = events.reduce((current, event) => applyEvent(current, event), createSessionState());
  assert.equal(state.messages.find((message) => message.streaming)?.content, "正在读取");
  assert.equal(state.status, "running");
  assert.deepEqual(state.queued, ["supplement"]);
  state = applyEvent(state, { id: 5, type: "message.delta", data: { content: "，已完成。" } });
  assert.equal(state.messages.find((message) => message.streaming)?.content, "正在读取，已完成。");
  const restored = hydrateSession({ messages: [{ id: "supplement", role: "user", content: "也检查测试" }], events });
  assert.deepEqual(restored.queued, ["supplement"]);
  assert.equal(restored.messages.filter((message) => message.streaming).length, 1);
  state = applyEvent(state, { id: 6, type: "conversation.injected", data: { message_ids: ["supplement"], run_id: "main-run" } });
  assert.deepEqual(state.queued, []);
});

test("approval lifecycle resumes the run and terminal events clear stale approvals", () => {
  let state = applyEvent(createSessionState(), { type: "approval.requested", data: { id: "a1", tool: "shell" } });
  state = applyEvent(state, { type: "approval.resolved", data: { id: "a1", decision: "reject" } });
  assert.equal(state.status, "running"); assert.equal(state.approvals.length, 0);
  state = applyEvent(state, { type: "approval.requested", data: { id: "a2" } });
  state = applyEvent(state, { type: "run.cancelled", data: {} });
  assert.equal(state.approvals.length, 0); assert.equal(state.status, "cancelled");
});

test("snapshot pending ingress restores the queue and injection keeps its displayed message", () => {
  let state = hydrateSession({
    messages: [{ id: "delivered", role: "user", content: "读取文件" }, { id: "inbox-one", role: "user", content: "也检查测试", delivery_status: "pending" }],
    events: [{ id: 1, type: "run.started", data: { run_id: "r1" } }, { id: 2, type: "approval.requested", data: { id: "approval-one", tool: "fs_read" } }],
  });
  assert.deepEqual(state.queued, ["inbox-one"]);
  assert.equal(state.status, "approval");
  assert.equal(state.messages.length, 2);
  state = applyEvent(state, { id: 3, type: "conversation.injected", data: { message_ids: ["inbox-one"], run_id: "r1" } });
  assert.deepEqual(state.queued, []);
  assert.equal(state.messages.length, 2);
  assert.equal(state.messages[1].delivery_status, "delivered");
  state = applyEvent(state, { id: 4, type: "message.completed", data: { message: { id: "inbox-one", role: "user", content: "也检查测试" } } });
  assert.equal(state.messages.length, 2);
});

test("durable ingress queued and delivered events update the live count without breaking streams", () => {
  let state = applyEvent(createSessionState(), { type: "message.delta", data: { content: "正在处理" } });
  const message = { id: "receipt-one", role: "user", content: "补充", delivery_status: "pending" };
  state = applyEvent(state, { type: "message.queued", data: { message } });
  assert.deepEqual(state.queued, ["receipt-one"]);
  assert.equal(state.messages[0].content, "正在处理");
  state = applyEvent(state, { type: "message.completed", data: { message: { ...message, delivery_status: "delivered" } } });
  assert.deepEqual(state.queued, []);
  assert.equal(state.messages.length, 2);
  state = applyEvent(state, { type: "message.queued", data: { message } });
  state = applyEvent(state, { type: "message.completed", data: { message } });
  assert.deepEqual(state.queued, []);
  assert.equal(state.messages.find((entry) => entry.id === message.id).delivery_status, "delivered");
  state = applyEvent(state, { type: "message.queued", data: { message: { ...message, id: "receipt-two" } } });
  state = applyEvent(state, { type: "run.cancelled", data: {} });
  assert.deepEqual(state.queued, []);
});

test("attention facts retain their system identity and never acknowledge an optimistic owner message", () => {
  const initial = createSessionState([{ id: "owner-pending", role: "user", content: "检查结果", pending: true }]);
  let state = applyEvent(initial, { type: "message.delta", data: { content: "正在回复" } });
  const message = { id: "system-fact", content: "检查结果", principal: "system:attention", source: "system:attention", state: "pending" };
  state = applyEvent(state, { type: "attention.queued", data: { message } });
  assert.deepEqual(state.queued, ["system-fact"]);
  assert.equal(state.messages.length, 3);
  assert.equal(state.messages.find((entry) => entry.streaming).content, "正在回复");
  const fact = state.messages.find((entry) => entry.id === "system-fact");
  assert.deepEqual(messagePresentation(fact), { kind: "system-context", label: "系统资料" });
  assert.deepEqual(messagePresentation({ role: "user", content: "system:attention", source: "system:attention" }), { kind: "user", label: "你的消息" });
  state = applyEvent(state, { type: "message.completed", data: { message: { ...fact, delivery_status: "delivered" } } });
  assert.deepEqual(state.queued, []);
  state = applyEvent(state, { type: "attention.queued", data: { message } });
  assert.equal(state.messages.find((entry) => entry.id === "system-fact").delivery_status, "delivered");
  assert.deepEqual(state.queued, []);
});

test("cancelled ingress removes pending facts and owner receipts while preserving delivered history and streams", () => {
  let state = hydrateSession({ messages: [
    { id: "history", role: "user", content: "已有消息", delivery_status: "delivered" },
    { id: "owner", role: "user", content: "补充", delivery_status: "pending" },
    { id: "fact", role: "user", content: "资料", principal: "system:attention", delivery_status: "pending" },
  ], events: [{ type: "run.started", data: { run_id: "current" } }, { type: "message.delta", data: { content: "正在回复" } }] });
  state = applyEvent(state, { type: "ingress.cancelled", data: { count: 2 } });
  assert.deepEqual(state.queued, []);
  assert.deepEqual(state.messages.map((message) => message.id), ["history", "stream:current"]);
  assert.equal(state.status, "running");
  assert.equal(state.messages[1].content, "正在回复");
  const restored = hydrateSession({ messages: state.messages, events: [{ type: "attention.queued", data: { message: { id: "fact", principal: "system:attention", content: "资料" } } }, { type: "ingress.cancelled", data: { count: 1 } }] });
  assert.equal(restored.messages.some((message) => message.id === "fact"), false);
});

test("interrupted generation retains partial output and exposes the real failure", () => {
  let state = applyEvent(createSessionState(), { type: "message.delta", data: { content: "已经生成" } });
  state = applyEvent(state, { type: "run.failed", data: { error: "模型连接超时" } });
  assert.equal(state.messages[0].content, "已经生成");
  assert.equal(state.messages[0].streaming, false);
  assert.equal(state.messages[0].interrupted, true);
  assert.equal(state.error, "模型连接超时");
});

test("Markdown URLs reject script and local file schemes", () => {
  assert.equal(safeURL("javascript:alert(1)"), null);
  assert.equal(safeURL("data:text/html,<script>"), null);
  assert.equal(safeURL("file:///etc/passwd"), null);
  assert.equal(safeURL("https://example.com/docs"), "https://example.com/docs");
  assert.throws(() => parseJSONField("[]"));
  assert.throws(() => parseJSONField("null"));
});

test("server Unix seconds, milliseconds and ISO dates display consistently", () => {
  const date = new Date("2026-10-18T03:25:00Z");
  const expected = formatTimestamp(date.toISOString(), date);
  assert.equal(formatTimestamp(date.getTime() / 1000, date), expected);
  assert.equal(formatTimestamp(date.getTime(), date), expected);
  assert.equal(formatTimestamp(undefined, date), "");
});

test("reminder datetime-local conversion preserves local wall time to the minute", () => {
  const timestamp = Math.floor(Date.now() / 60000) * 60;
  assert.equal(fromDateTimeLocal(toDateTimeLocal(timestamp)), timestamp);
  assert.throws(() => fromDateTimeLocal("invalid"));
});

test("tool failures encoded inside JSON results stay visibly failed", () => {
  const state = applyEvent(createSessionState(), { type: "tool.completed", data: { call_id: "one", name: "shell", result: '{"error":"用户拒绝了工具调用"}' } });
  assert.equal(state.tools[0].status, "failed");
});

test("a paused run settles streaming and approvals while preserving queued owner input", () => {
  const pending = { id: "later", role: "user", content: "补充要求", delivery_status: "pending" };
  let state = createSessionState([pending]);
  for (const event of [
    { type: "run.started", data: { run_id: "scope-run" } },
    { type: "message.delta", data: { content: "已核查第一项" } },
    { type: "approval.requested", data: { id: "old-approval" } },
    { id: 4, type: "run.paused", data: { run_id: "scope-run", observed_epoch: 2, reason: "主人暂停了任务" } },
  ]) state = applyEvent(state, event);
  assert.equal(state.status, "paused");
  assert.equal(isRunning(state.status), false);
  assert.equal(state.approvals.length, 0);
  assert.deepEqual(state.queued, [pending.id]);
  assert.equal(state.messages[1].content, "已核查第一项");
  assert.equal(state.messages[1].streaming, false);
  assert.equal(state.messages[1].interrupted, true);
  assert.equal(state.messages[1].paused, true);
  assert.equal(state.error, "");
});

test("paused history hydrates without an endless stream or dropping pending input", () => {
  const state = hydrateSession({
    messages: [{ id: "later", role: "user", content: "补充要求", delivery_status: "pending" }],
    events: [
      { id: 1, type: "run.started", data: { run_id: "scope-run" } },
      { id: 2, type: "message.delta", data: { content: "保留已生成部分" } },
      { id: 3, type: "tool.started", data: { call_id: "write-one", name: "fs_write", arguments: { path: "note.txt" } } },
      { id: 4, type: "tool.completed", data: { call_id: "write-one", name: "fs_write", status: "not_executed", code: "task_paused", result: '{"status":"not_executed"' } },
      { id: 5, type: "run.paused", data: { run_id: "scope-run", observed_epoch: 2 } },
    ],
  });
  assert.equal(state.status, "paused");
  assert.equal(state.cursor, "5");
  assert.deepEqual(state.queued, ["later"]);
  assert.equal(state.messages[1].content, "保留已生成部分");
  assert.equal(state.messages[1].streaming, false);
  assert.equal(state.messages[1].paused, true);
  assert.equal(state.tools[0].status, "not_executed");
  assert.equal(toolStatusLabel(state.tools[0]), "未执行 · 已暂停");
  assert.equal(applyEvent(state, { id: 5, type: "run.paused", data: {} }), state);
});

test("not-executed results remain distinct from successes and execution failures", () => {
  for (const [result, expected] of [[{ status: "not_executed", code: "task_paused" }, "未执行 · 已暂停"], [JSON.stringify({ status: "not_executed", reason: "收到新要求" }), "未执行"]]) {
    const state = applyEvent(createSessionState(), { type: "tool.completed", data: { call_id: "never-entered", result } });
    assert.equal(state.tools[0].status, "not_executed");
    assert.equal(toolStatusLabel(state.tools[0]), expected);
  }
  assert.equal(toolStatusLabel({ status: "failed" }), "执行失败");
  assert.equal(toolStatusLabel({ status: "completed" }), "已完成");
});
