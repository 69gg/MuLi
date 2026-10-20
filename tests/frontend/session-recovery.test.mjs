import test from "node:test";
import assert from "node:assert/strict";
import { applyEvent, createSessionState, hydrateSession, isRunning, toolStatusLabel } from "../../web/state.mjs";

const event = (id, type, data = {}) => ({ id: String(id), type, data, session_id: "main" });
function snapshot({ events = [], messages = [], runId = null, runStatus = "interrupted", run = "old-run", approvals = [], after = "100" } = {}) {
  return {
    session: { id: "main", status: runId ? "running" : "idle" }, messages, events, after,
    live: { session_id: "main", run_id: runId, status: runId ? approvals.length ? "approval" : "running" : runStatus,
      run: { id: run, status: runStatus }, pending_approvals: approvals },
  };
}
const started = event(1, "run.started", { id: "old-run" });
const requested = event(4, "approval.requested", { id: "old-approval", session_id: "main", run_id: "old-run" });

test("recovered facts override historical running and approval while retaining partial text and reasoning", () => {
  const state = hydrateSession(snapshot({ events: [started,
    event(2, "message.delta", { content: "已经生成" }),
    event(3, "reasoning.delta", { content: "已保存的思路" }), requested,
    event(5, "tool.started", { call_id: "old-tool", name: "unused" }),
  ] }));
  assert.equal(state.status, "interrupted"); assert.equal(isRunning(state.status), false);
  assert.equal(state.approvals.length, 0); assert.equal(state.tools[0].status, "unknown");
  assert.equal(toolStatusLabel(state.tools[0]), "结果未知");
  assert.equal(state.messages[0].content, "已经生成");
  assert.equal(state.messages[0].streaming, false); assert.equal(state.messages[0].interrupted, true);
  assert.equal(state.reasoning, "已保存的思路"); assert.equal(state.cursor, "100");
});

test("live pending futures restore approvals even when their requested event is outside the window", () => {
  const approval = { id: "live", status: "pending", session_id: "main", run_id: "old-run" };
  const state = hydrateSession(snapshot({ runId: "old-run", runStatus: "running", approvals: [approval], events: [started, event(2, "message.delta", { content: "继续生成" })] }));
  assert.equal(state.status, "approval"); assert.deepEqual(state.approvals, [approval]);
  assert.equal(state.messages[0].streaming, true);
});

test("a committed terminal for this run remains terminal during asynchronous cleanup", () => {
  for (const status of ["completed", "failed", "cancelled", "paused"]) {
    const state = hydrateSession(snapshot({ runId: "old-run", runStatus: "running", events: [started, event(2, `run.${status}`, { run_id: "old-run", error: "controlled failure" })] }));
    assert.equal(state.status, status); assert.equal(state.approvals.length, 0);
    assert.equal(state.error, status === "failed" ? "controlled failure" : "");
  }
});

test("an old terminal never overrides the active replacement run", () => {
  let state = hydrateSession(snapshot({ runId: "new-run", run: "new-run", runStatus: "queued", events: [started, event(2, "run.completed", { run_id: "old-run" })] }));
  assert.equal(state.status, "running"); assert.equal(state.runId, "new-run");
  state = applyEvent(state, event(101, "run.failed", { run_id: "old-run", error: "old" }));
  assert.equal(state.status, "running"); assert.equal(state.error, "");
});

test("an existing page consumes recovery over SSE without hydrating again", () => {
  let state = [started, event(2, "message.delta", { content: "部分输出" }),
    event(3, "reasoning.delta", { content: "思考" }), requested,
    event(5, "tool.started", { call_id: "unfinished", name: "unused" }),
    event(6, "tool.completed", { call_id: "skipped", status: "not_executed", result: '{"status":"not_executed"}' }),
    event(7, "tool.completed", { call_id: "failed", result: '{"error":"confirmed"}' }),
  ].reduce((current, item) => applyEvent(current, item), createSessionState());
  const recovery = event(8, "session.recovered", { kind: "run", run_id: "old-run", session_status: "idle" });
  state = applyEvent(state, recovery);
  assert.equal(state.status, "interrupted"); assert.equal(state.approvals.length, 0);
  assert.equal(state.messages[0].content, "部分输出"); assert.equal(state.messages[0].streaming, false);
  assert.equal(state.reasoning, "思考");
  assert.deepEqual(state.tools.map((tool) => tool.status), ["unknown", "not_executed", "failed"]);
  assert.equal(applyEvent(state, recovery) === state, true);
});

test("snapshot lower bounds preserve history hydration, exact future IDs and empty-ID events", () => {
  const after = "9007199254740993";
  let state = hydrateSession(snapshot({ after, events: [started, event(2, "message.delta", { content: "旧文本" }), requested] }));
  assert.equal(state.messages[0].content, "旧文本");
  assert.equal(applyEvent(state, event(after, "approval.requested", { id: "stale" })) === state, true);
  assert.equal(applyEvent(state, event("9007199254740992", "run.started", { id: "stale" })) === state, true);
  state = applyEvent(state, event("9007199254740994", "run.started", { id: "next" }));
  state = applyEvent(state, { id: "", type: "message.delta", data: { content: "新文本" } });
  assert.equal(state.status, "running"); assert.equal(state.runId, "next");
  assert.equal(state.messages.at(-1).content, "新文本");
});

test("snapshot continuation accepts later approval, resolution and replacement execution in order", () => {
  let state = hydrateSession(snapshot({ runId: "old-run", runStatus: "running", events: [started] }));
  state = applyEvent(state, event(101, "approval.requested", { id: "new-approval", run_id: "old-run" }));
  assert.equal(state.status, "approval"); assert.equal(state.approvals[0].id, "new-approval");
  state = applyEvent(state, event(102, "approval.resolved", { id: "new-approval", decision: "approve" }));
  assert.equal(state.status, "running"); assert.equal(state.approvals.length, 0);
  state = applyEvent(state, event(103, "run.completed", { run_id: "old-run" }));
  assert.equal(state.status, "completed");
  state = applyEvent(state, event(104, "run.started", { id: "replacement" }));
  assert.equal(state.status, "running"); assert.equal(state.runId, "replacement");
});

test("unknown status and outcome stay distinct from confirmed failure including truncated results", () => {
  for (const data of [
    { result: '{"status":"unknown","error":"unknown"}' },
    { result: '{"outcome":"unknown","error":"unknown"}' },
    { status: "unknown", result: '{"outcome":"unknown","error":"truncated' },
  ]) {
    const state = applyEvent(createSessionState(), event(1, "tool.completed", { call_id: "one", ...data }));
    assert.equal(state.tools[0].status, "unknown");
  }
});

test("synthetic unknown does not erase a verified terminal event but repairs an unverifiable old result", () => {
  const messages = [
    { id: "assistant", role: "assistant", tool_calls: [{ id: "one", name: "unused", arguments: "{}" }] },
    { id: "repair", role: "tool", tool_call_id: "one", interrupted: true, content: '{"status":"unknown","error":"检查实际状态"}' },
  ];
  for (const [result, status] of [
    ['{"ok":true}', "completed"], ['{"error":"confirmed"}', "failed"], ['{"status":"not_executed"}', "not_executed"],
    ['{"ok":"truncated', "unknown"],
  ]) {
    const state = hydrateSession(snapshot({ messages, events: [started, event(2, "tool.completed", { call_id: "one", result })] }));
    assert.equal(state.tools[0].status, status);
  }
});

test("an unverifiable legacy completion stays unknown after restart or recovery without a synthetic message", () => {
  const completed = event(2, "tool.completed", { call_id: "one", result: '{"error":"truncated' });
  const restored = hydrateSession(snapshot({ events: [started, completed] }));
  assert.equal(restored.tools[0].status, "unknown");
  let live = [started, completed].reduce((current, item) => applyEvent(current, item), createSessionState());
  live = applyEvent(live, event(3, "session.recovered", { run_id: "old-run", session_status: "idle" }));
  assert.equal(live.tools[0].status, "unknown");
});

test("old unknown receipts never overwrite a reused call ID in a currently live run or later turn", () => {
  for (const runId of ["new-run", "old-run"]) {
    const state = hydrateSession(snapshot({ runId, run: runId, runStatus: "running", events: [
      event(1, "run.started", { id: runId }), event(2, "tool.started", { call_id: "one", name: "current" }),
    ], messages: [
      { id: "old-batch", role: "assistant", tool_calls: [{ id: "one", name: "old", arguments: "{}" }] },
      { id: "old-repair", role: "tool", tool_call_id: "one", interrupted: true, content: '{"status":"unknown"}' },
      { id: "new-batch", role: "assistant", tool_calls: [{ id: "one", name: "current", arguments: "{}" }] },
    ] }));
    assert.equal(state.tools[0].name, "current"); assert.equal(state.tools[0].status, "running");
  }
});

test("a visible repaired call gets an unknown card even without its started event", () => {
  const state = hydrateSession(snapshot({ messages: [
    { id: "assistant", role: "assistant", tool_calls: [{ id: "one", name: "unused", arguments: '{"path":"saved"}' }] },
    { id: "repair", role: "tool", tool_call_id: "one", interrupted: true, content: '{"status":"unknown"}' },
  ] }));
  assert.equal(state.tools[0].status, "unknown"); assert.equal(state.tools[0].name, "unused");
  assert.deepEqual(state.tools[0].arguments, { path: "saved" });
});

test("malformed snapshot positions and mismatched live sessions fail closed", () => {
  for (const after of [1, "", "01", "-1", "9223372036854775808"]) assert.throws(() => hydrateSession(snapshot({ after })), /事件位置/);
  const detail = snapshot(); detail.live.session_id = "other";
  assert.throws(() => hydrateSession(detail), /不匹配/);
});
