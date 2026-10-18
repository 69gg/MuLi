import { request, readEvents } from "./api.mjs";
import { MODES, createSessionState, applyEvent, hydrateSession, isRunning, formatTimestamp, parseJSONField, toDateTimeLocal, fromDateTimeLocal, formatFullDate } from "./state.mjs";
import { renderMarkdown } from "./markdown.mjs";
import { getPath, setPath, settingsPatch } from "./settings.mjs";

const ICONS = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  chat: '<path d="M20 11.5a7.5 7.5 0 0 1-7.5 7.5H6l-4 3V11.5A7.5 7.5 0 0 1 9.5 4h3a7.5 7.5 0 0 1 7.5 7.5Z"/><path d="M7 10h8M7 14h5"/>',
  workflow: '<rect x="3" y="3" width="6" height="6" rx="1.5"/><rect x="15" y="15" width="6" height="6" rx="1.5"/><path d="M9 6h6a3 3 0 0 1 3 3v6M6 9v6a3 3 0 0 0 3 3h6"/>',
  memory: '<path d="M9 3a3 3 0 0 0-3 3v.5a3.5 3.5 0 0 0-2 6.3V15a4 4 0 0 0 4 4h1a3 3 0 0 0 3-3V6a3 3 0 0 0-3-3Zm6 0a3 3 0 0 1 3 3v.5a3.5 3.5 0 0 1 2 6.3V15a4 4 0 0 1-4 4h-1a3 3 0 0 1-3-3"/><path d="M7 10h2M6 15h3M15 10h2M15 15h3"/>',
  inbox: '<path d="M4 5h16l2 10v5H2v-5L4 5Z"/><path d="M2 15h6l2 3h4l2-3h6"/>',
  settings: '<path d="m9 3 1-1h4l1 4 4 1 2 4-3 3 1 4-4 2-3-2-4 1-3-3 1-4-2-3 2-4 3 1Z"/><circle cx="12" cy="11.5" r="3"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4.5 4.5"/>',
  refresh: '<path d="M20 6v5h-5M4 18v-5h5"/><path d="M19 10a7 7 0 0 0-12-5L4 8M5 14a7 7 0 0 0 12 5l3-3"/>',
  "chevron-down": '<path d="m6 9 6 6 6-6"/>',
  panel: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16M18 8h0M18 12h0"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  edit: '<path d="m15 4 5 5M4 20l5-1L21 7a2 2 0 0 0-5-5L4 14l-1 7Z"/>',
  trash: '<path d="M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7M14 10v7"/>',
  folder: '<path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/><path d="M3 9h18"/>',
  terminal: '<rect x="2.5" y="4" width="19" height="16" rx="2"/><path d="m6 9 3 3-3 3M12 16h5"/>',
  sparkles: '<path d="m12 3 2.2 6.8L21 12l-6.8 2.2L12 21l-2.2-6.8L3 12l6.8-2.2L12 3ZM20 2v4M18 4h4"/>',
  sliders: '<path d="M5 3v6M5 15v6M12 3v11M12 19v2M19 3v2M19 10v11"/><rect x="2" y="9" width="6" height="6" rx="1"/><rect x="9" y="14" width="6" height="5" rx="1"/><rect x="16" y="5" width="6" height="5" rx="1"/>',
  "arrow-up": '<path d="M12 19V5m-6 6 6-6 6 6"/>',
  "arrow-down": '<path d="M12 5v14m-6-6 6 6 6-6"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="1"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  copy: '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V3H4v13h4"/>',
  shield: '<path d="m12 2 8 3v6c0 5-4 8-8 11-4-3-8-6-8-11V5l8-3Z"/><path d="m8 11 3 3 5-6"/>',
  play: '<path d="m8 4 12 8-12 8V4Z"/>',
  bell: '<path d="M18 8a6 6 0 0 0-12 0v7l-3 3h18l-3-3V8ZM9 21h6"/>',
  link: '<path d="m9 15 6-6M7 14l-3 3a3 3 0 0 0 4 4l5-5a3 3 0 0 0 0-4M17 10l3-3a3 3 0 0 0-4-4l-5 5a3 3 0 0 0 0 4"/>',
  server: '<rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6.5h.01M7 17.5h.01M12 6.5h5M12 17.5h5"/>',
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = {
  view: "chat", status: null, connected: false, streamConnected: true, loading: true,
  sessions: [], activeSession: null, session: createSessionState(), mode: "auto", sending: false,
  timelineOpen: false, sessionsError: "", workflows: [], memories: [], notifications: [], reminders: [], notificationTab: "inbox", remindersError: "", memoryGraph: { nodes: [], edges: [] }, memoryTab: "list", graphEntity: "",
  viewError: "", viewLoading: false, memoryQuery: "", settings: null, settingsTab: "models",
  modelRole: "main", settingsDraft: null, service: null, settingsSaved: false,
  timelineUserChoice: false,
};
let sessionController = null;
let globalController = null;
let sessionRequestVersion = 0;
let queuedRender = false;
let messageNodes = new Map();
let toolNodes = new Map();
let dialogSubmit = null;

function element(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (value === undefined || value === null) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key in node && !key.startsWith("aria-")) node[key] = value;
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function icon(name) {
  const span = element("span", { "data-icon": name, "aria-hidden": "true" });
  span.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] ?? ICONS.info}</svg>`;
  return span;
}

function fillIcons(root = document) {
  $$('[data-icon]:empty', root).forEach((node) => node.replaceWith(icon(node.dataset.icon)));
}

function actionButton(label, action, variant = "secondary-button", iconName, data = {}) {
  return element("button", { type: "button", class: variant, dataset: { action, ...data } }, iconName && icon(iconName), label);
}

function iconButton(label, action, iconName, data = {}) {
  return element("button", { type: "button", class: "icon-button", "aria-label": label, title: label, dataset: { action, ...data } }, icon(iconName));
}

function toast(message, error = false) {
  const node = element("div", { class: `toast${error ? " error" : ""}`, role: error ? "alert" : "status" }, icon(error ? "info" : "check"), element("span", { text: message }));
  $("#toast-region").append(node);
  setTimeout(() => node.remove(), error ? 6500 : 3500);
}

function showError(error) { toast(error?.message ?? String(error), true); }
function pretty(value) {
  if (typeof value === "string") { try { return JSON.stringify(JSON.parse(value), null, 2); } catch { return value; } }
  return JSON.stringify(value, null, 2) ?? "";
}
function collection(payload) { return Array.isArray(payload) ? payload : payload?.items ?? []; }
function currentTitle() { return state.activeSession?.title || "新会话"; }
function busy() { return isRunning(state.session.status) || state.sending; }

function renderChrome() {
  const titles = { chat: currentTitle(), workflows: "工作流", memories: "记忆", notifications: "通知", settings: "设置" };
  $("#page-title").textContent = titles[state.view];
  document.title = `${titles[state.view]} · 沐璃`;
  $$(".nav-item[data-view]").forEach((node) => {
    const selected = node.dataset.view === state.view;
    node.classList.toggle("active", selected);
    node.setAttribute("aria-current", selected ? "page" : "false");
  });
  $("#rename-session").hidden = state.view !== "chat" || !state.activeSession;
  $("#model-label").textContent = state.status?.model || (state.loading ? "读取模型…" : state.connected ? "配置模型" : "未连接");
  $("#model-dot").className = `status-dot${state.connected && state.status?.configured ? " online" : state.connected ? " warning" : ""}`;
  $("#settings-attention").hidden = !state.connected || !!state.status?.configured;
  $("#connection-label").textContent = state.loading ? "连接工作台…" : state.connected ? state.streamConnected ? "工作台已连接" : "实时连接重连中" : "工作台未连接";
  $("#connection-dot").className = `status-dot${state.connected ? state.streamConnected ? " online" : " warning" : ""}`;
  const warning = !state.loading && (!state.connected || !state.streamConnected);
  $("#connection-banner").hidden = !warning;
  $("#connection-banner-text").textContent = !state.connected ? "服务暂未连接，输入内容会保留。" : "实时连接中断，正在重新连接。";
  const unread = state.notifications.filter((item) => !item.read).length;
  $("#notification-count").hidden = !unread;
  $("#notification-count").textContent = String(unread);
  $("#chat-view").hidden = state.view !== "chat";
  $("#other-view").hidden = state.view === "chat";
  $("#timeline-toggle").hidden = state.view !== "chat";
  $("#timeline").hidden = state.view !== "chat" || !state.timelineOpen;
  $("#timeline-toggle").setAttribute("aria-pressed", String(state.timelineOpen));
}

function renderSessions() {
  const query = $("#session-search").value.trim().toLowerCase();
  const fragment = document.createDocumentFragment();
  const sessions = state.sessions.filter((session) => !query || session.title?.toLowerCase().includes(query));
  if (!sessions.length) {
    fragment.append(element("p", { class: "sidebar-empty", text: state.loading ? "正在读取会话…" : state.sessionsError ? "会话暂未读取" : query ? "没有找到会话" : "新的对话会保存在这里" }));
  }
  for (const session of sessions) {
    const row = element("div", { class: `session-item${session.id === state.activeSession?.id ? " active" : ""}` });
    if (session.status === "running" || session.status === "approval") row.append(element("span", { class: "session-running", "aria-label": "运行中" }));
    row.append(element("button", { type: "button", class: "session-select", text: session.title || "未命名会话", title: session.title, dataset: { session: session.id } }));
    const remove = iconButton("删除会话", "delete-session", "trash", { id: session.id });
    remove.classList.add("session-delete"); row.append(remove); fragment.append(row);
  }
  $("#session-list").replaceChildren(fragment);
}

function renderComposer() {
  const input = $("#message-input");
  const running = busy();
  $("#mode-select").value = state.mode;
  $("#mode-select").disabled = running;
  $("#send-button").hidden = running;
  $("#send-button").disabled = !input.value.trim() || !state.connected;
  $("#stop-button").hidden = !running;
  $("#stop-button").disabled = state.sending || !state.activeSession;
  $("#composer-context").textContent = state.activeSession ? "当前会话" : "新会话";
  $("#composer-status").textContent = !state.connected ? state.loading ? "正在连接工作台" : "等待服务连接" : !state.status?.configured ? "请先在设置中配置模型" : running ? state.session.status === "approval" ? "等待你的批准" : "沐璃正在处理任务" : `${state.status.model} · ${MODES[state.mode]?.label ?? state.mode}`;
}

function scrollIsNearBottom() {
  const scroll = $("#conversation-scroll");
  return scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 100;
}

function scrollToBottom(force = false, smooth = false) {
  if (!force && !scrollIsNearBottom()) return;
  const scroll = $("#conversation-scroll");
  scroll.scrollTo({ top: scroll.scrollHeight, behavior: smooth ? "smooth" : "instant" });
  $("#scroll-bottom").hidden = true;
}

function renderChat() {
  const nearBottom = scrollIsNearBottom();
  $("#welcome").hidden = !!state.session.messages.length || busy();
  const container = $("#messages");
  const visibleMessages = state.session.messages.filter((message) => ["user", "assistant"].includes(message.role) && (message.content || message.pending || message.streaming));
  const ids = new Set(visibleMessages.map((message) => String(message.id)));
  for (const [id, node] of messageNodes) if (!ids.has(id)) { node.remove(); messageNodes.delete(id); }
  for (const message of visibleMessages) {
    const id = String(message.id);
    let node = messageNodes.get(id);
    if (!node) {
      node = element("article", { class: `message ${message.role}`, dataset: { messageId: id }, "aria-label": message.role === "user" ? "你的消息" : "沐璃的回复" });
      if (message.role !== "user") {
        node.append(element("div", { class: "message-heading" }, element("span", { class: "message-avatar" }, element("img", { src: "./assets/mark.svg", alt: "" })), element("span", { text: "沐璃" }), element("time", { class: "message-time", text: formatTimestamp(message.created_at), dateTime: message.created_at ?? "" })));
      }
      node.append(element("div", { class: "message-body" }));
      const actions = element("div", { class: "message-actions" }, iconButton("复制消息", "copy-message", "copy", { id }));
      node.append(actions); messageNodes.set(id, node); container.append(node);
    }
    if (node.dataset.content !== message.content) {
      const body = $(".message-body", node);
      if (message.role === "user") body.textContent = message.content ?? "";
      else body.replaceChildren(renderMarkdown(message.content));
      node.dataset.content = message.content ?? "";
    }
    node.classList.toggle("streaming", !!message.streaming);
    let interruption = $(".interrupted-label", node);
    if (message.interrupted && !interruption) { interruption = element("div", { class: "interrupted-label", text: "生成已停止" }); node.append(interruption); }
  }
  const reasoning = $("#reasoning-region");
  reasoning.hidden = !state.session.reasoning;
  if (state.session.reasoning) {
    if (!$("details", reasoning)) reasoning.replaceChildren(element("details", {}, element("summary", { text: "思考过程" }), element("div", { class: "reasoning-content" })));
    $(".reasoning-content", reasoning).textContent = state.session.reasoning;
  }
  renderApprovals();
  const runStatus = $("#run-status");
  runStatus.hidden = !busy() && !state.session.error;
  runStatus.classList.toggle("error", !!state.session.error);
  if (state.session.error) runStatus.replaceChildren(icon("info"), element("span", { text: state.session.error }));
  else if (busy()) runStatus.replaceChildren(element("span", { class: "thinking-dots", "aria-hidden": "true" }, element("i"), element("i"), element("i")), element("span", { text: state.session.status === "approval" ? "等待审批" : state.sending ? "正在发送…" : state.session.tools.some((tool) => tool.status === "running") ? "正在使用工具…" : "正在处理…" }));
  if (nearBottom) scrollToBottom(true);
  renderComposer();
  renderTimeline();
}

function renderApprovals() {
  const region = $("#approval-region");
  const existing = new Map($$(".approval-item", region).map((node) => [node.dataset.id, node]));
  const ids = new Set(state.session.approvals.map((item) => String(item.id)));
  for (const [id, node] of existing) if (!ids.has(id)) node.remove();
  for (const approval of state.session.approvals) {
    if (existing.has(String(approval.id))) continue;
    const node = element("section", { class: "approval-item", dataset: { id: approval.id }, "aria-label": "工具审批" },
      element("div", { class: "approval-heading" }, icon("shield"), "需要你的批准"),
      element("h3", { text: typeof approval.tool === "string" ? approval.tool : approval.tool?.name ?? "工具调用" }),
      approval.reason && element("p", { text: approval.reason }),
      element("pre", { text: pretty(approval.arguments) }),
      element("div", { class: "approval-actions" }, actionButton("拒绝", "approval", "secondary-button", undefined, { id: approval.id, decision: "reject" }), actionButton("批准执行", "approval", "primary-button", "check", { id: approval.id, decision: "approve" })));
    region.append(node);
  }
}

function renderTimeline() {
  const tools = state.session.tools;
  const complete = tools.filter((tool) => tool.status !== "running").length;
  const failed = tools.filter((tool) => tool.status === "failed").length;
  $("#timeline-summary").textContent = tools.length ? `${complete} / ${tools.length} 项工具调用结束${failed ? ` · ${failed} 项失败` : ""}` : state.activeSession ? "当前会话" : "开始任务后，在这里查看过程";
  const container = $("#tool-timeline");
  if (!tools.length) {
    container.replaceChildren(element("div", { class: "timeline-empty" }, icon("workflow"), "尚无工具调用"));
    toolNodes = new Map();
  } else {
    if (!toolNodes.size) container.replaceChildren();
    const ids = new Set(tools.map((tool) => String(tool.call_id)));
    for (const [id, node] of toolNodes) if (!ids.has(id)) { node.remove(); toolNodes.delete(id); }
    for (const tool of tools) {
      const id = String(tool.call_id);
      let node = toolNodes.get(id);
      if (!node) {
        node = element("div", { class: "tool-step" }, element("span", { class: "tool-step-dot" }), element("details", {}, element("summary"), element("div", { class: "tool-step-label", text: "参数" }), element("pre", { class: "tool-arguments" }), element("div", { class: "tool-step-label tool-result-label", text: "结果" }), element("pre", { class: "tool-result" })), element("div", { class: "tool-step-status" }));
        toolNodes.set(id, node); container.append(node);
      }
      node.className = `tool-step ${tool.status}`;
      $("summary", node).textContent = tool.name || "工具调用";
      $(".tool-arguments", node).textContent = pretty(tool.arguments ?? "未记录参数");
      $(".tool-result", node).textContent = pretty(tool.result ?? tool.error ?? "");
      $(".tool-result", node).hidden = tool.result === undefined && !tool.error;
      $(".tool-result-label", node).hidden = tool.result === undefined && !tool.error;
      $(".tool-step-status", node).textContent = tool.status === "running" ? "执行中" : tool.status === "failed" ? "执行失败" : "已完成";
    }
  }
  $("#timeline-footer").textContent = `${MODES[state.mode]?.label ?? state.mode} 模式${state.session.status === "cancelled" ? " · 任务已停止" : ""}`;
}

function queueRender() {
  if (queuedRender) return;
  queuedRender = true;
  requestAnimationFrame(() => { queuedRender = false; renderChrome(); if (state.view === "chat") renderChat(); });
}

async function refreshSessions() {
  try {
    state.sessions = collection(await request("/sessions")); state.sessionsError = "";
    const active = state.sessions.find((session) => session.id === state.activeSession?.id);
    if (active) state.activeSession = active;
  }
  catch (error) { state.sessionsError = error.message; }
  renderSessions(); renderChrome();
}

async function refreshNotifications() {
  try { state.notifications = collection(await request("/notifications")); renderChrome(); if (state.view === "notifications") renderOtherView(); } catch { /* Status is reported by the main connection indicator. */ }
}

async function connect() {
  state.loading = true; renderChrome();
  const results = await Promise.allSettled([request("/status"), request("/sessions"), request("/notifications")]);
  if (results[0].status === "fulfilled") {
    state.status = results[0].value; state.connected = true;
    if (!state.activeSession) state.mode = state.status.mode || "auto";
  } else { state.connected = false; }
  if (results[1].status === "fulfilled") { state.sessions = collection(results[1].value); state.sessionsError = ""; }
  else state.sessionsError = results[1].reason.message;
  if (results[2].status === "fulfilled") state.notifications = collection(results[2].value);
  state.loading = false;
  renderChrome(); renderSessions(); renderChat();
  if (state.connected) startGlobalEvents();
}

function waitToReconnect(milliseconds, signal) {
  return new Promise((resolve) => {
    if (signal.aborted) { resolve(); return; }
    const finish = () => { clearTimeout(timer); signal.removeEventListener("abort", finish); resolve(); };
    const timer = setTimeout(finish, milliseconds);
    signal.addEventListener("abort", finish, { once: true });
  });
}

async function subscribe(path, controller, { cursor = "", onEvent, reportConnection = false } = {}) {
  let delay = 1000;
  let after = cursor;
  while (!controller.signal.aborted) {
    try {
      await readEvents(path, {
        signal: controller.signal, after,
        onOpen: () => { delay = 1000; if (reportConnection) { state.streamConnected = true; renderChrome(); } },
        onEvent: (event) => { if (event.id) after = String(event.id); onEvent(event); },
      });
    } catch (error) {
      if (error.name === "AbortError" || controller.signal.aborted) return;
    }
    if (controller.signal.aborted) return;
    if (reportConnection) { state.streamConnected = false; renderChrome(); }
    await waitToReconnect(delay, controller.signal);
    delay = Math.min(delay * 2, 12000);
  }
}

function startGlobalEvents() {
  globalController?.abort();
  globalController = new AbortController();
  void subscribe("/events", globalController, { reportConnection: true, onEvent: (event) => {
    if (["notification.created", "notification.updated", "run.completed", "run.failed", "approval.requested"].includes(event.type)) void refreshNotifications();
    if (event.type.startsWith("run.") || event.type.startsWith("session.")) void refreshSessions();
    if (event.type === "workflow.updated") {
      const workflow = state.workflows.find((item) => item.id === event.data?.workflow_id);
      if (workflow && event.data.run) {
        const runs = workflow.runs ?? [];
        workflow.runs = [event.data.run, ...runs.filter((run) => run.id !== event.data.run.id)];
        if (state.view === "workflows") renderOtherView();
      }
      void refreshSessions();
    }
    if (event.type === "reminder.updated") {
      const item = event.data;
      if (item?.id) state.reminders = [item, ...state.reminders.filter((entry) => entry.id !== item.id)];
      if (state.view === "notifications" && state.notificationTab === "reminders") renderOtherView();
    }
  } });
}

function startSessionEvents(sessionId) {
  sessionController?.abort();
  sessionController = new AbortController();
  void subscribe(`/sessions/${encodeURIComponent(sessionId)}/events`, sessionController, {
    cursor: state.session.cursor,
    onEvent: (event) => {
      if (event.session_id && event.session_id !== state.activeSession?.id) return;
      state.session = applyEvent(state.session, event);
      if ((event.type === "tool.started" || event.type === "approval.requested") && !state.timelineUserChoice) state.timelineOpen = true;
      if (event.type === "message.completed" && event.data?.message?.role === "user") state.sending = false;
      if (["run.completed", "run.failed", "run.cancelled"].includes(event.type)) { state.sending = false; void refreshSessions(); }
      queueRender();
    },
  });
}

function resetMessageNodes() { $("#messages").replaceChildren(); messageNodes = new Map(); $("#tool-timeline").replaceChildren(); toolNodes = new Map(); }

async function loadSession(id) {
  const version = ++sessionRequestVersion;
  closeSidebar();
  try {
    const detail = await request(`/sessions/${encodeURIComponent(id)}`);
    if (version !== sessionRequestVersion) return;
    sessionController?.abort();
    state.activeSession = detail.session;
    state.session = hydrateSession(detail);
    if (isRunning(detail.session?.status) && !isRunning(state.session.status)) state.session.status = detail.session.status;
    state.mode = detail.session?.mode || state.mode;
    state.view = "chat"; state.sending = false; state.timelineUserChoice = false; state.timelineOpen = state.session.tools.some((tool) => tool.status === "running");
    resetMessageNodes(); renderChrome(); renderSessions(); renderChat();
    startSessionEvents(id);
    scrollToBottom(true);
  } catch (error) { showError(error); }
}

function newSession() {
  ++sessionRequestVersion;
  sessionController?.abort(); sessionController = null;
  state.activeSession = null; state.session = createSessionState(); state.sending = false; state.view = "chat"; state.timelineOpen = false; state.timelineUserChoice = false; state.mode = state.status?.mode || "auto";
  resetMessageNodes(); renderChrome(); renderSessions(); renderChat(); closeSidebar(); $("#message-input").focus();
}

async function sendMessage(event) {
  event.preventDefault();
  const input = $("#message-input");
  const content = input.value.trim();
  if (!content || busy() || !state.connected) return;
  if (!state.status?.configured) { await switchView("settings"); toast("先配置一个主模型，即可开始对话。"); return; }
  const intentVersion = sessionRequestVersion;
  const mode = state.mode;
  let targetSession = state.activeSession;
  let pendingId = null;
  const stillCurrent = () => intentVersion === sessionRequestVersion && (!targetSession || state.activeSession?.id === targetSession.id);
  input.value = ""; resizeComposer();
  state.sending = true; renderComposer();
  try {
    if (!targetSession) {
      const created = await request("/sessions", { method: "POST", body: { mode } });
      targetSession = created.session ?? created;
      state.sessions.unshift(targetSession);
      if (intentVersion === sessionRequestVersion) {
        state.activeSession = targetSession;
        renderChrome(); renderSessions(); startSessionEvents(targetSession.id);
      }
    }
    if (stillCurrent()) {
      pendingId = `pending:${crypto.randomUUID()}`;
      state.session.messages.push({ id: pendingId, role: "user", content, created_at: new Date().toISOString(), pending: true });
      renderChat(); scrollToBottom(true);
    }
    try {
      const result = await request(`/sessions/${encodeURIComponent(targetSession.id)}/messages`, { method: "POST", body: { content, mode } });
      if (stillCurrent()) {
        if (result.message) state.session = applyEvent(state.session, { type: "message.completed", data: { message: result.message } });
        const alreadyFinished = state.session.runId === result.run_id && ["completed", "cancelled", "failed"].includes(state.session.status);
        state.session.runId = result.run_id;
        if (!isRunning(state.session.status) && !alreadyFinished) state.session.status = "running";
        state.sending = false;
      }
      void refreshSessions();
    } catch (error) {
      if (stillCurrent()) state.session.messages = state.session.messages.filter((message) => message.id !== pendingId);
      throw error;
    }
  } catch (error) {
    showError(error);
    if (stillCurrent()) { state.sending = false; if (!input.value) input.value = content; resizeComposer(); }
  }
  renderChrome(); renderChat();
}

async function approve(id, decision, button) {
  const node = button.closest(".approval-item");
  const sessionId = state.activeSession?.id;
  $$("button", node).forEach((item) => item.disabled = true);
  try {
    await request(`/approvals/${encodeURIComponent(id)}`, { method: "POST", body: { decision } });
    if (state.activeSession?.id === sessionId) state.session = applyEvent(state.session, { type: "approval.resolved", data: { id, decision } });
    renderChat(); toast(decision === "approve" ? "已批准执行" : "已拒绝本次调用");
  } catch (error) { showError(error); $$("button", node).forEach((item) => item.disabled = false); }
}

async function stopRun(button) {
  if (!state.activeSession) return;
  button.disabled = true;
  try { await request(`/sessions/${encodeURIComponent(state.activeSession.id)}/stop`, { method: "POST" }); toast("已发送停止请求"); }
  catch (error) { showError(error); }
  renderComposer();
}

function resizeComposer() {
  const input = $("#message-input"); input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 220)}px`; renderComposer();
}

function syncSidebarAccessibility() {
  const mobile = window.matchMedia("(max-width: 760px)").matches;
  const hidden = mobile && !$("#sidebar").classList.contains("open");
  $("#sidebar").inert = hidden;
  $("#sidebar").setAttribute("aria-hidden", String(hidden));
  if (!mobile) $("#sidebar-backdrop").hidden = true;
}

function closeSidebar() {
  const hadFocus = $("#sidebar").contains(document.activeElement);
  $("#sidebar").classList.remove("open"); $("#sidebar-backdrop").hidden = true;
  if (hadFocus && window.matchMedia("(max-width: 760px)").matches) $('[data-action="toggle-sidebar"]').focus();
  syncSidebarAccessibility();
}

function openDialog({ title, content, submitLabel = "保存", danger = false, onSubmit }) {
  $("#dialog-title").textContent = title;
  $("#dialog-content").replaceChildren(content);
  $("#dialog-submit").textContent = submitLabel;
  $("#dialog-submit").className = danger ? "danger-button" : "primary-button";
  $("#dialog-error").hidden = true;
  $("#dialog-submit").disabled = false;
  dialogSubmit = onSubmit;
  $("#app-dialog").showModal();
  const input = $('input:not([type="checkbox"]), textarea, select', $("#dialog-content"));
  input?.focus({ preventScroll: true });
}

function confirmAction(title, message, submitLabel, onConfirm) {
  openDialog({ title, content: element("p", { text: message }), submitLabel, danger: true, onSubmit: onConfirm });
}

function field(label, name, value = "", options = {}) {
  const { type = "text", hint, rows, required = false, placeholder, min, max, step } = options;
  const input = type === "textarea"
    ? element("textarea", { name, value: value ?? "", rows: rows ?? 3, required, placeholder, class: options.json ? "json-field" : "" })
    : element("input", { name, type, value: value ?? "", required, placeholder, min, max, step, autocomplete: type === "password" ? "new-password" : "off" });
  if (options.format) input.dataset.format = options.format;
  if (options.maxLength) input.maxLength = options.maxLength;
  return element("label", { class: "field" }, element("span", { text: label }), input, hint && element("small", { text: hint }));
}

function selectField(label, name, value, choices, hint) {
  const select = element("select", { name });
  for (const [key, text] of choices) select.append(element("option", { value: key, text }));
  select.value = value ?? choices[0]?.[0];
  return element("label", { class: "field" }, element("span", { text: label }), select, hint && element("small", { text: hint }));
}

function switchField(label, name, checked) {
  return element("label", { class: "field-check" }, element("span", { text: label }), element("input", { type: "checkbox", class: "switch", name, checked: !!checked, role: "switch" }));
}

function viewHeading(title, subtitle, button) {
  return element("div", { class: "view-heading" }, element("div", {}, element("h2", { text: title }), subtitle && element("p", { text: subtitle })), button);
}

function emptyState(iconName, title, description) {
  return element("div", { class: "empty-state" }, icon(iconName), element("h3", { text: title }), description && element("p", { text: description }));
}

async function switchView(view) {
  state.view = view; state.viewError = ""; closeSidebar(); renderChrome();
  if (view === "chat") { renderChat(); return; }
  state.viewLoading = true; renderOtherView();
  try {
    if (view === "workflows") state.workflows = collection(await request("/workflows"));
    if (view === "memories") {
      const results = await Promise.allSettled([request(`/memories${state.memoryQuery ? `?query=${encodeURIComponent(state.memoryQuery)}` : ""}`), request("/memories/graph")]);
      if (results[0].status === "rejected") throw results[0].reason;
      state.memories = collection(results[0].value);
      state.memoryGraph = results[1].status === "fulfilled" ? results[1].value : { nodes: [], edges: [] };
    }
    if (view === "notifications") {
      const results = await Promise.allSettled([request("/notifications"), request("/reminders")]);
      if (results[0].status === "rejected") throw results[0].reason;
      state.notifications = collection(results[0].value);
      state.reminders = results[1].status === "fulfilled" ? collection(results[1].value) : [];
      state.remindersError = results[1].status === "rejected" ? results[1].reason.message : "";
    }
    if (view === "settings") {
      const results = await Promise.allSettled([request("/settings"), request("/service")]);
      if (results[0].status === "rejected") throw results[0].reason;
      state.settings = results[0].value; state.settingsDraft = structuredClone(state.settings); state.settingsSaved = false;
      state.service = results[1].status === "fulfilled" ? results[1].value : null;
    }
  } catch (error) { state.viewError = error.message; }
  state.viewLoading = false; renderOtherView(); renderChrome();
}

function renderOtherView() {
  const root = $("#other-view");
  if (state.viewLoading) { root.replaceChildren(element("div", { class: "view-content" }, emptyState("refresh", "正在读取…"))); return; }
  if (state.viewError) {
    const empty = emptyState("info", "暂时无法读取", state.viewError);
    empty.append(actionButton("重试", "retry-view", "secondary-button", "refresh"));
    root.replaceChildren(element("div", { class: "view-content" }, empty)); return;
  }
  if (state.view === "memories") root.replaceChildren(renderMemories());
  if (state.view === "workflows") root.replaceChildren(renderWorkflows());
  if (state.view === "notifications") root.replaceChildren(renderNotifications());
  if (state.view === "settings") root.replaceChildren(renderSettings());
}

function renderMemories() {
  const search = element("input", { type: "search", name: "memory-search", id: "memory-search", value: state.memoryQuery, placeholder: "检索内容、实体或偏好", "aria-label": "检索记忆" });
  const form = element("form", { class: "content-toolbar", onSubmit: async (event) => {
    event.preventDefault(); state.memoryQuery = search.value.trim();
    try { state.memories = collection(await request(`/memories?query=${encodeURIComponent(state.memoryQuery)}`)); renderOtherView(); $("#memory-search")?.focus(); } catch (error) { showError(error); }
  } }, element("div", { class: "search-field" }, icon("search"), search), element("button", { class: "secondary-button", type: "submit", text: "检索" }));
  const tabs = element("div", { class: "role-tabs memory-tabs", role: "tablist", "aria-label": "记忆视图" }, ...[["list", "记忆列表"], ["graph", "关系图"]].map(([key, label]) => element("button", { type: "button", role: "tab", class: state.memoryTab === key ? "active" : "", text: label, "aria-selected": String(state.memoryTab === key), dataset: { memoryTab: key } })));
  const root = element("div", { class: "view-content" }, viewHeading("记忆", "保留偏好、事实与长期上下文。", actionButton("新增记忆", "new-memory", "primary-button", "plus")), tabs);
  if (state.memoryTab === "graph") { root.append(renderMemoryGraph()); return root; }
  root.append(form, element("div", { class: "list-caption" }, `${state.memories.length} 条记忆`, state.memoryQuery ? `“${state.memoryQuery}”` : "全部记忆"));
  if (!state.memories.length) root.append(emptyState("memory", state.memoryQuery ? "没有找到相关记忆" : "还没有记忆", state.memoryQuery ? "换个关键词试试。" : "添加希望沐璃记住的内容。"));
  for (const item of state.memories) {
    root.append(element("article", { class: "memory-row" }, element("div", { class: "row-icon" }, icon("memory")), element("div", { class: "row-content" }, element("p", { text: item.content }), element("div", { class: "row-meta" }, element("span", { class: "row-tag", text: memoryKind(item.kind) }), ...(item.entities ?? []).map((entity) => element("span", { text: typeof entity === "string" ? entity : entity.name ?? "" })), element("time", { text: formatTimestamp(item.updated_at ?? item.created_at) }), item.matched_by && element("span", { text: { text: "文本匹配", graph: "关系匹配", "graph+text": "文本与关系匹配" }[item.matched_by] ?? item.matched_by }), item.relations?.length && actionButton(`${item.relations.length} 条关系`, "memory-evidence", "text-button", undefined, { id: item.id }))), element("div", { class: "row-actions" }, iconButton("编辑记忆", "edit-memory", "edit", { id: item.id }), iconButton("删除记忆", "delete-memory", "trash", { id: item.id }))));
  }
  return root;
}

function memoryKind(kind) { return { preference: "偏好", fact: "事实", observation: "观察", procedure: "流程", summary: "摘要" }[kind] ?? kind ?? "观察"; }

function editMemory(id) {
  const item = state.memories.find((entry) => String(entry.id) === String(id));
  const relations = element("div", { id: "memory-relations", class: "memory-relations-editor" });
  for (const relation of item?.relations ?? []) relations.append(relationEditor(relation));
  const content = element("div", {}, field("内容", "content", item?.content ?? "", { type: "textarea", rows: 5, required: true }), selectField("类型", "kind", item?.kind ?? "observation", [["observation", "观察"], ["preference", "偏好"], ["fact", "事实"], ["procedure", "流程"], ["summary", "摘要"]]), field("关联实体", "entities", (item?.entities ?? []).join(", "), { placeholder: "用逗号分隔" }), element("details", { class: "advanced-settings", open: !!item?.relations?.length }, element("summary", { text: "关系与来源" }), element("label", { class: "field" }, element("span", { text: "关系" })), relations, actionButton("添加关系", "add-memory-relation", "secondary-button", "plus"), element("div", { class: "field-row field-top-space" }, field("来源", "source", item?.source ?? "user"), field("置信度", "confidence", item?.confidence ?? 1, { type: "number", min: 0, max: 1, step: .05 }))));
  openDialog({ title: item ? "编辑记忆" : "新增记忆", content, onSubmit: async (form) => {
    const data = new FormData(form);
    const edges = $$(".relation-editor", form).map((row) => Object.fromEntries(["subject", "predicate", "object"].map((key) => [key, $(`[name=relation-${key}]`, row).value.trim()])));
    if (edges.some((edge) => !edge.subject || !edge.predicate || !edge.object)) throw new Error("请填写完整关系，或移除空白关系。");
    const payload = { content: String(data.get("content")).trim(), kind: data.get("kind"), entities: String(data.get("entities")).split(/[,，]/).map((entry) => entry.trim()).filter(Boolean), relations: edges, source: String(data.get("source")).trim() || "user", confidence: Number(data.get("confidence")) };
    if (!payload.content) throw new Error("请输入记忆内容。");
    await request(item ? `/memories/${encodeURIComponent(item.id)}` : "/memories", { method: item ? "PATCH" : "POST", body: payload });
    toast(item ? "记忆已更新" : "记忆已保存"); await switchView("memories");
  } });
}

function relationEditor(relation = {}) {
  return element("div", { class: "relation-editor" },
    element("input", { name: "relation-subject", value: relation.subject ?? "", placeholder: "主体", "aria-label": "关系主体", maxLength: 200 }),
    element("input", { name: "relation-predicate", value: relation.predicate ?? "", placeholder: "关系", "aria-label": "关系名称", maxLength: 200 }),
    element("input", { name: "relation-object", value: relation.object ?? "", placeholder: "对象", "aria-label": "关系对象", maxLength: 200 }),
    iconButton("移除关系", "remove-memory-relation", "close"));
}

function svgNode(tag, attributes = {}, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderMemoryGraph() {
  const graph = state.memoryGraph;
  if (!graph.edges?.length) return emptyState("memory", "还没有关联关系", "编辑记忆时，为实体添加关系。 ");
  const selected = graph.nodes.includes(state.graphEntity) ? state.graphEntity : graph.nodes[0];
  state.graphEntity = selected;
  const edges = graph.edges.filter((edge) => edge.subject === selected || edge.object === selected);
  const choose = element("select", { id: "graph-entity", "aria-label": "查看实体关系" }, graph.nodes.map((name) => element("option", { value: name, text: name })));
  choose.value = selected;
  choose.addEventListener("change", () => { state.graphEntity = choose.value; renderOtherView(); });
  const displayed = edges.slice(0, 8);
  const height = Math.max(250, displayed.length * 65 + 30);
  const svg = svgNode("svg", { viewBox: `0 0 740 ${height}`, role: "img", "aria-label": `${selected} 的 ${displayed.length} 条关联关系` });
  svg.append(svgNode("title", {}, `${selected} 的已保存关系`));
  const centerY = height / 2;
  for (const [index, edge] of displayed.entries()) {
    const y = 35 + index * 65;
    const target = edge.subject === selected ? edge.object : edge.subject;
    svg.append(svgNode("path", { d: `M 263 ${centerY} C 370 ${centerY}, 370 ${y}, 477 ${y}`, class: "graph-edge" }));
    svg.append(svgNode("text", { x: 371, y: (centerY + y) / 2 - 8, "text-anchor": "middle", class: "graph-predicate" }, `${edge.subject === selected ? "→" : "←"} ${edge.predicate}`));
    svg.append(svgNode("rect", { x: 478, y: y - 18, width: 210, height: 36, rx: 8, class: "graph-node" }));
    svg.append(svgNode("text", { x: 583, y: y + 4, "text-anchor": "middle", class: "graph-label" }, target.length > 23 ? `${target.slice(0, 22)}…` : target));
  }
  svg.append(svgNode("rect", { x: 52, y: centerY - 23, width: 210, height: 46, rx: 10, class: "graph-node graph-node-selected" }));
  svg.append(svgNode("text", { x: 157, y: centerY + 4, "text-anchor": "middle", class: "graph-label graph-label-selected" }, selected.length > 23 ? `${selected.slice(0, 22)}…` : selected));
  const root = element("div", {}, element("div", { class: "graph-toolbar" }, element("label", {}, "实体", choose), element("span", { text: `${graph.nodes.length} 个实体 · ${graph.edges.length} 条关系` })), element("div", { class: "memory-graph" }, svg), element("div", { class: "list-caption" }, "关系与证据", `${edges.length} 条${edges.length > displayed.length ? ` · 图中显示 ${displayed.length} 条` : ""}`));
  for (const edge of edges) root.append(element("div", { class: "graph-evidence-row" }, element("span", { class: "graph-evidence-triple" }, element("strong", { text: edge.subject }), element("span", { text: edge.predicate }), element("strong", { text: edge.object })), actionButton("查看来源", "memory-evidence", "text-button", "link", { id: edge.memory_id })));
  return root;
}

async function showMemoryEvidence(id) {
  let item = state.memories.find((memory) => String(memory.id) === String(id));
  if (!item) item = await request(`/memories/${encodeURIComponent(id)}`);
  const content = element("div", {}, element("p", { class: "memory-evidence-content", text: item.content }), statusList([["类型", memoryKind(item.kind)], ["来源", item.source], ["更新", formatTimestamp(item.updated_at)], ["置信度", `${Math.round(item.confidence * 100)}%`]]));
  if (item.relations?.length) {
    content.append(element("div", { class: "memory-evidence-relations" }, item.relations.map((edge) => element("div", { class: "graph-evidence-triple" }, element("strong", { text: edge.subject }), element("span", { text: edge.predicate }), element("strong", { text: edge.object })))));
  }
  const history = element("details", { class: "advanced-settings memory-history" }, element("summary", { text: "修改历史" }));
  const historyList = element("div", { class: "memory-history-list" });
  const loadHistory = async () => {
    if (history.dataset.loading || history.dataset.loaded) return;
    history.dataset.loading = "true";
    historyList.replaceChildren(element("p", { class: "settings-role-hint", text: "正在读取…" }));
    try {
      const revisions = collection(await request(`/memories/${encodeURIComponent(id)}/revisions`));
      historyList.replaceChildren();
      if (!revisions.length) historyList.append(element("p", { class: "settings-role-hint", text: "暂无修改记录。" }));
      for (const revision of revisions) {
        const snapshot = revision.snapshot ?? {};
        historyList.append(element("article", { class: "memory-revision" }, element("time", { text: formatFullDate(revision.changed_at) }), element("p", { class: "memory-evidence-content", text: snapshot.content ?? "" }), element("div", { class: "row-meta" }, memoryKind(snapshot.kind), snapshot.source), (snapshot.relations ?? []).map((edge) => element("div", { class: "graph-evidence-triple" }, element("strong", { text: edge.subject }), element("span", { text: edge.predicate }), element("strong", { text: edge.object })))));
      }
      history.dataset.loaded = "true";
    } catch (error) {
      historyList.replaceChildren(element("p", { class: "form-error", text: error.message }), element("button", { type: "button", class: "text-button", text: "重试", onClick: () => void loadHistory() }));
    } finally { delete history.dataset.loading; }
  };
  history.addEventListener("toggle", () => { if (history.open) void loadHistory(); });
  history.append(historyList); content.append(history);
  openDialog({ title: "记忆来源", content, submitLabel: "完成", onSubmit: async () => {} });
}

function renderWorkflows() {
  const root = element("div", { class: "view-content" }, viewHeading("工作流", "把重复任务整理成可运行的步骤。", element("div", { class: "view-heading-actions" }, actionButton("从模板开始", "workflow-templates", "secondary-button", "sparkles"), actionButton("新建工作流", "new-workflow", "primary-button", "plus"))), element("div", { class: "list-caption" }, `${state.workflows.length} 个工作流`, "任务编排"));
  if (!state.workflows.length) root.append(emptyState("workflow", "还没有工作流", "创建一个流程，按步骤协作完成任务。"));
  for (const workflow of state.workflows) {
    const steps = element("div", { class: "workflow-steps" });
    for (const [index, step] of (workflow.steps ?? []).entries()) steps.append(element("span", { class: "workflow-step-label" }, element("span", { class: "workflow-step-number", text: index + 1 }), step.title || step.name || step.id));
    const content = element("div", { class: "row-content" }, element("h3", { text: workflow.name }), workflow.description && element("p", { text: workflow.description }), steps, element("div", { class: "row-meta" }, `${workflow.steps?.length ?? 0} 个步骤`, `${MODES[workflow.mode]?.label ?? "Auto"} 模式`, element("time", { text: formatTimestamp(workflow.updated_at ?? workflow.created_at) })));
    const latest = workflow.runs?.[0];
    if (latest) content.append(renderWorkflowRun(latest));
    root.append(element("article", { class: "workflow-row" }, element("div", { class: "row-icon" }, icon("workflow")), content, element("div", { class: "row-actions" }, latest && ["running", "queued"].includes(latest.status) ? actionButton("停止", "cancel-workflow-run", "secondary-button", "stop", { id: latest.id }) : actionButton("运行", "run-workflow", "secondary-button", "play", { id: workflow.id }), iconButton("删除工作流", "delete-workflow", "trash", { id: workflow.id }))));
  }
  return root;
}

function renderWorkflowRun(run) {
  const labels = { pending: "等待中", queued: "排队中", running: "运行中", completed: "已完成", failed: "失败", cancelled: "已取消", timed_out: "已超时", skipped: "已跳过", interrupted: "已中断" };
  const details = element("details", { class: `workflow-run ${run.status}` }, element("summary", {}, element("span", { class: "status-dot", "aria-hidden": "true" }), `最近运行 · ${labels[run.status] ?? run.status}`, element("time", { text: formatTimestamp(run.updated_at ?? run.created_at) })));
  for (const node of Object.values(run.nodes ?? {})) details.append(element("div", { class: "workflow-run-node" }, element("span", { class: "workflow-node-status", text: labels[node.status] ?? node.status }), element("span", { text: node.title ?? node.id }), node.session_id && actionButton("会话", "workflow-session", "text-button", undefined, { session: node.session_id })));
  if (run.result) details.append(element("div", { class: "workflow-run-result" }, renderMarkdown(run.result)));
  if (run.error) details.append(element("p", { class: "form-error", text: run.error }));
  return details;
}

function workflowTemplates() {
  const templates = [
    { id: "review", title: "执行与复核", description: "执行任务，再独立检查结果。", steps: [{ title: "执行任务", role: "worker", prompt: "完成任务：\n\n[填写具体目标]", dependency: "none" }, { title: "独立复核", role: "reviewer", prompt: "独立检查任务结果与证据，指出未验证事项和遗漏，并给出修正建议。", dependency: "all" }] },
    { id: "research", title: "调研与整理", description: "收集资料，核验来源，再形成结论。", steps: [{ title: "收集资料", role: "worker", prompt: "调研主题：\n\n[填写主题]\n\n优先查找一手来源，记录事实、来源与时间。", dependency: "none" }, { title: "核验资料", role: "reviewer", prompt: "检查资料来源、时效性与相互矛盾之处，区分已证实信息和推测。", dependency: "previous" }, { title: "整理结论", role: "main", prompt: "基于已核验的资料，整理简洁、可追溯来源的结论。", dependency: "all" }] },
  ];
  const content = element("div", { class: "workflow-templates" });
  for (const template of templates) content.append(element("button", { type: "button", class: "workflow-template", onClick: () => { $("#app-dialog").close(); newWorkflow(template); } }, icon("workflow"), element("span", {}, element("strong", { text: template.title }), element("small", { text: template.description })), element("span", { text: "→" })));
  openDialog({ title: "选择模板", content, submitLabel: "关闭", onSubmit: async () => {} });
}

function newWorkflow(template = null) {
  const steps = element("div", { class: "workflow-steps-editor", id: "workflow-steps-editor" });
  const content = element("div", {}, field("名称", "name", template?.title ?? "", { required: true, placeholder: "例如：项目检查" }), field("说明", "description", template?.description ?? "", { placeholder: "可选" }), selectField("执行模式", "mode", state.mode, Object.entries(MODES).map(([key, value]) => [key, `${value.label} · ${value.description}`])), element("label", { class: "field" }, element("span", { text: "步骤" })), steps, element("div", { class: "field-top-space" }, actionButton("添加步骤", "add-workflow-step", "secondary-button", "plus")));
  openDialog({ title: "新建工作流", content, submitLabel: "创建工作流", onSubmit: async (form) => {
    const data = new FormData(form);
    const rows = $$(".workflow-step-editor", form);
    if (!rows.length) throw new Error("请至少添加一个步骤。");
    const workflowSteps = rows.map((row, index) => {
      const dependency = $("[name=step-dependency]", row).value;
      return { id: `step-${index + 1}`, title: $("[name=step-title]", row).value.trim(), prompt: $("[name=step-prompt]", row).value.trim(), role: $("[name=step-role]", row).value, depends_on: dependency === "none" ? [] : dependency === "all" ? Array.from({ length: index }, (_, before) => `step-${before + 1}`) : index ? [`step-${index}`] : [] };
    });
    if (workflowSteps.some((step) => !step.title || !step.prompt)) throw new Error("请填写每个步骤的标题和任务。");
    await request("/workflows", { method: "POST", body: { name: String(data.get("name")).trim(), description: String(data.get("description")).trim(), mode: data.get("mode"), steps: workflowSteps } });
    toast("工作流已创建"); await switchView("workflows");
  } });
  if (template) for (const step of template.steps) addWorkflowStep(step);
  else addWorkflowStep();
}

function addWorkflowStep(step = {}) {
  const root = $("#workflow-steps-editor");
  if (!root) return;
  if (root.children.length >= (state.status?.limits?.max_workflow_steps ?? 32)) { toast("已达到工作流步骤上限", true); return; }
  const row = element("div", { class: "workflow-step-editor" }, element("div", { class: "field-row" }, field("标题", "step-title", step.title ?? "", { required: true, placeholder: `步骤 ${root.children.length + 1}` }), selectField("模型角色", "step-role", step.role ?? "worker", [["worker", "执行模型"], ["reviewer", "审阅模型"], ["main", "主模型"]]), iconButton("移除步骤", "remove-workflow-step", "close")), field("任务", "step-prompt", step.prompt ?? "", { type: "textarea", required: true, placeholder: "描述此步骤需要完成的事" }), selectField("依赖", "step-dependency", step.dependency ?? (root.children.length ? "previous" : "none"), [["none", "无依赖 · 可并行"], ["previous", "上一步"], ["all", "前面所有步骤"]]));
  root.append(row);
}

async function runWorkflow(id, button) {
  button.disabled = true;
  try {
    const result = await request(`/workflows/${encodeURIComponent(id)}/run`, { method: "POST" });
    toast("工作流已开始"); await refreshSessions();
    if (result.session_id) await loadSession(result.session_id);
  } catch (error) { showError(error); }
  button.disabled = false;
}

function renderNotifications() {
  const unread = state.notifications.filter((item) => !item.read).length;
  const reminders = state.notificationTab === "reminders";
  const tabs = element("div", { class: "role-tabs", role: "tablist", "aria-label": "通知视图" }, ...[["inbox", "收件箱"], ["reminders", "提醒"]].map(([key, label]) => element("button", { type: "button", role: "tab", class: state.notificationTab === key ? "active" : "", text: label, "aria-selected": String(state.notificationTab === key), dataset: { notificationTab: key } })));
  const root = element("div", { class: "view-content" }, viewHeading("通知", reminders ? "到时间，在这里收到提醒。" : unread ? `${unread} 条未读通知。` : "任务结果与需要处理的消息。", reminders ? actionButton("新建提醒", "new-reminder", "primary-button", "plus") : unread ? actionButton("全部已读", "read-all", "secondary-button", "check") : undefined), tabs);
  if (reminders) { root.append(renderReminders()); return root; }
  root.append(element("div", { class: "list-caption" }, `${state.notifications.length} 条通知`, "收件箱"));
  if (!state.notifications.length) root.append(emptyState("inbox", "通知已清空", "任务完成或需要处理时，会显示在这里。"));
  for (const item of state.notifications) {
    const content = element("div", { class: "row-content" }, element("h3", { text: item.title || item.type || "任务通知" }), (item.content || item.message || item.body) && element("p", { text: item.content || item.message || item.body }), element("div", { class: "row-meta" }, element("time", { text: formatTimestamp(item.created_at) }), item.session_id && actionButton("查看会话", "notification-session", "text-button", undefined, { id: item.id, session: item.session_id })));
    root.append(element("article", { class: `notification-row${item.read ? "" : " unread"}` }, element("div", { class: "row-icon" }, icon(item.type?.includes("approval") ? "shield" : "bell")), content, !item.read && iconButton("标为已读", "read-notification", "check", { id: item.id })));
  }
  return root;
}

function intervalLabel(seconds) {
  if (!seconds) return "单次";
  if (seconds % 604800 === 0) return `每 ${seconds / 604800} 周`;
  if (seconds % 86400 === 0) return `每 ${seconds / 86400} 天`;
  if (seconds % 3600 === 0) return `每 ${seconds / 3600} 小时`;
  if (seconds % 60 === 0) return `每 ${seconds / 60} 分钟`;
  return `每 ${seconds} 秒`;
}

function renderReminders() {
  if (state.remindersError) {
    const empty = emptyState("info", "暂时无法读取提醒", state.remindersError);
    empty.append(actionButton("重试", "retry-view", "secondary-button", "refresh")); return empty;
  }
  const active = state.reminders.filter((item) => item.enabled).length;
  const root = element("div", {}, element("div", { class: "list-caption" }, `${state.reminders.length} 个提醒`, `${active} 个已启用`));
  if (!state.reminders.length) root.append(emptyState("bell", "还没有提醒", "选择时间，添加一次或重复提醒。"));
  const sorted = [...state.reminders].sort((first, second) => Number(second.enabled) - Number(first.enabled) || first.due_at - second.due_at);
  for (const item of sorted) {
    const completed = !item.enabled && !item.interval_seconds && item.fired_count > 0;
    root.append(element("article", { class: `notification-row reminder-row${item.enabled ? "" : " paused"}` }, element("div", { class: "row-icon" }, icon("bell")), element("div", { class: "row-content" }, element("h3", { text: item.title }), item.content && element("p", { text: item.content }), element("div", { class: "row-meta" }, element("time", { text: formatFullDate(item.due_at) }), element("span", { class: "row-tag", text: intervalLabel(item.interval_seconds) }), element("span", { text: item.enabled ? "已启用" : completed ? "已完成" : "已暂停" }), item.fired_count > 0 && element("span", { text: `已提醒 ${item.fired_count} 次` }))), element("div", { class: "row-actions" }, actionButton("改期", "edit-reminder", "text-button", undefined, { id: item.id }), iconButton(item.enabled ? "暂停提醒" : "启用提醒", "toggle-reminder", item.enabled ? "stop" : "play", { id: item.id }), iconButton("删除提醒", "delete-reminder", "trash", { id: item.id }))));
  }
  return root;
}

function editReminder(id) {
  const item = state.reminders.find((entry) => String(entry.id) === String(id));
  const standardIntervals = [3600, 86400, 604800];
  const intervalValue = !item?.interval_seconds ? "once" : standardIntervals.includes(item.interval_seconds) ? String(item.interval_seconds) : "custom";
  const custom = field("间隔（秒）", "custom-interval", item?.interval_seconds ?? 3600, { type: "number", min: 60, max: 31536000 });
  custom.hidden = intervalValue !== "custom";
  const interval = selectField("重复", "interval", intervalValue, [["once", "不重复"], ["3600", "每小时"], ["86400", "每天"], ["604800", "每周"], ["custom", "自定义"]]);
  $("select", interval).addEventListener("change", (event) => { custom.hidden = event.target.value !== "custom"; });
  const due = item?.due_at > Date.now() / 1000 ? item.due_at : Date.now() / 1000 + 3600;
  const content = element("div", {}, field("标题", "title", item?.title ?? "", { required: true, maxLength: 120 }), field("时间", "due_at", toDateTimeLocal(due), { type: "datetime-local", required: true }), interval, custom, field("内容", "content", item?.content ?? "", { type: "textarea", rows: 3, placeholder: "可选", maxLength: 4000 }), item && switchField("启用提醒", "enabled", item.enabled));
  openDialog({ title: item ? "编辑提醒" : "新建提醒", content, submitLabel: item ? "保存提醒" : "创建提醒", onSubmit: async (form) => {
    const data = new FormData(form);
    const dueAt = fromDateTimeLocal(String(data.get("due_at")));
    const enabled = item ? data.get("enabled") === "on" : true;
    if (enabled && dueAt <= Date.now() / 1000) throw new Error("请选择一个未来时间。");
    const selected = data.get("interval");
    const intervalSeconds = selected === "once" ? null : selected === "custom" ? Number(data.get("custom-interval")) : Number(selected);
    if (intervalSeconds !== null && (!Number.isInteger(intervalSeconds) || intervalSeconds < 60 || intervalSeconds > 31536000)) throw new Error("重复间隔需为 60 秒至一年。");
    const payload = { title: String(data.get("title")).trim(), content: String(data.get("content")).trim(), due_at: dueAt, interval_seconds: intervalSeconds, ...(item ? { enabled } : {}) };
    await request(item ? `/reminders/${encodeURIComponent(item.id)}` : "/reminders", { method: item ? "PATCH" : "POST", body: payload });
    toast(item ? "提醒已更新" : "提醒已创建"); await switchView("notifications");
  } });
}

async function toggleReminder(id, button) {
  const item = state.reminders.find((entry) => String(entry.id) === String(id));
  if (!item) return;
  if (!item.enabled && item.due_at <= Date.now() / 1000) { editReminder(id); return; }
  button.disabled = true;
  try { await request(`/reminders/${encodeURIComponent(id)}`, { method: "PATCH", body: { enabled: !item.enabled } }); await switchView("notifications"); }
  finally { button.disabled = false; }
}

async function markRead(id) {
  await request(`/notifications/${encodeURIComponent(id)}`, { method: "PATCH", body: { read: true } });
  const item = state.notifications.find((entry) => String(entry.id) === String(id)); if (item) item.read = true;
  renderChrome(); if (state.view === "notifications") renderOtherView();
}

function renderSettings() {
  if (!state.settingsDraft) return element("div", { class: "view-content" }, emptyState("settings", "设置暂不可用"));
  const nav = element("nav", { class: "settings-nav", "aria-label": "设置分组" });
  for (const [key, label] of [["models", "模型"], ["tools", "权限与工具"], ["connections", "连接"], ["system", "系统"]]) nav.append(element("button", { type: "button", class: state.settingsTab === key ? "active" : "", text: label, dataset: { settingsTab: key }, "aria-current": state.settingsTab === key ? "page" : "false" }));
  const form = element("form", { id: "settings-form", class: "settings-panel", onSubmit: saveSettings });
  if (state.settingsTab === "models") form.append(renderModelSettings());
  if (state.settingsTab === "tools") form.append(renderToolSettings());
  if (state.settingsTab === "connections") form.append(renderConnectionSettings());
  if (state.settingsTab === "system") form.append(renderSystemSettings());
  form.append(element("p", { class: "form-error", id: "settings-error", hidden: true }), element("div", { class: "settings-footer" }, element("button", { class: "primary-button", type: "submit", text: "保存设置" }), state.settingsSaved && element("span", { class: "settings-save-status", text: "已保存" })));
  return element("div", { class: "view-content" }, viewHeading("设置", "模型、工具与本地运行配置。"), element("div", { class: "settings-layout" }, nav, form));
}

function settingsSection(title, ...children) {
  return element("section", { class: "settings-section" }, element("h3", { text: title }), children);
}

function renderModelSettings() {
  const role = state.modelRole;
  const model = state.settingsDraft.models?.[role] ?? {};
  const path = `models.${role}`;
  const tabs = element("div", { class: "role-tabs", role: "tablist", "aria-label": "模型角色" });
  for (const [key, label] of [["main", "主模型"], ["reviewer", "审阅模型"], ["worker", "执行模型"]]) tabs.append(element("button", { type: "button", role: "tab", class: role === key ? "active" : "", text: label, "aria-selected": String(role === key), dataset: { modelRole: key } }));
  const protocol = model.protocol ?? "openai_chat";
  const advanced = element("details", { class: "advanced-settings" }, element("summary", { text: "高级参数" }),
    element("div", { class: "field-row" }, field("Temperature", `${path}.temperature`, model.temperature ?? "", { type: "number", step: .1, min: 0, max: 2, placeholder: "使用模型默认值" }), field("Top P", `${path}.top_p`, model.top_p ?? "", { type: "number", step: .05, min: 0, max: 1, placeholder: "使用模型默认值" })),
    element("div", { class: "field-row" }, field("最大输出 Token", `${path}.max_output_tokens`, model.max_output_tokens ?? "", { type: "number", min: 128, max: 131072, step: 1, placeholder: "使用服务默认值" }), field("超时（秒）", `${path}.timeout`, model.timeout ?? "", { type: "number", min: 1, max: 600, placeholder: "使用服务默认值" })),
    element("div", { class: "field-row" }, field("重试次数", `${path}.max_retries`, model.max_retries ?? "", { type: "number", min: 0, max: 5, step: 1 }), field("API Key 环境变量", `${path}.api_key_env`, model.api_key_env ?? "", { placeholder: "可选" })),
    field(protocol === "anthropic" ? "Thinking" : "Reasoning", `${path}.reasoning`, pretty(model.reasoning ?? {}), { type: "textarea", rows: 2, json: true, format: "json", hint: protocol === "anthropic" ? 'JSON，例如 {"type":"enabled","budget_tokens":1024}' : 'JSON，例如 {"effort":"high"}' }),
    field("Extra body", `${path}.extra_body`, pretty(model.extra_body ?? {}), { type: "textarea", rows: 3, json: true, format: "json" }),
    field("Extra headers", `${path}.extra_headers`, pretty(model.extra_headers ?? {}), { type: "textarea", rows: 3, json: true, format: "json", hint: "保持 •••• 可保留原值，移除字段即可删除。" }),
    switchField("流式输出", `${path}.stream`, model.stream !== false),
    model.api_key_set && switchField("清除已存 API Key", `${path}.clear_api_key`, false));
  return element("div", {}, tabs, role !== "main" && element("p", { class: "settings-role-hint", text: role === "worker" ? "未单独配置时，复用主模型。" : "未配置时，工具审查交由你确认。" }),
    settingsSection("模型连接", selectField("协议", `${path}.protocol`, protocol, [["openai_chat", "OpenAI · Chat Completions"], ["openai_responses", "OpenAI · Responses"], ["anthropic", "Anthropic · Messages"]]), field("模型名称", `${path}.model`, model.model ?? "", { placeholder: "填写提供商的模型 ID", required: role === "main" }), field("API Base URL", `${path}.base_url`, model.base_url ?? "", { type: "url", placeholder: "填写服务地址" }), field("API Key", `${path}.api_key`, model.api_key ?? "", { type: "password", placeholder: model.api_key_set ? "已配置，留空保留" : "输入 API Key", hint: model.api_key_set ? "已有密钥不会显示。" : undefined }), advanced));
}

function renderToolSettings() {
  const config = state.settingsDraft;
  const search = config.search ?? {};
  const computer = config.computer ?? {};
  const list = element("div");
  for (const tool of state.status?.tools ?? []) {
    list.append(element("div", { class: "tool-permission-row" }, element("div", {}, element("h4", { text: tool.name }), element("p", { text: tool.description || (tool.enabled === false ? "未启用" : "已启用") })), element("span", { class: "row-tag", text: tool.risk || "工具" }), element("input", { type: "checkbox", class: "switch", name: `tool-enabled:${tool.name}`, checked: !(config.disabled_tools ?? []).includes(tool.name), role: "switch", "aria-label": `启用 ${tool.name}` })));
  }
  return element("div", {},
    settingsSection("执行权限", selectField("默认模式", "default_mode", config.default_mode ?? "auto", Object.entries(MODES).map(([key, mode]) => [key, `${mode.label} · ${mode.description}`])), field("允许的工作目录", "workspace_roots", (config.workspace_roots ?? []).join("\n"), { type: "textarea", rows: 3, format: "lines", hint: "每行一个目录。" })),
    settingsSection("联网搜索", selectField("服务", "search.provider", search.provider ?? "searxng", [["searxng", "SearXNG"], ["brave", "Brave Search"], ["tavily", "Tavily"]]), field("服务地址", "search.base_url", search.base_url ?? "", { type: "url" }), element("div", { class: "field-row" }, field("API Key 环境变量", "search.api_key_env", search.api_key_env ?? ""), field("结果数量", "search.max_results", search.max_results ?? "", { type: "number", min: 1, max: 20 })), field("API Key", "search.api_key", search.api_key ?? "", { type: "password", placeholder: search.api_key_set ? "已配置，留空保留" : "可选" }), search.api_key_set && switchField("清除已存 API Key", "search.clear_api_key", false)),
    settingsSection("电脑操作", switchField("启用电脑操作", "computer.enabled", computer.enabled), field("后端", "computer.backend", computer.backend ?? "atspi"), field("Python 路径", "computer.python_path", computer.python_path ?? "", { placeholder: "使用默认运行环境" }), element("p", { text: state.status?.computer?.available ? "电脑操作已就绪。" : state.status?.computer?.reason || "电脑操作暂不可用。" })),
    list.children.length ? settingsSection("已注册工具", list) : null);
}

function renderConnectionSettings() {
  const config = state.settingsDraft.chiliarch ?? {};
  const status = state.status?.chiliarch;
  const pair = element("div", { class: "pairing-row" }, field("配对码", "pairing-code", "", { placeholder: "输入 Chiliarch 配对码" }), actionButton("配对", "pair-chiliarch", "secondary-button", "link"));
  const connectionLabels = { connected: "已连接", retrying: "正在重连", connecting: "正在连接", disabled: "未启用", unpaired: "未配对", disconnected: "未连接", failed: "连接失败" };
  const connectionLabel = status?.connected || status?.status === "connected" ? "已连接" : connectionLabels[status?.status] ?? "未连接";
  return element("div", {}, settingsSection("Chiliarch", switchField("启用 Chiliarch 连接", "chiliarch.enabled", config.enabled), field("服务地址", "chiliarch.base_url", config.base_url ?? "", { type: "url" }), field("Agent ID", "chiliarch.agent_id", config.agent_id ?? "", { placeholder: "配对后自动填写" }), statusList([["连接", connectionLabel], ["凭据", config.credential_set ? "已配对" : "未配对"]]), status?.last_error && element("p", { class: "form-error", text: status.last_error }), element("div", { class: "field-top-space" }, pair)), renderMCPSettings());
}

function renderMCPSettings() {
  const config = state.settingsDraft.mcp;
  const section = settingsSection("MCP");
  const servers = state.status?.mcp;
  if (Array.isArray(servers) && servers.length) {
    const labels = { connected: "已连接", disabled: "未启用", connecting: "正在连接", failed: "连接失败", error: "连接失败" };
    section.append(statusList(servers.map((server) => [server.name, labels[server.status] ?? server.status ?? "未连接"])));
  }
  if (!config || "configured" in config) {
    section.append(element("p", { text: "当前服务暂未提供可编辑的 MCP 配置。" }));
    return section;
  }
  section.append(element("details", { class: "advanced-settings" }, element("summary", { text: "服务器配置" }), field("MCP JSON", "mcp", pretty(config), { type: "textarea", rows: 10, json: true, format: "json", hint: "使用 mcpServers。保留 •••• 可沿用密钥，删除服务器即可移除连接。" })));
  return section;
}

function statusList(entries) {
  return element("dl", { class: "status-list" }, entries.map(([key, value]) => element("div", {}, element("dt", { text: key }), element("dd", { text: value ?? "—" }))));
}

function renderSystemSettings() {
  const config = state.settingsDraft;
  const limits = config.limits ?? {};
  const service = state.service;
  const serviceSection = settingsSection("用户服务", statusList([["服务", service?.supported === false ? "当前系统不支持" : service?.unit || "读取中"], ["安装", service?.installed ? "已安装" : "未安装"], ["状态", service?.active ? "运行中" : "未运行"], ["登录后启动", service?.enabled ? "已启用" : "未启用"]]));
  if (service?.supported) {
    const actions = element("div", { class: "service-actions" });
    if (!service.installed) actions.append(actionButton("安装用户服务", "service", "secondary-button", "server", { command: "install" }));
    else {
      actions.append(actionButton(service.active ? "重启" : "启动", "service", "secondary-button", "refresh", { command: service.active ? "restart" : "start" }));
      if (service.active) actions.append(actionButton("停止", "service", "secondary-button", "stop", { command: "stop" }));
      actions.append(actionButton(service.enabled ? "关闭自动启动" : "登录后启动", "service", "secondary-button", undefined, { command: service.enabled ? "disable" : "enable" }));
    }
    actions.append(actionButton("刷新状态", "refresh-service", "text-button", "refresh"));
    serviceSection.append(actions);
  }
  if (service?.log) serviceSection.append(element("details", { class: "advanced-settings" }, element("summary", { text: "服务日志" }), element("pre", { class: "service-log", text: Array.isArray(service.log) ? service.log.join("\n") : service.log })));
  if (service?.reason) serviceSection.append(element("p", { text: service.reason }));
  return element("div", {},
    settingsSection("工作台", statusList([["版本", state.status?.version], ["平台", state.status?.platform], ["运行任务", String(state.status?.running ?? 0)]]), element("div", { class: "field-row field-top-space" }, field("监听地址", "server.host", config.server?.host ?? ""), field("端口", "server.port", config.server?.port ?? "", { type: "number", min: 1, max: 65535 })), element("p", { text: "监听配置在下次启动时生效。" }), element("div", { class: "system-links" }, element("a", { href: "/api/export", download: "muli-export.jsonl", class: "secondary-button export-button", text: "导出数据" }), element("a", { href: "/api/docs", target: "_blank", rel: "noopener", class: "secondary-button export-button", text: "API 参考" }))),
    settingsSection("通知", switchField("桌面通知", "notifications.enabled", config.notifications?.enabled), state.status?.notifications?.available === false && element("p", { text: "桌面通知暂不可用。" }), state.status?.notifications?.last_error && element("p", { class: "form-error", text: state.status.notifications.last_error })),
    serviceSection,
    settingsSection("运行限制", element("div", { class: "field-row" }, field("并行 Agent 数", "limits.max_parallel_agents", limits.max_parallel_agents ?? "", { type: "number", min: 1, max: 32 }), field("未完成任务上限", "limits.max_pending_runs", limits.max_pending_runs ?? "", { type: "number", min: 1, placeholder: "使用服务默认值" })), element("div", { class: "field-row" }, field("工具步骤上限", "limits.max_tool_steps", limits.max_tool_steps ?? "", { type: "number", min: 1 }), field("任务时限（秒）", "limits.max_run_seconds", limits.max_run_seconds ?? "", { type: "number", min: 1 })), element("div", { class: "field-row" }, field("文件大小上限（字节）", "limits.max_file_bytes", limits.max_file_bytes ?? "", { type: "number", min: 1 }), field("工具输出字符上限", "limits.max_output_chars", limits.max_output_chars ?? "", { type: "number", min: 1 }))));
}

function collectSettingsForm() {
  const form = $("#settings-form");
  if (!form || !state.settingsDraft) return;
  for (const input of $$('[name]', form)) {
    if (!input.name || input.name === "pairing-code") continue;
    if (input.name.startsWith("tool-enabled:")) {
      const name = input.name.slice("tool-enabled:".length);
      const disabled = new Set(state.settingsDraft.disabled_tools ?? []);
      if (input.checked) disabled.delete(name); else disabled.add(name);
      state.settingsDraft.disabled_tools = [...disabled];
      continue;
    }
    let value;
    if (input.type === "checkbox") value = input.checked;
    else if (input.type === "number") {
      if (!input.value.trim()) continue;
      value = Number(input.value);
      if (!Number.isFinite(value)) throw new Error("数值格式不正确。");
    } else if (input.dataset.format === "json") {
      try { value = parseJSONField(input.value); } catch { throw new Error(`${input.closest("label")?.firstElementChild?.textContent ?? "参数"} 需要填写有效 JSON 对象。`); }
    } else if (input.dataset.format === "lines") value = input.value.split("\n").map((line) => line.trim()).filter(Boolean);
    else value = input.type === "password" ? input.value : input.value.trim();
    if (input.name.endsWith(".api_key") && !value) continue;
    setPath(state.settingsDraft, input.name, value);
  }
}

function displaySettingsError(error) {
  const node = $("#settings-error");
  if (node) { node.textContent = error.message ?? String(error); node.hidden = false; } else showError(error);
}

async function saveSettings(event) {
  event.preventDefault();
  const submit = $('button[type="submit"]', event.target);
  try {
    collectSettingsForm();
    const patch = settingsPatch(state.settings, state.settingsDraft);
    if (!Object.keys(patch).length) { toast("设置没有变化"); return; }
    submit.disabled = true;
    await request("/settings", { method: "PATCH", body: patch });
    const results = await Promise.allSettled([request("/settings"), request("/status")]);
    if (results[0].status === "fulfilled") { state.settings = results[0].value; state.settingsDraft = structuredClone(state.settings); }
    if (results[1].status === "fulfilled") { state.status = results[1].value; if (!state.activeSession) state.mode = state.status.mode || "auto"; }
    state.settingsSaved = true; renderOtherView(); renderChrome(); renderComposer(); toast("设置已保存");
  } catch (error) { displaySettingsError(error); submit.disabled = false; }
}

async function pairChiliarch(button) {
  const code = $('[name="pairing-code"]')?.value.trim();
  if (!code) { toast("请输入配对码", true); return; }
  button.disabled = true;
  try {
    collectSettingsForm();
    const patch = settingsPatch(state.settings, state.settingsDraft);
    if (Object.keys(patch).length) await request("/settings", { method: "PATCH", body: patch });
    await request("/integrations/chiliarch/pair", { method: "POST", body: { code, name: "MuLi" } });
    toast("配对已完成"); await switchView("settings");
  } catch (error) { displaySettingsError(error); button.disabled = false; }
}

async function serviceAction(action) {
  const perform = async () => {
    try {
      await request("/service", { method: "POST", body: { action } });
      toast(action === "stop" ? "已发送停止请求" : "已提交服务操作");
    } catch (error) {
      if (!["restart", "stop"].includes(action) || error.status !== 0) throw error;
      toast("服务连接已断开，正在检查状态。");
    }
    if (action === "stop") {
      state.connected = false; globalController?.abort(); sessionController?.abort(); renderChrome(); renderComposer();
    }
    if (action === "restart") setTimeout(() => void connect().then(() => refreshService()), 1800);
    else await refreshService();
  };
  if (["restart", "stop"].includes(action)) confirmAction(action === "stop" ? "停止用户服务" : "重启用户服务", action === "stop" ? "当前工作台连接将断开。再次使用时可通过本地命令启动服务。" : "工作台将短暂断开，正在运行的任务可能被中断。", action === "stop" ? "停止服务" : "重启服务", perform);
  else { try { await perform(); } catch (error) { showError(error); } }
}

async function refreshService() {
  try { state.service = await request("/service"); }
  catch (error) { if (state.connected) showError(error); }
  if (state.view === "settings" && state.settingsTab === "system") {
    try { collectSettingsForm(); } catch { /* Keep fields visible so the user can correct them. */ }
    renderOtherView();
  }
}

async function handleAction(action, button) {
  if (action === "new-session") newSession();
  if (action === "toggle-sidebar") { const open = !$("#sidebar").classList.contains("open"); $("#sidebar").classList.toggle("open", open); $("#sidebar-backdrop").hidden = !open; syncSidebarAccessibility(); if (open) $(".new-session").focus(); }
  if (action === "toggle-session-search") { const input = $("#session-search"); input.hidden = !input.hidden; if (!input.hidden) input.focus(); }
  if (action === "toggle-timeline") { state.timelineOpen = !state.timelineOpen; state.timelineUserChoice = true; renderChrome(); renderTimeline(); }
  if (action === "model-settings") { state.settingsTab = "models"; await switchView("settings"); }
  if (action === "refresh") { await connect(); if (state.activeSession) await loadSession(state.activeSession.id); if (state.view !== "chat") await switchView(state.view); }
  if (action === "retry-view") await switchView(state.view);
  if (action === "scroll-bottom") scrollToBottom(true, true);
  if (action === "stop") await stopRun(button);
  if (action === "approval") await approve(button.dataset.id, button.dataset.decision, button);
  if (action === "close-dialog") $("#app-dialog").close();
  if (action === "copy-message") {
    const message = state.session.messages.find((item) => String(item.id) === button.dataset.id);
    if (message) { await navigator.clipboard.writeText(message.content); toast("已复制"); }
  }
  if (action === "rename-session" && state.activeSession) {
    const id = state.activeSession.id;
    openDialog({ title: "重命名会话", content: field("名称", "title", currentTitle(), { required: true }), onSubmit: async (form) => {
      const title = String(new FormData(form).get("title")).trim();
      if (!title) throw new Error("请输入会话名称。");
      await request(`/sessions/${encodeURIComponent(id)}`, { method: "PATCH", body: { title } });
      state.activeSession.title = title; await refreshSessions(); renderChrome();
    } });
  }
  if (action === "delete-session") {
    const id = button.dataset.id;
    confirmAction("删除会话", "这段会话及其运行记录会被删除。", "删除会话", async () => {
      await request(`/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
      if (state.activeSession?.id === id) newSession(); await refreshSessions(); toast("会话已删除");
    });
  }
  if (action === "new-memory") editMemory();
  if (action === "edit-memory") editMemory(button.dataset.id);
  if (action === "delete-memory") confirmAction("删除记忆", "删除后，沐璃将不再检索这条记忆。", "删除记忆", async () => { await request(`/memories/${encodeURIComponent(button.dataset.id)}`, { method: "DELETE" }); await switchView("memories"); toast("记忆已删除"); });
  if (action === "add-memory-relation") $("#memory-relations")?.append(relationEditor());
  if (action === "remove-memory-relation") button.closest(".relation-editor")?.remove();
  if (action === "memory-evidence") await showMemoryEvidence(button.dataset.id);
  if (action === "new-workflow") newWorkflow();
  if (action === "workflow-templates") workflowTemplates();
  if (action === "add-workflow-step") addWorkflowStep();
  if (action === "remove-workflow-step") button.closest(".workflow-step-editor")?.remove();
  if (action === "run-workflow") await runWorkflow(button.dataset.id, button);
  if (action === "workflow-session") await loadSession(button.dataset.session);
  if (action === "cancel-workflow-run") {
    button.disabled = true;
    try { await request(`/workflow-runs/${encodeURIComponent(button.dataset.id)}/cancel`, { method: "POST" }); toast("工作流已停止"); await switchView("workflows"); } finally { button.disabled = false; }
  }
  if (action === "delete-workflow") confirmAction("删除工作流", "此工作流定义会被删除，已有会话保留。", "删除工作流", async () => { await request(`/workflows/${encodeURIComponent(button.dataset.id)}`, { method: "DELETE" }); await switchView("workflows"); toast("工作流已删除"); });
  if (action === "read-notification") await markRead(button.dataset.id);
  if (action === "new-reminder") editReminder();
  if (action === "edit-reminder") editReminder(button.dataset.id);
  if (action === "toggle-reminder") await toggleReminder(button.dataset.id, button);
  if (action === "delete-reminder") confirmAction("删除提醒", "删除后不再发送这条提醒，已收到的通知会保留。", "删除提醒", async () => { await request(`/reminders/${encodeURIComponent(button.dataset.id)}`, { method: "DELETE" }); await switchView("notifications"); toast("提醒已删除"); });
  if (action === "read-all") {
    const results = await Promise.allSettled(state.notifications.filter((item) => !item.read).map((item) => request(`/notifications/${encodeURIComponent(item.id)}`, { method: "PATCH", body: { read: true } })));
    if (results.some((result) => result.status === "rejected")) toast("部分通知未能更新，请重试。", true);
    await refreshNotifications();
  }
  if (action === "notification-session") { await markRead(button.dataset.id); await loadSession(button.dataset.session); }
  if (action === "pair-chiliarch") await pairChiliarch(button);
  if (action === "service") await serviceAction(button.dataset.command);
  if (action === "refresh-service") await refreshService();
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button || button.disabled) return;
  try {
    if (button.dataset.action) await handleAction(button.dataset.action, button);
    else if (button.dataset.view) await switchView(button.dataset.view);
    else if (button.dataset.session) await loadSession(button.dataset.session);
    else if (button.dataset.prompt) { $("#message-input").value = button.dataset.prompt; resizeComposer(); $("#message-input").focus(); }
    else if (button.dataset.settingsTab) { collectSettingsForm(); state.settingsTab = button.dataset.settingsTab; renderOtherView(); }
    else if (button.dataset.modelRole) { collectSettingsForm(); state.modelRole = button.dataset.modelRole; renderOtherView(); }
    else if (button.dataset.memoryTab) { state.memoryTab = button.dataset.memoryTab; renderOtherView(); }
    else if (button.dataset.notificationTab) { state.notificationTab = button.dataset.notificationTab; renderOtherView(); }
  } catch (error) { showError(error); }
});

$("#composer").addEventListener("submit", sendMessage);
$("#message-input").addEventListener("input", resizeComposer);
$("#message-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); if (!busy()) $("#composer").requestSubmit(); }
});
$("#mode-select").addEventListener("change", async (event) => {
  const previous = state.mode; state.mode = event.target.value;
  try { if (state.activeSession) { await request(`/sessions/${encodeURIComponent(state.activeSession.id)}`, { method: "PATCH", body: { mode: state.mode } }); state.activeSession.mode = state.mode; } }
  catch (error) { state.mode = previous; showError(error); }
  renderComposer(); renderTimeline();
});
$("#session-search").addEventListener("input", renderSessions);
$("#sidebar-backdrop").addEventListener("click", closeSidebar);
$("#conversation-scroll").addEventListener("scroll", () => { $("#scroll-bottom").hidden = scrollIsNearBottom() || !state.session.messages.length; }, { passive: true });
$("#dialog-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = $("#dialog-submit"); submit.disabled = true; $("#dialog-error").hidden = true;
  try { await dialogSubmit?.(event.target); $("#app-dialog").close(); }
  catch (error) { $("#dialog-error").textContent = error.message ?? String(error); $("#dialog-error").hidden = false; }
  finally { submit.disabled = false; }
});
document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); if (!$("#app-dialog").open) newSession(); }
  if (event.key === "Escape") closeSidebar();
  if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key) && event.target.getAttribute("role") === "tab") {
    const tabs = $$('[role="tab"]', event.target.closest('[role="tablist"]'));
    const index = tabs.indexOf(event.target);
    const nextIndex = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    event.preventDefault(); tabs[nextIndex]?.click();
    requestAnimationFrame(() => $('#other-view [role="tab"][aria-selected="true"]')?.focus());
  }
});
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && !state.connected) void connect(); });
window.addEventListener("online", () => void connect());
window.addEventListener("pagehide", () => { globalController?.abort(); sessionController?.abort(); });
window.matchMedia("(max-width: 760px)").addEventListener("change", syncSidebarAccessibility);

fillIcons();
syncSidebarAccessibility();
void connect();
