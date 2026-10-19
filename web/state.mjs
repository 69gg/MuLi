export const MODES = Object.freeze({
  ask: { label: "Ask", description: "执行工具前审批" },
  auto: { label: "Auto", description: "自动判断，敏感操作审批" },
  yolo: { label: "Yolo", description: "直接执行已启用的工具" },
});

export function createSessionState(messages = []) {
  return {
    messages: [...messages], tools: [], approvals: [], reasoning: "",
    status: "idle", runId: null, cursor: "", seen: new Set(), error: "",
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

export function applyEvent(state, event, { history = false } = {}) {
  if (!event || !event.type || (event.id && state.seen.has(String(event.id)))) return state;
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
      next.tools = upsert(next.tools, { ...data, status: "running", created_at: event.created_at }, "call_id");
      break;
    case "tool.completed": {
      let result = data.result;
      if (typeof result === "string") { try { result = JSON.parse(result); } catch { /* Plain-text tool output is valid. */ } }
      next.tools = upsert(next.tools, { ...data, status: data.error || result?.error ? "failed" : "completed", completed_at: event.created_at }, "call_id");
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
      next.status = event.type.slice(4);
      next.approvals = [];
      if (event.type === "run.cancelled") next.queued = [];
      next.messages = next.messages.map((message) => message.streaming ? { ...message, streaming: false, interrupted: event.type !== "run.completed" } : message);
      next.error = event.type === "run.failed" ? String(data.error ?? data.message ?? "本次任务未完成，请重试。") : "";
      break;
    default:
      break;
  }
  return next;
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
  if (pendingText) state.messages.push({ id: `stream:${state.runId ?? "current"}`, role: "assistant", content: pendingText, streaming: isRunning(state.status), interrupted: !isRunning(state.status) && state.status !== "completed" });
  return state;
}

export function isRunning(status) {
  return status === "running" || status === "approval";
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
