export const MODES = Object.freeze({
  ask: { label: "Ask", description: "执行工具前审批" },
  auto: { label: "Auto", description: "自动判断，敏感操作审批" },
  yolo: { label: "Yolo", description: "直接执行已启用的工具" },
});

export function createSessionState(messages = []) {
  return {
    messages: [...messages], tools: [], approvals: [], reasoning: "",
    timeline: messages.filter((message) => ["user", "assistant"].includes(message.role)).map((message) => ({ id: `message:${message.id}`, kind: "message", message })),
    timelineRound: 0, timelineSerial: 0,
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

/** An ordered UI projection; accepted messages and tool receipt rules stay canonical. */
function projectConversation(previous, next, event, history) {
  const items = conversationEntries(previous), data = event.data ?? {};
  const runId = data.run_id ?? event.run_id ?? next.runId ?? "current";
  let round = previous.timelineRound ?? 0, serial = previous.timelineSerial ?? 0;
  const key = () => `conversation:${event.id ?? `local-${++serial}`}`;
  const sameTurn = (item) => item.runId === runId && item.round === round;
  const appendDelta = (kind, content) => {
    if (!content) return;
    const last = items.at(-1);
    if (last?.kind === kind && sameTurn(last) && (kind !== "message" || last.message.streaming)) {
      items[items.length - 1] = kind === "message"
        ? { ...last, message: { ...last.message, content: last.message.content + content } }
        : { ...last, content: last.content + content };
    } else {
      const id = key();
      items.push(kind === "message"
        ? { id, kind, runId, round, message: { id: `stream:${id}`, role: "assistant", content, created_at: event.created_at, streaming: true } }
        : { id, kind, runId, round, content, created_at: event.created_at, streaming: true });
    }
  };
  if (event.type === "run.started") round++;
  if (event.type === "reasoning.delta") appendDelta("reasoning", data.content ?? "");
  if (event.type === "message.delta") appendDelta("message", data.content ?? "");
  if (["message.completed", "message.queued", "attention.queued"].includes(event.type) && data.message) {
    const raw = event.type === "attention.queued" ? { ...data.message, role: "user", delivery_status: "pending" } : data.message;
    const message = history ? next.messages.find((item) => item.id === raw.id) ?? raw : raw;
    const already = items.findIndex((item) => item.kind === "message" && item.message.id === message.id);
    if (message.role === "assistant") {
      const open = items.map((item, index) => item.kind === "message" && sameTurn(item) && item.message.streaming ? index : -1).filter((index) => index >= 0);
      const streamed = open.map((index) => items[index].message.content).join("");
      if (already >= 0) items[already] = { ...items[already], message };
      else if (open.length) {
        // Keep text either side of reasoning/tools in its original slot. The
        // accepted body is authoritative, and attachments belong on its last part.
        if ((message.content ?? "").startsWith(streamed)) {
          let offset = 0;
          for (const [position, index] of open.entries()) {
            const last = position === open.length - 1;
            const end = last ? (message.content ?? "").length : offset + items[index].message.content.length;
            items[index] = { ...items[index], message: { ...message, content: (message.content ?? "").slice(offset, end), ...(!last ? { attachments: [], attachment_ids: [] } : {}) } };
            offset = end;
          }
        } else {
          const last = open.at(-1);
          items[last] = { ...items[last], message };
          for (const index of open.slice(0, -1).reverse()) items.splice(index, 1);
        }
      } else if (message.content || message.attachments?.length || message.attachment_ids?.length) items.push({ id: key(), kind: "message", runId, round, message });
      for (let index = 0; index < items.length; index++) if (items[index].kind === "reasoning" && sameTurn(items[index])) items[index] = { ...items[index], streaming: false };
      round++;
    } else if (message.role === "user") {
      const queued = event.type !== "message.completed" || message.delivery_status === "pending" || data.queued;
      if (!(queued && previous.messages.some((item) => item.id === message.id && item.delivery_status === "delivered"))) {
        const attachmentIds = message.attachment_ids ?? message.attachments?.map((file) => file.id) ?? [];
        const optimistic = message.principal === "system:attention" ? -1 : items.findIndex((item) => item.kind === "message" && item.message.pending && (item.message.content === message.content || (!item.message.content && attachmentIds.length && JSON.stringify(item.message.attachment_ids ?? item.message.attachments?.map((file) => file.id) ?? []) === JSON.stringify(attachmentIds))));
        const index = already >= 0 ? already : optimistic;
        if (index >= 0) items[index] = { ...items[index], message };
        else items.push({ id: key(), kind: "message", runId, round, message });
      }
    }
  }
  if (["tool.started", "tool.completed"].includes(event.type)) {
    const matchesCall = (item) => item.kind === "tool" && item.runId === runId && item.tool.call_id === data.call_id;
    let index = items.findLastIndex((item) => matchesCall(item) && item.tool.status === "running");
    if (index < 0 && event.type === "tool.completed") index = items.findLastIndex((item) => matchesCall(item) && sameTurn(item));
    const old = index >= 0 ? items[index].tool : {};
    const tool = { ...old, ...data, run_id: runId, ...(event.type === "tool.started" ? { status: "running", created_at: event.created_at } : { ...toolReceipt(data), completed_at: event.created_at }) };
    if (index >= 0) items[index] = { ...items[index], tool };
    else items.push({ id: key(), kind: "tool", runId, round, tool });
  }
  if (event.type === "conversation.injected") {
    const ids = new Set((data.message_ids ?? []).map(String));
    for (let index = 0; index < items.length; index++) if (items[index].kind === "message" && ids.has(String(items[index].message.id))) items[index] = { ...items[index], message: { ...items[index].message, delivery_status: "delivered" } };
  }
  if (event.type === "ingress.cancelled") {
    for (let index = items.length - 1; index >= 0; index--) if (items[index].kind === "message" && (items[index].message.delivery_status === "pending" || previous.queued.includes(String(items[index].message.id)))) items.splice(index, 1);
  }
  if (["run.completed", "run.cancelled", "run.failed", "run.paused", "session.recovered"].includes(event.type)) {
    for (let index = 0; index < items.length; index++) {
      const item = items[index];
      if (item.runId !== runId) continue;
      if (item.kind === "reasoning") items[index] = { ...item, streaming: false };
      if (item.kind === "message" && item.message.streaming) items[index] = { ...item, message: { ...item.message, streaming: false, interrupted: event.type !== "run.completed", ...(event.type === "run.paused" ? { paused: true } : {}) } };
      if (item.kind === "tool") items[index] = { ...item, tool: unknownTools([item.tool], runId)[0] };
    }
  }
  return { ...next, timeline: items, timelineRound: round, timelineSerial: serial };
}

function conversationEntries(state) {
  const pendingIds = new Set(state.messages.filter((message) => message.pending).map((message) => String(message.id)));
  const items = (state.timeline ?? state.messages.map((message) => ({ id: `message:${message.id}`, kind: "message", message })))
    .filter((item) => item.kind !== "message" || !item.message.pending || pendingIds.has(String(item.message.id)));
  const represented = new Set(items.filter((item) => item.kind === "message").map((item) => String(item.message.id)));
  // Preserve the composer's visible slot before projecting the next SSE event.
  const pending = state.messages.filter((message) => message.pending && !represented.has(String(message.id)));
  return [...items, ...pending.map((message) => ({ id: `message:${message.id}`, kind: "message", message }))];
}

/** Entries are ready to render in order: message, reasoning, or tool. */
export function conversationItems(state) {
  return conversationEntries(state)
    .filter((item) => item.kind !== "message" || ["user", "assistant"].includes(item.message.role) && (item.message.content || item.message.attachments?.length || item.message.attachment_ids?.length || item.message.pending || item.message.streaming));
}

function restoreConversation(state, detail) {
  const items = [...state.timeline];
  const messages = (detail.messages ?? []).filter((message) => ["user", "assistant"].includes(message.role));
  const covered = new Set(items.filter((item) => item.kind === "message").map((item) => String(item.message.id)));
  let anchored = false;
  for (const [position, message] of messages.entries()) {
    if (covered.has(String(message.id))) { anchored = true; continue; }
    // The event window is bounded. Restore only the accepted message order;
    // missing historical thinking/tool interleaving is not reconstructed.
    const following = messages.slice(position + 1).find((item) => covered.has(String(item.id)));
    const at = following ? anchored ? items.findIndex((item) => item.kind === "message" && String(item.message.id) === String(following.id)) : items.findIndex((item) => !item.historical) : items.length;
    items.splice(at < 0 ? items.length : at, 0, { id: `message:${message.id}`, kind: "message", message, historical: true });
    covered.add(String(message.id));
  }
  for (const tool of state.tools) {
    if (tool.status !== "unknown" || items.some((item) => item.kind === "tool" && item.tool.call_id === tool.call_id && (!tool.run_id || item.runId === tool.run_id))) continue;
    const archive = (detail.messages ?? []).findIndex((message) => message.role === "tool" && message.tool_call_id === tool.call_id);
    const following = (detail.messages ?? []).slice(archive + 1).find((message) => covered.has(String(message.id)));
    const at = following ? items.findIndex((item) => item.kind === "message" && String(item.message.id) === String(following.id)) : items.length;
    items.splice(at < 0 ? items.length : at, 0, { id: `archive-tool:${tool.call_id}`, kind: "tool", runId: tool.run_id, tool, historical: true });
  }
  const liveRun = detail.live?.run_id ?? state.runId;
  const live = isRunning(state.status);
  return {
    ...state,
    timeline: items.map((item) => {
      if (item.kind === "tool" && (!live || item.runId !== liveRun)) return { ...item, tool: unknownTools([item.tool])[0] };
      if (item.kind === "reasoning" && (!live || item.runId !== liveRun)) return { ...item, streaming: false };
      if (item.kind === "message" && item.message.streaming && (!live || item.runId !== liveRun)) return { ...item, message: { ...item.message, streaming: false, interrupted: state.status !== "completed", ...(state.status === "paused" ? { paused: true } : {}) } };
      return item;
    }),
  };
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
  return projectConversation(state, next, event, history);
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
  state.timeline = [];
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
  return restoreConversation(state, detail);
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
