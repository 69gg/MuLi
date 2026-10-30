import test from "node:test";
import assert from "node:assert/strict";
import { applyEvent, conversationItems, createSessionState, hydrateSession } from "../../web/state.mjs";

function events() {
  let id = 0;
  return (type, data = {}) => ({ id: String(++id), type, data, created_at: 1700000000 + id });
}

function replay(entries, state = createSessionState()) {
  return entries.reduce((current, event) => applyEvent(current, event), state);
}

function orderedConversation() {
  const event = events();
  const owner = { id: "owner", role: "user", content: "请核对资料" };
  const first = { id: "first", role: "assistant", content: "我先读取资料。", created_at: 1700000005 };
  const final = { id: "final", role: "assistant", content: "已核对，结论如下。", attachment_ids: ["report"] };
  const entries = [
    event("message.completed", { message: owner }),
    event("run.started", { run_id: "run-one" }),
    event("reasoning.delta", { content: "先读取" }),
    event("reasoning.delta", { content: "并交叉核对。" }),
    event("message.delta", { content: first.content }),
    event("message.completed", { message: first }),
    event("tool.started", { call_id: "read", name: "fs_read", arguments: { path: "notes.txt" } }),
    event("tool.started", { call_id: "search", name: "memory_query", arguments: { query: "资料" } }),
    event("tool.completed", { call_id: "search", name: "memory_query", result: '{"items":["参考"]}' }),
    event("tool.completed", { call_id: "read", name: "fs_read", result: '{"content":"原文"}' }),
    event("reasoning.delta", { content: "资料一致，" }),
    event("reasoning.delta", { content: "可以给出结论。" }),
    event("message.delta", { content: "已核对，" }),
    event("message.delta", { content: "结论如下。" }),
    event("message.completed", { message: final }),
    event("run.completed", { run_id: "run-one" }),
  ];
  return { entries, messages: [owner, first, final] };
}

function visibleContent(state) {
  return conversationItems(state).map((item) => item.kind === "message"
    ? [item.kind, item.message.content]
    : item.kind === "reasoning" ? [item.kind, item.content] : [item.kind, item.tool.call_id, item.tool.result, item.tool.status]);
}

test("thinking, each reply round and reverse-order tool receipts keep their arrival positions", () => {
  const { entries } = orderedConversation();
  const started = replay(entries.slice(0, 8));
  const startedTools = conversationItems(started).filter((item) => item.kind === "tool");
  const state = replay(entries.slice(8), started);
  const items = conversationItems(state);
  assert.deepEqual(items.map((item) => item.kind), ["message", "reasoning", "message", "tool", "tool", "reasoning", "message"]);
  assert.deepEqual(items.filter((item) => item.kind === "message").map((item) => item.message.content), ["请核对资料", "我先读取资料。", "已核对，结论如下。"]);
  assert.deepEqual(items.filter((item) => item.kind === "reasoning").map((item) => [item.content, item.streaming]), [["先读取并交叉核对。", false], ["资料一致，可以给出结论。", false]]);
  const tools = items.filter((item) => item.kind === "tool");
  assert.deepEqual(tools.map((item) => item.id), startedTools.map((item) => item.id));
  assert.deepEqual(tools.map((item) => [item.tool.call_id, item.tool.status, item.tool.receipt_state]), [["read", "completed", "recorded"], ["search", "completed", "recorded"]]);
  assert.deepEqual(tools[0].tool.arguments, { path: "notes.txt" });
  assert.equal(startedTools[0].tool.status, "running");
  assert.equal(items[1].created_at, entries[2].created_at);
  assert.deepEqual(items.at(-1).message.attachment_ids, ["report"]);
});

test("an accepted reply closes only its own text fragments around a real queued supplement", () => {
  const event = events();
  const started = event("run.started", { run_id: "one" });
  const firstDelta = event("message.delta", { content: "已经核验" });
  let state = replay([
    started,
    firstDelta,
    event("message.queued", { message: { id: "supplement", role: "user", content: "也说明依据", delivery_status: "pending" } }),
    event("reasoning.delta", { content: "补充原文依据。" }),
    event("message.delta", { content: "；可以继续。" }),
  ]);
  const streamingItems = conversationItems(state);
  assert.equal(streamingItems[0].message.created_at, firstDelta.created_at);
  const ids = streamingItems.map((item) => item.id);
  const attachment = { id: "evidence", name: "evidence.txt", size: 12 };
  state = applyEvent(state, event("message.completed", { message: { id: "accepted", role: "assistant", content: "已经核验；可以继续。", attachments: [attachment], attachment_ids: [attachment.id] } }));
  const items = conversationItems(state);
  assert.deepEqual(items.map((item) => item.kind), ["message", "message", "reasoning", "message"]);
  assert.deepEqual(items.map((item) => item.id), ids);
  assert.equal(new Set(ids).size, ids.length);
  const fragments = items.filter((item) => item.kind === "message" && item.message.role === "assistant");
  assert.deepEqual(fragments.map((item) => item.message.content), ["已经核验", "；可以继续。"]);
  assert.equal(fragments.every((item) => !item.message.streaming), true);
  assert.deepEqual(fragments[0].message.attachments, []);
  assert.deepEqual(fragments[1].message.attachments, [attachment]);
  state = applyEvent(state, event("conversation.injected", { message_ids: ["supplement"], run_id: "one" }));
  assert.equal(conversationItems(state)[1].message.delivery_status, "delivered");
  assert.deepEqual(conversationItems(state).map((item) => item.id), ids);
});

test("provider call IDs reused in later rounds and runs never overwrite older tool blocks", () => {
  const event = events();
  let state = replay([
    event("run.started", { run_id: "first-run" }),
    event("message.completed", { message: { id: "round-one", role: "assistant", content: "", tool_calls: [{ id: "same", name: "fs_read" }] } }),
    event("tool.started", { call_id: "same", name: "fs_read", arguments: { path: "one.txt" } }),
    event("tool.completed", { call_id: "same", name: "fs_read", result: '{"content":"one"}' }),
  ]);
  const first = conversationItems(state).find((item) => item.kind === "tool");
  state = replay([
    event("message.completed", { message: { id: "round-two", role: "assistant", content: "", tool_calls: [{ id: "same", name: "fs_read" }] } }),
    event("tool.started", { call_id: "same", name: "fs_read", arguments: { path: "two.txt" } }),
    event("tool.completed", { call_id: "same", name: "fs_read", result: '{"content":"two"}' }),
    event("run.completed", { run_id: "first-run" }),
    event("run.started", { run_id: "second-run" }),
    event("tool.started", { call_id: "same", name: "fs_read", arguments: { path: "three.txt" } }),
    event("tool.completed", { call_id: "same", name: "fs_read", result: '{"content":"three"}' }),
  ], state);
  const tools = conversationItems(state).filter((item) => item.kind === "tool");
  assert.equal(tools.length, 3);
  assert.equal(tools[0].id, first.id);
  assert.equal(new Set(tools.map((item) => item.id)).size, 3);
  assert.deepEqual(tools.map((item) => [item.runId, item.tool.arguments.path, JSON.parse(item.tool.result).content]), [["first-run", "one.txt", "one"], ["first-run", "two.txt", "two"], ["second-run", "three.txt", "three"]]);
});

test("reload reconstructs the event order and restores full accepted attachments without duplicate text", () => {
  const { entries, messages } = orderedConversation();
  const oldMessages = [{ id: "old-owner", role: "user", content: "上次的问题" }, { id: "old-answer", role: "assistant", content: "上次的回复" }];
  const files = [{ id: "report", name: "report.txt", url: "/api/attachments/report" }];
  const canonical = [...messages.slice(0, -1), { ...messages.at(-1), attachments: files }];
  const detail = { messages: [...oldMessages, ...canonical], events: entries, after: entries.at(-1).id, session: { id: "main", status: "idle" }, live: { session_id: "main", status: "idle", run_id: null, pending_approvals: [] } };
  const restored = hydrateSession(detail);
  const items = conversationItems(restored);
  assert.deepEqual(visibleContent(restored), [["message", "上次的问题"], ["message", "上次的回复"], ...visibleContent(replay(entries))]);
  assert.deepEqual(items.at(-1).message.attachments, files);
  assert.equal(items.filter((item) => item.kind === "message" && item.message.id === "final").length, 1);
  assert.equal(items.filter((item) => item.kind === "reasoning").length, 2);
  assert.equal(applyEvent(restored, entries[4]) === restored, true);
});

test("an incomplete event tail is settled as interrupted and archived unknown receipts remain unknown", () => {
  const event = events();
  const restored = hydrateSession({
    session: { id: "main", status: "idle" }, live: { session_id: "main", status: "idle", run_id: null, pending_approvals: [] }, messages: [],
    events: [event("run.started", { run_id: "interrupted" }), event("reasoning.delta", { content: "正在核验" }), event("message.delta", { content: "已有部分结果" }), event("tool.started", { call_id: "write", name: "fs_write", arguments: { path: "result.txt" } })],
  });
  const items = conversationItems(restored);
  assert.equal(items[0].streaming, false);
  assert.equal(items[1].message.streaming, false);
  assert.equal(items[1].message.interrupted, true);
  assert.equal(items[2].tool.status, "unknown");
  const archive = hydrateSession({
    session: { id: "main", status: "idle" }, live: { session_id: "main", status: "idle", run_id: null, pending_approvals: [] }, events: [],
    messages: [
      { id: "invocation", role: "assistant", content: "", tool_calls: [{ id: "send", name: "channel_send", arguments: '{"text":"成果"}' }] },
      { id: "receipt", role: "tool", tool_call_id: "send", content: '{"status":"unknown","code":"transport_timeout"}' },
      { id: "warning", role: "assistant", content: "投递结果未知，请先核查。" },
    ],
  });
  assert.deepEqual(conversationItems(archive).map((item) => item.kind), ["tool", "message"]);
  assert.equal(conversationItems(archive)[0].tool.status, "unknown");
  assert.equal(conversationItems(archive)[0].tool.receipt_state, "recorded");
  assert.equal(conversationItems(archive)[1].message.content, "投递结果未知，请先核查。");
});

test("system facts do not consume optimistic owner messages and acknowledgement replaces only the owner", () => {
  const event = events();
  let state = createSessionState([{ id: "pending", role: "user", content: "检查资料", pending: true }]);
  state = applyEvent(state, event("attention.queued", { message: { id: "fact", principal: "system:attention", content: "检查资料" } }));
  assert.deepEqual(conversationItems(state).map((item) => item.message.id), ["pending", "fact"]);
  state = applyEvent(state, event("message.completed", { message: { id: "owner", role: "user", content: "检查资料" } }));
  assert.deepEqual(conversationItems(state).map((item) => item.message.id), ["owner", "fact"]);
  assert.equal(conversationItems(state)[1].message.principal, "system:attention");
});

test("attachment-only and directly appended composer messages are acknowledged without duplicate cards", () => {
  const event = events();
  const attachment = { id: "local-file", name: "notes.txt" };
  let state = createSessionState([{ id: "pending-file", role: "user", content: "", attachments: [attachment], pending: true }]);
  state = applyEvent(state, event("message.queued", { message: { id: "file-owner", role: "user", content: "请查看附件", attachment_ids: [attachment.id], delivery_status: "pending" } }));
  assert.equal(conversationItems(state).length, 1);
  assert.equal(conversationItems(state)[0].message.id, "file-owner");
  state = { ...state, messages: [...state.messages, { id: "composer-pending", role: "user", content: "补充一句", pending: true }] };
  assert.equal(conversationItems(state).at(-1).message.id, "composer-pending");
  state = applyEvent(state, event("message.queued", { message: { id: "supplement", role: "user", content: "补充一句", delivery_status: "pending" } }));
  assert.deepEqual(conversationItems(state).map((item) => item.message.id), ["file-owner", "supplement"]);
  state = applyEvent(state, event("conversation.injected", { message_ids: ["file-owner", "supplement"] }));
  assert.equal(conversationItems(state).every((item) => item.message.delivery_status === "delivered"), true);
});

test("a directly appended composer card keeps its first visible position and key across deltas and acknowledgement", () => {
  const event = events();
  let state = replay([event("run.started", { run_id: "active" }), event("message.delta", { content: "原先的回复" })]);
  state.messages.push({ id: "pending:local", role: "user", content: "先说明依据", pending: true });
  const firstPaint = conversationItems(state);
  const card = firstPaint.at(-1);
  const position = firstPaint.length - 1;
  assert.equal(state.timeline.some((item) => item.message?.id === "pending:local"), false);
  state = replay([event("reasoning.delta", { content: "核对依据" }), event("message.delta", { content: "随后补充" })], state);
  const beforeAck = conversationItems(state);
  assert.equal(beforeAck[position].id, card.id);
  assert.deepEqual(beforeAck.slice(position).map((item) => item.kind), ["message", "reasoning", "message"]);
  state = applyEvent(state, event("message.queued", { message: { id: "persisted:owner", role: "user", content: "先说明依据", delivery_status: "pending" } }));
  const afterAck = conversationItems(state);
  assert.equal(afterAck[position].id, card.id);
  assert.equal(afterAck[position].message.id, "persisted:owner");
  assert.deepEqual(afterAck.map((item) => item.id), beforeAck.map((item) => item.id));
  assert.equal(afterAck.filter((item) => item.message?.content === "先说明依据").length, 1);
});

test("a failed composer POST removes its fixed pending card without mutating state during rendering", () => {
  const event = events();
  let state = replay([event("run.started", { run_id: "active" })]);
  state.messages.push({ id: "pending:failed", role: "user", content: "尚未接纳", pending: true });
  const card = conversationItems(state)[0];
  state = applyEvent(state, event("message.delta", { content: "仍在处理原请求" }));
  assert.equal(conversationItems(state)[0].id, card.id);
  assert.equal(state.timeline.some((item) => item.id === card.id), true);
  state.messages = state.messages.filter((message) => message.id !== "pending:failed");
  const timeline = state.timeline;
  assert.deepEqual(conversationItems(state).map((item) => item.message?.content), ["仍在处理原请求"]);
  assert.equal(state.timeline === timeline, true);
  assert.equal(state.timeline.some((item) => item.id === card.id), true);
  state = applyEvent(state, event("reasoning.delta", { content: "继续原任务" }));
  assert.equal(state.timeline.some((item) => item.id === card.id), false);
});

test("snapshot and duplicate SSE events cannot append text or tool blocks twice", () => {
  const after = "9007199254740995";
  let state = hydrateSession({ messages: [], events: [], after });
  const older = { id: "9007199254740994", type: "message.delta", data: { content: "快照之前" } };
  assert.equal(applyEvent(state, older) === state, true);
  const delta = { id: "9007199254740996", type: "message.delta", data: { content: "新的正文" }, created_at: 1700000001 };
  state = applyEvent(state, delta);
  const same = applyEvent(state, delta);
  assert.equal(same === state, true);
  assert.deepEqual(conversationItems(state).map((item) => item.message.content), ["新的正文"]);
  const started = { id: "9007199254740997", type: "tool.started", data: { call_id: "read", name: "fs_read", arguments: {} } };
  state = applyEvent(state, started);
  assert.equal(applyEvent(state, started) === state, true);
  assert.equal(conversationItems(state).filter((item) => item.kind === "tool").length, 1);
});

test("exact completed reply candidates remain bound to their saved revision across later main runs", () => {
  const event = events();
  const first = { id: "saved-first", session_id: "main", role: "assistant", content: "第一条完整最终答复。", evidence_revision: "first-revision" };
  const second = { id: "saved-second", session_id: "main", role: "assistant", content: "第二条完整最终答复。", evidence_revision: "second-revision" };
  const entries = [
    event("run.started", { run_id: "first-run" }), event("message.completed", { message: first }),
    event("run.completed", { run_id: "first-run", result_message_id: first.id }),
    event("run.started", { run_id: "second-run" }), event("message.completed", { message: second }),
    event("run.completed", { run_id: "second-run", result_message_id: second.id }),
    event("run.started", { run_id: "third-run" }),
  ];
  const state = replay(entries);
  assert.deepEqual(state.replyCandidates, [
    { run_id: "first-run", message_id: first.id, revision: first.evidence_revision, completed: true },
    { run_id: "second-run", message_id: second.id, revision: second.evidence_revision, completed: true },
  ]);
  assert.equal(state.runId, "third-run"); assert.equal(state.status, "running");
  const restored = hydrateSession({ session: { id: "main" }, messages: [first, second], events: entries, live: { session_id: "main", status: "running", run_id: "third-run", pending_approvals: [] } });
  assert.deepEqual(restored.replyCandidates, state.replyCandidates);
});

test("reply completion candidates never guess a last assistant when the result identity or revision is absent", () => {
  const final = { id: "saved-final", role: "assistant", content: "完整正文。", evidence_revision: "original-revision" };
  for (const completion of [{ run_id: "run" }, { run_id: "different-run", result_message_id: final.id }, { run_id: "run", result_message_id: "different-message" }]) {
    const event = events();
    const state = replay([event("run.started", { run_id: "run" }), event("message.completed", { message: final }), event("run.completed", completion)]);
    assert.equal(state.replyCandidates.some((candidate) => candidate.completed), false);
  }
  const event = events();
  const missingRevision = replay([event("run.started", { run_id: "run" }), event("message.completed", { message: { ...final, evidence_revision: undefined } }), event("run.completed", { run_id: "run", result_message_id: final.id })]);
  assert.deepEqual(missingRevision.replyCandidates, []);
  const absentEvents = hydrateSession({ messages: [final], events: [], session: { id: "main", status: "idle" } });
  assert.deepEqual(absentEvents.replyCandidates, []);
});

test("hydration cannot bind an edited canonical revision to the original run's historical completion", () => {
  const event = events();
  const original = { id: "saved-final", session_id: "main", role: "assistant", content: "原最终正文。", evidence_revision: "original-revision" };
  const edited = { ...original, content: "编辑后的正文。", evidence_revision: "edited-revision" };
  const entries = [event("run.started", { run_id: "run" }), event("message.completed", { message: original }), event("run.completed", { run_id: "run", result_message_id: original.id })];
  const restored = hydrateSession({ session: { id: "main", status: "idle" }, messages: [edited], events: entries });
  assert.deepEqual(restored.replyCandidates, []);
  assert.equal(conversationItems(restored).find((item) => item.message?.id === original.id).message.evidence_revision, edited.evidence_revision);
  assert.equal(conversationItems(restored).find((item) => item.message?.id === original.id).message.content, edited.content);
});

test("editing and replacing canonical messages drops old proof and replay cannot revive it", () => {
  const event = events();
  const original = { id: "saved-final", role: "assistant", content: "原最终正文。", evidence_revision: "original-revision" };
  const started = event("run.started", { run_id: "run" }), accepted = event("message.completed", { message: original });
  const completed = event("run.completed", { run_id: "run", result_message_id: original.id });
  let state = replay([started, accepted, completed]);
  assert.equal(state.replyCandidates[0].completed, true);
  state = applyEvent(state, event("message.completed", { message: { ...original, evidence_revision: "edited-revision", content: "新版正文。" } }));
  assert.equal(state.replyCandidates.some((candidate) => candidate.completed), false);
  state = applyEvent(state, event("message.completed", { message: original }));
  state = applyEvent(state, completed);
  assert.equal(state.replyCandidates.some((candidate) => candidate.completed), false);
  state = { ...state, messages: [] };
  state = applyEvent(state, event("reasoning.delta", { content: "其它事件也会剪除已经离开窗口的候选。" }));
  assert.deepEqual(state.replyCandidates, []);
});
