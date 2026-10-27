/** Browser-only interactive demo. Nothing in this module calls a MuLi server. */
const encoder = new TextEncoder();
const clone = (value) => structuredClone(value);
const timestamp = () => Date.now() / 1000;
let sequence = 0;
const identifier = (prefix) => `${prefix}-${++sequence}`;
const response = (value, status = 200) => new Response(JSON.stringify(value), {
  status, headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
});
const failure = (detail, status = 400) => response({ detail, preview: true }, status);
const model = (name = "") => ({ protocol: "openai_chat", model: name, base_url: "", api_key_set: false, vision: !!name, stream: true, max_output_tokens: null, timeout: 60, max_retries: 1, reasoning: {}, thinking: {}, extra_body: {}, extra_headers: {}, max_response_bytes: 1048576 });
const mcpStatus = (config) => Object.entries(config?.mcpServers ?? {}).map(([name, server]) => ({ name, status: server.enabled === false ? "disabled" : "connected" }));

/** A fresh tab starts with fresh demo data; credentials are never persisted. */
export function createPreviewState() {
  const now = timestamp();
  const main = { id: "preview-main", name: "沐璃", title: "与沐璃对话", role: "main", mode: "auto", status: "idle", created_at: now, updated_at: now };
  const notes = "# 项目整理示例\n\n先梳理目标，再分解任务，由队友执行、复核后汇总。\n\n此文件由前端预览生成，没有读取本机项目。\n";
  const mcp = { mcpServers: { "示例资料库": { url: "https://mcp.preview.invalid/mcp", enabled: true, headers: {} } } };
  return {
    settings: {
      models: { main: model("MuLi · 交互演示"), worker: model(), reviewer: model(), vision: model(), compactor: model(), memory_extractor: model() },
      persona: { name: "沐璃", user_name: "你", introduction: "陪你把想法慢慢变成可完成的事。", prompt: "用自然、清楚的中文交流，尊重主人的选择。", append: "需要操作前，先说明计划与影响。" },
      default_mode: "auto", workspace_roots: ["/preview/workspace"], disabled_tools: [],
      limits: { max_parallel_agents: 3, max_pending_runs: 8, max_tool_steps: 24, max_run_seconds: 180, max_file_bytes: 10485760, max_output_chars: 6000, max_context_chars: 32000 },
      search: { provider: "searxng", base_url: "https://search.preview.invalid", max_results: 5 },
      memory: { enabled: true, auto_extract: true, max_candidates: 24, max_prompt_memories: 6 },
      compaction: { enabled: true, threshold: 0.8 }, attention: { enabled: true },
      channels: {
        onebot: { enabled: true, base_url: "http://preview.invalid", owner_ids: ["7"], allowed_groups: [], receive_files: true, account_id: "preview-bot", token_set: false },
        telegram: { enabled: false, owner_ids: [], allowed_groups: [], receive_files: true, token_set: false },
        qq: { enabled: false, owner_ids: [], allowed_groups: [], receive_files: true, token_set: false },
        weixin: { enabled: false, owner_ids: [], allowed_groups: [], receive_files: true, token_set: false },
        proactive: { external_enabled: false, default_destination: { kind: "web" }, memory_preference: null },
      },
      media: {
        embedding: { model: "preview-embedding", base_url: "https://preview.invalid/v1", dimensions: 768 },
        reranker: { model: "preview-reranker", base_url: "https://preview.invalid/v1" },
        asr: { model: "preview-asr", base_url: "https://preview.invalid/v1" },
        tts: { model: "preview-tts", base_url: "https://preview.invalid/v1", voice: "沐璃" },
      },
      voice: { enabled: false, tts_enabled: false, reply_enabled: false, wake_command: [], chunk_seconds: 2 },
      web_voice: { enabled: false }, computer: { enabled: false, backend: "atspi", capture: { enabled: false, max_file_bytes: 10485760 } },
      mcp,
      chiliarch: { enabled: false, base_url: "https://preview.invalid", agent_id: "", credential_set: false },
      plugins: { enabled: true, disabled: [] }, updates: { check_on_start: false },
      notifications: { enabled: false }, server: { host: "127.0.0.1", port: 8765, password_set: false },
      uploads: { max_file_bytes: 10485760 },
    },
    previewLimits: { max_upload_bytes: 20971520, max_uploads: 20, max_items: 50, max_message_chars: 64000 },
    sessions: [main], messages: { [main.id]: [] }, runs: {}, approvals: [], events: [], eventId: 0,
    notifications: [
      { id: "notice-welcome", title: "欢迎来到沐璃", content: "这是可操作的前端预览。发一条消息，体验规划、工具轨迹、审批和流式回答；设置只保存在当前页面。", type: "info", read: false, created_at: now },
      { id: "notice-review", title: "示例周报已整理", content: "队友已完成资料整理与交叉复核，示例附件可在附件页下载。", type: "task", read: true, created_at: now - 3600 },
    ],
    memories: [
      { id: "memory-style", content: "你偏好简洁的中文说明，先给结论，再给必要依据。", kind: "preference", layer: "preference", source: "演示手动记录", source_id: null, trusted_owner: true, confidence: 1, version: 1, status: "active", entities: ["你", "中文说明"], relations: [{ subject: "你", predicate: "偏好", object: "中文说明" }], created_at: now - 7200, updated_at: now - 7200 },
      { id: "memory-project", content: "项目整理时，先确认当前目标，再把执行和复核交给不同队友。", kind: "procedural", layer: "procedural", source: "演示手动记录", source_id: null, trusted_owner: true, confidence: 1, version: 1, status: "active", entities: ["项目整理", "协作"], relations: [{ subject: "项目整理", predicate: "使用", object: "协作" }], created_at: now - 3600, updated_at: now - 3600 },
    ], memoryHistory: {},
    memorySources: [
      { source_id: "preview-source-notes", source: "前端演示资料", content: notes, session_id: main.id, role: "tool", principal: "external:preview", trusted_owner: false, revision: "a".repeat(64), created_at: now - 3600, job: { state: "failed", attempts: 3, last_error: "RuntimeError" } },
      { source_id: "preview-source-guide", source: "前端演示资料", content: "这是已整理的示例原文🙂。\n所有操作只改变本页演示数据。", session_id: main.id, role: "tool", principal: "external:preview", trusted_owner: false, revision: "b".repeat(64), created_at: now - 7200, job: { state: "done", attempts: 1, last_error: "" } },
    ].map((item) => ({ ...item, total_bytes: encoder.encode(item.content).length })),
    members: [
      { id: "demo-researcher", session_id: "preview-researcher", name: "资料整理", role: "worker", parent_id: main.id, task: "整理示例项目结构与待办", status: "completed", generation: 1, last_message: "结构梳理与清单已完成。", created_at: now - 3600 },
      { id: "demo-reviewer", session_id: "preview-reviewer", name: "独立复核", role: "reviewer", parent_id: main.id, task: "检查整理结果是否遗漏边界", status: "completed", generation: 1, last_message: "已核对范围，保留需要你确认的事项。", created_at: now - 3500 },
    ], teamMessages: [],
    workflows: [{ id: "workflow-review", name: "资料整理与复核", description: "先整理资料，再独立复核，最后汇总为一份简报。", mode: "auto", steps: [{ id: "research", title: "整理资料", role: "worker", prompt: "整理资料与来源。", depends_on: [] }, { id: "review", title: "独立复核", role: "reviewer", prompt: "核对结论与来源。", depends_on: ["research"] }], runs: [], created_at: now - 86400, updated_at: now - 3600 }],
    workflowRuns: [], schedules: [{ id: "schedule-weekly", name: "每周资料回顾", enabled: true, blocked: false, due_at: now + 86400, interval_seconds: 604800, last_run_id: null, last_status: null }],
    reminders: [{ id: "reminder-review", title: "回顾本周目标", content: "看看已有成果、未核验部分和下一步。", due_at: now + 3600, interval_seconds: 604800, enabled: true, fired_count: 0, created_at: now }],
    uploads: [{ id: "demo-notes", name: "项目整理示例.md", mime_type: "text/markdown", size: encoder.encode(notes).length, source: "preview", created_at: now - 3600 }],
    files: new Map([["demo-notes", new Blob([notes], { type: "text/plain;charset=utf-8" })]]),
    service: { supported: true, unit: "muli-preview.service", installed: true, active: true, enabled: true, log: ["[预览] 所有服务操作均为内存模拟，不会安装或重启系统服务。"] },
    management: { plugins: [{ id: "preview-notes", name: "资料整理示例", version: "demo", status: "loaded" }], plans: [], update: { status: "checked", available: false, current: "交互预览", latest: "交互预览", checked_at: now } },
    extensions: { generation: 1, phase: "loaded", skills: { enabled: true, count: 2 }, mcp: mcpStatus(mcp), preview: true },
    voiceSetup: { state: "idle", preview: true }, voiceEnrolled: false,
    requests: [],
  };
}

function merge(target, changes) {
  for (const [key, value] of Object.entries(changes)) {
    if (["__proto__", "prototype", "constructor"].includes(key)) continue;
    if (value && typeof value === "object" && !Array.isArray(value)) {
      if (!target[key] || typeof target[key] !== "object" || Array.isArray(target[key])) target[key] = {};
      merge(target[key], value);
    } else target[key] = clone(value);
  }
  return target;
}

/** Exported for offline tests; the same handlers power the actual app preview. */
export function createPreviewBackend({ state = createPreviewState(), delay = 140 } = {}) {
  const listeners = new Set(), timers = new Set(), pendingDecisions = new Map();
  let disposed = false;
  const later = (fn, milliseconds = delay) => {
    const timer = setTimeout(() => { timers.delete(timer); if (!disposed) fn(); }, milliseconds);
    timers.add(timer); return timer;
  };
  const emit = (type, data = {}, sessionId = null) => {
    const event = { id: String(++state.eventId), type, data: clone(data), session_id: sessionId, created_at: timestamp() };
    state.events.push(event);
    // Keep the demo bounded even during a long preview session.
    if (state.events.length > 1200) state.events.splice(0, state.events.length - 1200);
    for (const listener of listeners) if (!listener.sessionId || listener.sessionId === sessionId) listener.send(event);
    return event;
  };
  const notify = (title, content, sessionId = null) => {
    const item = { id: identifier("notice"), title, content, type: "task", read: false, session_id: sessionId, created_at: timestamp() };
    state.notifications.unshift(item); state.notifications.splice(50); emit("notification.created", item); return item;
  };
  const session = (id) => state.sessions.find((item) => item.id === id);
  const trim = (items) => items.splice(state.previewLimits.max_items);
  const pendingRuns = () => Object.values(state.runs).filter((run) => ["running", "approval", "queued"].includes(run.status));
  const atCapacity = (items) => items.length >= state.previewLimits.max_items;
  const runLimit = () => Math.min(state.previewLimits.max_items, Math.max(1, Math.floor(Number(state.settings.limits.max_pending_runs) || 8)));
  const activeRun = (id) => Object.values(state.runs).find((run) => run.session_id === id && ["running", "approval", "queued"].includes(run.status));
  const addMessage = (sessionId, role, content, attachmentIds = []) => {
    const list = state.messages[sessionId] ??= [];
    const item = { id: identifier("message"), session_id: sessionId, role, principal: role === "user" ? "owner" : "assistant", source: "web", content, created_at: timestamp(), delivery_status: "delivered", seq: (list.at(-1)?.seq ?? 0) + 1, revision: "preview-1", attachment_ids: attachmentIds, attachments: state.uploads.filter((file) => attachmentIds.includes(file.id)) };
    list.push(item); if (list.length > 100) list.splice(0, list.length - 100); return item;
  };
  const detail = (id) => {
    const item = session(id), run = activeRun(id);
    return { session: item, messages: state.messages[id] ?? [], events: [], after: String(state.eventId), live: { session_id: id, status: run?.status ?? "idle", run_id: run?.id ?? null, pending_approvals: state.approvals.filter((approval) => approval.session_id === id && approval.status === "pending") } };
  };
  const tool = (run, name, args, result, complete) => {
    if (run.status !== "running") return;
    const data = { call_id: identifier("call"), run_id: run.id, name, arguments: args };
    emit("tool.started", data, run.session_id);
    later(() => { if (run.status === "running") { emit("tool.completed", { ...data, result, status: "completed" }, run.session_id); complete?.(); } }, delay * 2);
  };
  const finish = (run, text, attachmentIds = []) => {
    const chunks = text.match(/[\s\S]{1,9}/gu) ?? [];
    let offset = 0;
    const write = () => {
      if (run.status !== "running") return;
      if (offset < chunks.length) { emit("message.delta", { content: chunks[offset++], run_id: run.id }, run.session_id); later(write, Math.min(delay, 35)); return; }
      const message = addMessage(run.session_id, "assistant", text, attachmentIds);
      run.status = "completed"; run.result = text;
      const item = session(run.session_id); if (item) { item.status = "idle"; item.updated_at = timestamp(); }
      emit("message.completed", { message, run_id: run.id }, run.session_id);
      emit("run.completed", { run_id: run.id, result_message_id: message.id }, run.session_id);
      const queued = Object.values(state.runs).find((entry) => entry.session_id === run.session_id && entry.status === "queued");
      if (queued) later(() => startRun(queued));
    };
    write();
  };
  const execute = (run, call, approved) => {
    if (!["running", "approval"].includes(run.status)) return;
    run.status = "running";
    if (!approved) {
      emit("tool.completed", { ...call, result: { status: "not_executed", reason: "主人拒绝了本次演示操作" }, status: "not_executed" }, run.session_id);
      finish(run, "好的，这一步没有执行。\n\n我保留了前面的整理结果，不会写入文件或继续委派。你可以调整要求后再告诉我，也可以换到 Yolo 模式体验完整流程。\n\n这次预览没有访问真实文件或外部服务。");
      return;
    }
    emit("tool.started", call, run.session_id);
    later(() => {
      if (run.status !== "running") return;
      const isMemory = run.kind === "memory";
      if (isMemory) {
        const item = { id: identifier("memory"), content: run.prompt.replace(/^.*?记住[：:，,\s]*/u, "") || run.prompt, kind: "preference", layer: "preference", source: "演示对话", source_id: null, trusted_owner: true, confidence: 1, version: 1, status: "active", entities: [], relations: [], created_at: timestamp(), updated_at: timestamp() };
        state.memories.unshift(item); trim(state.memories); emit("memory.updated", { memory_ids: [item.id] });
      }
      emit("tool.completed", { ...call, result: isMemory ? { saved: true, preview: true } : { path: "演示简报.md", created: true, preview: true }, status: "completed" }, run.session_id);
      if (isMemory) {
        finish(run, `记下了：${run.prompt.replace(/^.*?记住[：:，,\s]*/u, "")}\n\n你可以在「设置与管理 → 记忆」里查看、更正或删除。这条记录只在本次页面预览内保留，刷新后会恢复示例数据。`);
        return;
      }
      const member = { id: identifier("agent"), name: "资料整理队友", role: "worker", parent_id: run.session_id, task: run.prompt, status: "running", generation: 1, created_at: timestamp() };
      state.members.unshift(member); trim(state.members); emit("team.updated", { agent_id: member.id });
      tool(run, "spawn_agent", { name: member.name, task: "按计划整理示例资料并核对结果" }, { agent_id: member.id, status: "running" }, () => {
        member.status = "completed"; member.last_message = "已整理目标、行动清单与待确认事项。"; emit("team.updated", { agent_id: member.id });
        tool(run, "wait_agents", { agent_ids: [member.id] }, { reports: [{ agent_id: member.id, result: member.last_message }] }, () => {
          notify("整理结果已就绪", "演示队友已完成整理，你可以在对话和附件页查看结果。", run.session_id);
          finish(run, `整理好了。\n\n### 先做这三件事\n1. **明确目标**：把“${run.prompt.slice(0, 48)}${run.prompt.length > 48 ? "…" : ""}”拆成可以核对的结果。\n2. **分工与复核**：队友整理资料，另一轮检查遗漏和边界。\n3. **交付并跟进**：把关键结论、待办与需要你确认的部分放在一起。\n\n我把示例整理文档附在下面了。你可以继续让我细化下一步，也可以去团队、记忆或设置页看看。\n\n*以上为浏览器内的流程演示，没有运行真实模型或工具。*`, ["demo-notes"]);
        });
      });
    }, delay * 2);
  };
  const ask = (run) => {
    const call = { call_id: identifier("call"), run_id: run.id, name: run.kind === "memory" ? "memory_save" : "fs_write", arguments: run.kind === "memory" ? { content: run.prompt } : { path: "演示简报.md", content: "保存本次整理结果（仅演示内存）" } };
    if (run.mode === "yolo") { execute(run, call, true); return; }
    run.status = "approval";
    const approval = { id: identifier("approval"), session_id: run.session_id, run_id: run.id, tool: call.name, arguments: call.arguments, risk: "write", reason: run.mode === "ask" ? "Ask 模式：这项演示操作需要你的批准。" : "Auto 模式：演示一个需要主人确认的操作。", status: "pending", created_at: timestamp() };
    state.approvals = state.approvals.filter((item) => item.status === "pending");
    state.approvals.push(approval); pendingDecisions.set(approval.id, (approved) => execute(run, call, approved));
    emit("approval.requested", approval, run.session_id);
  };
  const startRun = (run) => {
    if (!["queued", "running"].includes(run.status)) return;
    run.status = "running"; const item = session(run.session_id); if (item) item.status = "running";
    emit("run.started", { run_id: run.id }, run.session_id);
    emit("conversation.injected", { message_ids: [run.message_id] }, run.session_id);
    later(() => {
      if (run.status !== "running") return;
      emit("reasoning.delta", { content: "先确认你的目标，看看已有资料，再把需要操作的步骤单独列出。\n\n第一步是核对范围：我会区分已经知道的信息、可以从资料中确认的内容，以及仍需你作决定的部分。当前页面只使用演示资料，不会读取真实项目或连接外部搜索。\n\n接下来先查询已有记忆，再整理相关资料。工具会按实际出现的先后顺序留在对话里；返回结果只更新原来的工具块，不会把后面的内容移到前面。你可以展开参数与结果，也可以继续补充要求。\n\n如果下一步需要保存内容，我会单独提出审批。批准后再安排队友完成整理和复核；拒绝就停下这一步，保留已经得到的资料。最后把结论、待办和示例附件一起交给你。\n", run_id: run.id }, run.session_id);
      tool(run, "memory_query", { query: run.prompt.slice(0, 80) }, { items: state.memories.slice(0, 2).map((memory) => ({ content: memory.content, source: memory.source })) }, () => {
        if (run.kind === "memory") ask(run);
        else tool(run, "web_search", { query: run.prompt.slice(0, 80) }, { items: [{ title: "示例资料：目标、分工和验收", snippet: "将任务拆成资料整理、执行和独立复核。", source: "本地演示数据，无网络请求" }] }, () => {
          emit("reasoning.delta", { content: "已有信息足够形成一个小计划。下一步先保存简报，再让队友整理并复核；需要你确认的操作会单独显示。\n", run_id: run.id }, run.session_id); ask(run);
        });
      });
    });
  };
  const cancelRun = (run) => {
    run.status = "cancelled";
    for (const item of state.approvals.filter((entry) => entry.run_id === run.id && entry.status === "pending")) {
      item.status = "cancelled"; pendingDecisions.delete(item.id); emit("approval.resolved", { id: item.id, decision: "cancelled" }, run.session_id);
    }
    const item = session(run.session_id); if (item) item.status = "idle";
    emit("run.cancelled", { run_id: run.id }, run.session_id);
  };
  const stream = (sessionId, after, signal) => {
    let listener, close;
    const body = new ReadableStream({
      start(controller) {
        let closed = false;
        close = () => { if (!closed) { closed = true; listeners.delete(listener); signal?.removeEventListener("abort", close); controller.close(); } };
        listener = { sessionId, close, send(event) { if (!closed) controller.enqueue(encoder.encode(`id: ${event.id}\ndata: ${JSON.stringify(event)}\n\n`)); } };
        listeners.add(listener);
        controller.enqueue(encoder.encode(": preview connected\n\n"));
        for (const event of state.events) if (Number(event.id) > after && (!sessionId || event.session_id === sessionId)) listener.send(event);
        if (signal?.aborted) close(); else signal?.addEventListener("abort", close, { once: true });
      },
      cancel() { listeners.delete(listener); signal?.removeEventListener("abort", close); },
    });
    return new Response(body, { headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-store" } });
  };
  const page = (items, url, key = "next_after") => {
    const limit = Math.min(50, Math.max(1, Number(url.searchParams.get("limit")) || 20));
    const after = url.searchParams.get("after_id");
    const sorted = [...items].sort((a, b) => a.id.localeCompare(b.id));
    const candidates = after ? sorted.filter((item) => item.id > after) : sorted;
    const selected = candidates.slice(0, limit), more = candidates.length > selected.length;
    return { items: selected, has_more: more, [key]: more ? selected.at(-1).id : null };
  };
  const status = () => ({ configured: true, model: state.settings.models.main.model || "MuLi · 交互演示", mode: state.settings.default_mode, running: Object.values(state.runs).filter((run) => ["running", "approval"].includes(run.status)).length, version: "前端交互预览", platform: "浏览器 · 仅内存", limits: state.settings.limits, tools: ["memory_query", "web_search", "fs_write", "spawn_agent", "wait_agents", "memory_save", "file_publish", "channel_send"].map((name) => ({ name, description: `预览 ${name} 的交互与运行轨迹`, risk: ["memory_query", "web_search", "wait_agents"].includes(name) ? "read" : "write", enabled: !state.settings.disabled_tools.includes(name) })), mcp: state.extensions.mcp, extensions: state.extensions, chiliarch: { status: state.settings.chiliarch.enabled ? "connected" : "disabled" }, computer: { available: false }, notifications: { available: false }, server: { restart_required: false } });

  async function fetchPreview(input, options = {}) {
    const url = new URL(typeof input === "string" || input instanceof URL ? input : input.url, "http://preview.local");
    const method = (options.method ?? input?.method ?? "GET").toUpperCase();
    const signal = options.signal ?? input?.signal;
    if (signal?.aborted) throw new DOMException("请求已取消", "AbortError");
    if (disposed) return failure("预览已经关闭，请刷新页面。", 410);
    const path = url.pathname.replace(/^\/api(?=\/|$)/u, "") || "/";
    state.requests.push({ method, path }); if (state.requests.length > 300) state.requests.shift();
    if (method === "GET" && (path === "/events" || /^\/sessions\/[^/]+\/events$/u.test(path))) return stream(path === "/events" ? null : decodeURIComponent(path.split("/")[2]), Number(url.searchParams.get("after")) || 0, signal);
    let body = options.body;
    if (body === undefined && typeof input?.text === "function" && !["GET", "HEAD"].includes(method)) body = await input.text();
    if (typeof body === "string") { try { body = JSON.parse(body); } catch { return failure("演示请求需要 JSON。", 400); } }
    body ??= {};
    const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
    const id = parts[1];
    if (path === "/auth/status") return response({ required: false, authenticated: true });
    if (["/auth/login", "/auth/logout"].includes(path) && method === "POST") return response({ authenticated: true, preview: true });
    if (path === "/ui/event-position") return response({ after: String(state.eventId) });
    if (path === "/status" && method === "GET") return response(status());
    if (path === "/extensions" && method === "GET") return response(state.extensions);
    if (path === "/extensions/reload" && method === "POST") {
      if (state.extensions.phase !== "queued") {
        state.extensions = { ...state.extensions, phase: "queued" };
        later(() => { state.extensions = { ...state.extensions, generation: state.extensions.generation + 1, phase: "loaded", mcp: mcpStatus(state.settings.mcp) }; });
      }
      return response(state.extensions);
    }
    if (path === "/conversation" && method === "GET") return response(detail(state.sessions[0].id));
    if (path === "/settings") {
      if (method === "GET") return response(state.settings);
      if (method === "PATCH") {
        merge(state.settings, body);
        notify("预览设置已保存", "本次更改仅在当前页面内生效，没有连接模型、渠道或系统服务。");
        return response({ saved: true, preview: true });
      }
    }
    if (parts[0] === "sessions") {
      if (parts.length === 1 && method === "GET") return response({ items: state.sessions });
      if (parts.length === 2 && method === "GET") return session(id) ? response(detail(id)) : failure("没有这条演示会话。", 404);
      if (parts.length === 2 && method === "PATCH" && session(id)) { merge(session(id), { mode: body.mode ?? session(id).mode }); emit("session.updated", session(id), id); return response(session(id)); }
      if (parts.length === 2 && method === "DELETE" && id !== state.sessions[0].id) { state.sessions = state.sessions.filter((item) => item.id !== id); return response({ deleted: true }); }
      if (parts[2] === "stop" && method === "POST") { for (const run of Object.values(state.runs).filter((entry) => entry.session_id === id && ["running", "approval", "queued"].includes(entry.status))) cancelRun(run); return response({ stopped: true }); }
      if (parts[2] === "messages" && method === "POST" && session(id)) {
        if (typeof body.content !== "string" || !body.content.trim()) return failure("先写一点想做的事吧。");
        if (body.content.length > state.previewLimits.max_message_chars) return failure("消息超过前端预览长度上限。", 413);
        if (pendingRuns().length >= runLimit()) return failure("演示任务队列已满，请完成审批或停止当前任务后再发送。", 429);
        const message = addMessage(id, "user", body.content.trim(), body.attachment_ids ?? []);
        const busy = activeRun(id);
        const run = { id: identifier("run"), session_id: id, message_id: message.id, prompt: message.content, kind: /记住|偏好|记忆/u.test(message.content) ? "memory" : "research", mode: body.mode ?? session(id).mode, status: busy ? "queued" : "running", created_at: timestamp() };
        state.runs[run.id] = run;
        const terminal = Object.values(state.runs).filter((entry) => ["completed", "cancelled"].includes(entry.status));
        for (const old of terminal.slice(0, Math.max(0, terminal.length - 30))) delete state.runs[old.id];
        later(() => { emit("message.completed", { message }, id); if (!busy) startRun(run); else emit("message.queued", { message }, id); }, 60);
        return response({ message, run_id: run.id, queued: !!busy });
      }
      if (parts[2] === "messages" && method === "GET") {
        const list = state.messages[id] ?? [];
        return response({ session_id: id, messages: list.map((item) => ({ ...item, content_provenance: "accepted", content_bytes: encoder.encode(item.content).length, content_truncated: false })), through_seq: list.at(-1)?.seq ?? 0, has_older: false, next_before_seq: null });
      }
    }
    if (path === "/approvals" && method === "GET") return response({ items: state.approvals.filter((item) => item.status === "pending") });
    if (parts[0] === "approvals" && parts.length === 2 && method === "POST") {
      const item = state.approvals.find((entry) => entry.id === id && entry.status === "pending");
      if (!item || !["approve", "reject"].includes(body.decision)) return failure("这项演示审批已结束。", 409);
      item.status = body.decision === "approve" ? "approved" : "rejected";
      const decision = pendingDecisions.get(id); pendingDecisions.delete(id);
      emit("approval.resolved", { id, decision: body.decision }, item.session_id);
      later(() => decision?.(body.decision === "approve"), 60);
      return response({ resolved: true });
    }
    if (parts[0] === "notifications") {
      if (parts.length === 1 && method === "GET") return response({ items: state.notifications });
      const item = state.notifications.find((entry) => entry.id === id);
      if (item && method === "PATCH") { item.read = body.read === true; emit("notification.updated", item); return response(item); }
    }
    if (parts[0] === "reminders") {
      if (parts.length === 1 && method === "GET") return response({ items: state.reminders });
      if (parts.length === 1 && method === "POST") { if (atCapacity(state.reminders)) return failure("演示提醒数量已达上限。", 429); const item = { ...body, id: identifier("reminder"), enabled: true, fired_count: 0, created_at: timestamp() }; state.reminders.unshift(item); emit("reminder.updated", item); return response(item); }
      const item = state.reminders.find((entry) => entry.id === id);
      if (item && method === "PATCH") { merge(item, body); emit("reminder.updated", item); return response(item); }
      if (item && method === "DELETE") { state.reminders = state.reminders.filter((entry) => entry.id !== id); return response({ deleted: true }); }
    }
    if (path === "/memory/sources" && method === "GET") {
      const filter = url.searchParams.get("state") ?? "", limit = Number(url.searchParams.get("limit") ?? 20);
      const states = ["queued", "processing", "retry", "failed", "done", "deferred"];
      if (!Number.isInteger(limit) || limit < 1 || limit > 100 || (filter && !states.includes(filter))) return failure("来源目录参数无效。", 422);
      let start = 0;
      if (url.searchParams.has("cursor")) {
        let token; try { token = JSON.parse(url.searchParams.get("cursor")); } catch { return failure("演示来源目录游标无效。"); }
        if (!Array.isArray(token) || token.length !== 2 || token[0] !== filter || !Number.isInteger(token[1]) || token[1] < 0 || token[1] > state.memorySources.length) return failure("演示来源目录游标不属于当前筛选。");
        start = token[1];
      }
      const candidates = state.memorySources.slice(start, start + limit), items = candidates.filter((item) => !filter || item.job.state === filter).map(({ content, ...item }) => item);
      const next = start + candidates.length, more = next < state.memorySources.length;
      return response({ items, examined: candidates.length, omitted: candidates.length - items.length, has_more: more, next_cursor: more ? JSON.stringify([filter, next]) : null, consistency: "live", order: "newest_registered_first" });
    }
    if (path === "/memory/source" && method === "GET") {
      const item = state.memorySources.find((entry) => entry.source_id === url.searchParams.get("id"));
      if (!item) return failure("演示来源不存在。", 404);
      const offset = Number(url.searchParams.get("offset_bytes") ?? 0), length = Number(url.searchParams.get("length_bytes") ?? state.settings.limits.max_output_chars);
      const revision = url.searchParams.get("revision"), raw = encoder.encode(item.content);
      if (revision !== null && revision !== item.revision) return failure("演示来源版本已经变化。", 409);
      if (!Number.isSafeInteger(offset) || offset < 0 || offset > raw.length || (offset && !revision) || !Number.isSafeInteger(length) || length < 1 || (raw[offset] >= 0x80 && raw[offset] <= 0xbf)) return failure("原文字节位置无效。");
      let end = Math.min(raw.length, offset + Math.min(length, state.settings.limits.max_output_chars * 4)), content;
      while (end >= offset) {
        try { content = new TextDecoder("utf-8", { fatal: true }).decode(raw.slice(offset, end)); break; } catch { end--; }
      }
      if (!content && offset < raw.length) return failure("本段不足以容纳一个完整字符。");
      const { content: original, job, ...metadata } = item;
      return response({ ...metadata, id: item.source_id, digest: item.revision, field: "content", content, offset_bytes: offset, length_bytes: end - offset, next_offset_bytes: end, total_bytes: raw.length, has_more: end < raw.length, truncated: offset > 0 || end < raw.length });
    }
    if (path === "/memory/sources/retry" && method === "POST") {
      if (Object.keys(body).sort().join(",") !== "revision,source_id") return failure("只接受准确来源和版本。", 422);
      if (!state.settings.memory.enabled || state.settings.disabled_tools.includes("memory_retry_source")) return failure("演示记忆重试未启用。");
      const item = state.memorySources.find((entry) => entry.source_id === body.source_id);
      if (!item) return failure("演示来源不存在。", 404);
      if (item.revision !== body.revision) return failure("演示来源版本已经变化。", 409);
      const previous = item.job.state, eligible = ["failed", "deferred"].includes(previous);
      const pending = state.memorySources.filter((entry) => ["queued", "processing", "retry"].includes(entry.job.state)).length;
      const accepted = eligible && pending < (state.settings.memory.max_pending_jobs ?? state.previewLimits.max_items);
      const receipt = { source_id: item.source_id, revision: item.revision, previous_state: previous, state: accepted ? "queued" : previous, accepted, reason: accepted ? "queued" : eligible ? "capacity_full" : previous === "done" ? "already_done" : "already_pending" };
      if (accepted) {
        item.job = { state: "queued", attempts: 0, last_error: "" };
        later(() => { item.job.state = "processing"; item.job.attempts = 1; emit("memory.pipeline", { source_id: item.source_id }); });
        later(() => { item.job.state = "done"; emit("memory.updated", { memory_ids: [], source_id: item.source_id }); }, delay * 2);
      }
      return response(receipt);
    }
    if (parts[0] === "memories") {
      if (id === "graph" && method === "GET") { const edges = state.memories.flatMap((item) => item.relations ?? []); return response({ nodes: [...new Set(edges.flatMap((edge) => [edge.subject, edge.object]))], edges }); }
      if (parts.length === 1 && method === "GET") { const query = (url.searchParams.get("query") ?? "").toLowerCase(); return response({ items: state.memories.filter((item) => item.content.toLowerCase().includes(query)), revision: state.eventId }); }
      if (parts.length === 1 && method === "POST") { if (atCapacity(state.memories)) return failure("演示记忆数量已达上限。", 429); const item = { ...body, id: identifier("memory"), layer: body.layer ?? body.kind ?? "semantic", source: "演示手动记录", source_id: null, trusted_owner: true, confidence: 1, version: 1, status: "active", created_at: timestamp(), updated_at: timestamp() }; state.memories.unshift(item); emit("memory.updated", { memory_ids: [item.id] }); return response(item); }
      const item = state.memories.find((entry) => entry.id === id);
      if (item && parts[2] === "revisions" && method === "GET") return response({ items: [...(state.memoryHistory[id] ?? []), item].reverse() });
      if (item && parts.length === 2 && method === "GET") return response(item);
      if (item && parts.length === 2 && method === "PATCH") { const history = state.memoryHistory[id] ??= []; history.unshift(clone(item)); trim(history); merge(item, body); item.version++; item.updated_at = timestamp(); emit("memory.updated", { memory_ids: [id] }); return response(item); }
      if (item && method === "DELETE") { state.memories = state.memories.filter((entry) => entry.id !== id); delete state.memoryHistory[id]; emit("memory.forgotten", { memory_ids: [id] }); return response({ deleted: true }); }
    }
    if (parts[0] === "team") {
      if (parts.length === 1 && method === "GET") { const active = url.searchParams.get("active_only") === "true", offset = Number(url.searchParams.get("offset")) || 0, limit = Number(url.searchParams.get("limit")) || 50; const members = state.members.filter((item) => !active || ["running", "queued", "approval"].includes(item.status)); return response({ members: members.slice(offset, offset + limit), messages: state.teamMessages, total: members.length, limit, offset, active_only: active, has_more: members.length > offset + limit }); }
      const member = state.members.find((item) => item.id === id);
      if (member && parts[2] === "messages" && method === "POST") { member.last_message = body.content; const item = { id: identifier("team-message"), sender: "主人", content: body.content, created_at: timestamp() }; state.teamMessages.unshift(item); trim(state.teamMessages); emit("team.updated", { agent_id: id }); notify("协作消息已记录", "示例队友已收到你的补充，未启动真实任务。"); return response(item); }
      if (member && parts[2] === "stop" && method === "POST") { member.status = "cancelled"; emit("team.updated", { agent_id: id }); return response({ stopped: true }); }
    }
    if (parts[0] === "workflows") {
      if (parts.length === 1 && method === "GET") return response({ items: state.workflows });
      if (parts.length === 1 && method === "POST") { if (atCapacity(state.workflows)) return failure("演示工作流数量已达上限。", 429); const item = { ...body, id: identifier("workflow"), runs: [], created_at: timestamp(), updated_at: timestamp() }; state.workflows.unshift(item); return response(item); }
      const item = state.workflows.find((entry) => entry.id === id);
      if (item && parts.length === 2 && method === "DELETE") { state.workflows = state.workflows.filter((entry) => entry.id !== id); return response({ deleted: true }); }
      if (item && parts[2] === "run" && method === "POST") {
        if (state.workflowRuns.filter((run) => run.status === "running").length >= runLimit()) return failure("演示工作流队列已满。", 429);
        const run = { id: identifier("workflow-run"), workflow_id: id, name: item.name, status: "running", created_at: timestamp(), updated_at: timestamp(), nodes: item.steps.map((step) => ({ ...step, status: "running" })) };
        state.workflowRuns.unshift(run); trim(state.workflowRuns); item.runs.unshift(run); trim(item.runs); emit("workflow.updated", { workflow_id: id, run });
        later(() => { if (run.status !== "running") return; run.status = "completed"; run.updated_at = timestamp(); run.result = "演示流程已完成：资料整理 → 独立复核 → 结果汇总。"; run.nodes.forEach((node) => { node.status = "completed"; node.result = "演示步骤已完成。"; }); emit("workflow.updated", { workflow_id: id, run }); notify("工作流演示完成", run.result); }, 1600);
        return response({ run });
      }
    }
    if (path === "/workflow-runs/active" && method === "GET") return response(page(state.workflowRuns.filter((item) => ["pending", "queued", "running"].includes(item.status)), url));
    if (parts[0] === "workflow-runs" && parts[2] === "cancel" && method === "POST") { const item = state.workflowRuns.find((entry) => entry.id === id); if (item) { item.status = "cancelled"; emit("workflow.updated", { workflow_id: item.workflow_id, run: item }); return response({ cancelled: true }); } }
    if (path === "/workflow-schedules" && method === "GET") return response(page(state.schedules, url));
    if (parts[0] === "workflow-schedules" && parts[2] === "cancel" && method === "POST") { const item = state.schedules.find((entry) => entry.id === id); if (item) { item.enabled = false; return response({ cancelled: true }); } }
    if (parts[0] === "uploads") {
      if (parts.length === 1 && method === "GET") return response({ items: state.uploads });
      if (parts.length === 1 && method === "POST") {
        const blob = options.body instanceof Blob ? options.body : new Blob([options.body ?? ""]);
        if (blob.size > state.settings.uploads.max_file_bytes) return failure("文件超过预览附件大小上限。", 413);
        if (state.files.size >= state.previewLimits.max_uploads) return failure("预览附件数量已达上限，请刷新页面重置演示数据。", 429);
        const totalBytes = [...state.files.values()].reduce((total, file) => total + file.size, 0);
        if (totalBytes + blob.size > state.previewLimits.max_upload_bytes) return failure("预览附件总容量已达上限，请刷新页面重置演示数据。", 413);
        const headers = new Headers(options.headers); let name = headers.get("X-File-Name") || "附件"; try { name = decodeURIComponent(name); } catch { /* Keep the visible original name. */ }
        const item = { id: identifier("upload"), name, mime_type: blob.type || "application/octet-stream", size: blob.size, source: "preview", created_at: timestamp() };
        state.uploads.unshift(item); state.files.set(item.id, blob); emit("upload.created", item); return response(item);
      }
      if (["content", "download"].includes(parts[2]) && method === "GET") return state.files.has(id) ? new Response(state.files.get(id)) : failure("演示附件不存在。", 404);
    }
    if (path === "/media/transcribe" && method === "POST") return response({ text: "这是一段演示转写。你可以编辑文字，再决定是否发送。", preview: true });
    if (path === "/channels" && method === "GET") return response({ channels: Object.fromEntries(Object.entries(state.settings.channels).filter(([name]) => name !== "proactive").map(([name, item]) => [name, { enabled: item.enabled, available: true, running: item.enabled, owner_count: item.owner_ids?.length ?? 0, group_count: item.allowed_groups?.length ?? 0, account_id: item.account_id ?? "preview-bot" }])), outbox: { sent: 0, pending: 0, unknown: 0 }, deliveries: [], proactive: { attention_enabled: true, model_configured: true, memory_status: { reason: "not_configured" } } });
    if (path === "/voice" && method === "GET") return response({ preview: true, enabled: state.settings.voice.enabled, available: false, running: false, browser_asr_ready: false, health: { state: "idle", code: null, occurrence_id: null }, setup: state.voiceSetup, speaker: { enrolled: state.voiceEnrolled }, reason: "前端预览不访问麦克风或声学设备；单次设置只返回模拟结果。" });
    if (path === "/voice/setup" && method === "POST") {
      if (body.confirmed !== true) return failure("请确认本次模拟录音。", 400);
      if (state.settings.voice.enabled) return failure("请先关闭后台语音唤醒。", 409);
      if (!["enroll", "verify"].includes(body.operation)) return failure("未知设置操作。", 422);
      const seconds = body.seconds ?? 5;
      if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 1 || seconds > 15) return failure("录音时长无效。", 422);
      if (body.operation === "verify" && !state.voiceEnrolled) return failure("请先录制参考声纹。", 409);
      state.voiceSetup = { id: body.id ?? crypto.randomUUID(), operation: body.operation, state: "completed", requested_seconds: seconds, preview: true, ...(body.operation === "enroll" ? { enrolled: true } : { wake_detected: true, speaker_configured: true, speaker_verified: true }) };
      if (body.operation === "enroll") state.voiceEnrolled = true;
      return response(state.voiceSetup);
    }
    if (path === "/voice/setup/cancel" && method === "POST") {
      if (typeof body.id !== "string" || !body.id) return failure("缺少本次设置标识。", 422);
      if (body.id !== state.voiceSetup.id) return failure("语音设置已变化，未取消其它操作。", 409);
      return response(state.voiceSetup);
    }
    if (path === "/capture" && method === "GET") return response({ enabled: state.settings.computer.capture.enabled, available: false, running: false, reason: "前端预览不读取桌面或摄像头。" });
    if (path === "/service") {
      if (method === "GET") return response(state.service);
      if (method === "POST" && ["install", "start", "restart", "stop", "enable", "disable"].includes(body.action)) { if (body.action === "install") state.service.installed = true; if (["start", "restart"].includes(body.action)) state.service.active = true; if (body.action === "stop") state.service.active = false; if (["enable", "disable"].includes(body.action)) state.service.enabled = body.action === "enable"; state.service.log.unshift(`[预览] ${body.action}：仅更新演示状态。`); trim(state.service.log); return response(state.service); }
    }
    if (path === "/management" && method === "GET") return response(state.management);
    if (path === "/management/transactions" && method === "POST" && body.action === "configure") { merge(state.settings, body.changes ?? {}); const plan = { id: identifier("plan"), action: "configure", status: "completed", changed_sections: Object.keys(body.changes ?? {}), created_at: timestamp() }; state.management.plans.unshift(plan); trim(state.management.plans); notify("配置事务演示完成", "设置已留在本页内存，没有重启任何服务。 "); return response(plan); }
    if (path === "/integrations/chiliarch/pair" && method === "POST") { state.settings.chiliarch.agent_id = "preview-agent"; state.settings.chiliarch.credential_set = true; return response({ paired: true, preview: true }); }
    if (path === "/session-records" && method === "GET") return response({ items: state.sessions, has_more: false, next_cursor: null });
    return failure(`演示暂未实现 ${method} ${path}；没有发送到真实服务。`, 501);
  }
  return {
    state, fetch: fetchPreview, emit,
    dispose() { disposed = true; for (const timer of timers) clearTimeout(timer); timers.clear(); for (const listener of [...listeners]) listener.close(); pendingDecisions.clear(); },
  };
}

/** The actual preview click fence; ordinary application buttons are untouched. */
export function installPreviewNavigation({ root, pageURL, isLocalBlob, onBlocked }) {
  const page = new URL(pageURL);
  const guard = (event) => {
    const anchor = event.target?.closest?.("a[href]") ?? event.target?.parentElement?.closest?.("a[href]");
    if (!anchor) return;
    const value = anchor.getAttribute("href");
    let allowed = false;
    try {
      const url = new URL(value, page);
      allowed = (url.origin === page.origin && url.pathname === page.pathname && url.search === page.search && (url.hash !== "" || value === "#")) || isLocalBlob(url.href);
    } catch { /* Malformed and unsupported destinations stay inside the demo. */ }
    if (allowed) return;
    event.preventDefault(); event.stopPropagation(); onBlocked?.();
  };
  for (const type of ["click", "auxclick", "contextmenu"]) root.addEventListener(type, guard, true);
  return () => { for (const type of ["click", "auxclick", "contextmenu"]) root.removeEventListener(type, guard, true); };
}

/** Native attachment elements must also stay local: they do not use fetch(). */
function installPreviewURLs(backend) {
  const urls = new Map(), restores = [];
  const blobURL = (key, blob) => { if (!urls.has(key)) urls.set(key, URL.createObjectURL(blob)); return urls.get(key); };
  const localURL = (value) => {
    if (typeof value !== "string") return value;
    let url; try { url = new URL(value, location.href); } catch { return value; }
    if (url.origin !== location.origin || !url.pathname.startsWith("/api/")) return value;
    const match = /^\/api\/uploads\/([^/]+)\/(?:content|download)$/u.exec(url.pathname);
    if (match) { const id = decodeURIComponent(match[1]); return blobURL(id, backend.state.files.get(id) ?? new Blob(["演示附件不可用。"], { type: "text/plain;charset=utf-8" })); }
    if (url.pathname === "/api/export") return blobURL(`export-${backend.state.eventId}`, new Blob([JSON.stringify({ preview: true, messages: backend.state.messages, memories: backend.state.memories }, null, 2)], { type: "application/json" }));
    return blobURL("api-info", new Blob(["沐璃交互预览\n\n本页面没有运行真实 API。设置、对话、审批、团队与附件交互均由 preview.mjs 在浏览器内模拟。刷新页面会重置数据。"], { type: "text/plain;charset=utf-8" }));
  };
  for (const [type, key] of [[HTMLAnchorElement, "href"], [HTMLImageElement, "src"], [HTMLMediaElement, "src"]]) {
    const descriptor = Object.getOwnPropertyDescriptor(type.prototype, key);
    if (!descriptor?.set) continue;
    Object.defineProperty(type.prototype, key, { ...descriptor, set(value) { descriptor.set.call(this, localURL(value)); } });
    restores.push(() => Object.defineProperty(type.prototype, key, descriptor));
  }
  const removeNavigation = installPreviewNavigation({
    root: document, pageURL: location.href, isLocalBlob: (value) => [...urls.values()].includes(value),
    onBlocked() {
      const region = document.querySelector("#toast-region");
      if (!region) return;
      const note = document.createElement("div"); note.className = "toast"; note.setAttribute("role", "status");
      note.textContent = "交互预览不会打开外部链接或访问真实接口。";
      region.replaceChildren(note);
    },
  });
  return () => { removeNavigation(); restores.forEach((restore) => restore()); for (const value of urls.values()) URL.revokeObjectURL(value); };
}

export function installPreview({ target = globalThis, state = createPreviewState(), delay } = {}) {
  const backend = createPreviewBackend({ state, delay });
  const originalFetch = target.fetch;
  target.fetch = (input, options) => {
    const url = new URL(typeof input === "string" || input instanceof URL ? input : input.url, target.location?.href ?? "http://preview.local");
    if (url.pathname.startsWith("/api/") || url.pathname === "/openapi.json") return backend.fetch(input, options);
    if (target.location && url.origin === target.location.origin && /\.(?:mjs|js|css|svg|png|jpg|jpeg|webp|ico|woff2?)(?:$)/iu.test(url.pathname)) return originalFetch.call(target, input, options);
    return Promise.resolve(failure("前端预览不访问外部服务。", 501));
  };
  const restoreURLs = typeof document === "undefined" ? () => {} : installPreviewURLs(backend);
  return { ...backend, dispose() { backend.dispose(); target.fetch = originalFetch; restoreURLs(); } };
}

if (typeof document !== "undefined" && document.documentElement.hasAttribute("data-muli-preview")) {
  const backend = installPreview();
  window.addEventListener("pagehide", () => backend.dispose(), { once: true });
  await import("./app.mjs");
}
