import { request as apiRequest, readEvents, uploadFile, transcribeAttachment } from "./api.mjs";
import { MODES, createSessionState, applyEvent, hydrateSession, conversationItems, isRunning, toolStatusLabel, formatTimestamp, parseJSONField, toDateTimeLocal, fromDateTimeLocal, formatFullDate, messagePresentation } from "./state.mjs";
import { renderMarkdown } from "./markdown.mjs";
import { setPath, settingsPatch, changesListener, settingsExtras, mergeSettingsExtras, selectProactiveDestination, selectNotificationPreference, selectPreferenceDestination, preferenceStatusLabel, MODEL_ROLES, SETTINGS_GROUPS } from "./settings.mjs";
import { CHANNELS, loadSettingsResources, capabilityLabel, managementLabel, managementActionLabel, managementPlanLabel, updateLabel, retryChannelDelivery } from "./management.mjs";
import { initializeTheme, toggleTheme } from "./theme.mjs";
import { attachmentKind, attachmentURL, formatBytes } from "./attachments.mjs";
import { captureTeamView, createTeamPager, restoreTeamView } from "./team.mjs";
import { createTaskControl, taskControlView } from "./task-control.mjs";
import { VoiceInput, mergeTranscript } from "./voice-input.mjs";
import { VoiceSetup, voiceSetupLabel } from "./voice-setup.mjs";
import { initializeConversationWidth } from "./conversation-layout.mjs";

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
  moon: '<path d="M20 14a8 8 0 0 1-10-10 8 8 0 1 0 10 10Z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
  attachment: '<path d="m8 13 7-7a3 3 0 0 1 4 4l-9 9a5 5 0 0 1-7-7L13 2m-7 12 7-7a1.5 1.5 0 0 1 2 2l-7 7"/>',
  microphone: '<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8"/>',
  "arrow-left": '<path d="M20 12H4m7-7-7 7 7 7"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  team: '<circle cx="9" cy="8" r="3"/><path d="M2 21v-3a7 7 0 0 1 14 0v3M16 5a3 3 0 0 1 0 6m3 4a5 5 0 0 1 3 4v2"/>',
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = {
  view: "chat", status: null, connected: false, streamConnected: true, loading: true,
  sessions: [], activeSession: null, session: createSessionState(), mode: "auto", sending: false,
  sessionsError: "", workflows: [], memories: [], notifications: [], reminders: [], notificationTab: "inbox", remindersError: "", memoryGraph: { nodes: [], edges: [] }, memoryTab: "list", graphEntity: "",
  viewError: "", viewLoading: false, memoryQuery: "", settings: null, settingsTab: "overview",
  modelRole: "main", settingsDraft: null, service: null, settingsSaveStatus: "",
  management: null, channels: null, voice: null, capture: null, settingsStatusErrors: {},
  mainSessionId: null, uploads: [], attachments: [], uploading: 0, voiceDraft: null,
  team: { members: [], messages: [] }, recordQuery: "", approvals: [], approvalsError: "",
  auth: { required: false, authenticated: false },
};
let sessionController = null;
let globalController = null;
let connection = null;
let sessionRequestVersion = 0;
let viewRequestVersion = 0;
let extensionReload = null;
let queuedRender = false;
let messageNodes = new Map();
let dialogSubmit = null;
let dialogTeamView = null;
let dialogMemoryReturn = null;
let taskDialog = null;
let memoryDialog = null;
let memoryPath = null;
let memoryPathSequence = 0;
let memorySearchController = null;
let memorySources = null;
let memorySourceRetry = null;
const memorySourcePageSize = 20;
const memoryJobStates = { queued: "已排队", processing: "整理中", retry: "等待自动重试", failed: "整理失败", done: "整理完成", deferred: "等待接纳" };
let historyDialog = null;
let dialogHistoryReturn = null;
const historyPageSize = 50;
let workflowActivity = null;
const workflowStopping = new Set();
const workflowPageSize = 50;
let recordDirectory = null;
const recordPageSize = 50;
const recordQueryLimit = 120;
const taskControlModels = new Map();
const taskControlStoragePrefix = "muli.task-control:";
const teamPager = createTeamPager(request);
let draftRevision = 0;
let conversationLayout = null;
let conversationFollowing = true;
let conversationFollowFrame = null;
let conversationResizeObserver = null;
let conversationScrollIntent = null;
let conversationTouchY = null;
const voiceSetup = new VoiceSetup({
  request: (...args) => ownedRequest(...args),
  active: () => state.connected && state.view === "settings" && state.settingsTab === "inputs",
  onChange: () => renderVoiceSetupControls(),
});
state.voiceSetupController = voiceSetup;
const voiceInput = new VoiceInput({
  upload: uploadFile, transcribe: transcribeAttachment,
  onUploaded: (file) => { state.uploads = [file, ...state.uploads.filter((entry) => entry.id !== file.id)]; },
  onChange: () => renderVoiceInput(),
  onError: (error) => { if (error.status === 401) showLogin(); showError(error); },
  onDraft: (draft) => {
    const input = $("#message-input");
    const merged = mergeTranscript(draft.context, captureDraft(), draft.text);
    if (merged !== null && merged.length <= (state.status?.web_voice?.max_text_chars ?? 64000)) {
      input.value = merged; draftRevision += 1; resizeComposer(); input.focus();
      toast("转写已加入草稿，检查后点击发送。");
    } else {
      state.voiceDraft = draft;
      $("#voice-draft-text").value = draft.text;
      renderVoiceInput();
      toast("转写已保留，当前输入内容没有改动。");
    }
  },
});

function captureDraft() {
  return { session: state.activeSession?.id, revision: draftRevision, text: $("#message-input").value };
}

function renderVoiceInput() {
  const recording = voiceInput.status();
  const policy = state.status?.web_voice;
  const busyVoice = recording.phase !== "idle";
  const button = $("#voice-button");
  const reason = voiceInput.support();
  button.disabled = !state.connected || !policy?.available || !!reason || busyVoice || recording.pendingPermission || !!state.voiceDraft;
  button.title = !policy?.enabled ? "在设置中开启网页语音输入" : !policy?.configured ? "请先配置 ASR 模型服务" : reason || (state.voiceDraft ? "请先处理已有转写草稿" : recording.pendingPermission ? "等待浏览器权限提示结束" : "录音并转写为草稿");
  button.setAttribute("aria-label", button.title);
  $("#voice-progress").hidden = !busyVoice;
  const phases = { permission: "等待麦克风权限…", recording: `正在录音，最长 ${policy?.max_seconds ?? ""} 秒`, stopping: "正在结束录音…", uploading: "正在保存录音…", transcribing: "正在转写，不会自动发送…" };
  $("#voice-status").textContent = phases[recording.phase] ?? "";
  $("#voice-stop").hidden = recording.phase !== "recording";
  $("#voice-draft").hidden = !state.voiceDraft;
}

async function request(path, options) {
  try { return await apiRequest(path, options); }
  catch (error) { if (error.status === 401 && path !== "/auth/login") showLogin(); throw error; }
}

/** One active read and one shared next-read promise; events never add waiters. */
function createSummaryLane({ read, publish, failed = () => {}, current = () => true }) {
  let active = null, queued = null, dirty = false, enabled = false, disposed = false, error = null;
  const pending = () => {
    let resolve;
    const promise = new Promise((finish) => { resolve = finish; });
    return { promise, resolve };
  };
  const pump = () => {
    if (disposed || !enabled || active || !dirty || !current()) return;
    dirty = false;
    const pass = active = queued ?? pending(); queued = null;
    void (async () => {
      let fresh = false;
      try {
        const value = await read();
        if (disposed || !current()) return;
        publish(value); error = null; fresh = true;
      } catch (reason) {
        if (!disposed && current()) { error = reason; failed(reason); }
      } finally {
        pass.resolve(fresh);
        if (active !== pass) return;
        active = null;
        if (!fresh) {
          queued?.resolve(false); queued = null;
          // A failed read stops this attempt. A later real trigger may retry.
          return;
        }
        pump();
      }
    })();
  };
  return {
    get error() { return error; },
    request() {
      if (disposed || !current()) return Promise.resolve(false);
      dirty = true;
      const pass = queued ??= pending();
      pump();
      return pass.promise;
    },
    start() { enabled = true; pump(); },
    invalidate() {
      disposed = true; dirty = false;
      active?.resolve(false); queued?.resolve(false); active = null; queued = null;
    },
  };
}

function connectionCurrent(owner) {
  return !!owner && connection === owner && !owner.controller.signal.aborted;
}

function invalidateConnection() {
  state.voiceSetupController?.release();
  const owner = connection; connection = null;
  owner?.sessions.invalidate(); owner?.notifications.invalidate(); owner?.approvals.invalidate(); owner?.controller.abort();
  globalController?.abort(); sessionController?.abort();
  teamPager.invalidate?.();
  state.connected = false; state.streamConnected = false;
  state.approvals = []; state.approvalsError = "";
  $("#approval-region")?.replaceChildren();
  if (state.view === "notifications" && state.notificationTab === "approvals") $("#other-view")?.replaceChildren();
}

function beginConnection() {
  invalidateConnection();
  const owner = { controller: new AbortController(), ready: false, position: "0", approvalDecisions: new Set(), approvalEpoch: null };
  connection = owner;
  owner.sessions = createSummaryLane({
    current: () => connectionCurrent(owner), read: () => ownedRequest("/sessions", {}, owner),
    publish: (payload) => {
      state.sessions = collection(payload); state.sessionsError = "";
      const active = state.sessions.find((session) => session.id === state.activeSession?.id);
      if (active) state.activeSession = active;
      renderSessions(); renderChrome();
    },
    failed: (error) => { state.sessionsError = error.message; renderSessions(); renderChrome(); },
  });
  owner.notifications = createSummaryLane({
    current: () => connectionCurrent(owner), read: () => ownedRequest("/notifications", {}, owner),
    publish: (payload) => {
      state.notifications = collection(payload); renderChrome();
      if (state.view === "notifications" && state.notificationTab !== "approvals") renderOtherView();
    },
  });
  owner.approvals = createSummaryLane({
    current: () => connectionCurrent(owner),
    read: async () => {
      const epoch = owner.approvalEpoch;
      try { return { epoch, payload: await ownedRequest("/approvals", { cache: "no-store" }, owner) }; }
      catch (error) { return { epoch, error }; }
    },
    publish: ({ epoch, payload, error }) => {
      if (epoch !== owner.approvalEpoch) return;
      if (error) throw error;
      const items = payload?.items;
      if (!Array.isArray(items) || items.some((item) => !item || typeof item.id !== "string" || !item.id || typeof item.session_id !== "string" || !item.session_id || typeof item.run_id !== "string" || !item.run_id || typeof item.tool !== "string" || typeof item.reason !== "string" || item.status !== "pending" || !item.arguments || typeof item.arguments !== "object" || Array.isArray(item.arguments)) || new Set(items.map((item) => item.id)).size !== items.length) throw new Error("审批目录无效，请刷新。");
      state.approvals = items; state.approvalsError = ""; renderApprovalViews();
    },
    failed: (error) => { state.approvals = []; state.approvalsError = error.message; renderApprovalViews(); },
  });
  return owner;
}

/** Raw API calls keep a previous connection's 401 out of the login handler. */
async function ownedRequest(path, options = {}, owner = connection) {
  const cancelled = () => Object.assign(new Error("读取已失效"), { name: "AbortError" });
  if (!connectionCurrent(owner)) throw cancelled();
  const controller = new AbortController();
  const signals = [owner.controller.signal, options.signal].filter(Boolean);
  const abort = () => controller.abort();
  for (const signal of signals) {
    if (signal.aborted) abort();
    else signal.addEventListener("abort", abort, { once: true });
  }
  try {
    const result = await apiRequest(path, { ...options, signal: controller.signal });
    if (!connectionCurrent(owner) || controller.signal.aborted) throw cancelled();
    return result;
  } catch (error) {
    if (!connectionCurrent(owner) || controller.signal.aborted) throw cancelled();
    if (error.status === 401) showLogin();
    throw error;
  } finally {
    for (const signal of signals) signal.removeEventListener("abort", abort);
  }
}

function showLogin() {
  invalidateConnection();
  voiceInput.cancel();
  clearTaskControls();
  releaseMemoryDialog();
  releaseHistoryDialog();
  cancelMemorySearch(); releaseMemoryPath();
  releaseMemorySources();
  releaseWorkflowActivity();
  releaseRecordDirectory(); state.recordQuery = "";
  dialogTeamView = null; dialogMemoryReturn = null; dialogHistoryReturn = null;
  $("#app-dialog").close(); $("#dialog-content").replaceChildren(); dialogSubmit = null;
  globalController?.abort(); sessionController?.abort();
  state.connected = false; state.loading = false; state.auth.authenticated = false;
  $(".app-shell").inert = true; $("#auth-gate").hidden = false;
  $("#login-password").focus();
}

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
function currentTitle() { return "沐璃对话"; }
function busy() { return isRunning(state.session.status) || state.sending; }

function renderChrome() {
  const titles = { chat: currentTitle(), workflows: "工作流", memories: "记忆", notifications: "通知", settings: "设置", team: "团队", uploads: "附件", records: "运行记录" };
  $("#page-title").textContent = titles[state.view];
  document.title = `${titles[state.view]} · 沐璃`;
  $$(".management-nav [data-view]").forEach((node) => {
    const selected = node.dataset.view === state.view;
    node.classList.toggle("active", selected);
    node.setAttribute("aria-current", selected ? "page" : "false");
  });
  $("#model-label").textContent = state.status?.model || (state.loading ? "读取模型…" : state.connected ? "配置模型" : "未连接");
  $("#model-dot").className = `status-dot${state.connected && state.status?.configured ? " online" : state.connected ? " warning" : ""}`;
  $("#settings-attention").hidden = !state.connected || !!state.status?.configured;
  $("#connection-label").textContent = state.loading ? "连接工作台…" : state.connected ? state.streamConnected ? "工作台已连接" : "实时连接重连中" : "工作台未连接";
  $("#connection-dot").className = `status-dot${state.connected ? state.streamConnected ? " online" : " warning" : ""}`;
  const warning = !state.loading && (!state.connected || !state.streamConnected);
  $("#connection-banner").hidden = !warning;
  $("#connection-banner-text").textContent = !state.connected ? "服务暂未连接，输入内容会保留。" : "实时连接中断，正在重新连接。";
  const unread = state.notifications.filter((item) => !item.read).length;
  const needsAttention = unread + state.approvals.length;
  $("#notification-count").hidden = !needsAttention;
  $("#notification-count").textContent = String(needsAttention);
  $("#notification-count").setAttribute("aria-label", `${unread} 条未读通知，${state.approvals.length} 项待批准`);
  $("#inbox-attention").hidden = !needsAttention && !state.approvalsError;
  $("#chat-view").hidden = state.view !== "chat";
  if (state.view === "chat") conversationLayout?.refresh();
  $("#management-shell").hidden = state.view === "chat";
  $("#settings-toggle").setAttribute("aria-pressed", String(state.view !== "chat"));
}

function renderSessions() {
  if (state.view !== "records" || !state.auth.authenticated) return;
  if (!recordDirectory) void switchView("records");
  else markRecordDirectoryDirty();
}

function renderComposer() {
  const input = $("#message-input");
  const running = busy();
  $("#mode-select").value = state.mode;
  $("#mode-select").disabled = running;
  $("#send-button").disabled = (!input.value.trim() && !state.attachments.length) || state.sending || state.uploading > 0 || !state.activeSession || !state.connected;
  $("#send-button").setAttribute("aria-label", running ? "发送补充消息" : "发送消息");
  $("#stop-button").hidden = !running;
  $("#stop-button").disabled = state.sending || !state.activeSession;
  $("#composer-context").textContent = state.session.queued?.length ? `${state.session.queued.length} 条待处理` : "";
  $("#composer-context").hidden = !state.session.queued?.length;
  $("#composer-context").classList.toggle("has-queued", !!state.session.queued?.length);
  $("#composer-status").textContent = state.uploading ? `正在保存 ${state.uploading} 个附件…` : !state.connected ? state.loading ? "正在连接沐璃" : "等待服务连接" : !state.status?.configured ? "请先配置主模型" : running ? state.session.status === "approval" ? "等待批准 · 可以继续补充" : "正在处理 · 可以继续补充" : `${state.status.model} · ${MODES[state.mode]?.label ?? state.mode}`;
  renderVoiceInput();
  scheduleConversationTail();
}

function scrollToBottom(force = false, smooth = false) {
  if (!force && !conversationFollowing) return;
  conversationFollowing = true;
  conversationScrollIntent = null;
  writeConversationTail(smooth);
}

function writeConversationTail(smooth = false) {
  if (state.view !== "chat") return;
  const scroll = $("#conversation-scroll");
  if (scroll.clientHeight <= 0) return;
  scroll.scrollTo({ top: scroll.scrollHeight, behavior: smooth ? "smooth" : "instant" });
  $("#scroll-bottom").hidden = true;
}

function scheduleConversationTail() {
  if (!conversationResizeObserver && typeof ResizeObserver === "function") {
    conversationResizeObserver = new ResizeObserver(scheduleConversationTail);
    for (const selector of ["#conversation-scroll", "#messages", "#approval-region", "#run-status", ".composer-area", "#composer"]) {
      const node = $(selector);
      if (node) conversationResizeObserver.observe(node);
    }
  }
  if (state.view !== "chat") return;
  const follow = () => {
    conversationFollowFrame = null;
    if (conversationFollowing && state.view === "chat" && $("#conversation-scroll").clientHeight > 0) writeConversationTail();
  };
  if (typeof requestAnimationFrame !== "function") { follow(); return; }
  if (conversationFollowFrame === null) conversationFollowFrame = requestAnimationFrame(follow);
}

function mainScrollTarget(event, keyboard = false) {
  const scroll = $("#conversation-scroll");
  let node = event.target;
  while (node && node !== scroll) {
    if (node.matches?.('.reasoning-content, textarea, input, select, [contenteditable], [role="separator"]')) return false;
    // Space activates these controls; navigation keys may still scroll the
    // main viewport, which the following scroll event must confirm.
    if (keyboard && event.key === " " && node.matches?.("button, summary")) return false;
    node = node.parentNode;
  }
  return node === scroll;
}

function armConversationScroll(event, kind, upward) {
  if (event.isTrusted === false || !mainScrollTarget(event, kind === "key")) return;
  const scroll = $("#conversation-scroll");
  const intent = { kind, upward, top: scroll.scrollTop };
  conversationScrollIntent = intent;
  // Wheel/key default scrolling precedes the frame. A stale gesture must not
  // later turn a layout/programmatic scroll into an upward reading action.
  if (kind !== "scrollbar" && typeof requestAnimationFrame === "function") requestAnimationFrame(() => {
    if (conversationScrollIntent === intent) conversationScrollIntent = null;
  });
}

function onConversationScroll() {
  const scroll = $("#conversation-scroll"), intent = conversationScrollIntent;
  // Concurrent content growth must not erase a real upward gesture. Clamp the
  // starting position only to exclude a layout-only reduction of the range.
  const previousTop = intent && Math.min(intent.top, Math.max(0, scroll.scrollHeight - scroll.clientHeight));
  if (intent && intent.upward !== false && scroll.scrollTop < previousTop) conversationFollowing = false;
  else if (scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight <= 1) conversationFollowing = true;
  if (intent?.kind === "scrollbar") {
    intent.top = scroll.scrollTop;
  } else conversationScrollIntent = null;
  $("#scroll-bottom").hidden = conversationFollowing || !state.session.messages.length;
}

function renderChat() {
  const items = conversationItems(state.session).filter((item) => item.kind !== "message" || ["user", "assistant"].includes(item.message.role) && (item.message.content || item.message.attachments?.length || item.message.attachment_ids?.length || item.message.pending || item.message.streaming));
  $("#welcome").hidden = !!items.length || busy();
  const container = $("#messages");
  const ids = new Set(items.map((item) => String(item.id)));
  for (const [id, node] of messageNodes) if (!ids.has(id)) { node.remove(); messageNodes.delete(id); }
  for (const [position, item] of items.entries()) {
    const id = String(item.id);
    if (item.kind !== "message") {
      const previous = messageNodes.get(id);
      const contentChanged = item.kind === "reasoning" && previous?.dataset.content !== item.content;
      const node = renderProcessItem(item, previous);
      node.dataset.timelineId = id;
      messageNodes.set(id, node);
      if (container.children[position] !== node) container.insertBefore(node, container.children[position] ?? null);
      if (contentChanged && item.streaming && node.open) {
        const content = $(".reasoning-content", node);
        if (content?.dataset.follow !== "false" && content?.clientHeight > 0) content.scrollTop = content.scrollHeight;
      }
      continue;
    }
    const message = item.message;
    const presentation = messagePresentation(message);
    let node = messageNodes.get(id);
    if (!node) {
      node = element("article", { class: `message ${presentation.kind}`, dataset: { timelineId: id }, "aria-label": presentation.label });
      let content = node;
      if (presentation.kind === "system-context") {
        content = element("details", {}, element("summary", { class: "message-heading" }, icon("info"), element("span", { text: presentation.label }), element("time", { class: "message-time", text: formatTimestamp(message.created_at), dateTime: message.created_at ?? "" })));
        node.append(content);
      } else if (message.role !== "user") {
        content.append(element("div", { class: "message-heading" }, element("span", { class: "message-avatar" }, element("img", { src: "./assets/muli-avatar.webp", alt: "" })), element("span", { text: "沐璃" }), element("time", { class: "message-time", text: formatTimestamp(message.created_at), dateTime: message.created_at ?? "" })));
      }
      content.append(element("div", { class: "message-body" }));
      content.append(element("div", { class: "message-attachments" }));
      const actions = element("div", { class: "message-actions" }, iconButton("复制消息", "copy-message", "copy", { id }));
      content.append(actions); messageNodes.set(id, node);
    }
    node.dataset.messageId = String(message.id);
    if (container.children[position] !== node) container.insertBefore(node, container.children[position] ?? null);
    if (node.dataset.content !== message.content) {
      const body = $(".message-body", node);
      if (message.role === "user") body.textContent = message.content ?? "";
      else body.replaceChildren(renderMarkdown(message.content));
      node.dataset.content = message.content ?? "";
    }
    node.classList.toggle("streaming", !!message.streaming);
    const attachments = message.attachments ?? (message.attachment_ids ?? []).map((attachmentId) => state.uploads.find((file) => file.id === attachmentId) ?? { id: attachmentId, name: "附件" });
    const attachmentKey = JSON.stringify(attachments);
    if (node.dataset.attachments !== attachmentKey) {
      $(".message-attachments", node).replaceChildren(...attachments.map((file) => renderAttachment(file)));
      node.dataset.attachments = attachmentKey;
    }
    let interruption = $(".interrupted-label", node);
    if (message.interrupted && !interruption) { interruption = element("div", { class: "interrupted-label", text: message.paused ? "生成已暂停" : "生成已停止" }); node.append(interruption); }
  }
  renderApprovals();
  const runStatus = $("#run-status");
  runStatus.hidden = !busy() && !state.session.error;
  runStatus.classList.toggle("error", !!state.session.error);
  if (state.session.error) runStatus.replaceChildren(icon("info"), element("span", { text: state.session.error }));
  else if (busy()) runStatus.replaceChildren(element("span", { class: "thinking-dots", "aria-hidden": "true" }, element("i"), element("i"), element("i")), element("span", { text: state.session.status === "approval" ? "等待审批" : state.sending ? "正在发送…" : state.session.tools.some((tool) => tool.status === "running") ? "正在使用工具…" : "正在处理…" }));
  renderComposer();
  scheduleConversationTail();
}

function renderProcessItem(item, node) {
  if (item.kind === "reasoning") {
    if (!node) {
      const content = element("div", { class: "reasoning-content", role: "region", "aria-label": "思考内容", tabIndex: 0 });
      content.dataset.follow = "true";
      content.addEventListener("scroll", () => { content.dataset.follow = String(content.scrollHeight - content.scrollTop - content.clientHeight <= 24); });
      node = element("details", { class: "reasoning-step", open: true }, element("summary", {}, icon("sparkles"), element("span", { text: "思考过程" })), content);
    }
    const content = $(".reasoning-content", node);
    if (node.dataset.content !== item.content) {
      content.textContent = item.content;
      node.dataset.content = item.content;
    }
    node.classList.toggle("streaming", !!item.streaming);
    return node;
  }
  const tool = item.tool;
  if (!node) {
    node = element("details", { class: "tool-step" },
      element("summary", {}, icon("terminal"), element("code", { class: "tool-name" }), element("span", { class: "tool-step-status" }), element("time", { class: "tool-time" })),
      element("div", { class: "tool-section" }, element("div", { class: "tool-step-label", text: "参数" }), element("pre", { class: "tool-arguments" })),
      element("div", { class: "tool-section tool-result-section" }, element("div", { class: "tool-step-label", text: "结果" }), element("pre", { class: "tool-result" })));
  }
  node.className = "tool-step " + tool.status;
  $(".tool-name", node).textContent = tool.name || "工具调用";
  $(".tool-step-status", node).textContent = toolStatusLabel(tool);
  const argumentsText = pretty(tool.arguments ?? "未记录参数"), resultText = pretty(tool.result ?? tool.error ?? "");
  if (node.dataset.arguments !== argumentsText) { $(".tool-arguments", node).textContent = argumentsText; node.dataset.arguments = argumentsText; }
  if (node.dataset.result !== resultText) { $(".tool-result", node).textContent = resultText; node.dataset.result = resultText; }
  $(".tool-result-section", node).hidden = tool.result === undefined && !tool.error;
  $(".tool-time", node).textContent = formatTimestamp(tool.created_at ?? tool.completed_at);
  return node;
}

function approvalCard(approval) {
  const session = state.sessions.find((item) => item.id === approval.session_id);
  return element("section", { class: "approval-item", dataset: { id: approval.id }, "aria-label": "工具审批" },
    element("div", { class: "approval-heading" }, icon("shield"), "需要你的批准"),
    element("h3", { text: approval.tool }),
    element("div", { class: "row-meta" }, session?.title && element("span", { text: session.title }), element("span", { text: `会话 ${approval.session_id}` }), element("span", { text: `运行 ${approval.run_id}` })),
    approval.reason && element("p", { text: approval.reason }),
    element("pre", { text: pretty(approval.arguments) }),
    element("div", { class: "approval-actions" }, actionButton("拒绝", "approval", "secondary-button", undefined, { id: approval.id, decision: "reject" }), actionButton("批准执行", "approval", "primary-button", "check", { id: approval.id, decision: "approve" })));
}

function renderApprovalViews() {
  const focused = document.activeElement?.closest?.(".approval-item");
  const focus = focused ? { id: focused.dataset.id, decision: document.activeElement.dataset.decision } : null;
  renderApprovals(); renderChrome();
  if (state.view === "notifications" && state.notificationTab === "approvals") renderOtherView();
  if (focus) {
    const root = state.view === "chat" ? $("#approval-region") : $("#other-view");
    const buttons = $$('[data-action="approval"]', root).filter((button) => !button.disabled);
    const target = buttons.find((button) => button.dataset.id === focus.id && button.dataset.decision === focus.decision) ?? buttons[0] ?? (state.view === "chat" ? $("#message-input") : $('[data-notification-tab="approvals"]', root));
    target?.focus({ preventScroll: true });
  }
}

function renderApprovals() {
  const region = $("#approval-region");
  if (state.approvalsError) {
    region.replaceChildren(element("div", { class: "approval-error", role: "status" }, element("p", { text: state.approvalsError }), actionButton("刷新待批准", "refresh-approvals", "text-button", "refresh"))); scheduleConversationTail(); return;
  }
  $(".approval-error", region)?.remove();
  const existing = new Map($$(".approval-item", region).map((node) => [node.dataset.id, node]));
  const ids = new Set(state.approvals.map((item) => item.id));
  for (const [id, node] of existing) if (!ids.has(id)) node.remove();
  for (const approval of state.approvals) {
    const node = existing.get(approval.id) ?? approvalCard(approval);
    if (!existing.has(approval.id)) region.append(node);
    $$("button", node).forEach((button) => { button.disabled = !!connection?.approvalDecisions.has(approval.id); });
  }
  scheduleConversationTail();
}

function queueRender() {
  if (queuedRender) return;
  queuedRender = true;
  requestAnimationFrame(() => { queuedRender = false; renderChrome(); if (state.view === "chat") renderChat(); });
}

function refreshSessions(owner = connection) {
  return owner?.sessions.request() ?? Promise.resolve(false);
}

function refreshNotifications(owner = connection) {
  return owner?.notifications.request() ?? Promise.resolve(false);
}

function refreshApprovals(owner = connection) {
  if (owner) owner.approvalEpoch = Symbol();
  return owner?.approvals.request() ?? Promise.resolve(false);
}

async function connect() {
  const owner = beginConnection();
  state.loading = true; $(".app-shell").inert = true; renderChrome();
  try {
    const auth = await ownedRequest("/auth/status", {}, owner);
    if (!connectionCurrent(owner)) return;
    state.auth = auth;
    if (state.auth.required && !state.auth.authenticated) { state.loading = false; showLogin(); return; }
    const position = await ownedRequest("/ui/event-position", { cache: "no-store" }, owner);
    if (!connectionCurrent(owner)) return;
    if (typeof position?.after !== "string" || !/^(0|[1-9][0-9]*)$/.test(position.after) || BigInt(position.after) > 9223372036854775807n) throw new Error("事件位置无效，请重新连接。");
    owner.position = position.after; owner.ready = true;
    $("#auth-gate").hidden = true; $(".app-shell").inert = false;
  } catch (error) {
    if (!connectionCurrent(owner)) return;
    invalidateConnection();
    state.connected = false; state.loading = false; renderChrome(); renderComposer();
    return;
  }
  const notifications = refreshNotifications(owner), sessions = refreshSessions(owner), approvals = refreshApprovals(owner);
  owner.notifications.start(); owner.sessions.start(); owner.approvals.start();
  const results = await Promise.allSettled([ownedRequest("/status", {}, owner), ownedRequest("/conversation", {}, owner), notifications, sessions, ownedRequest("/uploads", {}, owner), approvals]);
  if (!connectionCurrent(owner)) return;
  if (results[0].status === "fulfilled") {
    state.status = results[0].value; state.connected = true;
    if (!state.activeSession) state.mode = state.status.mode || "auto";
  } else { state.connected = false; }
  if (results[1].status === "fulfilled") {
    const detail = results[1].value;
    try {
      const session = hydrateSession(detail);
      state.activeSession = detail.session; state.mainSessionId = detail.session.id;
      state.session = session; state.mode = detail.session.mode || state.mode;
      state.sending = false; resetMessageNodes();
      startSessionEvents(detail.session.id, owner);
    } catch { state.connected = false; }
  } else { state.connected = false; }
  if (results[4].status === "fulfilled") state.uploads = collection(results[4].value);
  state.loading = false;
  renderChrome(); renderSessions(); renderChat();
  if (state.connected) { startGlobalEvents(owner); revalidateVisibleState(owner); }
  else invalidateConnection();
}

function revalidateVisibleState(owner) {
  if (!connectionCurrent(owner)) return;
  // These projections used to receive missed changes from the global replay.
  if (["workflows", "notifications", "team", "memories", "uploads"].includes(state.view)) void switchView(state.view, owner);
  if (state.view === "records") renderSessions();
  if (taskDialog) void refreshTaskBriefDialog(taskDialog, owner);
  invalidateMemoryPath();
  if (memoryDialog) invalidateMemoryEvidence({ type: "memory.updated", data: { memory_ids: [memoryDialog.id] } });
}

function waitToReconnect(milliseconds, signal) {
  return new Promise((resolve) => {
    if (signal.aborted) { resolve(); return; }
    const finish = () => { clearTimeout(timer); signal.removeEventListener("abort", finish); resolve(); };
    const timer = setTimeout(finish, milliseconds);
    signal.addEventListener("abort", finish, { once: true });
  });
}

async function subscribe(path, controller, { cursor = "", onEvent, onReconnect = () => {}, reportConnection = false, owner = connection } = {}) {
  let delay = 1000;
  let after = cursor;
  let opened = false;
  const current = () => connectionCurrent(owner) && !controller.signal.aborted;
  while (current()) {
    try {
      await readEvents(path, {
        signal: controller.signal, after,
        onOpen: () => { if (!current()) return; delay = 1000; if (opened) onReconnect(); opened = true; if (reportConnection) { state.streamConnected = true; renderChrome(); } },
        onEvent: (event) => { if (!current()) return; onEvent(event); if (event.id !== undefined && event.id !== null) after = String(event.id); },
        onId: (id) => { if (current()) after = id; },
      });
    } catch (error) {
      if (error.name === "AbortError" || !current()) return;
      if (error.status === 401) { showLogin(); return; }
    }
    if (!current()) return;
    if (reportConnection) { state.streamConnected = false; renderChrome(); }
    await waitToReconnect(delay, controller.signal);
    delay = Math.min(delay * 2, 12000);
  }
}

function startGlobalEvents(owner = connection) {
  if (!connectionCurrent(owner) || !owner.ready) return;
  globalController?.abort();
  globalController = new AbortController();
  void subscribe("/events", globalController, { owner, cursor: owner.position, reportConnection: true, onReconnect: () => { void refreshApprovals(owner); }, onEvent: (event) => {
    if (event.type === "approval.resolved" && typeof event.data?.id === "string") {
      state.approvals = state.approvals.filter((item) => item.id !== event.data.id); renderApprovalViews();
    }
    if (event.type.startsWith("approval.") || ["run.completed", "run.failed", "run.cancelled", "session.recovered"].includes(event.type)) void refreshApprovals(owner);
    if (["notification.created", "notification.updated", "run.completed", "run.failed", "approval.requested", "attention.queued", "ingress.cancelled"].includes(event.type)) void refreshNotifications(owner);
    if (event.type.startsWith("run.") || event.type.startsWith("session.")) void refreshSessions(owner);
    if (event.type === "workflow.updated") {
      const workflow = state.workflows.find((item) => item.id === event.data?.workflow_id);
      if (workflow && event.data.run) {
        const runs = workflow.runs ?? [];
        workflow.runs = [event.data.run, ...runs.filter((run) => run.id !== event.data.run.id)];
      }
      refreshWorkflowActivity(owner);
      void refreshSessions(owner);
    }
    if (event.type === "reminder.updated") {
      const item = event.data;
      if (item?.id) state.reminders = [item, ...state.reminders.filter((entry) => entry.id !== item.id)];
      if (state.view === "notifications" && state.notificationTab === "reminders") renderOtherView();
    }
    if (event.type.startsWith("team.") && state.view === "team") void refreshTeam({}, owner);
    if (event.type === "commitment.updated" && taskDialog && event.data?.id === taskDialog.model.state.id) void refreshTaskBriefDialog(taskDialog, owner);
    if (["memory.updated", "memory.forgotten"].includes(event.type)) { invalidateMemoryPath(); invalidateMemoryEvidence(event); }
    if (event.type === "upload.created" && state.view === "uploads") void refreshUploads(owner);
  } });
}

function startSessionEvents(sessionId, owner = connection) {
  if (!connectionCurrent(owner)) return;
  sessionController?.abort();
  sessionController = new AbortController();
  void subscribe(`/sessions/${encodeURIComponent(sessionId)}/events`, sessionController, {
    cursor: state.session.cursor, owner,
    onEvent: (event) => {
      if (event.session_id && event.session_id !== state.activeSession?.id) return;
      const next = applyEvent(state.session, event);
      if (next === state.session) return;
      state.session = next;
      if (["run.completed", "run.failed", "run.cancelled", "run.paused", "session.recovered"].includes(event.type)) { state.sending = false; void refreshSessions(owner); }
      queueRender();
    },
  });
}

function resetMessageNodes() { $("#messages").replaceChildren(); messageNodes = new Map(); conversationFollowing = true; conversationScrollIntent = null; conversationTouchY = null; }

async function loadSession(id, trigger = null) {
  if (id === state.mainSessionId) { await switchView("chat"); return; }
  const slot = openHistoryDialog(id, "运行记录", trigger, "detail");
  await loadSessionRecord(slot);
}

function releaseHistoryDialog() {
  const slot = historyDialog; historyDialog = null;
  if (!slot) return;
  slot.controller?.abort(); slot.controller = null;
  slot.page = null; slot.bodyPage = null; slot.selected = null;
  slot.starts = []; slot.offsets = []; slot.content.replaceChildren();
}

function historyDialogCurrent(slot) {
  return historyDialog === slot && state.auth.authenticated && $("#app-dialog").open && $("#dialog-content").firstElementChild === slot.content;
}

function openHistoryDialog(sessionId, title, trigger, mode) {
  const content = element("div", { class: "history-archive" });
  openDialog({ title, content, readOnly: true });
  $("#dialog-content").scrollTop = 0;
  const slot = { sessionId: String(sessionId), title, content, trigger, mode, page: null, bodyPage: null, selected: null, controller: null, through: null, before: null, starts: [null], offsets: [0], segment: 0, error: "", retry: null, focusAfterLoad: false, returnMessageId: null };
  historyDialog = slot;
  dialogHistoryReturn = { trigger, sessionId: slot.sessionId, view: state.view, action: trigger?.dataset.action };
  const heading = $("#dialog-title"); heading.tabIndex = -1; heading.focus({ preventScroll: true });
  return slot;
}

async function loadSessionRecord(slot) {
  if (!historyDialogCurrent(slot)) return;
  slot.controller?.abort(); const controller = new AbortController(); slot.controller = controller;
  slot.content.replaceChildren(element("p", { role: "status", text: "正在读取记录…" }));
  try {
    const detail = await request(`/sessions/${encodeURIComponent(slot.sessionId)}`, { signal: controller.signal, cache: "no-store" });
    if (!historyDialogCurrent(slot) || slot.controller !== controller) return;
    if (detail?.session?.id !== slot.sessionId || !Array.isArray(detail.messages)) throw new Error("记录响应不一致，请重新读取。");
    slot.title = detail.session.title || "运行记录";
    $("#dialog-title").textContent = slot.title;
    const content = element("div", { class: "session-record" });
    content.append(element("div", { class: "history-record-actions" }, element("button", { type: "button", class: "text-button", text: "查看正文归档", onClick: () => void showHistoryArchive(slot.sessionId, { trigger: slot.trigger, title: slot.title, fromRecord: true }) })));
    for (const message of detail.messages) {
      if (!["user", "assistant"].includes(message.role)) continue;
      content.append(element("article", { class: "record-message" }, element("strong", { text: messagePresentation(message).kind === "system-context" ? "系统资料" : message.role === "user" ? "任务" : "沐璃" }), element("div", { class: "message-body" }, renderMarkdown(message.content ?? "")), element("div", { class: "message-attachments" }, (message.attachments ?? []).map((file) => renderAttachment(file)))));
    }
    if (content.children.length === 1) content.append(emptyState("chat", "还没有消息"));
    slot.content.replaceChildren(content);
  } catch (error) {
    if (!historyDialogCurrent(slot) || slot.controller !== controller) return;
    slot.content.replaceChildren(element("p", { class: "form-error", text: error.message }), element("button", { type: "button", class: "text-button", text: "重试", onClick: () => void loadSessionRecord(slot) }));
  } finally { if (slot.controller === controller) slot.controller = null; }
}

function historyRole(message) {
  if (message.principal === "system:attention") return "系统资料";
  const roles = { user: "用户记录", assistant: "助手回复", tool: "工具输出", system: "系统记录", developer: "开发者记录" };
  return Object.hasOwn(roles, message.role) ? roles[message.role] : "未知角色";
}

function historyProvenance(message) {
  return { runtime_excerpt: "保存时已节选", unknown: "完整性未标记" }[message.content_provenance] ?? "";
}

function historyMetadataValid(message) {
  return message && Number.isSafeInteger(message.seq) && message.seq > 0 && typeof message.revision === "string" && !!message.revision && typeof message.role === "string"
    && ["accepted", "runtime_excerpt", "unknown"].includes(message.content_provenance)
    && ["source", "principal"].every((key) => message[key] === undefined || typeof message[key] === "string")
    && (message.metadata_omitted === undefined || Array.isArray(message.metadata_omitted) && message.metadata_omitted.every((key) => typeof key === "string"));
}

function historyDirectory(payload, sessionId, before, through) {
  const invalid = () => { throw new Error("归档响应不完整或序号超出可用范围，请重新读取。"); };
  if (!payload || payload.session_id !== sessionId || !Array.isArray(payload.messages) || payload.messages.length > historyPageSize || !Number.isSafeInteger(payload.through_seq) || payload.through_seq < 0 || typeof payload.has_older !== "boolean" || through !== null && payload.through_seq !== through) invalid();
  const ids = new Set(); let last = 0;
  for (const message of payload.messages) {
    if (!historyMetadataValid(message) || typeof message.id !== "string" || !message.id || ids.has(message.id) || message.seq <= last || message.seq > payload.through_seq || before !== null && message.seq >= before || typeof message.content_truncated !== "boolean") invalid();
    ids.add(message.id); last = message.seq;
    if (typeof message.content === "string") {
      const bytes = new TextEncoder().encode(message.content).length;
      if (!Number.isSafeInteger(message.content_bytes) || message.content_bytes < bytes || message.content_truncated !== (bytes < message.content_bytes)) invalid();
    } else if (message.content !== null || message.content_bytes !== null || message.content_unavailable !== true || message.content_truncated) invalid();
  }
  if (payload.has_older ? !payload.messages.length || !Number.isSafeInteger(payload.next_before_seq) || payload.next_before_seq !== payload.messages[0].seq : payload.next_before_seq !== null) invalid();
  return payload;
}

async function showHistoryArchive(sessionId, { trigger = null, title = "已保存记录", fromRecord = false } = {}) {
  if (!sessionId) return;
  const slot = openHistoryDialog(sessionId, "正文归档", trigger, "list");
  slot.recordTitle = title; slot.fromRecord = fromRecord;
  await loadHistoryPage(slot);
}

async function loadHistoryPage(slot, before = null, starts = [null], returnMessageId = null) {
  if (!historyDialogCurrent(slot)) return;
  slot.focusAfterLoad = slot.content.contains(document.activeElement);
  slot.controller?.abort(); const controller = new AbortController(); slot.controller = controller;
  slot.mode = "list"; slot.page = null; slot.bodyPage = null; slot.selected = null;
  slot.error = ""; slot.retry = () => void loadHistoryPage(slot, before, starts, returnMessageId);
  renderHistoryDialog(slot);
  try {
    const query = new URLSearchParams({ limit: String(historyPageSize) });
    if (before !== null) query.set("before_seq", String(before));
    if (slot.through !== null) query.set("through_seq", String(slot.through));
    const payload = await request(`/sessions/${encodeURIComponent(slot.sessionId)}/messages?${query}`, { signal: controller.signal, cache: "no-store" });
    if (!historyDialogCurrent(slot) || slot.controller !== controller) return;
    slot.page = historyDirectory(payload, slot.sessionId, before, slot.through);
    slot.through = payload.through_seq; slot.before = before; slot.starts = starts; slot.returnMessageId = returnMessageId;
  } catch (error) {
    if (!historyDialogCurrent(slot) || slot.controller !== controller) return;
    slot.error = error.status === 404 ? "记录已不可用。" : error.message;
    if (error.status === 404) slot.retry = null;
  } finally {
    if (historyDialogCurrent(slot) && slot.controller === controller) { slot.controller = null; renderHistoryDialog(slot); }
  }
}

function historyContentValid(page, slot, offset) {
  return historyMetadataValid(page) && page.session_id === slot.sessionId && page.message_id === slot.selected.id && page.seq === slot.selected.seq && page.revision === slot.selected.revision && page.field === "content" && typeof page.content === "string"
    && [page.offset_bytes, page.length_bytes, page.next_offset_bytes, page.total_bytes].every((value) => Number.isSafeInteger(value) && value >= 0)
    && page.total_bytes === slot.selected.content_bytes && page.offset_bytes === offset && page.length_bytes === new TextEncoder().encode(page.content).length && page.next_offset_bytes === offset + page.length_bytes
    && typeof page.has_more === "boolean" && page.has_more === (page.next_offset_bytes < page.total_bytes) && page.next_offset_bytes <= page.total_bytes && (!page.has_more || page.length_bytes > 0);
}

async function selectHistoryMessage(slot, message) {
  if (!historyDialogCurrent(slot)) return;
  const { content, ...selected } = message;
  slot.selected = selected; slot.page = null; slot.offsets = [0]; slot.segment = 0;
  await loadHistoryContent(slot, 0, 0);
}

async function loadHistoryContent(slot, offset, segment) {
  if (!historyDialogCurrent(slot) || !slot.selected) return;
  slot.focusAfterLoad = slot.content.contains(document.activeElement);
  slot.controller?.abort(); const controller = new AbortController(); slot.controller = controller;
  slot.mode = "body"; slot.bodyPage = null; slot.error = "";
  slot.retry = () => void loadHistoryContent(slot, offset, segment);
  renderHistoryDialog(slot);
  try {
    const query = new URLSearchParams({ offset_bytes: String(offset), revision: slot.selected.revision });
    const page = await request(`/sessions/${encodeURIComponent(slot.sessionId)}/messages/${encodeURIComponent(slot.selected.id)}/content?${query}`, { signal: controller.signal, cache: "no-store" });
    if (!historyDialogCurrent(slot) || slot.controller !== controller) return;
    if (!historyContentValid(page, slot, offset)) throw new Error("正文响应不一致，请返回记录重新读取。");
    slot.bodyPage = page; slot.offsets = [...slot.offsets.slice(0, segment), offset]; slot.segment = segment;
  } catch (error) {
    if (!historyDialogCurrent(slot) || slot.controller !== controller) return;
    slot.error = error.status === 404 ? "正文已不可用，请返回记录。" : error.status === 409 ? "正文已更新，请返回记录重新选择。" : error.message;
    if ([404, 409].includes(error.status)) { slot.retry = null; slot.offsets = [0]; slot.segment = 0; }
  } finally {
    if (historyDialogCurrent(slot) && slot.controller === controller) { slot.controller = null; renderHistoryDialog(slot); }
  }
}

function renderHistoryDialog(slot) {
  if (!historyDialogCurrent(slot)) return;
  const root = slot.content;
  const restoreFocus = slot.focusAfterLoad && (root.contains(document.activeElement) || document.activeElement === document.body);
  root.setAttribute("aria-busy", String(!!slot.controller)); root.replaceChildren();
  if (slot.controller) $("#dialog-content").scrollTop = 0;
  $("#dialog-title").textContent = slot.mode === "body" ? "已保存正文" : "正文归档";
  if (slot.mode === "body") {
    const id = slot.selected.id;
    root.append(element("button", { type: "button", class: "text-button", text: "返回记录", onClick: () => void loadHistoryPage(slot, slot.before, slot.starts, id) }));
    const page = slot.bodyPage;
    if (page) {
      const previous = element("button", { type: "button", class: "text-button", text: "上一段", disabled: slot.segment === 0, onClick: () => void loadHistoryContent(slot, slot.offsets[slot.segment - 1], slot.segment - 1) });
      const next = element("button", { type: "button", class: "text-button", text: "下一段", disabled: !page.has_more, onClick: () => void loadHistoryContent(slot, page.next_offset_bytes, slot.segment + 1) });
      root.append(element("nav", { class: "history-navigation", "aria-label": "正文分段" }, previous, element("span", { class: "history-position", tabIndex: -1, text: `第 ${slot.segment + 1} 段 · ${page.has_more ? "尚有后文" : "已到所存正文末尾"}` }), next));
      root.append(statusList([["角色", historyRole(page)], ["序号", page.seq], ["身份", page.principal ?? "未记录"], ["来源", page.source ?? "未记录"]]));
      if (page.metadata_omitted?.length) root.append(element("p", { class: "history-note", text: "部分来源元数据未显示。" }));
      const provenance = historyProvenance(page);
      if (provenance) root.append(element("p", { class: "history-note", text: provenance }));
      root.append(element("pre", { class: "history-content", text: page.content }));
    }
  } else {
    if (slot.fromRecord) root.append(element("button", { type: "button", class: "text-button", text: "返回任务详情", onClick: () => void loadSession(slot.sessionId, slot.trigger) }));
    root.append(element("p", { class: "history-note", text: slot.fromRecord ? `${slot.recordTitle} · 已保存的消息正文` : "已保存的消息正文" }));
    if (slot.page) {
      const newer = element("button", { type: "button", class: "text-button", text: "较新一页", disabled: slot.starts.length < 2, onClick: () => void loadHistoryPage(slot, slot.starts.at(-2), slot.starts.slice(0, -1)) });
      const older = element("button", { type: "button", class: "text-button", text: "更早一页", disabled: !slot.page.has_older, onClick: () => void loadHistoryPage(slot, slot.page.next_before_seq, [...slot.starts, slot.page.next_before_seq]) });
      root.append(element("nav", { class: "history-navigation", "aria-label": "正文归档分页" }, newer, element("span", { class: "history-position", tabIndex: -1, text: `第 ${slot.starts.length} 页 · ${slot.page.has_older ? "尚有更早记录" : "已到最早"}` }), older));
      for (const message of slot.page.messages) {
        const row = element("article", { class: "history-message" }, element("div", { class: "history-message-heading" }, element("strong", { text: historyRole(message) }), element("span", { text: `#${message.seq}` })));
        row.append(element("p", { class: "history-preview", text: message.content ?? "此消息无文本正文。" }));
        row.append(element("p", { class: "history-note", text: `身份：${message.principal ?? "未记录"} · 来源：${message.source ?? "未记录"}` }));
        const notes = [message.content_truncated ? "正文预览" : "", historyProvenance(message)].filter(Boolean);
        if (message.metadata_omitted?.length) notes.push("部分来源元数据未显示");
        if (notes.length) row.append(element("p", { class: "history-note", text: notes.join(" · ") }));
        if (typeof message.content === "string") row.append(element("button", { type: "button", class: "text-button", text: "查看正文", dataset: { historyMessageId: message.id }, onClick: () => void selectHistoryMessage(slot, message) }));
        root.append(row);
      }
      if (!slot.page.messages.length) root.append(element("p", { role: "status", text: "此页没有已保存记录。" }));
    }
  }
  if (slot.controller) root.append(element("p", { role: "status", class: "history-note", text: "正在读取…" }));
  if (slot.error) {
    root.append(element("p", { class: "form-error", role: "alert", text: slot.error }));
    if (slot.retry) root.append(element("button", { type: "button", class: "text-button", text: "重试", onClick: slot.retry }));
  }
  if (!slot.controller && slot.focusAfterLoad) {
    if (restoreFocus) {
      const target = slot.returnMessageId && $$("[data-history-message-id]", root).find((button) => button.dataset.historyMessageId === slot.returnMessageId) || $(".history-position", root) || $("#dialog-title");
      target.focus({ preventScroll: true });
    }
    slot.focusAfterLoad = false;
  }
}

async function sendMessage(event) {
  event.preventDefault();
  const input = $("#message-input");
  const content = input.value.trim();
  if ((!content && !state.attachments.length) || state.sending || state.uploading || !state.connected || !state.activeSession) return;
  if (!state.status?.configured) { state.settingsTab = "models"; await switchView("settings"); toast("先配置一个主模型，即可开始对话。"); return; }
  const intentVersion = sessionRequestVersion;
  const mode = state.mode;
  const wasRunning = busy();
  const attachments = [...state.attachments];
  let targetSession = state.activeSession;
  let pendingId = null;
  const stillCurrent = () => intentVersion === sessionRequestVersion && (!targetSession || state.activeSession?.id === targetSession.id);
  input.value = ""; draftRevision += 1; state.attachments = []; renderComposerAttachments(); resizeComposer();
  state.sending = true; renderComposer();
  try {
    if (stillCurrent()) {
      pendingId = `pending:${crypto.randomUUID()}`;
      state.session.messages.push({ id: pendingId, role: "user", content, attachments, created_at: new Date().toISOString(), pending: true });
      renderChat(); scrollToBottom(true);
    }
    try {
      const result = await request(`/sessions/${encodeURIComponent(targetSession.id)}/messages`, { method: "POST", body: { content, mode, attachment_ids: attachments.map((file) => file.id) } });
      if (stillCurrent()) {
        if (result.message) state.session = applyEvent(state.session, { type: "message.completed", data: { message: result.message } });
        const alreadyFinished = state.session.runId === result.run_id && ["completed", "cancelled", "failed"].includes(state.session.status);
        state.session.runId = result.run_id;
        if (!isRunning(state.session.status) && !alreadyFinished) state.session.status = "running";
        state.sending = false;
        if (result.queued && wasRunning) toast("补充消息已排队");
      }
      void refreshSessions();
    } catch (error) {
      if (stillCurrent()) state.session.messages = state.session.messages.filter((message) => message.id !== pendingId);
      throw error;
    }
  } catch (error) {
    showError(error);
    if (stillCurrent()) { state.sending = false; if (!input.value) input.value = content; state.attachments = [...attachments, ...state.attachments.filter((file) => !attachments.some((existing) => existing.id === file.id))]; renderComposerAttachments(); resizeComposer(); }
  }
  renderChrome(); renderChat();
}

async function approve(id, decision, button) {
  const owner = connection;
  const approval = state.approvals.find((item) => item.id === id);
  const region = state.view === "chat" ? $("#approval-region") : state.view === "notifications" && state.notificationTab === "approvals" ? $("#other-view") : null;
  if (!connectionCurrent(owner) || !approval || !region?.contains(button) || owner.approvalDecisions.has(id) || !["approve", "reject"].includes(decision) || button.closest(".approval-item")?.dataset.id !== id) return;
  owner.approvalDecisions.add(id); renderApprovalViews();
  try {
    const result = await ownedRequest(`/approvals/${encodeURIComponent(id)}`, { method: "POST", body: { decision } }, owner);
    if (result?.resolved !== true) throw new Error("审批回执无效，请刷新确认。");
    state.approvals = state.approvals.filter((item) => item.id !== id);
    if (state.activeSession?.id === approval.session_id) state.session = applyEvent(state.session, { type: "approval.resolved", data: { id, decision } });
    renderApprovalViews(); toast(decision === "approve" ? "已批准执行" : "已拒绝本次调用");
    await refreshApprovals(owner);
  } catch (error) {
    if (connectionCurrent(owner)) { showError(error); await refreshApprovals(owner); }
  } finally {
    owner.approvalDecisions.delete(id);
    if (connectionCurrent(owner)) renderApprovalViews();
  }
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

function openDialog({ title, content, submitLabel = "保存", danger = false, readOnly = false, returnMemoryId = null, returnMemoryTrigger = null, onSubmit }) {
  releaseMemorySourceRetry();
  releaseTaskDialog();
  releaseMemoryDialog();
  releaseHistoryDialog(); dialogHistoryReturn = null;
  dialogMemoryReturn = returnMemoryId ? { id: returnMemoryId, trigger: returnMemoryTrigger, pathButton: returnMemoryTrigger?.dataset.memoryPathRef ? memoryPath?.button : null } : null;
  const teamView = captureTeamView($("#other-view"));
  dialogTeamView = teamView?.inside ? teamView : null;
  $("#dialog-title").textContent = title;
  $("#dialog-content").replaceChildren(content);
  $("#dialog-submit").textContent = submitLabel;
  $("#dialog-submit").className = danger ? "danger-button" : "primary-button";
  $("#dialog-error").hidden = true;
  $("#dialog-submit").disabled = false;
  $("#dialog-submit").hidden = readOnly;
  $('#dialog-form .dialog-actions [data-action="close-dialog"]').textContent = readOnly ? "关闭" : "取消";
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

async function switchView(view, owner = connection) {
  if (!connectionCurrent(owner) || !owner.ready) return;
  if (view !== "settings") state.voiceSetupController?.release();
  if (view === "settings" && state.view === "settings" && state.settingsDraft && !state.viewError && !state.viewLoading && $(".settings-layout", $("#other-view"))) {
    collectSettingsForm(); renderOtherView(); return;
  }
  cancelMemorySearch(); releaseMemoryPath();
  releaseMemorySources();
  releaseWorkflowActivity();
  releaseRecordDirectory();
  if (view !== state.view && (memoryDialog || historyDialog)) { $("#app-dialog").close(); releaseMemoryDialog(); releaseHistoryDialog(); }
  if (view !== "chat") voiceInput.cancel();
  if (state.view === "settings" && view !== "settings") collectSettingsForm();
  const version = ++viewRequestVersion;
  const current = () => connectionCurrent(owner) && version === viewRequestVersion && state.view === view;
  const changedView = state.view !== view;
  state.view = view; state.viewError = ""; renderChrome();
  if (changedView) animatePanel(view === "chat" ? $("#chat-view") : $("#management-shell"));
  if (view === "chat") { state.viewLoading = false; renderChat(); $("#message-input").focus(); return; }
  if (view === "memories" && state.memoryTab === "sources") {
    state.viewLoading = false; renderOtherView();
    await loadMemorySources(memorySources);
    return;
  }
  if (view === "records") {
    const slot = recordDirectory = { query: state.recordQuery, cursor: null, starts: [null], page: null, controller: null, error: "", retry: null, dirty: false, root: null };
    state.viewLoading = false; renderOtherView();
    const heading = $(".view-heading h2", slot.root);
    heading.tabIndex = -1; heading.focus({ preventScroll: true });
    await loadRecordDirectory(slot, { query: state.recordQuery });
    return;
  }
  if (view === "workflows") {
    const slot = workflowActivity = { page: null, after: "", starts: [""], controller: null, loading: false, error: "", refreshPending: false, definitionsController: new AbortController(), definitionsLoading: true, definitionsError: "", schedulesOpen: false, schedulesPage: null, schedulesAfter: "", schedulesStarts: [""], schedulesController: null, schedulesLoading: false, schedulesError: "", schedulesStopping: new Set() };
    state.workflows = []; state.viewLoading = false; renderOtherView();
    const heading = $(".view-heading h2", $("#other-view"));
    if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); }
    const definitions = ownedRequest("/workflows", { signal: slot.definitionsController.signal, cache: "no-store" }, owner).then((payload) => {
      if (current() && workflowActivityCurrent(slot)) state.workflows = collection(payload);
    }).catch((error) => {
      if (current() && workflowActivityCurrent(slot)) slot.definitionsError = error.message;
    }).finally(() => {
      if (current() && workflowActivityCurrent(slot)) { slot.definitionsLoading = false; renderOtherView(); }
    });
    await Promise.allSettled([definitions, loadWorkflowActivity(slot, "", [""], owner)]);
    return;
  }
  state.viewLoading = true; renderOtherView();
  try {
    if (view === "uploads") { const payload = await ownedRequest("/uploads", {}, owner); if (current()) state.uploads = collection(payload); }
    if (view === "team") { const page = await teamPager.load({}, (path) => ownedRequest(path, {}, owner)); if (current() && page) state.team = page; }
    if (view === "memories") {
      const query = state.memoryQuery;
      const results = await Promise.allSettled([ownedRequest(`/memories${query ? `?query=${encodeURIComponent(query)}` : ""}`, {}, owner), ownedRequest("/memories/graph", {}, owner)]);
      if (!current() || query !== state.memoryQuery || !state.auth.authenticated) return;
      if (results[0].status === "rejected") throw results[0].reason;
      state.memories = collection(results[0].value);
      state.memoryGraph = results[1].status === "fulfilled" ? results[1].value : { nodes: [], edges: [] };
    }
    if (view === "notifications") {
      const results = await Promise.allSettled([refreshNotifications(owner), ownedRequest("/reminders", {}, owner), refreshApprovals(owner)]);
      if (!current()) return;
      if (state.notificationTab !== "approvals") {
        if (results[0].status === "rejected") throw results[0].reason;
        if (!results[0].value) throw owner.notifications.error ?? new Error("通知暂时无法读取，请重试。");
      }
      state.reminders = results[1].status === "fulfilled" ? collection(results[1].value) : [];
      state.remindersError = results[1].status === "rejected" ? results[1].reason.message : "";
    }
    if (view === "settings") {
      const dirty = state.settingsDraft && Object.keys(settingsPatch(state.settings, state.settingsDraft)).length;
      const results = await Promise.allSettled([dirty ? null : request("/settings"), refreshSettingsResources()]);
      if (!current()) return;
      if (results[0].status === "rejected") throw results[0].reason;
      if (results[0].value) { state.settings = results[0].value; state.settingsDraft = structuredClone(state.settings); state.settingsSaveStatus = ""; }
    }
  } catch (error) { if (current()) state.viewError = error.message; }
  if (!current()) return;
  state.viewLoading = false; renderOtherView(); renderChrome();
  const heading = $(".view-heading h2", $("#other-view"));
  if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); }
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
  if (state.view === "workflows") { const saved = captureWorkflowView(root); root.replaceChildren(renderWorkflows()); restoreWorkflowView(root, saved); }
  if (state.view === "notifications") root.replaceChildren(renderNotifications());
  if (state.view === "settings") {
    const layout = $(".settings-layout", root);
    if (layout && state.settingsDraft) {
      for (const button of $$("[data-settings-tab]", $(".settings-nav", layout))) {
        const active = button.dataset.settingsTab === state.settingsTab;
        button.classList.toggle("active", active);
        button.setAttribute("aria-current", active ? "page" : "false");
      }
      const panel = $(".settings-content", layout);
      const changedTab = panel.dataset.settingsTab !== state.settingsTab;
      panel.replaceChildren(renderSettingsPanel());
      panel.dataset.settingsTab = state.settingsTab;
      if (changedTab) animatePanel(panel);
    } else root.replaceChildren(renderSettings());
  }
  if (state.view === "uploads") root.replaceChildren(renderUploads());
  if (state.view === "team") { const saved = captureTeamView(root); root.replaceChildren(renderTeam()); restoreTeamView(root, saved); }
  if (state.view === "records") { const content = renderRecords(); if (root.firstElementChild !== content) root.replaceChildren(content); }
  const nav = $(".settings-nav", root);
  const active = $(".settings-nav .active", root);
  if (nav && active && nav.scrollWidth > nav.clientWidth) nav.scrollLeft = Math.max(0, active.offsetLeft - nav.offsetLeft - (nav.clientWidth - active.offsetWidth) / 2);
}

function animatePanel(node) {
  if (!node?.animate || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
  for (const animation of node.getAnimations()) animation.cancel();
  node.animate([{ opacity: 0.65, transform: "translateY(5px)" }, { opacity: 1, transform: "none" }], { duration: 180, easing: "cubic-bezier(.2,.7,.2,1)" });
}

function switchSettingsTab(tab) {
  if (!SETTINGS_GROUPS.some(([key]) => key === tab) || tab === state.settingsTab) return;
  collectSettingsForm();
  state.settingsTab = tab;
  renderOtherView();
  $("#other-view").scrollTop = 0;
}

function renderThemeControl() {
  const dark = document.documentElement.dataset.theme === "dark";
  const button = $("#theme-toggle");
  button.replaceChildren(icon(dark ? "sun" : "moon"));
  button.setAttribute("aria-label", dark ? "切换浅色" : "切换深色");
  button.title = dark ? "切换浅色" : "切换深色";
}

function renderAttachment(file, { compact = false, removable = false } = {}) {
  const kind = attachmentKind(file);
  const root = element("div", { class: `attachment ${compact ? "compact" : kind}` });
  if (!compact && kind === "image") root.append(element("a", { href: attachmentURL(file), target: "_blank", rel: "noopener", class: "attachment-preview", "aria-label": `查看 ${file.name}` }, element("img", { src: attachmentURL(file), alt: file.name || "图片附件", loading: "lazy" })));
  if (!compact && (kind === "audio" || kind === "video")) root.append(element(kind, { src: attachmentURL(file), controls: true, preload: "metadata", "aria-label": file.name || "媒体附件" }));
  root.append(element("div", { class: "attachment-info" }, icon(kind === "file" ? "attachment" : kind === "image" ? "sparkles" : "play"), element("span", { class: "attachment-name", text: file.name || "附件", title: file.name }), element("small", { text: formatBytes(file.size) }), removable ? iconButton("移除附件引用", "remove-attachment", "close", { id: file.id }) : element("a", { href: attachmentURL(file, true), download: file.name || "", class: "icon-button", "aria-label": `下载 ${file.name || "附件"}`, title: "下载" }, icon("download"))));
  return root;
}

function renderComposerAttachments() {
  const root = $("#composer-attachments");
  root.hidden = !state.attachments.length;
  root.replaceChildren(...state.attachments.map((file) => renderAttachment(file, { compact: true, removable: true })));
  renderComposer();
}

async function addFiles(files) {
  const pending = [...files];
  state.uploading += pending.length; renderComposer();
  for (const file of pending) {
    try {
      const uploaded = await uploadFile(file);
      state.uploads = [uploaded, ...state.uploads.filter((entry) => entry.id !== uploaded.id)];
      if (!state.attachments.some((entry) => entry.id === uploaded.id)) state.attachments.push(uploaded);
      renderComposerAttachments();
    } catch (error) { if (error.status === 401) showLogin(); toast(`${file.name}：${error.message}`, true); }
    finally { state.uploading -= 1; renderComposer(); }
  }
  if (state.view === "uploads") renderOtherView();
}

async function refreshUploads(owner = connection) {
  const version = viewRequestVersion;
  const current = () => connectionCurrent(owner) && version === viewRequestVersion && state.view === "uploads";
  try { const payload = await ownedRequest("/uploads", {}, owner); if (current()) { state.uploads = collection(payload); renderOtherView(); } }
  catch (error) { if (current()) showError(error); }
}

function renderUploads() {
  const root = element("div", { class: "view-content" }, viewHeading("附件", "文件保存在本地，发送时只引用附件。", actionButton("添加文件", "attach-files", "primary-button", "plus")));
  if (!state.uploads.length) root.append(emptyState("attachment", "还没有附件", "图片、文档与音视频都可以添加。"));
  for (const file of state.uploads) root.append(element("article", { class: "upload-row" }, renderAttachment(file), actionButton("带入对话", "use-attachment", "text-button", "chat", { id: file.id })));
  return root;
}

async function refreshTeam(changes = {}, owner = connection) {
  const version = viewRequestVersion;
  const current = () => connectionCurrent(owner) && version === viewRequestVersion && state.view === "team";
  try {
    const page = await teamPager.load(changes, (path) => ownedRequest(path, {}, owner));
    if (current() && page) { state.team = page; renderOtherView(); }
  } catch (error) { if (current()) showError(error); }
}

function readSavedTaskControl(identifier) {
  try { return JSON.parse(sessionStorage.getItem(taskControlStoragePrefix + identifier)); }
  catch { return null; }
}

function saveTaskControl(model) {
  try {
    const key = taskControlStoragePrefix + model.state.id;
    if (model.state.submission) sessionStorage.setItem(key, JSON.stringify(model.state.submission));
    else sessionStorage.removeItem(key);
  } catch { /* The in-memory request still keeps its retry ID if storage is unavailable. */ }
}

function releaseTaskDialog() {
  if (!taskDialog) return;
  const { model } = taskDialog;
  taskDialog.readController?.abort();
  taskDialog = null;
  if (!model.state.submission) {
    taskControlModels.delete(model.state.id);
    model.clear();
  }
}

function clearTaskControls() {
  taskDialog?.readController?.abort();
  for (const model of taskControlModels.values()) model.clear();
  taskControlModels.clear(); taskDialog = null;
  try {
    for (const key of Object.keys(sessionStorage)) if (key.startsWith(taskControlStoragePrefix)) sessionStorage.removeItem(key);
  } catch { /* Storage can be blocked by the browser. */ }
}

function taskDialogCurrent(slot) {
  return taskDialog === slot && $("#app-dialog").open && $("#dialog-content").firstElementChild === slot.content;
}

function renderTaskBriefDialog(slot = taskDialog) {
  if (!slot || !taskDialogCurrent(slot)) return;
  const data = slot.model.state;
  const view = taskControlView(data);
  const focused = document.activeElement;
  const focusedAction = slot.content.contains(focused) ? focused.dataset.taskControlAction : null;
  if (data.detail && slot.renderedDetail !== data.detail) {
    const item = data.detail;
    const evidenceOpen = $("details", slot.brief)?.open ?? false;
    $("#dialog-title").textContent = item.title || "任务说明";
    slot.brief.replaceChildren(element("small", { text: `任务说明 · 修订 ${item.revision}` }));
    if (!item.brief) slot.brief.append(element("p", { text: "尚未登记任务说明。" }));
    else {
      slot.brief.append(element("h3", { text: "目标" }), element("p", { text: item.brief.goal }));
      if (item.brief.constraints?.length) slot.brief.append(element("h3", { text: "要求" }), element("ul", {}, item.brief.constraints.map((text) => element("li", { text }))));
      if (item.brief.evidence_refs?.length) {
        const evidence = element("details", { class: "advanced-settings", open: evidenceOpen }, element("summary", { text: "依据" }));
        for (const reference of item.brief.evidence_refs) evidence.append(element("p", { text: reference.kind === "message" ? `会话 ${reference.session_id} · 消息 ${reference.seq}` : `附件 ${reference.id}` }));
        slot.brief.append(evidence);
      }
    }
    slot.renderedDetail = item;
  }
  slot.readStatus.textContent = slot.readError || (!data.detail ? "正在读取…" : !view.fresh ? slot.loading ? "任务说明有更新，正在读取…" : "任务说明有更新，请刷新。" : "");
  slot.readStatus.className = slot.readError ? "form-error" : "task-control-note";
  slot.readStatus.hidden = !slot.readStatus.textContent;
  slot.panel.hidden = !view.capable && !data.submission;
  slot.status.textContent = view.busy ? "正在提交…" : view.unknown ? "提交结果待确认" : view.label;
  slot.status.classList.toggle("paused", view.paused);
  slot.scope.textContent = view.capable ? `范围：${data.current.scope.member_count} 位队友（含子任务）` : "";
  slot.note.textContent = view.workflowBlocked ? "此任务含活动工作流，先停止或等待结束。" : view.paused ? "已开始的操作会收尾。关闭页面不会恢复任务。" : "已开始的操作会收尾。";
  slot.error.textContent = data.error || (data.editing && data.conflict ? "要求已有更新，请核对后再提交。" : "");
  slot.error.hidden = !slot.error.textContent;
  slot.editor.hidden = !data.editing;
  slot.input.disabled = !!data.submission;
  if (slot.input.value !== data.draft) slot.input.value = data.draft;
  const localButton = (label, action, callback, { primary = false, disabled = false } = {}) => element("button", { type: "button", class: primary ? "primary-button" : "secondary-button", disabled, dataset: { taskControlAction: action }, onClick: callback }, label);
  const refresh = () => void refreshTaskBriefDialog(slot);
  const actions = [];
  if (view.unknown) actions.push(localButton("重试原操作", "retry", () => void submitTaskControl(slot.model, data.submission.action), { primary: true }));
  else if (data.editing) {
    actions.push(localButton(data.conflict ? "确认要求并重新规划" : "交沐璃重新规划", "replan", () => void submitTaskControl(slot.model, "request_replan"), { primary: true, disabled: !view.canReplan }));
    actions.push(localButton("取消编辑", "cancel-edit", () => { slot.model.cancelEdit(); renderTaskBriefDialog(slot); }, { disabled: view.busy }));
  } else if (view.paused) {
    actions.push(localButton("补充要求并交沐璃", "edit", () => { slot.model.edit(); renderTaskBriefDialog(slot); slot.input.focus(); }, { primary: true, disabled: !view.canReplan }));
    if (data.current.control.replan_request_id) actions.push(localButton("重新暂停", "pause", () => void submitTaskControl(slot.model, "pause"), { disabled: !view.canPause }));
    actions.push(localButton("查看主对话", "chat", () => { $("#app-dialog").close(); void switchView("chat"); }));
  } else if (view.capable) actions.push(localButton("暂停任务树", "pause", () => void submitTaskControl(slot.model, "pause"), { primary: true, disabled: !view.canPause }));
  if (view.workflowBlocked) actions.push(localButton("查看工作流", "workflows", () => { $("#app-dialog").close(); void switchView("workflows"); }));
  if (view.capable || data.submission || slot.readError) actions.push(localButton("刷新状态", "refresh", refresh, { disabled: slot.loading || view.busy }));
  slot.actions.replaceChildren(...actions);
  slot.plan.hidden = !view.plan;
  const planLabel = view.plan?.epoch === data.current?.control?.epoch ? "已确认计划" : "此前计划";
  if (view.plan && (slot.renderedPlan !== view.plan || slot.planLabel !== planLabel)) {
    const wasOpen = $("details", slot.plan)?.open ?? false;
    slot.plan.replaceChildren(element("details", { class: "advanced-settings", open: wasOpen }, element("summary", { text: planLabel }), element("p", { text: view.plan.text })));
    slot.renderedPlan = view.plan; slot.planLabel = planLabel;
  }
  if (focusedAction && !focused.isConnected) {
    const next = $$('[data-task-control-action]', slot.actions).find((button) => button.dataset.taskControlAction === focusedAction && !button.disabled);
    if (next) next.focus({ preventScroll: true });
    else { slot.status.tabIndex = -1; slot.status.focus({ preventScroll: true }); }
  }
}

async function refreshTaskBriefDialog(slot = taskDialog, owner = connection) {
  if (!slot || !taskDialogCurrent(slot) || !connectionCurrent(owner)) return;
  if (slot.loading && slot.readOwner === owner) { slot.refreshAgain = true; return; }
  slot.readController?.abort();
  const controller = slot.readController = new AbortController(); slot.readOwner = owner;
  const current = () => connectionCurrent(owner) && taskDialogCurrent(slot) && slot.readController === controller;
  slot.loading = true; slot.readError = ""; slot.refreshAgain = false; renderTaskBriefDialog(slot);
  try {
    const item = await ownedRequest(`/commitments/${encodeURIComponent(slot.model.state.id)}`, { signal: controller.signal }, owner);
    if (!current()) return;
    if (!slot.model.acceptDetail(item)) slot.readError = "任务说明已有更新，请再次刷新。";
  } catch (error) {
    if (current()) slot.readError = error.message;
  } finally {
    if (current()) {
      slot.loading = false; renderTaskBriefDialog(slot);
      if (slot.refreshAgain) { slot.refreshAgain = false; void refreshTaskBriefDialog(slot, owner); }
    }
  }
}

async function submitTaskControl(model, action) {
  const current = () => taskControlModels.get(model.state.id) === model;
  if (!current() || model.state.phase === "submitting") return;
  try {
    const body = model.begin(action);
    saveTaskControl(model);
    if (taskDialog?.model === model) renderTaskBriefDialog();
    const result = await request(`/commitments/${encodeURIComponent(model.state.id)}/control`, { method: "POST", body });
    if (!current()) return;
    model.complete(result);
    saveTaskControl(model);
    if (taskDialog?.model === model) {
      renderTaskBriefDialog();
      await refreshTaskBriefDialog();
    } else taskControlModels.delete(model.state.id);
  } catch (error) {
    if (!current()) return;
    model.fail(error); saveTaskControl(model);
    if (taskDialog?.model === model) {
      renderTaskBriefDialog();
      if (error.status === 409) await refreshTaskBriefDialog();
    } else if (!model.state.submission) taskControlModels.delete(model.state.id);
  }
}

async function showTaskBrief(identifier) {
  const model = taskControlModels.get(identifier) ?? createTaskControl(identifier, { pending: readSavedTaskControl(identifier) });
  const content = element("div", { class: "task-brief" });
  const slot = {
    model, content, loading: false, refreshAgain: false, readError: "",
    readStatus: element("p", { role: "status" }), brief: element("div"),
    panel: element("section", { class: "task-control-panel", "aria-label": "任务控制", hidden: true }),
    status: element("strong", { class: "task-control-status", "aria-live": "polite" }),
    scope: element("small"), note: element("p", { class: "task-control-note" }),
    error: element("p", { class: "form-error", role: "alert", hidden: true }),
    editor: element("div", { class: "task-control-editor", hidden: true }),
    input: element("textarea", { name: "additional_requirements", rows: 4, placeholder: "可留空，按当前要求重新规划" }),
    actions: element("div", { class: "task-control-actions" }), plan: element("div"),
  };
  slot.input.addEventListener("input", () => model.setDraft(slot.input.value));
  slot.editor.append(element("label", { class: "field" }, element("span", { text: "补充要求" }), slot.input));
  slot.panel.append(element("div", { class: "task-control-heading" }, slot.status, slot.scope), slot.note, slot.plan, slot.editor, slot.error, slot.actions);
  content.append(slot.brief, slot.readStatus, slot.panel);
  openDialog({ title: "任务说明", content, readOnly: true, onSubmit: null });
  taskControlModels.set(identifier, model); taskDialog = slot;
  renderTaskBriefDialog(slot);
  await refreshTaskBriefDialog(slot);
}

function renderTeam() {
  const root = element("div", { class: "view-content" }, viewHeading("团队", "沐璃分派的任务与协作进展。", actionButton("刷新", "refresh-team", "secondary-button", "refresh")));
  root.append(actionButton("与沐璃安排协作", "configure-prompt", "configuration-link", "chat", { prompt: "请帮我安排一组协作任务：" }));
  const filter = element("div", { class: "team-filter", role: "group", "aria-label": "团队筛选" });
  for (const [active, label] of [[false, "全部"], [true, "进行中"]]) {
    const button = actionButton(label, "filter-team", "text-button", null, { active: String(active) });
    button.setAttribute("aria-pressed", String(!!state.team.active_only === active));
    filter.append(button);
  }
  filter.append(element("small", { text: `${state.team.total ?? 0} 位队友` }));
  root.append(filter);
  const members = state.team.members ?? [];
  const status = { running: "运行中", queued: "排队中", paused: "已暂停", idle: "空闲", completed: "已完成", failed: "失败", closed: "已关闭", cancelled: "已停止", interrupted: "已中断", approval: "等待审批" };
  const roles = { worker: "执行", reviewer: "复核" };
  if (!members.length) root.append(emptyState("team", state.team.active_only ? "没有进行中的协作" : "当前没有协作任务"));
  for (const member of members) {
    const content = element("div", { class: "row-content" }, element("h3", { text: member.name || member.id }), member.task && element("p", { text: member.task }), element("div", { class: "row-meta", text: [roles[member.role] ?? member.role, status[member.status] ?? member.status].filter(Boolean).join(" · ") }));
    if (member.last_message) content.append(element("p", { class: "team-last-message", text: (typeof member.last_message === "string" ? member.last_message : member.last_message.content ?? "") + (member.last_message_truncated ? "…" : "") }));
    root.append(element("article", { class: "team-row" }, element("div", { class: "row-icon" }, icon("team")), content, element("div", { class: "row-actions" }, member.commitment_id && iconButton("查看任务说明", "view-task-brief", "info", { id: member.id, commitment: member.commitment_id }), member.status !== "closed" && iconButton("发送消息", "message-team", "chat", { id: member.id }), member.session_id && iconButton("查看记录", "workflow-session", "panel", { session: member.session_id }), ["running", "queued", "approval", "paused"].includes(member.status) && iconButton("停止任务", "stop-team", "stop", { id: member.id }))));
  }
  if ((state.team.total ?? 0) > state.team.limit) {
    const previous = actionButton("上一页", "page-team", "secondary-button", null, { offset: Math.max(0, state.team.offset - state.team.limit), direction: "previous" });
    previous.disabled = state.team.offset === 0;
    const next = actionButton("下一页", "page-team", "secondary-button", null, { offset: state.team.offset + state.team.limit, direction: "next" });
    next.disabled = !state.team.has_more;
    root.append(element("nav", { class: "team-pagination", "aria-label": "团队分页" }, previous, element("span", { text: `${state.team.offset + 1}–${state.team.offset + members.length} / ${state.team.total}` }), next));
  }
  if (state.team.messages?.length) {
    const details = element("details", { class: "team-messages advanced-settings" }, element("summary", { text: "协作消息" }));
    for (const message of state.team.messages) details.append(element("div", { class: "team-message" }, element("strong", { text: message.from_name || message.sender || message.from || "成员" }), element("p", { text: (message.content ?? "") + (message.truncated ? "…" : "") }), element("time", { text: formatTimestamp(message.created_at) })));
    root.append(details);
  }
  return root;
}

function recordDirectoryCurrent(slot) {
  return !!slot && recordDirectory === slot && state.view === "records" && state.auth.authenticated && $("#other-view").firstElementChild === slot.root;
}

function releaseRecordDirectory() {
  const slot = recordDirectory; recordDirectory = null;
  if (!slot) return;
  slot.controller?.abort(); slot.controller = null;
  slot.page = null; slot.retry = null; slot.starts = [];
  slot.root?.replaceChildren();
}

function recordDirectoryPage(payload, cursor, starts) {
  if (!payload || !Array.isArray(payload.items) || payload.items.length > recordPageSize || typeof payload.has_more !== "boolean") throw new Error("记录目录响应不完整，请重新读取。");
  const seen = new Set();
  const date = (value) => typeof value === "number" && value > 0 && Number.isFinite(value) && Number.isFinite(new Date(value * 1000).getTime()) ? value : null;
  const items = payload.items.map((item) => {
    if (!item || typeof item.id !== "string" || !item.id || seen.has(item.id) || (item.title != null && typeof item.title !== "string") || (item.status != null && typeof item.status !== "string")) throw new Error("记录目录响应不完整，请重新读取。");
    seen.add(item.id);
    return { id: item.id, title: item.title ?? null, status: item.status ?? null, created_at: date(item.created_at), updated_at: date(item.updated_at) };
  });
  if (payload.has_more ? !items.length || typeof payload.next_cursor !== "string" || !payload.next_cursor || payload.next_cursor === cursor || starts.includes(payload.next_cursor) : payload.next_cursor !== null) throw new Error("记录目录翻页位置无效，请重新读取。");
  return { items, has_more: payload.has_more, next_cursor: payload.next_cursor };
}

function renderRecordNotice(slot) {
  if (!recordDirectoryCurrent(slot)) return;
  slot.refresh.disabled = !!slot.controller;
  slot.notice.textContent = slot.controller ? "正在读取记录…" : slot.dirty ? "记录有更新，可刷新列表。" : "";
  slot.notice.hidden = !slot.notice.textContent;
}

function markRecordDirectoryDirty() {
  const slot = recordDirectory;
  if (!recordDirectoryCurrent(slot)) return;
  slot.dirty = true; renderRecordNotice(slot);
}

async function loadRecordDirectory(slot, { query = slot.query, cursor = null, starts = [null], focus = false, allowFallback = true } = {}) {
  if (!recordDirectoryCurrent(slot)) return;
  slot.controller?.abort();
  const controller = slot.controller = new AbortController();
  slot.query = query; slot.cursor = cursor; slot.starts = [...starts];
  slot.page = null; slot.error = ""; slot.retry = null; slot.dirty = false;
  renderRecordList(slot);
  if (focus && !$("#app-dialog").open) $("#other-view").scrollTop = 0;
  const current = () => recordDirectoryCurrent(slot) && slot.controller === controller && !controller.signal.aborted;
  try {
    if ([...query].length > recordQueryLimit) throw new Error(`标题搜索最多 ${recordQueryLimit} 个字符。`);
    slot.retry = { query, cursor, starts: [...starts], focus };
    const params = new URLSearchParams({ query, limit: recordPageSize });
    if (cursor !== null) params.set("cursor", cursor);
    const payload = await request(`/session-records?${params}`, { signal: controller.signal, cache: "no-store" });
    if (!current()) return;
    const page = recordDirectoryPage(payload, cursor, starts);
    // Deletion can empty a later page; return one known page, never chase cursors.
    if (cursor !== null && !page.items.length && allowFallback) {
      const previous = starts.length > 1 ? starts.slice(0, -1) : [null];
      await loadRecordDirectory(slot, { query, cursor: previous.at(-1), starts: previous, focus, allowFallback: false });
      return;
    }
    slot.page = page; slot.retry = null;
  } catch (error) {
    if (current()) slot.error = error.status === 404 ? "当前服务尚不支持完整记录目录。" : error.message;
  } finally {
    if (current()) {
      slot.controller = null;
      const active = document.activeElement;
      const restoreFocus = focus && !$("#app-dialog").open && (slot.navigation.contains(active) || active === document.body);
      renderRecordList(slot);
      if (restoreFocus) slot.position.focus({ preventScroll: true });
    }
  }
}

function renderRecords() {
  const slot = recordDirectory;
  if (!slot) return element("div", { class: "view-content" });
  if (slot.root) return slot.root;
  slot.refresh = element("button", { type: "button", class: "text-button", text: "刷新列表", onClick: () => void loadRecordDirectory(slot, { focus: true }) });
  slot.input = element("input", { type: "search", value: state.recordQuery, placeholder: "按标题搜索", "aria-label": "按标题搜索", onInput: (event) => { state.recordQuery = event.target.value; } });
  const form = element("form", { class: "content-toolbar record-toolbar", onSubmit: (event) => { event.preventDefault(); void loadRecordDirectory(slot, { query: slot.input.value, focus: true }); } },
    element("div", { class: "search-field" }, icon("search"), slot.input), element("button", { type: "submit", class: "secondary-button", text: "搜索" }),
    element("button", { type: "button", class: "text-button", text: "清空筛选", onClick: () => { slot.input.value = ""; state.recordQuery = ""; void loadRecordDirectory(slot, { query: "", focus: true }); } }));
  slot.notice = element("p", { class: "record-list-status", role: "status", hidden: true });
  slot.navigation = element("nav", { class: "record-pagination", "aria-label": "记录目录分页" });
  slot.result = element("div", { id: "record-directory-result", "aria-busy": "false" });
  slot.root = element("div", { class: "view-content record-directory" }, viewHeading("运行记录", "主对话与各项任务的持久化记录。", slot.refresh), form, slot.notice, slot.navigation, slot.result);
  return slot.root;
}

function renderRecordList(slot = recordDirectory) {
  if (!recordDirectoryCurrent(slot)) return;
  renderRecordNotice(slot);
  const page = slot.page;
  slot.position = element("span", { class: "record-page-position", tabIndex: -1, text: `第 ${slot.starts.length} 页${slot.query ? ` · “${slot.query}”` : ""}${page ? ` · 本页 ${page.items.length} 条${page.has_more ? " · 尚有更早记录" : " · 已到最早"}` : ""}` });
  const newer = element("button", { type: "button", class: "text-button", text: "较新一页", disabled: !!slot.controller || slot.starts.length < 2, onClick: () => void loadRecordDirectory(slot, { cursor: slot.starts.at(-2), starts: slot.starts.slice(0, -1), focus: true }) });
  const older = element("button", { type: "button", class: "text-button", text: "更早一页", disabled: !!slot.controller || !page?.has_more, onClick: () => void loadRecordDirectory(slot, { cursor: page.next_cursor, starts: [...slot.starts, page.next_cursor], focus: true }) });
  slot.navigation.replaceChildren(newer, slot.position, older);
  slot.result.setAttribute("aria-busy", String(!!slot.controller)); slot.result.replaceChildren();
  if (slot.error) {
    slot.result.append(element("p", { class: "form-error", role: "alert", text: slot.error }));
    if (slot.retry) slot.result.append(element("button", { type: "button", class: "secondary-button", text: "重试", onClick: () => void loadRecordDirectory(slot, slot.retry) }));
  }
  if (!page) return;
  const statuses = { running: "会话运行中", queued: "会话排队中", paused: "会话已暂停", idle: "无当前执行", approval: "等待审批" };
  for (const session of page.items) {
    const main = session.id === state.mainSessionId;
    const timestamp = session.updated_at ?? session.created_at;
    const title = session.title === null ? "标题未记录" : session.title || "空标题";
    const row = element("article", { class: "record-row" }, element("div", { class: "row-content" }, element("h3", { text: title }), element("div", { class: "row-meta" }, main ? "主对话" : "任务", element("span", { text: Object.hasOwn(statuses, session.status) ? statuses[session.status] : session.status ? `状态：${session.status}` : "状态未记录" }), element("time", { text: timestamp === null ? "日期未记录" : formatTimestamp(timestamp) }))), actionButton(main ? "回到对话" : "查看", "workflow-session", "text-button", undefined, { session: session.id }));
    if (main) row.append(element("button", { type: "button", class: "text-button", text: "查看正文归档", dataset: { action: "record-archive", session: session.id }, onClick: (event) => void showHistoryArchive(session.id, { trigger: event.currentTarget, title }) }));
    else row.append(iconButton("删除记录", "delete-session", "trash", { id: session.id }));
    slot.result.append(row);
  }
  if (!page.items.length) slot.result.append(emptyState("chat", slot.query ? "没有找到记录" : "还没有运行记录"));
}

function renderMemories() {
  releaseMemoryPath();
  const search = element("input", { type: "search", name: "memory-search", id: "memory-search", value: state.memoryQuery, placeholder: "检索内容、实体或偏好", "aria-label": "检索记忆" });
  const form = element("form", { class: "content-toolbar", onSubmit: async (event) => {
    event.preventDefault(); await searchMemories(search.value.trim());
  } }, element("div", { class: "search-field" }, icon("search"), search), element("button", { class: "secondary-button", type: "submit", text: "检索" }));
  const tabs = element("div", { class: "role-tabs memory-tabs", role: "tablist", "aria-label": "记忆视图" }, ...[["list", "记忆列表"], ["sources", "来源"], ["graph", "关系图"]].map(([key, label]) => element("button", { type: "button", role: "tab", class: state.memoryTab === key ? "active" : "", text: label, "aria-selected": String(state.memoryTab === key), dataset: { memoryTab: key } })));
  const root = element("div", { class: "view-content" }, viewHeading("记忆", "保留偏好、事实与长期上下文。", actionButton("新增记忆", "new-memory", "primary-button", "plus")), tabs);
  if (state.memoryTab === "graph") { root.append(renderMemoryGraph()); return root; }
  if (state.memoryTab === "sources") {
    memorySources ??= { filter: state.memorySourceState ?? "", page: null, cursor: null, controller: null, error: "", receipt: null };
    root.append(renderMemorySources(memorySources)); return root;
  }
  root.append(form, element("div", { class: "list-caption" }, `${state.memories.length} 条记忆`, state.memoryQuery ? `“${state.memoryQuery}”` : "全部记忆"));
  if (!state.memories.length) root.append(emptyState("memory", state.memoryQuery ? "没有找到相关记忆" : "还没有记忆", state.memoryQuery ? "换个关键词试试。" : "添加希望沐璃记住的内容。"));
  for (const item of state.memories) {
    const meta = element("div", { class: "row-meta" }, element("span", { class: "row-tag", text: memoryKind(item.kind) }), memoryNeedsReview(item) && element("span", { class: "row-tag", text: "待核对" }), ...(item.entities ?? []).map((entity) => element("span", { text: typeof entity === "string" ? entity : entity.name ?? "" })), element("time", { text: formatTimestamp(item.updated_at ?? item.created_at) }), item.matched_by && element("span", { text: { text: "文本匹配", lexical: "文本匹配", graph: "关系匹配", "graph+text": "文本与关系匹配" }[item.matched_by] ?? item.matched_by }));
    const content = element("div", { class: "row-content" }, element("p", { text: item.content }), meta);
    if (item.path_omitted === true) meta.append(element("span", { class: "memory-path-note", text: "召回路径因预算省略" }));
    else if (state.memoryQuery && memoryGraphPath(item)) {
      const regionId = `memory-path-${++memoryPathSequence}`, query = state.memoryQuery;
      const region = element("div", { id: regionId, class: "memory-recall-path", role: "region", "aria-label": "本次检索路径", hidden: true });
      const button = element("button", { type: "button", class: "text-button memory-path-toggle", text: "召回路径", "aria-expanded": "false", "aria-controls": regionId, dataset: { memoryPath: String(item.id) }, onClick: () => void toggleMemoryPath(item.id, button, region, query) });
      meta.append(button); content.append(region);
    }
    root.append(element("article", { class: "memory-row" }, element("div", { class: "row-icon" }, icon("memory")), content, element("div", { class: "row-actions" }, actionButton("核查", "memory-evidence", "text-button", undefined, { id: item.id }), iconButton("更正记忆", "edit-memory", "edit", { id: item.id }), iconButton("删除记忆", "delete-memory", "trash", { id: item.id }))));
  }
  return root;
}

function cancelMemorySearch() { memorySearchController?.abort(); memorySearchController = null; }

function releaseMemorySourceRetry() {
  const pending = memorySourceRetry; memorySourceRetry = null;
  pending?.controller?.abort();
  if (pending && $("#app-dialog").open && $("#dialog-content").firstElementChild === pending.content) $("#app-dialog").close();
}

function releaseMemorySources() {
  const slot = memorySources; memorySources = null;
  slot?.controller?.abort(); releaseMemorySourceRetry();
  if (slot) { slot.page = null; slot.receipt = null; slot.result?.replaceChildren(); slot.receiptRegion?.replaceChildren(); slot.notice?.replaceChildren(); slot.root?.replaceChildren(); }
}

function memorySourcesCurrent(slot) {
  return !!slot && memorySources === slot && state.auth.authenticated && state.view === "memories" && state.memoryTab === "sources";
}

function memorySourceActionCurrent(slot, trigger) {
  return memorySourcesCurrent(slot) && !slot.controller && !!trigger && !trigger.disabled && $("#other-view").contains(slot.root) && slot.result.contains(trigger);
}

async function selectMemoryTab(tab) {
  cancelMemorySearch(); releaseMemoryPath(); releaseMemorySources();
  if (memoryDialog?.catalogSource) { $("#app-dialog").close(); releaseMemoryDialog(); }
  state.memoryTab = tab; renderOtherView();
  if (tab === "sources") await loadMemorySources(memorySources);
}

function memorySourcePage(payload, cursor) {
  if (!payload || !Array.isArray(payload.items) || !Number.isSafeInteger(payload.examined) || payload.examined < 0 || payload.examined > memorySourcePageSize || !Number.isSafeInteger(payload.omitted) || payload.omitted < 0 || payload.items.length + payload.omitted !== payload.examined || payload.consistency !== "live" || payload.order !== "newest_registered_first" || typeof payload.has_more !== "boolean") throw new Error("来源目录响应不完整，请刷新。");
  if (payload.has_more ? typeof payload.next_cursor !== "string" || !payload.next_cursor || payload.next_cursor === cursor || !payload.examined : payload.next_cursor !== null) throw new Error("来源目录续页位置无效，请刷新。");
  const seen = new Set();
  for (const item of payload.items) {
    if (!item || typeof item.source_id !== "string" || !item.source_id || seen.has(item.source_id) || ![item.source, item.session_id, item.principal].every((value) => typeof value === "string") || !["user", "assistant", "tool"].includes(item.role) || typeof item.trusted_owner !== "boolean" || (item.trusted_owner && item.role !== "user") || typeof item.revision !== "string" || !/^[a-f0-9]{64}$/.test(item.revision) || !Number.isFinite(item.created_at) || !Number.isSafeInteger(item.total_bytes) || item.total_bytes < 0 || !item.job || !Object.hasOwn(memoryJobStates, item.job.state) || !Number.isSafeInteger(item.job.attempts) || item.job.attempts < 0 || typeof item.job.last_error !== "string") throw new Error("来源身份或整理状态不完整，请刷新。");
    seen.add(item.source_id);
  }
  return payload;
}

function renderMemorySources(slot) {
  slot.filterInput = element("select", { "aria-label": "来源整理状态" }, element("option", { value: "", text: "全部状态" }), ...Object.entries(memoryJobStates).map(([key, label]) => element("option", { value: key, text: label })));
  slot.filterInput.value = slot.filter;
  slot.filterInput.addEventListener("change", () => void loadMemorySources(slot, { filter: slot.filterInput.value }));
  slot.refresh = element("button", { type: "button", class: "secondary-button", text: "刷新来源", onClick: () => void loadMemorySources(slot) });
  slot.next = element("button", { type: "button", class: "secondary-button", text: "继续翻页", onClick: () => void loadMemorySources(slot, { cursor: slot.page?.next_cursor }) });
  slot.position = element("p", { class: "settings-role-hint", role: "status", tabIndex: -1 });
  slot.result = element("div"); slot.notice = element("div", { "aria-live": "polite" }); slot.receiptRegion = element("div", { "aria-live": "polite" });
  slot.root = element("section", { "aria-label": "已保存来源" }, element("div", { class: "content-toolbar" }, element("label", {}, "整理状态", slot.filterInput), slot.refresh), element("p", { class: "settings-role-hint", text: "查看已保存资料及整理进度。" }), slot.position, slot.notice, slot.receiptRegion, slot.result, slot.next);
  updateMemorySources(slot); return slot.root;
}

function memoryRetryReceipt(receipt) {
  const reason = { queued: "已接纳到后台队列，尚未完成整理", already_pending: "已有待处理作业，未重复排队", already_done: "作业已经完成，未重新执行", capacity_full: "后台容量已满，本次未接纳" }[receipt.reason];
  return element("div", { class: "advanced-settings" }, element("p", { role: "status", text: reason }), statusList([["来源 ID", receipt.source_id], ["来源版本", receipt.revision], ["原状态", memoryJobStates[receipt.previous_state]], ["当前状态", memoryJobStates[receipt.state]], ["本次接纳", receipt.accepted ? "是" : "否"]]), element("p", { class: "settings-role-hint", text: "请刷新来源目录核对后续状态；排队不代表完成。" }));
}

function updateMemorySources(slot) {
  if (!slot.root) return;
  const loading = !!slot.controller, page = slot.page;
  const focused = slot.result.contains(document.activeElement);
  slot.root.setAttribute("aria-busy", String(loading)); slot.filterInput.disabled = loading; slot.refresh.disabled = loading;
  slot.next.disabled = loading || !page?.has_more;
  slot.position.textContent = loading ? "正在读取来源…" : page ? `本页 ${page.items.length} 个来源 · 检查 ${page.examined} 个 · 略过 ${page.omitted} 个 · ${page.has_more ? "仍可继续翻页" : "已到末尾"}` : "尚未读取来源。";
  slot.notice.replaceChildren();
  if (slot.error) slot.notice.append(element("p", { class: "form-error", role: "status", text: slot.error }), element("button", { type: "button", class: "text-button", text: "重试读取", disabled: loading, onClick: () => void loadMemorySources(slot, slot.retry) }));
  slot.receiptRegion.replaceChildren(...(slot.receipt ? [memoryRetryReceipt(slot.receipt)] : []));
  slot.result.replaceChildren();
  if (page && !page.items.length) slot.result.append(element("p", { class: "settings-role-hint", text: page.has_more ? "本窗口没有匹配来源，请继续翻页。" : "本窗口没有匹配来源。" }));
  for (const item of page?.items ?? []) {
    const read = element("button", { type: "button", class: "text-button", text: "核原文", disabled: loading, onClick: () => void showCatalogSource(slot, item, read) });
    const actions = element("div", { class: "row-actions" }, read);
    if (["failed", "deferred"].includes(item.job.state)) {
      const retry = element("button", { type: "button", class: "text-button", text: "重试此来源", disabled: loading, onClick: () => confirmMemorySourceRetry(slot, item, retry) });
      actions.append(retry);
    }
    const details = element("details", { class: "advanced-settings" }, element("summary", { text: "来源详情" }), statusList([["来源", item.source], ["身份", item.principal], ["角色", item.role], ["原会话", item.session_id || "未记录"], ["可信标记", item.trusted_owner ? "主人来源" : "未标为主人来源"]]));
    slot.result.append(element("article", { class: "memory-row" }, element("div", { class: "row-icon" }, icon("memory")), element("div", { class: "row-content" }, element("p", { class: "memory-source-content", text: item.source_id }), element("div", { class: "row-meta" }, element("span", { class: "row-tag", text: memoryJobStates[item.job.state] }), element("time", { text: formatTimestamp(item.created_at) }), `${item.total_bytes} 字节 · 尝试 ${item.job.attempts} 次`), details, item.job.last_error && element("p", { class: "form-error", text: item.job.last_error })), actions));
  }
  if (focused) slot.position.focus({ preventScroll: true });
}

async function loadMemorySources(slot, { cursor = null, filter = slot?.filter ?? "" } = {}) {
  if (!memorySourcesCurrent(slot)) return;
  slot.controller?.abort();
  if (filter !== slot.filter) { slot.page = null; slot.cursor = null; slot.receipt = null; }
  slot.filter = filter; state.memorySourceState = filter; slot.filterInput.value = filter;
  const controller = new AbortController(); slot.controller = controller;
  const owner = connection, current = () => memorySourcesCurrent(slot) && slot.controller === controller && connectionCurrent(owner);
  slot.error = ""; slot.retry = { cursor, filter }; updateMemorySources(slot);
  try {
    const query = new URLSearchParams({ limit: String(memorySourcePageSize) });
    if (cursor) query.set("cursor", cursor);
    if (filter) query.set("state", filter);
    const payload = await request(`/memory/sources?${query}`, { signal: controller.signal, cache: "no-store" });
    if (!current()) return;
    slot.page = memorySourcePage(payload, cursor); slot.cursor = cursor;
  } catch (error) {
    if (current() && !controller.signal.aborted) slot.error = error.message;
  } finally {
    if (current()) { slot.controller = null; updateMemorySources(slot); }
  }
}

function confirmMemorySourceRetry(slot, item, trigger) {
  if (!memorySourceActionCurrent(slot, trigger)) return;
  const content = element("div", {}, element("p", { text: "只重试这一准确来源，保留已有进度。可能向已配置的提炼或向量服务发送正文；未确认保存的请求可能再次收费。排队不代表完成。" }), statusList([["来源 ID", item.source_id], ["来源版本", item.revision], ["目录读取时状态", memoryJobStates[item.job.state]]]));
  openDialog({ title: "重试来源整理", content, submitLabel: "确认重试", onSubmit: async () => {
    const current = () => memorySourceRetry === pending && memorySourceActionCurrent(slot, trigger) && $("#app-dialog").open && $("#dialog-content").firstElementChild === content;
    if (!current()) throw new Error("来源目录已关闭，请重新确认。");
    if (pending.controller) throw new Error("请求已经提交，请等待实际回执。");
    const controller = new AbortController(); pending.controller = controller;
    try {
      const receipt = await request("/memory/sources/retry", { method: "POST", signal: controller.signal, body: JSON.stringify({ source_id: item.source_id, revision: item.revision }) });
      if (!current()) return;
      if (receipt?.source_id !== item.source_id || receipt.revision !== item.revision || !Object.hasOwn(memoryJobStates, receipt.previous_state) || !Object.hasOwn(memoryJobStates, receipt.state) || typeof receipt.accepted !== "boolean" || !["queued", "already_pending", "already_done", "capacity_full"].includes(receipt.reason)) throw new Error("重试回执不完整，请刷新来源状态核查，勿直接重复请求。");
      slot.receipt = receipt; updateMemorySources(slot);
      content.replaceChildren(memoryRetryReceipt(receipt)); dialogSubmit = null;
      $("#dialog-submit").hidden = true;
      $('#dialog-form .dialog-actions [data-action="close-dialog"]').textContent = "关闭";
    } finally { if (memorySourceRetry === pending) pending.controller = null; }
  } });
  const pending = memorySourceRetry = { content, controller: null };
}

async function showCatalogSource(catalog, item, trigger) {
  if (!memorySourceActionCurrent(catalog, trigger)) return;
  const content = element("div", { class: "memory-verification" });
  openDialog({ title: "核查来源原文", content, readOnly: true, returnMemoryId: item.source_id, returnMemoryTrigger: trigger });
  const origin = { ...item, id: item.source_id, digest: item.revision };
  const slot = { id: item.source_id, sourceId: item.source_id, catalogSource: true, content, item: { source_id: item.source_id, origin }, offsets: [0], pageIndex: 0, page: null, editing: false };
  memoryDialog = slot;
  slot.sourceRegion = element("div", { class: "memory-source-page", "aria-live": "polite" });
  content.append(statusList([["来源 ID", item.source_id], ["身份", item.principal], ["角色", item.role], ["来源", item.source], ["可信标记", item.trusted_owner ? "主人来源" : "未标为主人来源"]]), slot.sourceRegion);
  await loadMemorySource(slot, 0, 0);
}

async function searchMemories(query) {
  cancelMemorySearch(); releaseMemoryPath();
  const controller = new AbortController(), version = ++viewRequestVersion;
  memorySearchController = controller; state.memoryQuery = query;
  const current = () => memorySearchController === controller && version === viewRequestVersion && state.view === "memories" && state.memoryTab === "list" && state.auth.authenticated;
  try {
    const payload = await request(`/memories?query=${encodeURIComponent(query)}`, { signal: controller.signal, cache: "no-store" });
    if (!current()) return;
    state.memories = collection(payload); renderOtherView(); $("#memory-search")?.focus();
  } catch (error) { if (current() && !controller.signal.aborted) showError(error); }
  finally { if (memorySearchController === controller) memorySearchController = null; }
}

function memoryGraphPath(item) {
  const via = item?.graph_via, seed = via?.seed;
  const id = (value) => typeof value === "string" && value.length > 0;
  const optionalId = (value) => value === null || id(value);
  if (!via || via.complete !== true || !Number.isSafeInteger(via.revision) || via.revision < 0 || !id(via.seed_id) || !seed || seed.entity_id !== via.seed_id || !id(seed.name) || !id(seed.canonical) || !optionalId(seed.alias) || !optionalId(seed.memory_id) || !optionalId(seed.source_id) || typeof seed.has_more !== "boolean" || !["source", "manual", "direct", "legacy_unknown"].includes(seed.kind) || !Array.isArray(via.steps) || !via.steps.length) return null;
  let from = via.seed_id;
  for (const step of via.steps) {
    if (!step || !id(step.memory_id) || !optionalId(step.source_id) || !id(step.subject_id) || !id(step.object_id) || !id(step.subject) || !id(step.object) || !id(step.predicate) || step.from_id !== from || !["forward", "reverse"].includes(step.direction)) return null;
    const forward = step.direction === "forward";
    if (step.from_id !== (forward ? step.subject_id : step.object_id) || step.to_id !== (forward ? step.object_id : step.subject_id)) return null;
    from = step.to_id;
  }
  return via.steps.at(-1).memory_id === String(item.id) ? via : null;
}

function memoryPathCurrent(slot) {
  return memoryPath === slot && state.view === "memories" && state.memoryTab === "list" && state.memoryQuery === slot.query && $("#other-view").contains(slot.region) && !slot.region.hidden;
}

function releaseMemoryPath() {
  const slot = memoryPath; memoryPath = null;
  if (!slot) return;
  slot.controller?.abort(); slot.controller = null; slot.via = null;
  slot.region.replaceChildren(); slot.region.hidden = true; slot.button.setAttribute("aria-expanded", "false");
}

function memoryPathNotice(slot, message) {
  if (!memoryPathCurrent(slot)) return;
  const focused = slot.region.contains(document.activeElement);
  const retry = element("button", { type: "button", class: "text-button", text: "重新检索", onClick: () => void loadMemoryPath(slot) });
  slot.region.replaceChildren(element("p", { role: "status", text: message }), retry);
  if (focused && !$("#app-dialog").open) retry.focus({ preventScroll: true });
}

function invalidateMemoryPath() {
  const slot = memoryPath;
  if (!slot) return;
  if (!memoryPathCurrent(slot)) { releaseMemoryPath(); return; }
  slot.controller?.abort(); slot.controller = null; slot.via = null;
  memoryPathNotice(slot, "关系已有变更，请重新检索。");
}

async function toggleMemoryPath(id, button, region, query) {
  const root = $("#other-view");
  if (state.view !== "memories" || state.memoryTab !== "list" || state.memoryQuery !== query || !root.contains(button) || !root.contains(region)) return;
  if (memoryPath?.button === button) { releaseMemoryPath(); button.focus({ preventScroll: true }); return; }
  releaseMemoryPath();
  const slot = { id: String(id), query, button, region, controller: null, via: null };
  memoryPath = slot; region.hidden = false; button.setAttribute("aria-expanded", "true");
  await loadMemoryPath(slot);
}

async function loadMemoryPath(slot) {
  if (!memoryPathCurrent(slot)) return;
  slot.controller?.abort(); slot.via = null;
  const controller = new AbortController(); slot.controller = controller;
  if (slot.region.contains(document.activeElement)) slot.button.focus({ preventScroll: true });
  slot.region.replaceChildren(element("p", { role: "status", text: "正在读取路径…" }));
  try {
    const payload = await request(`/memories?query=${encodeURIComponent(slot.query)}`, { signal: controller.signal, cache: "no-store" });
    if (!memoryPathCurrent(slot) || slot.controller !== controller) return;
    const item = collection(payload).find((item) => String(item.id) === slot.id), via = memoryGraphPath(item);
    if (item?.path_omitted === true) { memoryPathNotice(slot, "召回路径因预算省略，请重新检索。"); return; }
    if (!via || (payload.revision !== undefined && payload.revision !== via.revision)) { memoryPathNotice(slot, "当前未返回该路径，请重新检索。"); return; }
    slot.via = via; renderMemoryPath(slot, payload.truncated === true);
  } catch (error) {
    if (memoryPathCurrent(slot) && slot.controller === controller && !controller.signal.aborted) memoryPathNotice(slot, error.status === 404 ? "路径已不可用，请重新检索。" : error.status === 409 ? "关系已有变更，请重新检索。" : error.message);
  } finally { if (slot.controller === controller) slot.controller = null; }
}

function renderMemoryPath(slot, truncated) {
  const via = slot.via, seed = via.seed;
  const label = { direct: "规范名称", source: "原始登记", manual: "手工登记", legacy_unknown: "旧索引，依据未登记" }[seed.kind];
  const proofButton = (id, key, text) => actionButton(text, "memory-evidence", "text-button", undefined, { id, memoryPathRef: key });
  const origin = element("div", { class: "memory-path-origin" }, element("span", { class: "memory-path-triple", text: seed.alias ? `${seed.alias} → ${seed.name}` : seed.name }), element("span", { class: "memory-path-note", text: label }));
  if (seed.kind !== "direct" && seed.memory_id) origin.append(proofButton(seed.memory_id, "seed", "核查登记"));
  if (seed.has_more) origin.append(element("span", { class: "memory-path-note", text: "还有登记未展示" }));
  const steps = element("ol", { class: "memory-path-steps" });
  for (const [index, step] of via.steps.entries()) {
    const forward = step.direction === "forward", from = forward ? step.subject : step.object, to = forward ? step.object : step.subject;
    steps.append(element("li", {}, element("div", { class: "memory-path-step" }, element("span", { class: "memory-path-triple", text: `${from} ${forward ? "—" : "←"}${step.predicate}${forward ? "→" : "—"} ${to}` }), proofButton(step.memory_id, `step-${index}`, "核查"))));
  }
  slot.region.replaceChildren(element("p", { class: "memory-path-title", text: "本次检索路径" }), origin, steps);
  if (truncated) slot.region.append(element("span", { class: "memory-path-note", text: "部分检索结果" }));
}

const MEMORY_KINDS = { observation: "观察", preference: "偏好", fact: "事实", procedure: "流程", summary: "摘要", identity: "身份", episodic: "情景", semantic: "语义事实", procedural: "流程经验" };
function memoryKind(kind) { return Object.hasOwn(MEMORY_KINDS, kind) ? MEMORY_KINDS[kind] : kind ?? MEMORY_KINDS.observation; }
function memoryNeedsReview(item) { return item.status === "conflict" || !!item.conflicts?.length; }

function memoryEditor(item) {
  const relations = element("div", { id: "memory-relations", class: "memory-relations-editor" });
  for (const relation of item?.relations ?? []) relations.append(relationEditor(relation));
  const kind = item?.kind ?? "observation", choices = Object.entries(MEMORY_KINDS);
  if (!Object.hasOwn(MEMORY_KINDS, kind)) choices.push([kind, `当前类型：${kind}`]);
  return element("div", {}, field("内容", "content", item?.content ?? "", { type: "textarea", rows: 5, required: true }), selectField("类型", "kind", kind, choices), field("关联实体", "entities", (item?.entities ?? []).join(", "), { placeholder: "用逗号分隔" }), element("details", { class: "advanced-settings", open: !!item?.relations?.length }, element("summary", { text: "关系与来源标签" }), element("label", { class: "field" }, element("span", { text: "关系" })), relations, actionButton("添加关系", "add-memory-relation", "secondary-button", "plus"), element("div", { class: "field-row field-top-space" }, field("来源标签", "source", item?.source ?? "user"), field("置信度", "confidence", item?.confidence ?? 1, { type: "number", min: 0, max: 1, step: .05 }))));
}

async function saveMemory(item, form) {
  const data = new FormData(form);
  const edges = $$(".relation-editor", form).map((row) => Object.fromEntries(["subject", "predicate", "object"].map((key) => [key, $(`[name=relation-${key}]`, row).value.trim()])));
  if (edges.some((edge) => !edge.subject || !edge.predicate || !edge.object)) throw new Error("请填写完整关系，或移除空白关系。");
  const payload = { content: String(data.get("content")).trim(), kind: data.get("kind"), entities: String(data.get("entities")).split(/[,，]/).map((entry) => entry.trim()).filter(Boolean), relations: edges, source: String(data.get("source")).trim() || "user", confidence: Number(data.get("confidence")) };
  if (!payload.content) throw new Error("请输入记忆内容。");
  await request(item ? `/memories/${encodeURIComponent(item.id)}` : "/memories", { method: item ? "PATCH" : "POST", body: payload });
  toast(item ? "记忆已更新" : "记忆已保存");
}

async function editMemory(id) {
  if (id) return showMemoryEvidence(id, { editing: true });
  openDialog({ title: "新增记忆", content: memoryEditor(), onSubmit: async (form) => { await saveMemory(null, form); await switchView("memories"); } });
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
  const root = element("div", {}, element("div", { class: "graph-toolbar" }, element("label", {}, "实体", choose), element("span", { text: `${graph.nodes.length} 个实体 · ${graph.edges.length} 条关系${graph.truncated ? " · 部分关系" : ""}` })), element("div", { class: "memory-graph" }, svg), element("div", { class: "list-caption" }, "关系与证据", `${edges.length} 条${edges.length > displayed.length ? ` · 图中显示 ${displayed.length} 条` : ""}`));
  for (const edge of edges) root.append(element("div", { class: "graph-evidence-row" }, element("span", { class: "graph-evidence-triple" }, element("strong", { text: edge.subject }), element("span", { text: edge.predicate }), element("strong", { text: edge.object })), actionButton("查看来源", "memory-evidence", "text-button", "link", { id: edge.memory_id })));
  return root;
}

function resetMemoryReads(slot) {
  for (const key of ["readController", "sourceController", "historyController"]) { slot[key]?.abort(); slot[key] = null; }
  slot.sourceRegion?.replaceChildren(); slot.historyList?.replaceChildren();
  slot.page = null; slot.offsets = [0]; slot.pageIndex = 0;
}

function releaseMemoryDialog() {
  const slot = memoryDialog;
  memoryDialog = null;
  if (slot) { resetMemoryReads(slot); slot.item = null; slot.content.replaceChildren(); dialogSubmit = null; }
}

function memoryDialogCurrent(slot) {
  return memoryDialog === slot && $("#app-dialog").open && $("#dialog-content").firstElementChild === slot.content;
}

function memoryUnavailable(slot, message) {
  if (!memoryDialogCurrent(slot)) return;
  resetMemoryReads(slot); slot.item = null; slot.editing = false;
  dialogSubmit = null; $("#dialog-submit").hidden = true;
  $('#dialog-form .dialog-actions [data-action="close-dialog"]').textContent = "关闭";
  slot.content.replaceChildren(element("p", { role: "status", text: message }), element("button", { type: "button", class: "secondary-button", text: slot.catalogSource ? "刷新来源目录" : "重新核查", onClick: () => {
    if (slot.catalogSource) { $("#app-dialog").close(); void loadMemorySources(memorySources); }
    else void refreshMemoryEvidence(slot);
  } }));
}

function invalidateMemoryEvidence(event) {
  const slot = memoryDialog;
  if (!slot || !memoryDialogCurrent(slot)) return;
  const data = event.data ?? {};
  const sourceUnresolved = slot.sourceId === undefined && !!slot.readController;
  if (!(data.memory_ids ?? []).includes(slot.id) && !(data.source_id && (data.source_id === slot.sourceId || sourceUnresolved))) return;
  resetMemoryReads(slot);
  if (slot.editing) {
    slot.invalidated = true; $("#dialog-submit").disabled = true;
    slot.editorNotice.replaceChildren(element("span", { text: "记忆已有变更，请重新核查。" }), element("button", { type: "button", class: "text-button", text: "重新核查", onClick: () => void refreshMemoryEvidence(slot) }));
    slot.editorNotice.hidden = false;
  } else memoryUnavailable(slot, event.type === "memory.forgotten" ? "原文已不可用。" : "记忆已有更新，请重新核查。");
}

async function showMemoryEvidence(id, { editing = false, trigger = null } = {}) {
  const content = element("div", { class: "memory-verification" });
  openDialog({ title: editing ? "更正记忆" : "核查记忆", content, readOnly: true, returnMemoryId: String(id), returnMemoryTrigger: trigger });
  const slot = { id: String(id), content, item: null, sourceId: state.memories.find((item) => String(item.id) === String(id))?.source_id, offsets: [0], pageIndex: 0, page: null, editing: false };
  memoryDialog = slot;
  await refreshMemoryEvidence(slot, editing);
}

async function refreshMemoryEvidence(slot, editing = false) {
  if (!memoryDialogCurrent(slot)) return;
  resetMemoryReads(slot); slot.item = null; slot.editing = false; slot.invalidated = false;
  dialogSubmit = null; $("#dialog-submit").hidden = true; $("#dialog-submit").disabled = false;
  $("#dialog-error").hidden = true;
  $('#dialog-form .dialog-actions [data-action="close-dialog"]').textContent = "关闭";
  slot.content.replaceChildren(element("p", { role: "status", text: "正在核查…" }));
  const controller = new AbortController(); slot.readController = controller;
  try {
    const item = await request(`/memories/${encodeURIComponent(slot.id)}`, { signal: controller.signal, cache: "no-store" });
    if (!memoryDialogCurrent(slot) || slot.readController !== controller) return;
    if (String(item?.id) !== slot.id) throw new Error("记忆响应不一致，请重新核查。");
    slot.item = item; slot.sourceId = item.source_id;
    if (editing) renderMemoryCorrection(slot);
    else renderMemoryEvidence(slot);
  } catch (error) {
    if (memoryDialogCurrent(slot) && slot.readController === controller) memoryUnavailable(slot, error.status === 404 ? "记忆已不可用。" : error.message);
  } finally { if (slot.readController === controller) slot.readController = null; }
}

function renderMemoryCorrection(slot) {
  const item = slot.item;
  slot.editing = true;
  $("#dialog-title").textContent = "更正记忆";
  slot.editorNotice = element("div", { class: "memory-editor-notice", role: "status", hidden: true });
  slot.content.replaceChildren(slot.editorNotice, memoryEditor(item));
  const submit = $("#dialog-submit"); submit.hidden = false; submit.disabled = false; submit.textContent = "保存更正";
  $('#dialog-form .dialog-actions [data-action="close-dialog"]').textContent = "取消";
  dialogSubmit = async (form) => {
    if (!memoryDialogCurrent(slot) || slot.invalidated) throw new Error("记忆已有变更，请重新核查。");
    await saveMemory(item, form);
    if (!memoryDialogCurrent(slot)) return;
    await switchView("memories");
    if (memoryDialogCurrent(slot)) {
      await refreshMemoryEvidence(slot);
      if (memoryDialogCurrent(slot) && slot.item) {
        const heading = $("#dialog-title"); heading.tabIndex = -1; heading.focus({ preventScroll: true });
      }
    }
  };
  $("textarea", slot.content)?.focus({ preventScroll: true });
}

function renderMemoryEvidence(slot) {
  const item = slot.item;
  $("#dialog-title").textContent = "核查记忆";
  const status = { active: "生效中", conflict: "待核对", superseded: "已替代", forgotten: "已遗忘" }[item.status] ?? item.status ?? "未记录";
  const trusted = (value) => value === true ? "主人来源" : value === false ? "未标为主人来源" : "未记录";
  slot.content.replaceChildren(element("p", { class: "memory-evidence-content memory-current-content", text: item.content }), statusList([["记忆 ID", item.id], ["记忆版本", Number.isSafeInteger(item.version) ? String(item.version) : "未记录"], ["层次", { identity: "身份", preference: "偏好", episodic: "经历", semantic: "事实", procedural: "流程" }[item.layer] ?? item.layer ?? "未记录"], ["类型", memoryKind(item.kind)], ["状态", status], ["来源标签", item.source], ["记忆可信标记", trusted(item.trusted_owner)], ["置信度", Number.isFinite(item.confidence) ? `${Math.round(item.confidence * 100)}%` : "未记录"], ["更新", formatTimestamp(item.updated_at)]]));
  if (item.quote) slot.content.append(element("section", { class: "memory-original-quote" }, element("h3", { text: "原始引文" }), element("blockquote", { text: item.quote })));
  const origin = item.origin;
  if (origin) {
    slot.content.append(element("h3", { class: "memory-section-title", text: "原始来源" }), statusList([["身份", origin.principal ?? "未记录"], ["角色", origin.role ?? "未记录"], ["渠道", origin.source ?? "未记录"], ["来源可信标记", trusted(origin.trusted_owner)], ["时间", formatTimestamp(origin.created_at)]]));
    slot.sourceRegion = element("div", { class: "memory-source-page", "aria-live": "polite" });
    const source = element("details", { class: "advanced-settings memory-source" }, element("summary", { text: "查看原文" }), slot.sourceRegion);
    source.addEventListener("toggle", () => { if (slot.content.contains(source) && source.open && !slot.page && !slot.sourceController) void loadMemorySource(slot, 0, 0); });
    slot.content.append(source);
  } else slot.content.append(element("p", { class: "settings-role-hint", text: item.source_id ? "原文已不可用。" : "无关联原文。" }));
  if (item.conflicts?.length) slot.content.append(element("div", { class: "memory-conflicts" }, element("span", { text: "待核对" }), item.conflicts.map((id) => actionButton("核查另一条", "memory-evidence", "text-button", undefined, { id }))));
  if (item.relations?.length) slot.content.append(element("details", { class: "advanced-settings" }, element("summary", { text: `${item.relations.length} 条关系` }), element("div", { class: "memory-evidence-relations" }, item.relations.map((edge) => element("div", { class: "graph-evidence-triple" }, element("strong", { text: edge.subject }), element("span", { text: edge.predicate }), element("strong", { text: edge.object }))))));
  slot.historyList = element("div", { class: "memory-history-list" });
  const history = element("details", { class: "advanced-settings memory-history" }, element("summary", { text: "修改历史" }), slot.historyList);
  slot.historyLoaded = false;
  history.addEventListener("toggle", () => { if (slot.content.contains(history) && history.open && !slot.historyLoaded && !slot.historyController) void loadMemoryHistory(slot); });
  slot.content.append(history, element("div", { class: "memory-evidence-actions" }, element("button", { type: "button", class: "secondary-button", text: "更正", onClick: () => void refreshMemoryEvidence(slot, true) })));
}

function validMemorySource(page, slot, offset) {
  const origin = slot.item.origin;
  return (slot.catalogSource ? page?.memory_id === undefined : page?.memory_id === slot.id) && page?.source_id === slot.item.source_id && page.source_id === origin.id && page.revision === origin.digest && page.field === "content" && typeof page.content === "string"
    && [page.offset_bytes, page.length_bytes, page.next_offset_bytes, page.total_bytes].every((value) => Number.isSafeInteger(value) && value >= 0)
    && page.offset_bytes === offset && page.length_bytes === new TextEncoder().encode(page.content).length && page.next_offset_bytes === offset + page.length_bytes
    && typeof page.has_more === "boolean" && page.has_more === (page.next_offset_bytes < page.total_bytes) && page.next_offset_bytes <= page.total_bytes && (!page.has_more || page.length_bytes > 0);
}

async function loadMemorySource(slot, offset, index) {
  if (!memoryDialogCurrent(slot) || slot.editing || slot.sourceController || !slot.item?.origin) return;
  const origin = slot.item.origin;
  if (!origin.digest || origin.id !== slot.item.source_id) { memoryUnavailable(slot, "原文关联已变化，请重新核查。"); return; }
  const controller = new AbortController(); slot.sourceController = controller;
  slot.sourceError = ""; slot.sourceRetry = { offset, index }; renderMemorySource(slot);
  try {
    const query = new URLSearchParams({ offset_bytes: String(offset), revision: origin.digest });
    if (slot.catalogSource) query.set("id", slot.sourceId);
    const path = slot.catalogSource ? `/memory/source?${query}` : `/memories/${encodeURIComponent(slot.id)}/source?${query}`;
    const page = await request(path, { signal: controller.signal, cache: "no-store" });
    if (!memoryDialogCurrent(slot) || slot.sourceController !== controller) return;
    if (!validMemorySource(page, slot, offset)) { memoryUnavailable(slot, "原文响应不一致，请重新核查。"); return; }
    slot.page = page; slot.offsets = [...slot.offsets.slice(0, index), offset]; slot.pageIndex = index;
  } catch (error) {
    if (!memoryDialogCurrent(slot) || slot.sourceController !== controller) return;
    if ([404, 409].includes(error.status)) { memoryUnavailable(slot, error.status === 404 ? "原文已不可用。" : "原文已更新，请重新核查。"); return; }
    slot.sourceError = error.message;
  } finally {
    if (memoryDialogCurrent(slot) && slot.sourceController === controller) { slot.sourceController = null; renderMemorySource(slot); }
  }
}

function renderMemorySource(slot) {
  const page = slot.page;
  const focused = slot.sourceRegion.contains(document.activeElement);
  slot.sourceRegion.setAttribute("aria-busy", String(!!slot.sourceController));
  slot.sourceRegion.replaceChildren();
  if (page) {
    slot.sourceRegion.append(element("p", { class: "memory-source-content", text: page.content }));
    const previous = element("button", { type: "button", class: "text-button", text: "上一段", disabled: !!slot.sourceController || slot.pageIndex === 0, onClick: () => void loadMemorySource(slot, slot.offsets[slot.pageIndex - 1], slot.pageIndex - 1) });
    const next = element("button", { type: "button", class: "text-button", text: "下一段", disabled: !!slot.sourceController || !page.has_more, onClick: () => void loadMemorySource(slot, page.next_offset_bytes, slot.pageIndex + 1) });
    const position = element("span", { class: "memory-source-position", tabIndex: -1, text: `第 ${slot.pageIndex + 1} 段 · ${page.has_more ? "尚有后文" : "已到末尾"}` });
    slot.sourceRegion.append(element("nav", { class: "memory-source-navigation", "aria-label": "原文分段" }, previous, position, next));
    if (focused) position.focus({ preventScroll: true });
  }
  if (slot.sourceController) slot.sourceRegion.append(element("p", { class: "settings-role-hint", role: "status", text: "正在读取…" }));
  if (slot.sourceError) slot.sourceRegion.append(element("p", { class: "form-error", text: slot.sourceError }), element("button", { type: "button", class: "text-button", text: "重试", onClick: () => void loadMemorySource(slot, slot.sourceRetry.offset, slot.sourceRetry.index) }));
}

async function loadMemoryHistory(slot) {
  if (!memoryDialogCurrent(slot) || slot.editing || slot.historyController) return;
  const controller = new AbortController(); slot.historyController = controller;
  slot.historyList.replaceChildren(element("p", { class: "settings-role-hint", text: "正在读取…" }));
  try {
    const revisions = collection(await request(`/memories/${encodeURIComponent(slot.id)}/revisions`, { signal: controller.signal, cache: "no-store" }));
    if (!memoryDialogCurrent(slot) || slot.historyController !== controller) return;
    slot.historyList.replaceChildren();
    if (!revisions.length) slot.historyList.append(element("p", { class: "settings-role-hint", text: "暂无修改记录。" }));
    for (const revision of revisions) {
      const snapshot = revision.snapshot ?? {};
      slot.historyList.append(element("article", { class: "memory-revision" }, element("time", { text: formatFullDate(revision.changed_at) }), element("p", { class: "memory-evidence-content", text: snapshot.content ?? "" }), element("div", { class: "row-meta" }, memoryKind(snapshot.kind), snapshot.source), (snapshot.relations ?? []).map((edge) => element("div", { class: "graph-evidence-triple" }, element("strong", { text: edge.subject }), element("span", { text: edge.predicate }), element("strong", { text: edge.object })))));
    }
    slot.historyLoaded = true;
  } catch (error) {
    if (!memoryDialogCurrent(slot) || slot.historyController !== controller) return;
    if ([404, 409].includes(error.status)) { memoryUnavailable(slot, "记忆已变化，请重新核查。"); return; }
    slot.historyList.replaceChildren(element("p", { class: "form-error", text: error.message }), element("button", { type: "button", class: "text-button", text: "重试", onClick: () => void loadMemoryHistory(slot) }));
  } finally { if (slot.historyController === controller) slot.historyController = null; }
}

function workflowActivityCurrent(slot) {
  return !!slot && workflowActivity === slot && state.view === "workflows" && state.auth.authenticated;
}

function releaseWorkflowActivity() {
  const slot = workflowActivity; workflowActivity = null;
  slot?.controller?.abort(); slot?.definitionsController.abort(); slot?.schedulesController?.abort();
  if (slot) { slot.page = null; slot.schedulesPage = null; slot.schedulesStopping.clear(); }
  $("#workflow-active-runs")?.replaceChildren();
  $("#workflow-schedules")?.replaceChildren();
}

function workflowActivityPage(payload, after) {
  const statuses = ["pending", "queued", "running"];
  if (!payload || !Array.isArray(payload.items) || payload.items.length > workflowPageSize || typeof payload.has_more !== "boolean") throw new Error("活动运行响应不完整，请重试。");
  const seen = new Set();
  const items = payload.items.map((run) => {
    if (!run || typeof run.id !== "string" || !run.id || run.id === after || seen.has(run.id) || typeof run.workflow_id !== "string" || !run.workflow_id || typeof run.name !== "string" || !statuses.includes(run.status)) throw new Error("活动运行响应不完整，请重试。");
    seen.add(run.id);
    return { id: run.id, workflow_id: run.workflow_id, name: run.name, status: run.status, created_at: run.created_at, updated_at: run.updated_at };
  });
  if (payload.has_more ? !items.length || payload.next_after !== items.at(-1).id || payload.next_after === after : payload.next_after !== null) throw new Error("活动运行翻页位置无效，请重新读取。");
  return { items, has_more: payload.has_more, next_after: payload.next_after };
}

async function loadWorkflowActivity(slot, after = "", starts = [""], owner = connection) {
  if (!workflowActivityCurrent(slot) || !connectionCurrent(owner)) return;
  slot.controller?.abort();
  const controller = slot.controller = new AbortController();
  slot.readOwner = owner;
  slot.loading = true; slot.error = ""; slot.refreshPending = false;
  renderOtherView();
  const current = () => connectionCurrent(owner) && workflowActivityCurrent(slot) && slot.controller === controller && !controller.signal.aborted;
  try {
    const payload = await ownedRequest(`/workflow-runs/active?${new URLSearchParams({ after_id: after, limit: workflowPageSize })}`, { signal: controller.signal, cache: "no-store" }, owner);
    if (!current()) return;
    const page = workflowActivityPage(payload, after);
    // A moving page may become empty. Return to the head once, without following has_more.
    if (after && !page.items.length) { await loadWorkflowActivity(slot, "", [""], owner); return; }
    slot.page = page; slot.after = after; slot.starts = [...starts];
  } catch (error) {
    if (current()) slot.error = error.message;
  } finally {
    if (connectionCurrent(owner) && workflowActivityCurrent(slot) && slot.controller === controller) {
      slot.controller = null; slot.loading = false;
      if (slot.refreshPending) void loadWorkflowActivity(slot, "", [""], owner);
      else renderOtherView();
    }
  }
}

function refreshWorkflowActivity(owner = connection) {
  const slot = workflowActivity;
  if (!workflowActivityCurrent(slot) || !connectionCurrent(owner)) return;
  if (slot.controller && slot.readOwner === owner) { slot.refreshPending = true; slot.controller.abort(); }
  else void loadWorkflowActivity(slot, "", [""], owner);
}

function captureWorkflowView(root) {
  if (!$("#workflow-active-runs", root)) return null;
  const focused = document.activeElement;
  const inside = root.contains(focused);
  return {
    inside, action: inside ? focused?.dataset?.action : null,
    id: focused?.dataset?.id, direction: focused?.dataset?.direction,
    heading: inside ? focused?.getAttribute("id") : null,
    summary: inside && focused?.tagName === "SUMMARY" ? focused.closest("[data-workflow-history-run]")?.dataset.workflowHistoryRun : null,
    open: $$("[data-workflow-history-run]", root).filter((node) => node.open).map((node) => node.dataset.workflowHistoryRun),
  };
}

function restoreWorkflowView(root, saved) {
  if (!saved) return;
  const histories = $$("[data-workflow-history-run]", root);
  for (const details of histories) details.open = saved.open.includes(details.dataset.workflowHistoryRun);
  if (!saved.inside) return;
  const button = saved.action && $$("[data-action]", root).find((node) => node.dataset.action === saved.action && node.dataset.id === saved.id && node.dataset.direction === saved.direction);
  const summary = saved.summary && histories.find((node) => node.dataset.workflowHistoryRun === saved.summary)?.querySelector("summary");
  const heading = saved.heading && $$("[id]", root).find((node) => node.getAttribute("id") === saved.heading);
  const target = button && !button.disabled ? button : summary || heading || $("#workflow-active-heading", root);
  target?.focus({ preventScroll: true });
}

function renderWorkflowActivity() {
  const slot = workflowActivity;
  const refresh = actionButton("刷新", "refresh-workflow-runs", "text-button", "refresh");
  refresh.disabled = !slot || slot.loading;
  const root = element("section", { id: "workflow-active-runs", class: "workflow-activity", "aria-labelledby": "workflow-active-heading", "aria-busy": String(!!slot?.loading) }, element("div", { class: "workflow-section-heading" }, element("h3", { id: "workflow-active-heading", tabIndex: -1, text: "进行中" }), refresh));
  if (!slot) return root;
  if (slot.loading) root.append(element("p", { class: "workflow-list-status", role: "status", text: "正在读取活动运行…" }));
  if (slot.error) root.append(element("p", { class: "form-error", role: "alert", text: slot.error }));
  if (slot.page) {
    for (const run of slot.page.items) {
      const labels = { pending: "等待中", queued: "排队中", running: "运行中" };
      const stop = actionButton(workflowStopping.has(run.id) ? "正在提交…" : "停止", "cancel-workflow-run", "secondary-button", "stop", { id: run.id });
      stop.disabled = workflowStopping.has(run.id);
      stop.setAttribute("aria-label", `停止 ${run.name}（${run.id}）`);
      root.append(element("article", { class: "workflow-active-row" }, element("div", { class: "row-content" }, element("h4", { text: run.name }), element("code", { class: "workflow-run-id", text: run.id }), element("div", { class: "row-meta" }, labels[run.status], element("time", { text: formatTimestamp(run.updated_at ?? run.created_at) }))), element("div", { class: "row-actions" }, stop)));
    }
    if (!slot.page.items.length && !slot.loading && !slot.error) root.append(element("p", { class: "workflow-list-status", text: "没有进行中的工作流" }));
    if (slot.page.items.length) {
      const previous = actionButton("上一页", "page-workflow-runs", "text-button", undefined, { direction: "previous" });
      const next = actionButton("下一页", "page-workflow-runs", "text-button", undefined, { direction: "next" });
      previous.disabled = slot.loading || slot.starts.length < 2; next.disabled = slot.loading || !slot.page.has_more;
      root.append(element("nav", { class: "workflow-pagination", "aria-label": "活动运行分页" }, element("span", { text: `第 ${slot.starts.length} 页 · 本页 ${slot.page.items.length} 项${slot.page.has_more ? " · 有后续" : ""}` }), element("div", {}, previous, next)));
    }
  }
  return root;
}

async function loadWorkflowSchedules(slot, after = "", starts = [""]) {
  const owner = connection;
  if (!workflowActivityCurrent(slot) || !slot.schedulesOpen || !connectionCurrent(owner)) return;
  slot.schedulesController?.abort();
  const controller = slot.schedulesController = new AbortController();
  slot.schedulesLoading = true; slot.schedulesError = ""; renderOtherView();
  const current = () => workflowActivityCurrent(slot) && slot.schedulesOpen && connectionCurrent(owner) && slot.schedulesController === controller && !controller.signal.aborted;
  try {
    const payload = await ownedRequest(`/workflow-schedules?${new URLSearchParams({ after_id: after, limit: workflowPageSize })}`, { signal: controller.signal, cache: "no-store" }, owner);
    if (!current()) return;
    if (!payload || !Array.isArray(payload.items) || payload.items.length > workflowPageSize || typeof payload.has_more !== "boolean") throw new Error("定时委托响应不完整，请重试。");
    const seen = new Set();
    const items = payload.items.map((item) => {
      if (!item || typeof item.id !== "string" || !item.id || item.id === after || seen.has(item.id) || typeof item.name !== "string" || typeof item.enabled !== "boolean" || !(item.due_at === null || (Number.isFinite(item.due_at) && item.due_at > 0)) || !(item.last_run_id === null || (typeof item.last_run_id === "string" && item.last_run_id)) || !(item.last_status === null || ["pending", "queued", "running", "completed", "failed", "cancelled", "interrupted", "timed_out"].includes(item.last_status))) throw new Error("定时委托响应不完整，请重试。");
      seen.add(item.id);
      if (typeof item.blocked !== "boolean") throw new Error("定时委托响应不完整，请重试。");
      return { id: item.id, name: item.name, enabled: item.enabled, blocked: item.blocked, due_at: item.due_at, interval_seconds: item.interval_seconds, last_run_id: item.last_run_id, last_status: item.last_status };
    });
    if (payload.has_more ? !items.length || payload.next_after !== items.at(-1).id : payload.next_after !== null) throw new Error("定时委托翻页位置无效，请重新读取。");
    slot.schedulesPage = { items, has_more: payload.has_more, next_after: payload.next_after };
    slot.schedulesAfter = after; slot.schedulesStarts = [...starts];
  } catch (error) { if (current()) slot.schedulesError = error.message; }
  finally {
    if (workflowActivityCurrent(slot) && slot.schedulesController === controller) {
      slot.schedulesController = null; slot.schedulesLoading = false; renderOtherView();
    }
  }
}

function renderWorkflowSchedules() {
  const slot = workflowActivity;
  const root = element("section", { id: "workflow-schedules", hidden: !slot?.schedulesOpen });
  if (!slot?.schedulesOpen) return root;
  const refresh = actionButton("刷新", "refresh-workflow-schedules", "text-button", "refresh");
  refresh.disabled = slot.schedulesLoading;
  root.append(element("div", { class: "list-caption" }, "定时委托", refresh));
  if (slot.schedulesLoading) root.append(element("p", { class: "workflow-list-status", role: "status", text: "正在读取…" }));
  if (slot.schedulesError) root.append(element("p", { class: "form-error", role: "alert", text: slot.schedulesError }));
  if (!slot.schedulesLoading && !slot.schedulesError && !slot.schedulesPage?.items.length) root.append(element("p", { class: "workflow-list-status", text: "暂无定时委托。可在对话中请沐璃安排。" }));
  for (const item of slot.schedulesPage?.items ?? []) {
    const status = !item.enabled ? "已停用" : item.blocked ? "暂缓执行" : item.due_at === null ? "无后续计划" : "等待下次执行";
    const recent = { pending: "等待中", queued: "排队中", running: "运行中", completed: "已完成", failed: "失败", cancelled: "已取消", interrupted: "已中断", timed_out: "超时" };
    const content = element("div", { class: "row-content" }, element("h4", { text: item.name }), element("code", { class: "workflow-run-id", text: item.id }), element("div", { class: "row-meta" }, status, item.due_at !== null && element("time", { text: formatTimestamp(item.due_at) })));
    if (item.last_run_id) content.append(element("div", { class: "row-meta" }, `最近执行：${recent[item.last_status] ?? "状态未记录"}`, element("code", { text: item.last_run_id })));
    const actions = element("div", { class: "row-actions" });
    if (item.enabled) {
      const cancel = actionButton(slot.schedulesStopping.has(item.id) ? "正在提交…" : "取消委托", "cancel-workflow-schedule", "secondary-button", "stop", { id: item.id });
      cancel.disabled = slot.schedulesLoading || slot.schedulesStopping.has(item.id); actions.append(cancel);
    }
    root.append(element("article", { class: "workflow-active-row" }, content, actions));
  }
  if (slot.schedulesStarts.length > 1 || slot.schedulesPage?.has_more) {
    const previous = actionButton("上一页", "page-workflow-schedules", "text-button", undefined, { direction: "previous" });
    const next = actionButton("下一页", "page-workflow-schedules", "text-button", undefined, { direction: "next" });
    previous.disabled = slot.schedulesLoading || slot.schedulesStarts.length <= 1;
    next.disabled = slot.schedulesLoading || !slot.schedulesPage?.has_more;
    root.append(element("div", { class: "row-actions" }, previous, next));
  }
  return root;
}

function renderWorkflows() {
  const schedules = actionButton("定时委托", "toggle-workflow-schedules", "secondary-button");
  schedules.setAttribute("aria-expanded", String(!!workflowActivity?.schedulesOpen));
  schedules.setAttribute("aria-controls", "workflow-schedules");
  const root = element("div", { class: "view-content" }, viewHeading("工作流", "把重复任务整理成可运行的步骤。", element("div", { class: "view-heading-actions" }, schedules, actionButton("从模板开始", "workflow-templates", "secondary-button", "sparkles"), actionButton("新建工作流", "new-workflow", "primary-button", "plus"))), renderWorkflowActivity(), renderWorkflowSchedules(), element("div", { class: "list-caption" }, "最近定义", `${state.workflows.length} 个工作流`));
  if (workflowActivity?.definitionsLoading) root.append(element("p", { class: "workflow-list-status", text: "正在读取定义…" }));
  else if (workflowActivity?.definitionsError) root.append(element("p", { class: "form-error", role: "alert", text: workflowActivity.definitionsError }), actionButton("重试读取定义", "retry-view", "text-button", "refresh"));
  else if (!state.workflows.length) root.append(emptyState("workflow", "还没有工作流", "创建一个流程，按步骤协作完成任务。"));
  for (const workflow of state.workflows) {
    const steps = element("div", { class: "workflow-steps" });
    for (const [index, step] of (workflow.steps ?? []).entries()) steps.append(element("span", { class: "workflow-step-label" }, element("span", { class: "workflow-step-number", text: index + 1 }), step.title || step.name || step.id));
    const content = element("div", { class: "row-content" }, element("h3", { text: workflow.name }), workflow.description && element("p", { text: workflow.description }), steps, element("div", { class: "row-meta" }, `${workflow.steps?.length ?? 0} 个步骤`, `${MODES[workflow.mode]?.label ?? "Auto"} 模式`, element("time", { text: formatTimestamp(workflow.updated_at ?? workflow.created_at) })));
    const latest = workflow.runs?.[0];
    if (latest) content.append(renderWorkflowRun(latest));
    root.append(element("article", { class: "workflow-row" }, element("div", { class: "row-icon" }, icon("workflow")), content, element("div", { class: "row-actions" }, actionButton("运行", "run-workflow", "secondary-button", "play", { id: workflow.id }), iconButton("删除工作流", "delete-workflow", "trash", { id: workflow.id }))));
  }
  return root;
}

function renderWorkflowRun(run) {
  const labels = { pending: "等待中", queued: "排队中", running: "运行中", completed: "已完成", failed: "失败", cancelled: "已取消", timed_out: "已超时", skipped: "已跳过", interrupted: "已中断" };
  const details = element("details", { class: `workflow-run ${run.status}`, dataset: { workflowHistoryRun: run.id } }, element("summary", {}, element("span", { class: "status-dot", "aria-hidden": "true" }), `最近运行 · ${labels[run.status] ?? run.status}`, element("time", { text: formatTimestamp(run.updated_at ?? run.created_at) })));
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
    toast("工作流已开始"); refreshWorkflowActivity(); await refreshSessions();
    if (result.session_id) await loadSession(result.session_id);
  } catch (error) { showError(error); }
  button.disabled = false;
}

function renderNotifications() {
  const unread = state.notifications.filter((item) => !item.read).length;
  const reminders = state.notificationTab === "reminders";
  const approvals = state.notificationTab === "approvals";
  const tabs = element("div", { class: "role-tabs", role: "tablist", "aria-label": "通知视图" }, ...[["inbox", "收件箱"], ["approvals", `待批准${state.approvals.length ? ` ${state.approvals.length}` : ""}`], ["reminders", "提醒"]].map(([key, label]) => element("button", { type: "button", role: "tab", class: state.notificationTab === key ? "active" : "", text: label, "aria-selected": String(state.notificationTab === key), dataset: { notificationTab: key } })));
  const root = element("div", { class: "view-content" }, viewHeading("通知", approvals ? "主助手与队友等待你的决定。" : reminders ? "到时间，在这里收到提醒。" : unread ? `${unread} 条未读通知。` : "任务结果与需要处理的消息。", approvals ? actionButton("刷新", "refresh-approvals", "secondary-button", "refresh") : reminders ? actionButton("新建提醒", "new-reminder", "primary-button", "plus") : unread ? actionButton("全部已读", "read-all", "secondary-button", "check") : undefined), tabs);
  if (approvals) {
    if (state.approvalsError) root.append(emptyState("info", "暂时无法读取待批准", state.approvalsError));
    else if (!state.approvals.length) root.append(emptyState("shield", "暂无待批准", "当前没有正在等待的工具调用。"));
    for (const item of state.approvals) {
      const node = approvalCard(item);
      $$("button", node).forEach((button) => { button.disabled = !!connection?.approvalDecisions.has(item.id); });
      root.append(node);
    }
    return root;
  }
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
  const owner = connection;
  await ownedRequest(`/notifications/${encodeURIComponent(id)}`, { method: "PATCH", body: { read: true } }, owner);
  await refreshNotifications(owner);
}

function renderSettings() {
  if (!state.settingsDraft) return element("div", { class: "view-content" }, emptyState("settings", "设置暂不可用"));
  const nav = element("nav", { class: "settings-nav", "aria-label": "设置分组" });
  for (const [key, label] of SETTINGS_GROUPS) nav.append(element("button", { type: "button", class: state.settingsTab === key ? "active" : "", text: label, dataset: { settingsTab: key }, "aria-current": state.settingsTab === key ? "page" : "false" }));
  const refresh = actionButton("刷新状态", "refresh-settings-status", "text-button", "refresh");
  return element("div", { class: "view-content settings-view" }, viewHeading("设置", "模型、工具与本地运行配置。", refresh), element("div", { class: "settings-layout" }, nav, element("div", { class: "settings-content", dataset: { settingsTab: state.settingsTab } }, renderSettingsPanel())));
}

function renderSettingsPanel() {
  if (state.settingsTab !== "inputs") state.voiceSetupController?.release();
  if (state.settingsTab === "overview") return renderConfigurationOverview();
  const form = element("form", { id: "settings-form", class: "settings-panel", onSubmit: saveSettings });
  if (state.settingsTab === "models") form.append(renderModelSettings());
  if (state.settingsTab === "tools") form.append(renderToolSettings());
  if (state.settingsTab === "connections") form.append(renderConnectionSettings());
  if (state.settingsTab === "system") form.append(renderSystemSettings());
  if (state.settingsTab === "persona") form.append(renderPersonaSettings());
  if (state.settingsTab === "media") form.append(renderMediaSettings());
  if (state.settingsTab === "channels") form.append(renderChannelSettings());
  if (state.settingsTab === "inputs") form.append(renderInputSettings());
  if (state.settingsTab === "maintenance") form.append(renderMaintenanceSettings());
  form.append(element("p", { class: "form-error", id: "settings-error", hidden: true }), element("div", { class: "settings-footer" }, element("button", { class: "primary-button", type: "submit", text: "保存设置" }), element("span", { class: "settings-save-status", id: "settings-save-status", text: state.settingsSaveStatus, hidden: !state.settingsSaveStatus })));
  return form;
}

function renderConfigurationOverview() {
  const form = element("form", { class: "configuration-form", onSubmit: async (event) => {
    event.preventDefault();
    const input = $("textarea", form);
    const content = input.value.trim();
    if (!content) return;
    await switchView("chat");
    $("#message-input").value = `请调整沐璃的配置：\n${content}`; resizeComposer();
    if (state.status?.configured) $("#composer").requestSubmit();
  } }, element("label", { class: "field" }, element("span", { text: "想调整什么？" }), element("textarea", { rows: 4, placeholder: "例如：每天晚上九点提醒我整理笔记", required: true, "aria-label": "配置要求" })), element("button", { type: "submit", class: "primary-button", text: "交给沐璃" }));
  const examples = element("div", { class: "configuration-examples" });
  for (const [label, prompt] of [["检查配置", "请检查当前配置和未就绪的能力。"], ["接入消息渠道", "帮我配置一个消息渠道，先告诉我需要哪些资料。"], ["配置语音", "请检查语音唤醒需要的本地组件，并帮我规划配置。"], ["查看更新", "请查看更新状态和待应用的管理操作。"]]) examples.append(actionButton(label, "configure-prompt", "text-button", undefined, { prompt }));
  return element("div", { class: "settings-panel configuration-overview" }, form, examples, !state.status?.configured && element("div", { class: "configuration-bootstrap" }, element("p", { text: "先连接一个主模型，即可通过对话配置。" }), actionButton("连接主模型", "model-settings", "secondary-button", "link")), settingsResourceError("status"), settingsSection("当前状态", statusList([["主模型", state.status?.model || "未配置"], ["执行模式", MODES[state.mode]?.label ?? state.mode], ["运行任务", state.status ? String(state.status.running ?? 0) : "状态未知"], ["语音", capabilityLabel(state.voice)], ["桌面采集", capabilityLabel(state.capture)], ["待应用操作", state.management ? String((state.management.plans ?? []).filter((item) => ["pending", "applying"].includes(item.status)).length) : "状态未知"], ["工作目录", (state.settingsDraft.workspace_roots ?? []).join("、")]])));
}

function settingsSection(title, ...children) {
  return element("section", { class: "settings-section" }, element("h3", { text: title }), children);
}

async function refreshSettingsResources(owner = connection, current = () => true) {
  const { values, errors } = await loadSettingsResources((path) => ownedRequest(path, {}, owner));
  if (!connectionCurrent(owner) || !current()) return;
  for (const [key, value] of Object.entries(values)) state[key] = value;
  state.settingsStatusErrors = errors;
}

function settingsResourceError(key) {
  const error = state.settingsStatusErrors[key];
  return error && element("p", { class: "form-error", text: `状态暂不可用：${error}` });
}

function settingsJSON(path, config, excluded = [], rows = 7, title = "更多参数") {
  return element("details", { class: "advanced-settings" }, element("summary", { text: title }), field("参数 JSON", path, pretty(settingsExtras(config, excluded)), { type: "textarea", rows, json: true, format: "json-merge", hint: "填写要覆盖的字段，其余保留。" }));
}

function statusDetails(value) {
  return value && element("details", { class: "advanced-settings" }, element("summary", { text: "完整状态" }), element("pre", { class: "service-log", text: pretty(value) }));
}

function renderChannelSettings() {
  const config = state.settingsDraft.channels ?? {};
  const root = element("div", {}, settingsResourceError("channels"));
  const channelStatus = state.channels?.channels ?? {};
  const commonFields = ["enabled", "base_url", "owner_ids", "allowed_groups", "receive_files"];
  for (const [key, label] of CHANNELS) {
    const profile = config[key] ?? {}; const path = `channels.${key}`; const status = channelStatus[key];
    const privateOwner = ["onebot", "telegram", "qq"].includes(key);
    const enabled = switchField("启用渠道", `${path}.enabled`, profile.enabled);
    const owner = field(key === "onebot" ? "主人 QQ 号" : privateOwner ? "主人用户 ID" : "你的用户 ID", `${path}.owner_ids`, (profile.owner_ids ?? []).join("\n"), { type: "textarea", rows: 2, format: "lines", required: privateOwner && Boolean(profile.enabled), hint: key === "onebot" ? "启用前必填。每行一个 QQ 号，仅接收主人私聊。" : privateOwner ? "启用前必填。每行一个平台用户 ID，仅接收主人私聊。" : "每行一个 ID。" });
    if (privateOwner) $("input", enabled).addEventListener("change", (event) => { $("textarea", owner).required = event.target.checked; });
    const credentials = element("details", { class: "advanced-settings" }, element("summary", { text: "连接凭据" }));
    for (const [name, title] of [["token", "令牌"], ["webhook_secret", "Webhook 密钥"], ["client_secret", "客户端密钥"]]) {
      credentials.append(field(title, `${path}.${name}`, profile[name] ?? "", { type: "password", placeholder: profile[`${name}_set`] ? "已配置，留空保留" : "按渠道需要填写" }));
      if (profile[`${name}_set`]) credentials.append(switchField(`清除${title}`, `${path}.clear_${name}`, profile[`clear_${name}`]));
    }
    root.append(element("details", { class: "media-profile advanced-settings" },
      element("summary", {}, element("span", { text: label }), element("small", { text: capabilityLabel(status) })),
      enabled,
      field("服务地址", `${path}.base_url`, profile.base_url ?? "", { type: "url" }),
      owner,
      !privateOwner && field("允许的群组 ID", `${path}.allowed_groups`, (profile.allowed_groups ?? []).join("\n"), { type: "textarea", rows: 2, format: "lines" }),
      switchField("接收附件", `${path}.receive_files`, profile.receive_files), credentials,
      settingsJSON(path, profile, commonFields),
      status && statusList([["运行", capabilityLabel(status)], ["已授权用户", String(status.owner_count ?? 0)], ...(privateOwner ? [["接收范围", "仅主人私聊"]] : [["允许的群组", String(status.group_count ?? 0)]])]),
      status?.last_error && element("p", { class: "form-error", text: status.last_error })));
  }
  root.append(renderProactiveSettings(config), settingsSection("发送队列", state.channels && statusList(Object.entries(state.channels.outbox ?? {}).map(([key, count]) => [deliveryLabel(key), String(count)])), settingsJSON("channels", config, [...CHANNELS.map(([key]) => key), "proactive"]), renderDeliveries()));
  return root;
}

function renderProactiveSettings(config) {
  const policy = config.proactive ?? { external_enabled: false, default_destination: { kind: "web" } };
  const destination = policy.default_destination;
  const selected = destination.kind === "channel" ? destination.route.name : "web";
  const control = selectField("无发令会话时通知到", "proactive-destination", selected, [["web", "WebUI"], ...CHANNELS], "选择外部渠道会同时启用独立事项外发；绑定到任务的回复继续走原渠道。");
  $("select", control).addEventListener("change", (event) => {
    try {
      collectSettingsForm();
      const name = event.target.value;
      const profile = state.settingsDraft.channels?.[name] ?? {};
      const account = profile.account_id || profile.app_id || profile.user_id || state.channels?.channels?.[name]?.account_id || "";
      selectProactiveDestination(state.settingsDraft, name, account);
      renderOtherView();
    } catch (error) { displaySettingsError(error); }
  });
  const section = settingsSection("主动通知", control, element("p", { text: "当前仅自主启动更新检查可使用默认目的地；旧提醒和系统日志留在 WebUI。需开启主动关注并配置主模型，排队不代表送达。" }));
  section.append(...proactiveRouteFields(destination, "channels.proactive.default_destination.route"));
  const preference = policy.memory_preference;
  const toggle = switchField("使用明确的记忆偏好", "proactive-memory-enabled", Boolean(preference));
  $("input", toggle).addEventListener("change", (event) => {
    try {
      collectSettingsForm();
      selectNotificationPreference(state.settingsDraft, event.target.checked);
      renderOtherView();
    } catch (error) { displaySettingsError(error); }
  });
  section.append(toggle);
  if (preference) {
    const path = "channels.proactive.memory_preference";
    const name = preference.destination.kind === "channel" ? preference.destination.route.name : "web";
    const preferred = selectField("该偏好用于通知到", "proactive-memory-destination", name, [["web", "WebUI"], ...CHANNELS]);
    $("select", preferred).addEventListener("change", (event) => {
      try {
        collectSettingsForm();
        const chosen = event.target.value;
        const profile = state.settingsDraft.channels?.[chosen] ?? {};
        const account = profile.account_id || profile.app_id || profile.user_id || state.channels?.channels?.[chosen]?.account_id || "";
        selectPreferenceDestination(state.settingsDraft, chosen, account);
        renderOtherView();
      } catch (error) { displaySettingsError(error); }
    });
    section.append(field("偏好记忆 ID", `${path}.memory_id`, preference.memory_id, { required: true, maxLength: 200 }), field("记忆版本", `${path}.version`, preference.version, { type: "number", min: 1, required: true }), preferred, ...proactiveRouteFields(preference.destination, `${path}.destination.route`), element("p", { class: "settings-role-hint", text: "从记忆页核对偏好及原始证据，填写确切 ID 和版本。地址由本次设置明确选择；后台提炼不会改动通知设置。偏好失效后，新事项用普通默认，旧依赖项会受阻且不会改投。" }));
  }
  section.append(switchField("暂停独立事项外发", "proactive-pause", !policy.external_enabled), element("p", { class: "settings-role-hint", text: "更改默认只影响新独立事项。暂停会阻塞尚未发送的独立通知；恢复后需明确重试，结果未知不会自动补发。" }));
  const status = state.channels?.proactive;
  if (status) section.append(statusList([["主动关注", status.attention_enabled ? "已开启" : "已关闭"], ["主模型", status.model_configured ? "已配置" : "未配置"], ["记忆依据（已保存配置）", preferenceStatusLabel(status.memory_status?.reason ?? "not_configured")], ["当前独立来源", "启动时自主更新检查"]]));
  return section;
}

function proactiveRouteFields(destination, path) {
  if (destination.kind !== "channel") return [];
  const route = destination.route;
  const privateOwner = ["onebot", "telegram", "qq"].includes(route.name);
  const kinds = privateOwner ? [["private", "私聊"]] : [["private", "私聊"], ["group", "已允许的群组"]];
  return [field("机器人账号 ID", `${path}.account_id`, route.account_id, { required: true, maxLength: 200 }), selectField("会话类型", `${path}.kind`, route.kind, kinds), field("收件对象 ID", `${path}.target`, route.target, { required: true, maxLength: 200 }), field("已绑定的主人 ID", `${path}.sender_id`, route.sender_id, { required: true, maxLength: 200, hint: privateOwner ? "与私聊收件对象相同。" : "私聊时与收件对象相同；群聊需明确主人和 allowed_groups，不能自动选取。" })];
}

function deliveryLabel(value) {
  return ({ pending: "待发送", retry: "待重试", sending: "发送中", sent: "已发送", failed: "发送失败", blocked: "原路投递受阻", unknown: "结果未知" })[value] ?? value;
}

function renderDeliveries() {
  if (!state.channels) return null;
  const details = element("details", { class: "advanced-settings" }, element("summary", { text: "最近发送记录" }));
  const deliveries = state.channels.deliveries ?? [];
  if (!deliveries.length) details.append(element("p", { class: "settings-role-hint", text: "暂无发送记录。" }));
  for (const item of deliveries) details.append(element("article", { class: "management-item" }, element("div", { class: "management-item-heading" }, element("strong", { text: CHANNELS.find(([key]) => key === item.name)?.[1] ?? item.name }), element("span", { text: deliveryLabel(item.state) })), element("p", { text: [item.target ?? item.route?.target, item.created_at && formatFullDate(item.created_at)].filter(Boolean).join(" · ") }), item.last_error && element("p", { class: "form-error", text: item.last_error }), ["failed", "unknown", "blocked"].includes(item.state) && actionButton("重试发送", "retry-delivery", "text-button field-top-space", "refresh", { id: item.id })));
  return details;
}

function renderInputSettings() {
  const voice = state.settingsDraft.voice ?? {};
  const capture = state.settingsDraft.computer?.capture ?? {};
  const voiceStatus = state.voice; const captureStatus = state.capture;
  const voiceState = element("div", { id: "voice-status-summary" }, voiceStatus && voiceStatusSummary(voiceStatus));
  const captureState = captureStatus && statusList([["状态", capabilityLabel(captureStatus)], ["截图后端", captureStatus.screenshot?.backend || "未检测到"], ["录屏后端", captureStatus.recording?.backend || "未检测到"], ["采集权限", captureStatus.permission_verified ? "已验证" : "尚未验证"]]);
  return element("div", {},
    settingsSection("网页语音输入", switchField("允许点击录音并生成草稿", "web_voice.enabled", state.settingsDraft.web_voice?.enabled), element("p", { text: "需要配置 ASR。转写只进入可编辑草稿，点击发送后才成为用户消息；与后台唤醒独立。" }), settingsJSON("web_voice", state.settingsDraft.web_voice ?? {}, ["enabled"])),
    settingsSection("语音", settingsResourceError("voice"), voiceState,
      voiceStatus?.last_error && element("p", { class: "form-error", text: voiceStatus.last_error }),
      voiceStatus?.reason && element("p", { class: "settings-role-hint", text: voiceStatus.reason }),
      switchField("启用语音唤醒", "voice.enabled", voice.enabled),
      field("唤醒词", "voice.wake_word", voice.wake_word ?? ""),
      selectField("计算设备", "voice.device", voice.device ?? "auto", [["auto", "自动"], ["cpu", "CPU"], ["rocm", "ROCm"], ["cuda", "CUDA"]]),
      field("录音设备", "voice.input_device", voice.input_device ?? "", { placeholder: "使用系统默认设备", hint: voiceStatus?.recorder?.reason || undefined }),
      switchField("合成唤醒提示语", "voice.tts_enabled", voice.tts_enabled),
      renderVoiceSetup(),
      settingsJSON("voice", voice, ["enabled", "wake_word", "device", "input_device", "tts_enabled"], 10),
      element("button", { type: "button", class: "text-button field-top-space", text: "设置语音识别与合成模型", dataset: { settingsTab: "media" } }), statusDetails(voiceStatus)),
    settingsSection("截图与录屏", settingsResourceError("capture"), captureState,
      captureStatus?.reason && element("p", { class: "settings-role-hint", text: captureStatus.reason }),
      switchField("启用桌面采集", "computer.capture.enabled", capture.enabled),
      field("采集后端", "computer.capture.backend", capture.backend ?? "auto"),
      field("采集设备", "computer.capture.device", capture.device ?? "", { placeholder: "使用当前图形会话" }),
      element("div", { class: "field-row" }, field("最长录屏（秒）", "computer.capture.max_seconds", capture.max_seconds ?? "", { type: "number", min: 1, max: 300 }), field("录屏帧率", "computer.capture.fps", capture.fps ?? "", { type: "number", min: 1, max: 30 })),
      settingsJSON("computer.capture", capture, ["enabled", "backend", "device", "max_seconds", "fps"]), statusDetails(captureStatus)));
}

function renderVoiceSetup() {
  const root = element("div", { class: "field-top-space", id: "voice-setup-controls" },
    element("p", { id: "voice-setup-result", class: "settings-role-hint", "aria-live": "polite", text: voiceSetupLabel(voiceSetup.status()) }),
    element("div", { class: "field-row" }, actionButton("协助配置", "voice-setup-help", "text-button", "chat"), actionButton("录制参考", "voice-setup-enroll", "text-button", "mic"), actionButton("验证一次", "voice-setup-verify", "text-button", "check"), actionButton("取消", "voice-setup-cancel", "text-button", "stop")));
  queueMicrotask(renderVoiceSetupControls);
  return root;
}

function voiceStatusSummary(status) {
  return statusList([["状态", capabilityLabel(status)], ["录音后端", status.recorder?.backend || "未检测到"], ["声学唤醒", status.wake?.available ? "预检可用" : "未就绪"], ["参考声纹", status.speaker?.enrolled ? "已录制" : "未录制"], ["语音识别", status.asr ? "已配置" : "未配置"], ["提示播放", status.playback?.backend || "未检测到"], ["设备选择", status.wake?.configured_provider ?? status.device?.selected ?? "状态未知"], ["设备运行验证", status.device?.runtime_verified ? "已验证" : "尚未验证"]]);
}

function renderVoiceSetupControls() {
  const root = $("#voice-setup-controls");
  if (!root || state.view !== "settings" || state.settingsTab !== "inputs") return;
  const setup = voiceSetup.status(), status = state.voice ?? {};
  const summary = $("#voice-status-summary");
  if (summary && state.voice) summary.replaceChildren(voiceStatusSummary(status));
  const busy = ["recording", "stopping"].includes(setup.phase);
  $("#voice-setup-result", root).textContent = status.enabled && !busy ? "先保存关闭后台唤醒，再进行单次设置。" : voiceSetupLabel(setup);
  for (const operation of ["enroll", "verify"]) {
    const button = $(`[data-action="voice-setup-${operation}"]`, root);
    button.disabled = busy || !state.connected || !!status.enabled;
  }
  const cancel = $('[data-action="voice-setup-cancel"]', root);
  cancel.hidden = !busy; cancel.disabled = setup.phase === "stopping";
}

function renderMaintenanceSettings() {
  const config = state.settingsDraft; const management = state.management;
  const plugins = config.plugins ?? {}; const updates = config.updates ?? {};
  const inventory = element("div", { class: "management-list" });
  if (management && !management.plugins?.length) inventory.append(element("p", { class: "settings-role-hint", text: "暂无本地插件。" }));
  for (const plugin of management?.plugins ?? []) inventory.append(element("article", { class: "management-item" }, element("div", { class: "management-item-heading" }, element("strong", { text: plugin.name || plugin.id }), element("span", { text: managementLabel(plugin.status) })), element("p", { text: [plugin.id, plugin.version].filter(Boolean).join(" · ") }), plugin.error && element("p", { class: "form-error", text: plugin.error })));
  const plans = element("div", { class: "management-list" });
  if (management && !management.plans?.length) plans.append(element("p", { class: "settings-role-hint", text: "暂无管理操作。" }));
  for (const plan of management?.plans ?? []) plans.append(element("article", { class: "management-item" }, element("div", { class: "management-item-heading" }, element("strong", { text: managementActionLabel(plan) }), element("span", { text: managementPlanLabel(plan) })), element("p", { text: [plan.created_at && formatFullDate(plan.created_at), plan.version, ...(plan.changed_sections ?? [])].filter(Boolean).join(" · ") }), plan.error && element("p", { class: "form-error", text: plan.error })));
  const update = management?.update;
  return element("div", {}, settingsResourceError("management"),
    settingsSection("本地插件", switchField("启用插件", "plugins.enabled", plugins.enabled), field("禁用的插件 ID", "plugins.disabled", (plugins.disabled ?? []).join("\n"), { type: "textarea", rows: 2, format: "lines" }), inventory,
      management?.plugins_restart_required && element("p", { class: "settings-notice", text: "插件配置已保存，重启后生效。" }), settingsJSON("plugins", plugins, ["enabled", "disabled"])),
    settingsSection("版本更新", statusList([["状态", updateLabel(management)], ["当前版本", update?.current ?? state.status?.version], ["最新版本", update?.latest], ["上次检查", update?.checked_at ? formatFullDate(update.checked_at) : "—"]]), switchField("启动时检查更新", "updates.check_on_start", updates.check_on_start), settingsJSON("updates", updates, ["check_on_start"]), actionButton("交给沐璃检查", "configure-prompt", "text-button", "chat", { prompt: "请检查 MuLi 的更新状态，有更新时先告诉我版本与变更。" })),
    settingsSection("管理操作", plans),
    state.status?.server?.restart_required && element("p", { class: "settings-notice", text: "监听配置已保存，重启后生效。" }));
}

function renderModelSettings() {
  const role = state.modelRole;
  const model = state.settingsDraft.models?.[role] ?? {};
  const path = `models.${role}`;
  const tabs = element("div", { class: "role-tabs", role: "tablist", "aria-label": "模型角色" });
  for (const [key, label] of MODEL_ROLES) tabs.append(element("button", { type: "button", role: "tab", class: role === key ? "active" : "", text: label, "aria-selected": String(role === key), dataset: { modelRole: key } }));
  const protocol = model.protocol ?? "openai_chat";
  const protocolField = selectField("协议", `${path}.protocol`, protocol, [["openai_chat", "OpenAI · Chat Completions"], ["openai_responses", "OpenAI · Responses"], ["anthropic", "Anthropic · Messages"]]);
  const modelField = field("模型名称", `${path}.model`, model.model ?? "", { placeholder: "填写提供商的模型 ID", required: role === "main" });
  const baseField = field("API Base URL", `${path}.base_url`, model.base_url ?? "", { type: "url", placeholder: "填写服务地址" });
  const token = field("最大输出 Token", `${path}.max_output_tokens`, model.max_output_tokens ?? "", { type: "number", min: 128, max: 131072, step: 1, placeholder: "使用服务默认值", hint: "Anthropic 配置需要填写；其它协议可留空。" });
  const advanced = element("details", { class: "advanced-settings" }, element("summary", { text: "高级参数" }),
    element("div", { class: "field-row" }, field("Temperature", `${path}.temperature`, model.temperature ?? "", { type: "number", step: .1, min: 0, max: 2, placeholder: "使用模型默认值" }), field("Top P", `${path}.top_p`, model.top_p ?? "", { type: "number", step: .05, min: 0, max: 1, placeholder: "使用模型默认值" })),
    field("超时（秒）", `${path}.timeout`, model.timeout ?? "", { type: "number", min: 1, max: 600, placeholder: "使用服务默认值" }),
    element("div", { class: "field-row" }, field("重试次数", `${path}.max_retries`, model.max_retries ?? "", { type: "number", min: 0, max: 5, step: 1 }), field("API Key 环境变量", `${path}.api_key_env`, model.api_key_env ?? "", { placeholder: "可选" })),
    field("Reasoning", `${path}.reasoning`, pretty(model.reasoning ?? {}), { type: "textarea", rows: 2, json: true, format: "json" }),
    field("Thinking", `${path}.thinking`, pretty(model.thinking ?? {}), { type: "textarea", rows: 2, json: true, format: "json" }),
    field("Reasoning effort", `${path}.reasoning_effort`, model.reasoning_effort ?? "", { placeholder: "使用模型默认值", format: "nullable" }),
    field("Extra body", `${path}.extra_body`, pretty(model.extra_body ?? {}), { type: "textarea", rows: 3, json: true, format: "json" }),
    field("Extra headers", `${path}.extra_headers`, pretty(model.extra_headers ?? {}), { type: "textarea", rows: 3, json: true, format: "json", hint: "保持 •••• 可保留原值，移除字段即可删除。" }),
    field("响应大小上限（字节）", `${path}.max_response_bytes`, model.max_response_bytes ?? "", { type: "number", min: 1024 }),
    switchField("流式输出", `${path}.stream`, model.stream !== false),
    model.api_key_set && switchField("清除已存 API Key", `${path}.clear_api_key`, false));
  const hints = { worker: "未单独配置时，复用主模型。", compactor: "未单独配置时，复用主模型。", memory_extractor: "用于后台提取记忆；未配置时复用主模型。", reviewer: "未单独配置时，复用主模型。", vision: "主模型支持图片时复用主模型；否则需要单独配置支持图片的视觉模型。" };
  const content = element("div", {}, tabs, hints[role] && element("p", { class: "settings-role-hint", text: hints[role] }),
    settingsSection("模型连接", protocolField, modelField, baseField, field("API Key", `${path}.api_key`, model.api_key ?? "", { type: "password", placeholder: model.api_key_set ? "已配置，留空保留" : "输入 API Key", hint: model.api_key_set ? "已有密钥不会显示。" : undefined }), token, switchField("支持图片", `${path}.vision`, model.vision), advanced));
  const protocolInput = $("select", protocolField);
  const modelInput = $("input", modelField);
  const baseInput = $("input", baseField);
  const updateRequired = () => { $("input", token).required = protocolInput.value === "anthropic" && (role === "main" || !!modelInput.value.trim() || !!baseInput.value.trim()); };
  protocolInput.addEventListener("change", updateRequired);
  modelInput.addEventListener("input", updateRequired);
  baseInput.addEventListener("input", updateRequired);
  updateRequired();
  return content;
}

function renderPersonaSettings() {
  const persona = state.settingsDraft.persona ?? {};
  return element("div", {}, settingsSection("称呼", field("如何称呼你", "persona.user_name", persona.user_name ?? "你", { required: true }), field("助手名称", "persona.name", persona.name ?? "沐璃", { required: true }), field("简介", "persona.introduction", persona.introduction ?? "", { type: "textarea", rows: 3 })), settingsSection("提示词", field("主提示词", "persona.prompt", persona.prompt ?? "", { type: "textarea", rows: 8, placeholder: "留空使用默认提示词" }), field("补充要求", "persona.append", persona.append ?? "", { type: "textarea", rows: 4 })));
}

function renderMediaSettings() {
  const root = element("div");
  for (const [key, label] of [["embedding", "嵌入"], ["reranker", "重排序"], ["asr", "语音识别"], ["tts", "语音合成"]]) {
    const path = `media.${key}`; const config = state.settingsDraft.media?.[key] ?? {};
    const details = element("details", { class: "media-profile advanced-settings" }, element("summary", {}, element("span", { text: label }), element("small", { text: config.model || "未配置" })), field("模型", `${path}.model`, config.model ?? ""), field("服务地址", `${path}.base_url`, config.base_url ?? "", { type: "url" }), field("API Key", `${path}.api_key`, "", { type: "password", placeholder: config.api_key_set ? "已配置，留空保留" : "可选" }));
    if (key === "embedding") details.append(field("维数", `${path}.dimensions`, config.dimensions ?? "", { type: "number", min: 1 }), field("查询指令", `${path}.query_instruction`, config.query_instruction ?? "", { type: "textarea", rows: 2 }));
    if (key === "asr") details.append(field("语言", `${path}.language`, config.language ?? "", { placeholder: "自动识别" }));
    if (key === "tts") details.append(field("声音", `${path}.voice`, config.voice ?? ""), field("音频格式", `${path}.response_format`, config.response_format ?? "mp3"), field("语速", `${path}.speed`, config.speed ?? "", { type: "number", min: .01, max: 4, step: .1 }), field("朗读要求", `${path}.instructions`, config.instructions ?? "", { type: "textarea", rows: 2 }));
    details.append(element("details", { class: "advanced-settings" }, element("summary", { text: "高级参数" }), field("API Key 环境变量", `${path}.api_key_env`, config.api_key_env ?? ""), field("超时（秒）", `${path}.timeout`, config.timeout ?? "", { type: "number", min: 1, max: 600 }), field("Extra body", `${path}.extra_body`, pretty(config.extra_body ?? {}), { type: "textarea", rows: 3, json: true, format: "json" }), field("Extra headers", `${path}.extra_headers`, pretty(config.extra_headers ?? {}), { type: "textarea", rows: 3, json: true, format: "json" }), config.api_key_set && switchField("清除已存 API Key", `${path}.clear_api_key`, false)));
    root.append(details);
  }
  root.append(settingsSection("记忆与上下文", settingsJSON("memory", state.settingsDraft.memory ?? {}, [], 10, "记忆处理"), settingsJSON("compaction", state.settingsDraft.compaction ?? {}, [], 7, "上下文压缩"), state.settingsDraft.attention && settingsJSON("attention", state.settingsDraft.attention, [], 7, "主动关注")));
  return root;
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
    settingsSection("电脑操作", switchField("启用电脑操作", "computer.enabled", computer.enabled), field("后端", "computer.backend", computer.backend ?? "auto"), field("Linux Python 路径", "computer.python_path", computer.python_path ?? "", { placeholder: "使用默认运行环境" }), element("p", { text: state.status?.computer?.available ? "电脑操作已就绪。" : state.status?.computer?.reason || "电脑操作暂不可用。" })),
    list.children.length ? settingsSection("已注册工具", list) : null);
}

function renderConnectionSettings() {
  const config = state.settingsDraft.chiliarch ?? {};
  const pair = element("div", { class: "pairing-row" }, field("配对码", "pairing-code", "", { placeholder: "输入 Chiliarch 配对码" }), actionButton("配对", "pair-chiliarch", "secondary-button", "link"));
  return element("div", {}, settingsSection("Chiliarch", switchField("启用 Chiliarch 连接", "chiliarch.enabled", config.enabled), field("服务地址", "chiliarch.base_url", config.base_url ?? "", { type: "url" }), field("Agent ID", "chiliarch.agent_id", config.agent_id ?? "", { placeholder: "配对后自动填写" }), element("div", { id: "chiliarch-status" }, connectionStatus()), element("div", { class: "field-top-space" }, pair)), renderMCPSettings());
}

function connectionStatus() {
  const status = state.status?.chiliarch;
  const connectionLabels = { connected: "已连接", retrying: "正在重连", connecting: "正在连接", disabled: "未启用", unpaired: "未配对", disconnected: "未连接", failed: "连接失败" };
  const connectionLabel = status?.connected || status?.status === "connected" ? "已连接" : connectionLabels[status?.status] ?? "未连接";
  return [statusList([["连接", connectionLabel], ["凭据", state.settingsDraft.chiliarch?.credential_set ? "已配对" : "未配对"]]), status?.last_error && element("p", { class: "form-error", text: status.last_error })].filter(Boolean);
}

function renderMCPSettings() {
  const config = state.settingsDraft.mcp;
  const reload = actionButton("重新加载", "reload-extensions", "text-button", "refresh");
  reload.disabled = extensionReload?.owner === connection && extensionReload.pending;
  const section = settingsSection("MCP 与 Skills", element("div", { id: "extensions-status", role: "status", "aria-live": "polite" }, extensionStatus()), reload);
  section.append(element("p", { class: "settings-role-hint", text: "按已保存的配置重新加载，不会保存当前草稿。" }));
  if (!config || "configured" in config) {
    section.append(element("p", { text: "当前服务暂未提供可编辑的 MCP 配置。" }));
    return section;
  }
  section.append(element("details", { class: "advanced-settings" }, element("summary", { text: "服务器配置" }), field("MCP JSON", "mcp", pretty(config), { type: "textarea", rows: 10, json: true, format: "json", hint: "使用 mcpServers。保留 •••• 可沿用密钥，删除服务器即可移除连接。" })));
  return section;
}

function extensionStatus() {
  const status = state.status?.extensions;
  const labels = { discovered: "等待加载", queued: "已排队", preparing: "正在加载", loaded: "已加载", failed: "加载失败", restart_required: "需要重启" };
  const skills = status?.skills;
  const content = [statusList([["扩展", labels[status?.phase] ?? "状态未知"], ["Skills", skills?.enabled === false ? "未启用" : Number.isSafeInteger(skills?.count) ? `${skills.count} 项` : "状态未知"]])];
  const hint = ({ queued: "重载已排队，尚未生效。", failed: "加载失败，保留当前可用扩展。", restart_required: "需要重启后才能应用，当前可用扩展保留。" })[status?.phase];
  if (hint) content.push(element("p", { class: "settings-role-hint", text: hint }));
  const attempt = extensionReload?.owner === connection ? extensionReload : null;
  if (attempt?.error) content.push(element("p", { class: "form-error", text: attempt.error }));
  const servers = status?.mcp ?? state.status?.mcp;
  if (Array.isArray(servers) && servers.length) {
    const labels = { connected: "已连接", disabled: "未启用", connecting: "正在连接", failed: "连接失败", error: "连接失败" };
    content.push(statusList(servers.map((server) => [server.name, labels[server.status] ?? server.status ?? "未连接"])));
  }
  return content;
}

function refreshExtensionsView() {
  if (state.view !== "settings" || state.settingsTab !== "connections") return;
  $("#extensions-status")?.replaceChildren(...extensionStatus());
  $("#chiliarch-status")?.replaceChildren(...connectionStatus());
  const pending = extensionReload?.owner === connection && extensionReload.pending;
  for (const action of ["reload-extensions", "refresh-settings-status"]) {
    const button = $(`[data-action="${action}"]`, $("#other-view"));
    if (button) button.disabled = pending;
  }
}

async function requestExtensions(reload) {
  const owner = connection, version = viewRequestVersion;
  if (!connectionCurrent(owner) || state.view !== "settings" || state.settingsTab !== "connections" || (extensionReload?.owner === owner && extensionReload.pending)) return;
  const current = () => connectionCurrent(owner) && version === viewRequestVersion && state.view === "settings";
  const attempt = extensionReload = { owner, pending: true, error: "" };
  const focused = document.activeElement;
  refreshExtensionsView();
  try {
    if (reload) {
      const value = await ownedRequest("/extensions/reload", { method: "POST", body: {} }, owner);
      if (!current()) return;
      if (!value || typeof value.phase !== "string") throw new Error("扩展状态暂不可用");
      state.status = { ...state.status, extensions: value, mcp: value.mcp };
    } else await refreshSettingsResources(owner, current);
  } catch {
    if (current()) attempt.error = reload ? "重载请求未确认，请刷新状态核对。" : "暂时无法读取扩展状态，请刷新后重试。";
  } finally {
    attempt.pending = false;
    if (connectionCurrent(owner) && extensionReload === attempt) {
      refreshExtensionsView();
      if (current() && document.activeElement === document.body && $("#other-view").contains(focused) && !focused.disabled) focused.focus({ preventScroll: true });
    }
  }
}

function statusList(entries) {
  return element("dl", { class: "status-list" }, entries.map(([key, value]) => element("div", {}, element("dt", { text: key }), element("dd", { text: value ?? "—" }))));
}

function renderSystemSettings() {
  const config = state.settingsDraft;
  const limits = config.limits ?? {};
  const service = state.service;
  const serviceSection = settingsSection("用户服务", settingsResourceError("service"), statusList([["服务", service?.supported === false ? "当前系统不支持" : service?.unit || "状态未知"], ["安装", !service ? "状态未知" : service.installed ? "已安装" : "未安装"], ["状态", !service ? "状态未知" : service.active ? "运行中" : "未运行"], ["登录后启动", !service ? "状态未知" : service.enabled ? "已启用" : "未启用"]]));
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
    settingsSection("工作台", statusList([["版本", state.status?.version], ["平台", state.status?.platform], ["运行任务", String(state.status?.running ?? 0)]]), element("div", { class: "field-row field-top-space" }, field("监听地址", "server.host", config.server?.host ?? ""), field("端口", "server.port", config.server?.port ?? "", { type: "number", min: 1, max: 65535 })), element("p", { text: "监听变更将排队执行重启事务。" }), element("div", { class: "system-links" }, element("a", { href: "/api/export", download: "muli-export.jsonl", class: "secondary-button export-button", text: "导出数据" }), element("a", { href: "/api/docs", target: "_blank", rel: "noopener", class: "secondary-button export-button", text: "API 参考" }))),
    settingsSection("通知", switchField("桌面通知", "notifications.enabled", config.notifications?.enabled), state.status?.notifications?.available === false && element("p", { text: "桌面通知暂不可用。" }), state.status?.notifications?.last_error && element("p", { class: "form-error", text: state.status.notifications.last_error })),
    settingsSection("访问", field("访问密码", "server.password", "", { type: "password", placeholder: config.server?.password_set ? "已设置，留空保留" : "尚未设置" }), config.server?.password_set && switchField("清除访问密码", "server.clear_password", false), state.auth.required && actionButton("退出登录", "logout", "secondary-button")),
    serviceSection,
    settingsSection("运行限制", element("div", { class: "field-row" }, field("并行 Agent 数", "limits.max_parallel_agents", limits.max_parallel_agents ?? "", { type: "number", min: 1 }), field("未完成任务上限", "limits.max_pending_runs", limits.max_pending_runs ?? "", { type: "number", min: 1, placeholder: "使用服务默认值" })), element("div", { class: "field-row" }, field("工具步骤上限", "limits.max_tool_steps", limits.max_tool_steps ?? "", { type: "number", min: 1 }), field("任务时限（秒）", "limits.max_run_seconds", limits.max_run_seconds ?? "", { type: "number", min: 1 })), element("div", { class: "field-row" }, field("文件大小上限（字节）", "limits.max_file_bytes", limits.max_file_bytes ?? "", { type: "number", min: 1 }), field("工具输出字符上限", "limits.max_output_chars", limits.max_output_chars ?? "", { type: "number", min: 1 })), settingsJSON("limits", limits, ["max_parallel_agents", "max_pending_runs", "max_tool_steps", "max_run_seconds", "max_file_bytes", "max_output_chars"])),
    settingsSection("附件库", settingsJSON("uploads", config.uploads ?? {})));
}

function collectSettingsForm() {
  const form = $("#settings-form");
  if (!form || !state.settingsDraft) return;
  for (const input of $$('[name]', form)) {
    if (!input.name || ["pairing-code", "proactive-destination", "proactive-pause", "proactive-memory-enabled", "proactive-memory-destination"].includes(input.name)) continue;
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
      if (!input.value.trim()) {
        if (!/^models\.[^.]+\.max_output_tokens$/.test(input.name)) continue;
        value = null;
      } else {
        value = Number(input.value);
        if (!Number.isFinite(value)) throw new Error("数值格式不正确。");
      }
    } else if (["json", "json-merge"].includes(input.dataset.format)) {
      try { value = parseJSONField(input.value); } catch { throw new Error(`${input.closest("label")?.firstElementChild?.textContent ?? "参数"} 需要填写有效 JSON 对象。`); }
    } else if (input.dataset.format === "lines") value = input.value.split("\n").map((line) => line.trim()).filter(Boolean);
    else if (input.dataset.format === "nullable") value = input.value.trim() || null;
    else value = input.type === "password" ? input.value : input.value.trim();
    if (input.dataset.format === "json-merge") mergeSettingsExtras(state.settingsDraft, input.name, value);
    else setPath(state.settingsDraft, input.name, value);
  }
  const pause = $('[name="proactive-pause"]', form);
  if (pause) setPath(state.settingsDraft, "channels.proactive.external_enabled", !pause.checked);
}

function displaySettingsError(error) {
  const node = $("#settings-error");
  if (node) { node.textContent = error.message ?? String(error); node.hidden = false; } else showError(error);
}

function displaySettingsSaveStatus(value) {
  state.settingsSaveStatus = value;
  const node = $("#settings-save-status");
  if (node) { node.textContent = value; node.hidden = !value; }
}

async function saveSettings(event) {
  event.preventDefault();
  const submit = $('button[type="submit"]', event.target);
  try {
    collectSettingsForm();
    const patch = settingsPatch(state.settings, state.settingsDraft);
    if (!Object.keys(patch).length) { toast("设置没有变化"); return; }
    submit.disabled = true;
    displaySettingsSaveStatus("");
    const errorNode = $("#settings-error");
    if (errorNode) errorNode.hidden = true;
    if (changesListener(patch)) {
      const plan = await request("/management/transactions", { method: "POST", body: { action: "configure", changes: patch } });
      if (plan?.id) state.management = { ...state.management, plans: [plan, ...(state.management?.plans ?? []).filter((item) => item.id !== plan.id)] };
      displaySettingsSaveStatus("已排队，空闲后重启");
      toast(state.settingsSaveStatus);
      return;
    }
    await request("/settings", { method: "PATCH", body: patch });
    const results = await Promise.allSettled([request("/settings"), refreshSettingsResources()]);
    if (results[0].status === "fulfilled") { state.settings = results[0].value; state.settingsDraft = structuredClone(state.settings); }
    if (results[1].status === "fulfilled" && !state.activeSession) state.mode = state.status?.mode || "auto";
    state.settingsSaveStatus = "已保存"; renderOtherView(); renderChrome(); renderComposer(); toast("设置已保存");
  } catch (error) { displaySettingsError(error); }
  finally { submit.disabled = false; }
}

async function pairChiliarch(button) {
  const code = $('[name="pairing-code"]')?.value.trim();
  if (!code) { toast("请输入配对码", true); return; }
  button.disabled = true;
  try {
    collectSettingsForm();
    const patch = settingsPatch(state.settings, state.settingsDraft);
    if (changesListener(patch)) throw new Error("请先保存监听变更，等待重启后再配对。");
    if (Object.keys(patch).length) await request("/settings", { method: "PATCH", body: patch });
    await request("/integrations/chiliarch/pair", { method: "POST", body: { code, name: "MuLi" } });
    state.settingsDraft = null;
    toast("配对已完成"); await switchView("settings");
  } catch (error) { displaySettingsError(error); }
  finally { button.disabled = false; }
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
  if (action === "voice-setup-help") {
    state.voiceSetupController?.release();
    await switchView("chat");
    const input = $("#message-input");
    if (!input.value.trim()) input.value = "请帮我配置本地语音：先检查状态并准备已有本地模型的配置，保持后台唤醒关闭，再指导我明确登记声纹和做一次有限验证。不要自动安装依赖或下载模型。";
    else toast("已保留草稿，可以直接请沐璃协助配置语音。");
    draftRevision += 1; resizeComposer(); $("#message-input").focus();
  }
  if (["voice-setup-enroll", "voice-setup-verify"].includes(action)) {
    const operation = action === "voice-setup-enroll" ? "enroll" : "verify";
    const root = $("#voice-setup-controls"), owner = connection, generation = voiceSetup.generation;
    confirmAction(operation === "enroll" ? "录制参考声纹" : "验证本地语音", state.voice?.preview ? "仅模拟一次设置，不访问麦克风或声学设备。" : "将使用本机麦克风录制 5 秒，仅在本地登记或比对，不转写、不发送消息。", state.voice?.preview ? "模拟录音" : "开始录音", async () => {
      if (connectionCurrent(owner) && generation === voiceSetup.generation && root?.isConnected && root.contains(button)) {
        $("#app-dialog").close();
        const result = await voiceSetup.run(operation);
        if (result && connectionCurrent(owner) && state.view === "settings" && state.settingsTab === "inputs") {
          await refreshSettingsResources(owner, () => state.view === "settings" && state.settingsTab === "inputs");
          renderVoiceSetupControls();
        }
      }
    });
  }
  if (action === "voice-setup-cancel") await voiceSetup.stop();
  if (action === "toggle-theme") { toggleTheme(); renderThemeControl(); }
  if (action === "logout") {
    voiceInput.cancel(); state.voiceDraft = null; $("#voice-draft-text").value = "";
    $("#message-input").value = ""; draftRevision += 1; resizeComposer();
    await request("/auth/logout", { method: "POST" }); state.settings = null; state.settingsDraft = null; state.status = null; state.session = createSessionState(); resetMessageNodes(); showLogin();
  }
  if (action === "open-settings") { state.settingsTab = "overview"; await switchView("settings"); }
  if (action === "attach-files") $("#file-input").click();
  if (action === "voice-record") {
    if (state.voiceDraft) throw new Error("请先处理已有转写草稿。");
    await voiceInput.start(state.status?.web_voice, captureDraft());
  }
  if (action === "voice-stop") await voiceInput.stop();
  if (action === "voice-cancel") voiceInput.cancel();
  if (action === "voice-draft-dismiss") { state.voiceDraft = null; $("#voice-draft-text").value = ""; renderVoiceInput(); }
  if (action === "voice-draft-append") {
    const input = $("#message-input");
    const text = $("#voice-draft-text").value;
    const merged = input.value ? `${input.value}\n${text}` : text;
    if (merged.length > (state.status?.web_voice?.max_text_chars ?? 64000)) throw new Error("合并后的草稿超过上限，请先缩短内容。");
    input.value = merged; draftRevision += 1; state.voiceDraft = null; $("#voice-draft-text").value = ""; renderVoiceInput(); resizeComposer(); input.focus();
  }
  if (action === "remove-attachment") { state.attachments = state.attachments.filter((file) => file.id !== button.dataset.id); renderComposerAttachments(); }
  if (action === "use-attachment") {
    const file = state.uploads.find((entry) => entry.id === button.dataset.id);
    if (file && !state.attachments.some((entry) => entry.id === file.id)) state.attachments.push(file);
    renderComposerAttachments(); await switchView("chat");
  }
  if (action === "configure-prompt") { await switchView("chat"); $("#message-input").value = button.dataset.prompt ?? ""; resizeComposer(); }
  if (action === "refresh-team") await refreshTeam();
  if (action === "view-task-brief") await showTaskBrief(button.dataset.commitment);
  if (action === "filter-team") await refreshTeam({ offset: 0, activeOnly: button.dataset.active === "true" });
  if (action === "page-team") await refreshTeam({ offset: Number(button.dataset.offset) });
  if (action === "message-team") {
    const id = button.dataset.id;
    openDialog({ title: "发送协作消息", content: field("消息", "content", "", { type: "textarea", required: true }), submitLabel: "发送", onSubmit: async (form) => {
      const content = String(new FormData(form).get("content")).trim();
      if (!content) throw new Error("请输入消息。");
      await request(`/team/${encodeURIComponent(id)}/messages`, { method: "POST", body: { content } });
      await refreshTeam(); toast("协作消息已发送");
    } });
  }
  if (action === "stop-team") { await request(`/team/${encodeURIComponent(button.dataset.id)}/stop`, { method: "POST" }); await refreshTeam(); toast("已发送停止请求"); }
  if (action === "model-settings") { state.settingsTab = "models"; await switchView("settings"); }
  if (action === "refresh") { await connect(); if (!["chat", "records"].includes(state.view)) await switchView(state.view); }
  if (action === "retry-view") await switchView(state.view);
  if (action === "scroll-bottom") scrollToBottom(true, true);
  if (action === "stop") await stopRun(button);
  if (action === "approval") await approve(button.dataset.id, button.dataset.decision, button);
  if (action === "refresh-approvals") await refreshApprovals();
  if (action === "close-dialog") $("#app-dialog").close();
  if (action === "copy-message") {
    const message = conversationItems(state.session).find((item) => item.kind === "message" && String(item.id) === button.dataset.id)?.message ?? state.session.messages.find((item) => String(item.id) === button.dataset.id);
    if (message) { await navigator.clipboard.writeText(message.content); toast("已复制"); }
  }
  if (action === "delete-session") {
    const slot = recordDirectory;
    if (state.view === "records" && (!recordDirectoryCurrent(slot) || !slot.root.contains(button))) return;
    const id = button.dataset.id;
    confirmAction("删除会话", "这段会话及其运行记录会被删除。", "删除会话", async () => {
      await request(`/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
      await refreshSessions();
      if (recordDirectoryCurrent(slot)) await loadRecordDirectory(slot, { cursor: slot.cursor, starts: slot.starts });
      toast("记录已删除");
    });
    dialogHistoryReturn = { trigger: button, sessionId: id, view: state.view, action: "delete-session" };
  }
  if (action === "new-memory") await editMemory();
  if (action === "edit-memory") await editMemory(button.dataset.id);
  if (action === "delete-memory") confirmAction("删除记忆", "这条记忆会被遗忘；如有关联原文，同源记忆也会一并遗忘。", "删除记忆", async () => { await request(`/memories/${encodeURIComponent(button.dataset.id)}`, { method: "DELETE" }); await switchView("memories"); toast("记忆已删除"); });
  if (action === "add-memory-relation") $("#memory-relations")?.append(relationEditor());
  if (action === "remove-memory-relation") button.closest(".relation-editor")?.remove();
  if (action === "memory-evidence") await showMemoryEvidence(button.dataset.id, { trigger: button });
  if (action === "new-workflow") newWorkflow();
  if (action === "workflow-templates") workflowTemplates();
  if (action === "add-workflow-step") addWorkflowStep();
  if (action === "remove-workflow-step") button.closest(".workflow-step-editor")?.remove();
  if (action === "run-workflow") await runWorkflow(button.dataset.id, button);
  if (action === "workflow-session") await loadSession(button.dataset.session, button);
  if (action === "toggle-workflow-schedules") {
    const slot = workflowActivity;
    if (!workflowActivityCurrent(slot) || !$("#other-view").contains(button)) return;
    slot.schedulesOpen = !slot.schedulesOpen;
    if (slot.schedulesOpen) await loadWorkflowSchedules(slot);
    else { slot.schedulesController?.abort(); slot.schedulesController = null; slot.schedulesLoading = false; slot.schedulesPage = null; renderOtherView(); }
  }
  if (["refresh-workflow-schedules", "page-workflow-schedules"].includes(action)) {
    const slot = workflowActivity;
    if (!workflowActivityCurrent(slot) || !slot.schedulesOpen || !$("#workflow-schedules")?.contains(button) || slot.schedulesLoading) return;
    if (action === "refresh-workflow-schedules") await loadWorkflowSchedules(slot);
    else if (button.dataset.direction === "next" && slot.schedulesPage?.has_more) await loadWorkflowSchedules(slot, slot.schedulesPage.next_after, [...slot.schedulesStarts, slot.schedulesPage.next_after]);
    else if (button.dataset.direction === "previous" && slot.schedulesStarts.length > 1) await loadWorkflowSchedules(slot, slot.schedulesStarts.at(-2), slot.schedulesStarts.slice(0, -1));
  }
  if (action === "cancel-workflow-schedule") {
    const slot = workflowActivity, id = button.dataset.id, owner = connection;
    if (!workflowActivityCurrent(slot) || !slot.schedulesOpen || !$("#workflow-schedules")?.contains(button) || !slot.schedulesPage?.items.some((item) => item.id === id && item.enabled) || slot.schedulesStopping.has(id)) return;
    slot.schedulesStopping.add(id); renderOtherView();
    try {
      await ownedRequest(`/workflow-schedules/${encodeURIComponent(id)}/cancel`, { method: "POST" }, owner);
      if (workflowActivityCurrent(slot) && connectionCurrent(owner)) { toast("已提交取消请求"); await loadWorkflowSchedules(slot, slot.schedulesAfter, slot.schedulesStarts); }
    } finally { slot.schedulesStopping.delete(id); if (workflowActivityCurrent(slot) && connectionCurrent(owner)) renderOtherView(); }
  }
  if (["refresh-workflow-runs", "page-workflow-runs"].includes(action)) {
    const slot = workflowActivity;
    if (!workflowActivityCurrent(slot) || !$("#other-view").contains(button) || slot.loading) return;
    if (action === "refresh-workflow-runs") await loadWorkflowActivity(slot);
    else if (button.dataset.direction === "next" && slot.page?.has_more) await loadWorkflowActivity(slot, slot.page.next_after, [...slot.starts, slot.page.next_after]);
    else if (button.dataset.direction === "previous" && slot.starts.length > 1) await loadWorkflowActivity(slot, slot.starts.at(-2), slot.starts.slice(0, -1));
  }
  if (action === "cancel-workflow-run") {
    const slot = workflowActivity, id = button.dataset.id;
    if (!workflowActivityCurrent(slot) || !$("#workflow-active-runs")?.contains(button) || !slot.page?.items.some((run) => run.id === id) || workflowStopping.has(id)) return;
    workflowStopping.add(id); renderOtherView();
    try {
      await request(`/workflow-runs/${encodeURIComponent(id)}/cancel`, { method: "POST" });
      if (workflowActivityCurrent(slot)) { toast("已提交停止请求"); refreshWorkflowActivity(); }
    } finally { workflowStopping.delete(id); if (workflowActivityCurrent(slot)) renderOtherView(); }
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
  if (action === "reload-extensions") await requestExtensions(true);
  if (action === "refresh-settings-status") {
    if (state.view === "settings" && state.settingsTab === "connections") return requestExtensions(false);
    collectSettingsForm(); button.disabled = true; try { await refreshSettingsResources(); renderOtherView(); renderChrome(); } finally { button.disabled = false; }
  }
  if (action === "retry-delivery") {
    const delivery = state.channels?.deliveries?.find((item) => item.id === button.dataset.id);
    if (!delivery) throw new Error("发送记录已变化，请刷新状态。");
    const perform = async (allowUnknown = false) => {
      collectSettingsForm(); button.disabled = true;
      try { await retryChannelDelivery(request, delivery, { allowUnknown }); await refreshSettingsResources(); renderOtherView(); toast("已提交重试"); }
      finally { button.disabled = false; }
    };
    if (delivery.state === "unknown") confirmAction("重试发送", "上次发送结果未知，再次发送可能重复。", "仍然重试", () => perform(true));
    else await perform();
  }
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button || button.disabled) return;
  try {
    if (button.dataset.action) await handleAction(button.dataset.action, button);
    else if (button.dataset.view) await switchView(button.dataset.view);
    else if (button.dataset.session) await loadSession(button.dataset.session, button);
    else if (button.dataset.settingsTab) switchSettingsTab(button.dataset.settingsTab);
    else if (button.dataset.modelRole) { collectSettingsForm(); state.modelRole = button.dataset.modelRole; renderOtherView(); }
    else if (button.dataset.memoryTab) await selectMemoryTab(button.dataset.memoryTab);
    else if (button.dataset.notificationTab) { state.notificationTab = button.dataset.notificationTab; renderOtherView(); }
  } catch (error) { showError(error); }
});

$("#composer").addEventListener("submit", sendMessage);
$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target; const button = $('button[type="submit"]', form); const input = $("#login-password");
  button.disabled = true; $("#login-error").hidden = true;
  try { await request("/auth/login", { method: "POST", body: { password: input.value } }); input.value = ""; await connect(); }
  catch (error) { $("#login-error").textContent = error.message; $("#login-error").hidden = false; }
  finally { button.disabled = false; }
});
$("#message-input").addEventListener("input", () => { draftRevision += 1; resizeComposer(); });
$("#message-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("#composer").requestSubmit(); }
});
$("#mode-select").addEventListener("change", async (event) => {
  const previous = state.mode; state.mode = event.target.value;
  try { if (state.activeSession) { await request(`/sessions/${encodeURIComponent(state.activeSession.id)}`, { method: "PATCH", body: { mode: state.mode } }); state.activeSession.mode = state.mode; } }
  catch (error) { state.mode = previous; showError(error); }
  renderComposer();
});
$("#file-input").addEventListener("change", (event) => { void addFiles(event.target.files); event.target.value = ""; });
$("#conversation-scroll").addEventListener("scroll", onConversationScroll, { passive: true });
$("#conversation-scroll").addEventListener("wheel", (event) => {
  if (event.deltaY) armConversationScroll(event, "wheel", event.deltaY < 0);
}, { passive: true });
$("#conversation-scroll").addEventListener("keydown", (event) => {
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (["ArrowUp", "PageUp", "Home"].includes(event.key) || event.key === " " && event.shiftKey) armConversationScroll(event, "key", true);
  else if (["ArrowDown", "PageDown", "End", " "].includes(event.key)) armConversationScroll(event, "key", false);
});
$("#conversation-scroll").addEventListener("touchstart", (event) => {
  conversationTouchY = mainScrollTarget(event) && event.touches?.length === 1 ? event.touches[0].clientY : null;
}, { passive: true });
$("#conversation-scroll").addEventListener("touchmove", (event) => {
  if (conversationTouchY === null || event.touches?.length !== 1) return;
  const y = event.touches[0].clientY;
  if (y !== conversationTouchY) armConversationScroll(event, "touch", y > conversationTouchY);
  conversationTouchY = y;
}, { passive: true });
for (const type of ["touchend", "touchcancel"]) $("#conversation-scroll").addEventListener(type, () => {
  conversationTouchY = null;
  if (conversationScrollIntent?.kind === "touch") conversationScrollIntent = null;
}, { passive: true });
$("#conversation-scroll").addEventListener("pointerdown", (event) => {
  if (event.target === event.currentTarget && event.button === 0 && event.pointerType !== "touch") armConversationScroll(event, "scrollbar", null);
}, { passive: true });
for (const type of ["pointerup", "pointercancel"]) window.addEventListener(type, () => { if (conversationScrollIntent?.kind === "scrollbar") conversationScrollIntent = null; }, { passive: true });
window.addEventListener("blur", () => { conversationScrollIntent = null; conversationTouchY = null; });
window.addEventListener("resize", scheduleConversationTail, { passive: true });
$("#dialog-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!dialogSubmit) return;
  const callback = dialogSubmit;
  const content = $("#dialog-content").firstElementChild;
  const current = () => dialogSubmit === callback && $("#dialog-content").firstElementChild === content && $("#app-dialog").open;
  const submit = $("#dialog-submit"); submit.disabled = true; $("#dialog-error").hidden = true;
  try { await callback(event.target); if (current()) $("#app-dialog").close(); }
  catch (error) { if (current()) { $("#dialog-error").textContent = error.message ?? String(error); $("#dialog-error").hidden = false; } }
  finally { if (current()) submit.disabled = false; }
});
$("#app-dialog").addEventListener("close", () => {
  if ($("#app-dialog").open) return;
  releaseMemorySourceRetry();
  releaseTaskDialog();
  releaseMemoryDialog();
  releaseHistoryDialog();
  const saved = dialogTeamView; dialogTeamView = null;
  if (state.auth.authenticated && state.view === "team") restoreTeamView($("#other-view"), saved);
  const memoryReturn = dialogMemoryReturn; dialogMemoryReturn = null;
  if (state.auth.authenticated && state.view === "memories" && memoryReturn) {
    const root = $("#other-view"), fallback = $$("[data-action=memory-evidence]", root).find((button) => button.dataset.id === memoryReturn.id);
    const target = [memoryReturn.trigger, memoryReturn.pathButton, fallback].find((button) => button && root.contains(button));
    if (target) target.focus({ preventScroll: true });
    else { const heading = $(".view-heading h2", root); if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); } }
  }
  const historyReturn = dialogHistoryReturn; dialogHistoryReturn = null;
  if (state.auth.authenticated && historyReturn?.view === state.view) {
    const action = historyReturn.action ?? "workflow-session";
    const fallback = state.view === "chat" ? $("#message-input") : $$("[data-action]", $("#other-view")).find((button) => button.dataset.action === action && (action === "delete-session" ? button.dataset.id : button.dataset.session) === historyReturn.sessionId);
    const target = historyReturn.trigger && document.contains(historyReturn.trigger) ? historyReturn.trigger : fallback;
    if (target) target.focus({ preventScroll: true });
    else if (state.view === "records" && recordDirectoryCurrent(recordDirectory)) recordDirectory.position?.focus({ preventScroll: true });
    else { const heading = $(".view-heading h2", $("#other-view")); if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); } }
  }
});
document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); if (!$("#app-dialog").open) void switchView("chat"); }
  if (event.key === "Escape" && state.view !== "chat" && !$("#app-dialog").open) void switchView("chat");
  if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key) && event.target.getAttribute("role") === "tab") {
    const tabs = $$('[role="tab"]', event.target.closest('[role="tablist"]'));
    const index = tabs.indexOf(event.target);
    const nextIndex = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    event.preventDefault(); tabs[nextIndex]?.click();
    requestAnimationFrame(() => $('#other-view [role="tab"][aria-selected="true"]')?.focus());
  }
});
function recoverConnection() {
  if (document.visibilityState === "visible" && !connectionCurrent(connection) && $("#auth-gate").hidden) void connect();
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") { voiceInput.cancel(); state.voiceSetupController?.release(); }
  recoverConnection();
});
window.addEventListener("online", recoverConnection);
window.addEventListener("pageshow", (event) => { if (event.persisted) recoverConnection(); });
window.addEventListener("pagehide", () => { invalidateConnection(); voiceInput.cancel(); if (memoryDialog || historyDialog) $("#app-dialog").close(); releaseMemoryDialog(); releaseHistoryDialog(); cancelMemorySearch(); releaseMemoryPath(); releaseMemorySources(); releaseWorkflowActivity(); releaseRecordDirectory(); });

fillIcons();
conversationLayout = initializeConversationWidth($("#chat-view"));
initializeTheme(); renderThemeControl();
document.addEventListener("muli:themechange", renderThemeControl);
void connect();
