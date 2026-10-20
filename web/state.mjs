export const MODES = Object.freeze({
  ask: { label: "Ask", description: "执行工具前审批" },
  auto: { label: "Auto", description: "自动判断，敏感操作审批" },
  yolo: { label: "Yolo", description: "直接执行已启用的工具" },
});

export function createSessionState(messages = []) {
  return {
    messages: [...messages], tools: [], approvals: [], reasoning: "",
    status: "idle", runId: null, cursor: "", seen: new Set(), error: "",
    snapshotAfter: "", terminalRun: null,
    queued: [...new Set(messages.filter((message) => message.role === "user" && message.delivery_status === "pending").map((message) => String(message.id)))],
  };
}

export function messagePresentation(message) {
  if (message.principal === "system:attention") return { kind: "system-context", label: "系统资料" };
  return message.role === "user" ? { kind: "user", label: "你的消息" } : { kind: "assistant", label: "沐璃的回复" };
}

function upsert(items, item, key = "id") {
  const index = items.findIndex((entry) => String(entry[key]) === String(item[key]));
  if (index < 0) return [...items, item];
  return items.map((entry, position) => position === index ? { ...entry, ...item } : entry);
}

const TOOL_TERMINAL = new Set(["completed", "failed", "not_executed", "unknown"]);

function toolReceipt(data) {
  let result = data.result, recorded = TOOL_TERMINAL.has(data.status);
  if (typeof result === "string") {
    try { result = JSON.parse(result); recorded = true; } catch { /* Older events may be plain text or truncated JSON. */ }
  } else if (result !== undefined) recorded = true;
  const status = data.status === "unknown" || result?.status === "unknown" || data.outcome === "unknown" || result?.outcome === "unknown" ? "unknown"
    : data.status === "not_executed" || result?.status === "not_executed" ? "not_executed"
      : data.status === "failed" || result?.status === "failed" || data.error || result?.error ? "failed" : "completed";
  return { status, code: data.code ?? result?.code, receipt_state: recorded ? "recorded" : "unverified" };
}

function unknownTools(tools, runId = null) {
  return tools.map((tool) => (tool.status === "running" || tool.receipt_state === "unverified") && (!runId || !tool.run_id || tool.run_id === runId)
    ? { ...tool, status: "unknown", error: "运行已中断，执行结果未知；请先核查实际状态，不要自动重做。" }
    : tool);
}

function atSnapshotPosition(state, id) {
  return state.snapshotAfter !== "" && id !== undefined && id !== null && /^(0|[1-9][0-9]*)$/.test(String(id))
    && BigInt(id) <= BigInt(state.snapshotAfter);
}

export function applyEvent(state, event, { history = false } = {}) {
  if (!event || !event.type || (!history && atSnapshotPosition(state, event.id)) || (event.id && state.seen.has(String(event.id)))) return state;
  const next = { ...state, seen: new Set(state.seen) };
  if (event.id) {
    next.seen.add(String(event.id));
    next.cursor = String(event.id);
  }
  const data = event.data ?? {};
  switch (event.type) {
    case "run.started":
      next.status = "running";
      next.runId = data.run_id ?? data.id ?? event.run_id ?? null;
      next.error = "";
      next.reasoning = "";
      next.terminalRun = null;
      break;
    case "message.delta": {
      if (history) break;
      const id = `stream:${next.runId ?? "current"}`;
      const existing = next.messages.find((message) => message.id === id);
      next.messages = upsert(next.messages, {
        id, role: "assistant", content: (existing?.content ?? "") + (data.content ?? ""), streaming: true,
      });
      break;
    }
    case "reasoning.delta":
      if (!history) next.reasoning += data.content ?? "";
      break;
    case "attention.queued":
    case "message.queued":
    case "message.completed": {
      if (history || !data.message) break;
      const message = event.type === "attention.queued" ? { ...data.message, role: "user", delivery_status: "pending" } : data.message;
      const queued = message.role === "user" && (message.delivery_status === "pending" || event.type === "message.queued" || data.queued);
      if (queued && next.messages.some((entry) => String(entry.id) === String(message.id) && entry.delivery_status === "delivered")) break;
      let messages = message.role === "assistant" ? next.messages.filter((entry) => !entry.streaming) : next.messages;
      if (message.role === "user") {
        const attachmentIds = message.attachment_ids ?? message.attachments?.map((file) => file.id) ?? [];
        const pending = message.principal === "system:attention" ? -1 : messages.findIndex((entry) => entry.pending && (entry.content === message.content || (!entry.content && attachmentIds.length && JSON.stringify(entry.attachment_ids ?? entry.attachments?.map((file) => file.id) ?? []) === JSON.stringify(attachmentIds))));
        if (pending >= 0) messages = messages.filter((_, index) => index !== pending);
        next.queued = queued ? [...new Set([...next.queued, String(message.id)])] : next.queued.filter((id) => id !== String(message.id));
      }
      next.messages = upsert(messages, message);
      break;
    }
    case "conversation.queued":
      if (data.message_id) next.queued = [...new Set([...next.queued, String(data.message_id)])];
      break;
    case "conversation.injected": {
      const injected = new Set((data.message_ids ?? []).map(String));
      next.queued = next.queued.filter((id) => !injected.has(id));
      next.messages = next.messages.map((message) => injected.has(String(message.id)) ? { ...message, delivery_status: "delivered" } : message);
      break;
    }
    case "ingress.cancelled": {
      const cancelled = new Set(next.queued);
      next.queued = [];
      next.messages = next.messages.filter((message) => message.delivery_status !== "pending" && !cancelled.has(String(message.id)));
      break;
    }
    case "tool.started":
      next.tools = upsert(next.tools, { ...data, run_id: data.run_id ?? next.runId, status: "running", created_at: event.created_at }, "call_id");
      break;
    case "tool.completed": {
      const receipt = toolReceipt(data);
      next.tools = upsert(next.tools, { ...data, run_id: data.run_id ?? next.runId, ...receipt, completed_at: event.created_at }, "call_id");
      break;
    }
    case "approval.requested":
      next.approvals = upsert(next.approvals, { ...data, status: "pending" });
      next.status = "approval";
      break;
    case "approval.resolved":
      next.approvals = next.approvals.filter((approval) => String(approval.id) !== String(data.id));
      if (!next.approvals.length && next.status === "approval") next.status = "running";
      break;
    case "run.completed":
    case "run.cancelled":
    case "run.failed":
    case "run.paused":
      if (data.run_id && next.runId && String(data.run_id) !== String(next.runId)) break;
      next.status = event.type.slice(4);
      next.terminalRun = { id: data.run_id ?? next.runId, status: next.status, error: data.error ?? data.message };
      next.approvals = [];
      next.tools = unknownTools(next.tools, next.terminalRun.id);
      if (event.type === "run.cancelled") next.queued = [];
      next.messages = next.messages.map((message) => message.streaming ? { ...message, streaming: false, interrupted: event.type !== "run.completed", ...(event.type === "run.paused" ? { paused: true } : {}) } : message);
      next.error = event.type === "run.failed" ? String(data.error ?? data.message ?? "本次任务未完成，请重试。") : "";
      break;
    case "session.recovered":
      if (data.run_id && next.runId && String(data.run_id) !== String(next.runId)) break;
      if (isRunning(next.status) || next.status === "idle") next.status = data.session_status === "paused" ? "paused" : "interrupted";
      next.approvals = [];
      next.tools = unknownTools(next.tools);
      next.messages = next.messages.map((message) => message.streaming ? { ...message, streaming: false, interrupted: true } : message);
      break;
    default:
      break;
  }
  return next;
}

function reconcileLive(state, detail) {
  const live = detail.live ?? (detail.session ? { session_id: detail.session.id, status: detail.session.status === "paused" ? "paused" : "idle", run_id: null, pending_approvals: [] } : null);
  if (!live) return state;
  if (live.session_id !== detail.session?.id) throw new Error("会话恢复状态不匹配，请重新连接。");
  const runId = live.run_id ?? live.run?.id ?? state.runId;
  const terminal = state.terminalRun?.id && String(state.terminalRun.id) === String(runId) ? state.terminalRun : null;
  let status = terminal?.status ?? live.status;
  if (!live.run_id && isRunning(status)) status = "interrupted";
  if (!live.run_id && status === "idle" && isRunning(state.status)) status = "interrupted";
  const approvals = live.run_id && isRunning(status) ? (live.pending_approvals ?? []).filter((item) => item.status === "pending" && item.session_id === live.session_id && item.run_id === live.run_id) : [];
  if (approvals.length) status = "approval";
  let tools = state.tools.map((tool) => (!live.run_id || tool.run_id !== live.run_id) ? unknownTools([tool])[0] : tool);
  const calls = new Map(), unknownReceipts = new Map();
  for (const message of detail.messages ?? []) {
    for (const call of message.role === "assistant" ? message.tool_calls ?? [] : []) {
      calls.set(call.id, call); unknownReceipts.delete(call.id);
    }
    if (message.role !== "tool") continue;
    const receipt = toolReceipt({ result: message.content });
    if (receipt.status === "unknown") unknownReceipts.set(message.tool_call_id, { message, receipt, call: calls.get(message.tool_call_id) });
    else unknownReceipts.delete(message.tool_call_id);
  }
  for (const { message, receipt, call } of unknownReceipts.values()) {
    const existing = tools.find((tool) => tool.call_id === message.tool_call_id);
    // A real terminal event can precede its tool message. A later synthetic
    // unknown must not erase that receipt, even after another process recovered.
    if (existing?.receipt_state === "recorded" && existing.status !== "running") continue;
    // The old archive has no invocation key. An older batch cannot prove the
    // outcome of a currently live call which happens to reuse its provider ID.
    if (existing?.status === "running" && existing.run_id === live.run_id) continue;
    if (!existing && !call) continue;
    let argumentsValue = call?.arguments;
    if (typeof argumentsValue === "string") { try { argumentsValue = JSON.parse(argumentsValue); } catch { /* Retain the recorded arguments. */ } }
    tools = upsert(tools, { ...existing, call_id: message.tool_call_id, name: existing?.name ?? call?.name, arguments: existing?.arguments ?? argumentsValue, result: message.content, ...receipt }, "call_id");
  }
  return { ...state, status, runId, approvals, tools,
    error: status === "failed" ? String(terminal?.error ?? live.run?.error ?? state.error) : "",
    messages: state.messages.map((message) => message.streaming && !isRunning(status) ? { ...message, streaming: false, interrupted: true } : message),
  };
}

export function hydrateSession(detail) {
  let state = createSessionState(detail.messages ?? []);
  let pendingText = "";
  let reasoning = "";
  for (const event of detail.events ?? []) {
    const previous = state;
    state = applyEvent(state, event, { history: true });
    if (state === previous) continue;
    if (event.type === "run.started") { pendingText = ""; reasoning = ""; }
    if (event.type === "message.delta") pendingText += event.data?.content ?? "";
    if (event.type === "reasoning.delta") reasoning += event.data?.content ?? "";
    if (event.type === "message.completed" && event.data?.message?.role === "assistant") pendingText = "";
  }
  state.reasoning = reasoning;
  const partialRunId = state.runId;
  state = reconcileLive(state, detail);
  const streaming = isRunning(state.status) && (!detail.live || partialRunId === detail.live.run_id);
  if (pendingText) state.messages.push({ id: `stream:${partialRunId ?? "current"}`, role: "assistant", content: pendingText, streaming, interrupted: !streaming && state.status !== "completed", ...(state.status === "paused" ? { paused: true } : {}) });
  if (detail.after !== undefined) {
    if (typeof detail.after !== "string" || !/^(0|[1-9][0-9]*)$/.test(detail.after) || BigInt(detail.after) > 9223372036854775807n) throw new Error("会话事件位置无效，请重新连接。");
    state.snapshotAfter = detail.after; state.cursor = detail.after;
  }
  return state;
}

export function isRunning(status) {
  return status === "running" || status === "approval";
}

export function toolStatusLabel(tool) {
  return tool.status === "running" ? "执行中" : tool.status === "failed" ? "执行失败" : tool.status === "not_executed" ? tool.code === "task_paused" ? "未执行 · 已暂停" : "未执行" : tool.status === "completed" ? "已完成" : tool.status === "unknown" ? "结果未知" : "状态未知";
}

export function formatTimestamp(value, now = new Date()) {
  if (value === undefined || value === null || value === "") return "";
  const numeric = typeof value === "number" ? value : /^\d+(\.\d+)?$/.test(value) ? Number(value) : null;
  const date = new Date(numeric === null ? value : numeric < 1e12 ? numeric * 1000 : numeric);
  if (Number.isNaN(date.getTime())) return "";
  if (date.toDateString() === now.toDateString()) return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
  return date.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}

export function safeURL(value) {
  try {
    const url = new URL(value);
    return ["https:", "http:", "mailto:"].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

export function parseJSONField(value, fallback = {}) {
  if (!value.trim()) return fallback;
  const result = JSON.parse(value);
  if (!result || typeof result !== "object" || Array.isArray(result)) throw new Error("请输入 JSON 对象。");
  return result;
}

export function toDateTimeLocal(unixSeconds) {
  const date = new Date(unixSeconds * 1000);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function fromDateTimeLocal(value) {
  const result = new Date(value).getTime() / 1000;
  if (!Number.isFinite(result)) throw new Error("请选择有效的提醒时间。");
  return result;
}

export function formatFullDate(unixSeconds) {
  const date = new Date(unixSeconds * 1000);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("zh-CN", { month: "long", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
}
