/** Browser-only interactive demo. Nothing in this module calls a MuLi server. */
const encoder = new TextEncoder();
const watchOutputBytes = 64 * 1024; // Server MAX_MATCH_PROOF_BYTES, also for the demo envelope.
const previewNotesUploadId = "b".repeat(32);
const commitmentStates = ["open", "in_progress", "blocked", "done", "cancelled"];
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

// A short local WAV tone demonstrates attachment controls, not speech synthesis.
function previewSpeechAudio() {
  const rate = 8000, samples = rate / 4, bytes = new Uint8Array(44 + samples * 2), data = new DataView(bytes.buffer);
  const text = (offset, value) => bytes.set(encoder.encode(value), offset);
  text(0, "RIFF"); data.setUint32(4, bytes.length - 8, true); text(8, "WAVE"); text(12, "fmt ");
  data.setUint32(16, 16, true); data.setUint16(20, 1, true); data.setUint16(22, 1, true);
  data.setUint32(24, rate, true); data.setUint32(28, rate * 2, true); data.setUint16(32, 2, true); data.setUint16(34, 16, true);
  text(36, "data"); data.setUint32(40, samples * 2, true);
  for (let index = 0; index < samples; index++) data.setInt16(44 + index * 2, Math.round(4000 * (1 - index / samples) * Math.sin(2 * Math.PI * 440 * index / rate)), true);
  return new Blob([bytes], { type: "audio/wav" });
}

/** A fresh tab starts with fresh demo data; credentials are never persisted. */
export function createPreviewState() {
  const now = timestamp();
  const main = { id: "preview-main", name: "沐璃", title: "与沐璃对话", role: "main", mode: "auto", status: "idle", created_at: now, updated_at: now };
  const notes = "# 项目整理示例\n\n先梳理目标，再分解任务，由队友执行、复核后汇总。\n\n此文件由前端预览生成，没有读取本机项目。\n";
  const notesAttachmentId = "a".repeat(32), notesRevision = "a".repeat(64);
  const mcp = { mcpServers: { "示例资料库": { url: "https://mcp.preview.invalid/mcp", enabled: true, headers: {} } } };
  // Persisted-looking examples only: no coordinator, mailbox or worker runs here.
  const ownerQuestions = ["open", "saved", "queued", "claimed", "cancelled", "stale"].map((kind, index) => {
    const id = `oq:${"a".repeat(64)}:${String(index + 1).repeat(64)}`, answered = ["saved", "queued", "claimed"].includes(kind);
    const body = "按已有资料的版本整理，保留未核验项。（本页示例）";
    const owner = { input_id: `preview-owner-answer-${kind}`, input_seq: index + 1, archive_seq: index + 1, run_id: `preview-owner-run-${kind}`, attachment_ids: [], reply_origin: { kind: "web" }, original_text: kind === "cancelled" ? `/取消问题 ${id}` : `/${kind === "saved" ? "回答问题" : "回答并继续"} ${id}\n${body}`, body: kind === "cancelled" ? null : body, source: "web", principal: "owner" };
    return {
      id, version: 1, task_id: "preview-task-waiting", main_id: main.id, worker_id: "demo-researcher",
      message: "用哪一个文件版本做最终整理？🙂\n请确认后再继续，旧要求仍需保留。（本页示例）", attachment_ids: [previewNotesUploadId],
      status: answered ? "answered" : kind === "cancelled" ? "cancelled" : "open", notice: "本页模拟记录；未连接真实队友，排队与领取仅为示例状态。",
      created_at: now - 3600, updated_at: now - 1800,
      author: { run_id: "preview-original-question-run", generation: 1, tool: { message_id: "preview-original-question-message", seq: 1 } },
      answer: answered ? { revision: String(index + 1).repeat(64), interpretation: "按主人原答保存。（本页示例）", delivery: kind === "saved" ? "record_only" : "dispatch", recorded_at: now - 1800, owner } : null,
      delivery: ["queued", "claimed"].includes(kind) ? { key: `owner-question-answer:${id}`, state: kind, input_id: `preview-answer-delivery-${kind}`, input_seq: index + 10, queued_at: now - 1800, claim: kind === "claimed" ? { run_id: "preview-claimed-run", message_id: "preview-claim-message", seq: 2, input_seq: index + 10, claimed_at: now - 900 } : null, interpretation: "交回原队友。（本页示例）", owner: { input_id: owner.input_id, same_as: "answer.owner" } } : null,
      cancellation: kind === "cancelled" ? { interpretation: "主人关闭了这条示例问题。", owner } : null,
      preview_binding: kind === "stale" ? "stale" : "current",
    };
  });
  return {
    settings: {
      models: { main: model("MuLi · 交互演示"), worker: model(), reviewer: model(), vision: model(), compactor: model(), memory_extractor: model() },
      persona: { name: "沐璃", user_name: "你", introduction: "陪你把想法慢慢变成可完成的事。", prompt: "用自然、清楚的中文交流，尊重主人的选择。", append: "需要操作前，先说明计划与影响。" },
      default_mode: "auto", workspace_roots: ["/preview/workspace"], disabled_tools: [],
      limits: { max_parallel_agents: 3, max_pending_runs: 8, max_tool_steps: 24, max_run_seconds: 180, max_file_bytes: 10485760, max_output_chars: 6000, max_context_chars: 32000 },
      search: { provider: "searxng", base_url: "https://search.preview.invalid", max_results: 5 },
      memory: { enabled: true, auto_extract: true, max_candidates: 24, max_prompt_memories: 6 },
      compaction: { enabled: true, threshold: 0.8 }, attention: { enabled: true, events: ["memory"] },
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
    channelDeliveries: [{ id: "preview-delivery", channel: "onebot", account_id: "preview-bot", owner_id: "7", state: "unknown", confirmed_parts: 1, created_at: now - 1800, parts: [{ text: "这是本页模拟保存的发送正文🙂。\n记录与平台确认状态仅用于演示，没有连接真实消息渠道。" }, { attachment_id: previewNotesUploadId }, { text: "这是尚未确认的后续模拟分段。" }] }],
    notifications: [
      { id: "notice-welcome", title: "欢迎来到沐璃", content: "这是可操作的前端预览。发一条消息，体验规划、工具轨迹、审批和流式回答；设置只保存在当前页面。", type: "info", read: false, created_at: now },
      { id: "notice-review", title: "示例周报已整理", content: "队友已完成资料整理与交叉复核，示例附件可在附件页下载。", type: "task", read: true, created_at: now - 3600 },
    ],
    memories: [
      { id: "memory-style", content: "你偏好简洁的中文说明，先给结论，再给必要依据。", kind: "preference", layer: "preference", source: "演示手动记录", source_id: null, trusted_owner: true, confidence: 1, version: 1, status: "active", entities: ["你", "中文说明"], relations: [{ subject: "你", predicate: "偏好", object: "中文说明" }], created_at: now - 7200, updated_at: now - 7200 },
      { id: "memory-project", content: "项目整理时，先确认当前目标，再把执行和复核交给不同队友。", kind: "procedural", layer: "procedural", source: "演示手动记录", source_id: null, trusted_owner: true, confidence: 1, version: 1, status: "active", entities: ["项目整理", "协作"], relations: [{ subject: "项目整理", predicate: "使用", object: "协作" }], created_at: now - 3600, updated_at: now - 3600 },
    ], memoryHistory: {},
    memoryWatches: [
      { id: "1".repeat(64), question: "以后收藏的笔记里，留意传送带题的功能关系。", match_terms: ["传送带", "功能关系"], state: "active", revision: 1, reply_preference: { kind: "default_web" } },
      { id: "2".repeat(64), question: "留意收藏资料里的社团报名时间。", match_terms: ["社团", "报名"], state: "active", revision: 1, reply_preference: { kind: "registered_channel", channel: "onebot" } },
      { id: "3".repeat(64), question: "留意旧项目的设计回顾。", match_terms: ["设计", "回顾"], state: "cancelled", revision: 2, reply_preference: { kind: "registered_web" } },
    ],
    memorySources: [
      { source_id: `attachment:${notesAttachmentId}`, source: `attachment:${notesAttachmentId}:${notesRevision}`, content: notes, session_id: main.id, role: "tool", principal: "external:attachment", trusted_owner: false, revision: notesRevision, created_at: now - 3600, job: { state: "failed", attempts: 3, last_error: "RuntimeError" } },
      { source_id: "preview-source-guide", source: "前端演示资料", content: "这是已整理的示例原文🙂。\n所有操作只改变本页演示数据。", session_id: main.id, role: "tool", principal: "external:preview", trusted_owner: false, revision: "b".repeat(64), created_at: now - 7200, job: { state: "done", attempts: 1, last_error: "" } },
      ...[["e", "c", "1", "期限：七日\n材料：学生证\n"], ["f", "d", "2", "期限：十日\n材料：身份证\n"]].map(([id, original, revision, content]) => ({ source_id: `attachment:${id.repeat(32)}`, source: `attachment:${id.repeat(32)}:${original.repeat(64)}`, content, session_id: "", role: "tool", principal: "external:attachment", trusted_owner: false, revision: revision.repeat(64), source_revision: original.repeat(64), format: "text", scope: "utf8_text", document_revision: null, attachment_id: id.repeat(32), created_at: now - 1800, job: { state: "done", attempts: 1, last_error: "" }, comparisonDemo: true })),
    ].map((item) => ({ ...item, total_bytes: encoder.encode(item.content).length })),
    members: [
      { id: "demo-researcher", session_id: "preview-researcher", name: "资料整理", role: "worker", parent_id: main.id, commitment_id: "preview-task-waiting", task: "整理示例项目结构与待办", status: "completed", generation: 1, last_message: "结构梳理与清单已完成。", created_at: now - 3600 },
      { id: "demo-reviewer", session_id: "preview-reviewer", name: "独立复核", role: "reviewer", parent_id: main.id, task: "检查整理结果是否遗漏边界", status: "completed", generation: 1, last_message: "已核对范围，保留需要你确认的事项。", created_at: now - 3500 },
    ], teamMessages: [],
    // Saved ledger examples are independent of the demo teammates and reset
    // with this page. Reading them never schedules or resumes any work.
    commitments: [
      { id: "preview-task-revisit", title: "回顾旧项目目标（本页示例）", status: "open", revision: 3, next_step: "核对完整说明，再由你决定如何提问。", evidence: "本页示例附件中的项目整理说明。", source_id: `attachment:${notesAttachmentId}`, created_at: now - 172800, updated_at: now - 86400, brief: { goal: "重新梳理旧项目的目标、交付物和待确认事项。\n保留原始背景和每一项约束，再整理一份便于讨论的清单。\n这是一条没有关联队友的本页示例任务。", constraints: ["先核对已有说明，不因旧任务而自动继续执行。", "完整保留中段要求：列出已知事实、尚未核验的内容和需要你确认的问题。", "仅展示本页模拟资料，不访问真实文件、模型或消息渠道。"], evidence_refs: [{ kind: "attachment", id: previewNotesUploadId }] } },
      { id: "preview-task-waiting", title: "确认整理范围（本页示例）", status: "blocked", revision: 2, next_step: "等待你确认需要整理的范围。", created_at: now - 7200, updated_at: now - 3600, control: { epoch: 1, approved_epoch: 0, replan_request_id: null, plan: null }, brief: { goal: "确认示例项目哪些资料需要整理，以及最后希望得到什么。", constraints: ["范围未确认前保持账本受阻状态。", "查看说明或带入草稿都不会启动队友。"], evidence_refs: [] } },
      { id: "preview-task-done", title: "整理项目清单（本页示例）", status: "done", revision: 4, next_step: "清单已保存，后续变化由你决定是否讨论。", created_at: now - 86400, updated_at: now - 3600, brief: { goal: "整理一份示例项目清单，区分已完成事项与待确认问题。", constraints: ["完成状态只表示本页保存的账本记录。", "保留原说明供回顾，不自动创建后续任务。"], evidence_refs: [{ kind: "attachment", id: previewNotesUploadId }] } },
    ], ownerQuestions,
    workflows: [{ id: "workflow-review", name: "资料整理与复核", description: "先整理资料，再独立复核，最后汇总为一份简报。", mode: "auto", steps: [{ id: "research", title: "整理资料", role: "worker", prompt: "整理资料与来源。", depends_on: [] }, { id: "review", title: "独立复核", role: "reviewer", prompt: "核对结论与来源。", depends_on: ["research"] }], runs: [], created_at: now - 86400, updated_at: now - 3600 }],
    workflowRuns: [], schedules: [
      { id: "schedule-weekly", kind: "workflow_schedule", name: "每周资料回顾", enabled: true, blocked: false, due_at: now + 86400, interval_seconds: 604800, last_run_id: null, last_status: null },
      { id: "schedule-calendar", kind: "calendar_workflow_schedule", name: "早晨资料简报", calendar: { version: 1, timezone: "Asia/Shanghai", local_time: "08:00", weekdays: [1, 2, 3, 4, 5] }, enabled: true, blocked: false, due_at: now + 7200, interval_seconds: null, last_run_id: null, last_status: null },
    ],
    reminders: [
      { id: "reminder-review", kind: "reminder", title: "回顾本周目标", content: "看看已有成果、未核验部分和下一步。", due_at: now + 3600, interval_seconds: 604800, enabled: true, fired_count: 0, created_at: now },
      { id: "calendar-study", kind: "calendar_reminder", title: "早晨背单词", content: "每天留一点时间复习。", calendar: { version: 1, timezone: "Asia/Shanghai", local_time: "07:30", weekdays: [1, 2, 3, 4, 5] }, due_at: now + 7200, interval_seconds: null, enabled: true, fired_count: 0, created_at: now },
    ],
    uploads: [{ id: previewNotesUploadId, name: "项目整理示例.md", mime_type: "text/markdown", size: encoder.encode(notes).length, source: "preview", created_at: now - 3600 }],
    files: new Map([[previewNotesUploadId, new Blob([notes], { type: "text/plain;charset=utf-8" })]]),
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
  const commitmentMatches = (item, status) => status === "all" || (status === "active" ? !["done", "cancelled"].includes(item.status) : item.status === status);
  const commitmentMetadata = (item) => {
    const fields = ["id", "status", "revision", "created_at", "updated_at", "title", "next_step", "assignee", "evidence", "source_id"];
    const result = Object.fromEntries(fields.filter((field) => Object.hasOwn(item, field)).map((field) => [field, item[field]]));
    result.has_brief = Object.hasOwn(item, "brief");
    if (item.control) result.control = { epoch: item.control.epoch, approved_epoch: item.control.approved_epoch, paused: item.control.epoch > item.control.approved_epoch, replan_requested: !!item.control.replan_request_id, has_plan: item.control.plan != null };
    return result;
  };
  const trim = (items) => items.splice(state.previewLimits.max_items);
  const pendingRuns = () => Object.values(state.runs).filter((run) => ["running", "approval", "queued"].includes(run.status));
  const atCapacity = (items) => items.length >= state.previewLimits.max_items;
  const runLimit = () => Math.min(state.previewLimits.max_items, Math.max(1, Math.floor(Number(state.settings.limits.max_pending_runs) || 8)));
  const activeRun = (id) => Object.values(state.runs).find((run) => run.session_id === id && ["running", "approval", "queued"].includes(run.status));
  const addMessage = (sessionId, role, content, attachmentIds = []) => {
    const list = state.messages[sessionId] ??= [];
    const item = { id: identifier("message"), session_id: sessionId, role, principal: role === "user" ? "owner" : "assistant", source: "web", content, created_at: timestamp(), delivery_status: "delivered", seq: (list.at(-1)?.seq ?? 0) + 1, revision: "preview-1", evidence_revision: identifier("revision"), attachment_ids: attachmentIds, attachments: state.uploads.filter((file) => attachmentIds.includes(file.id)) };
    list.push(item); if (list.length > 100) list.splice(0, list.length - 100); return item;
  };
  const detail = (id) => {
    const item = session(id), run = activeRun(id);
    return { session: item, messages: state.messages[id] ?? [], events: state.events.filter((event) => event.session_id === id), after: String(state.eventId), live: { session_id: id, status: run?.status ?? "idle", run_id: run?.id ?? null, pending_approvals: state.approvals.filter((approval) => approval.session_id === id && approval.status === "pending") } };
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
      run.status = "completed"; run.result = text; run.result_message_id = message.id;
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
          finish(run, `整理好了。\n\n### 先做这三件事\n1. **明确目标**：把“${run.prompt.slice(0, 48)}${run.prompt.length > 48 ? "…" : ""}”拆成可以核对的结果。\n2. **分工与复核**：队友整理资料，另一轮检查遗漏和边界。\n3. **交付并跟进**：把关键结论、待办与需要你确认的部分放在一起。\n\n我把示例整理文档附在下面了。你可以继续让我细化下一步，也可以去团队、记忆或设置页看看。\n\n*以上为浏览器内的流程演示，没有运行真实模型或工具。*`, [previewNotesUploadId]);
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
  const status = () => ({ configured: true, preview: true, model: state.settings.models.main.model || "MuLi · 交互演示", mode: state.settings.default_mode, running: Object.values(state.runs).filter((run) => ["running", "approval"].includes(run.status)).length, version: "前端交互预览", platform: "浏览器 · 仅内存", limits: state.settings.limits, tools: ["memory_query", "web_search", "fs_write", "spawn_agent", "wait_agents", "memory_save", "file_publish", "channel_send"].map((name) => ({ name, description: `预览 ${name} 的交互与运行轨迹`, risk: ["memory_query", "web_search", "wait_agents"].includes(name) ? "read" : "write", enabled: !state.settings.disabled_tools.includes(name) })), mcp: state.extensions.mcp, extensions: state.extensions, chiliarch: { status: state.settings.chiliarch.enabled ? "connected" : "disabled" }, computer: { available: false }, notifications: { available: false }, server: { restart_required: false } });
  let capabilityObservation = null;
  const capabilityDirectory = () => {
    const tools = status().tools.map((tool) => ({ ...tool, requires_approval: false, origin: { kind: "unrecorded" } }));
    const signature = JSON.stringify(tools);
    if (capabilityObservation?.signature !== signature) capabilityObservation = { signature, revision: identifier("catalog"), items: tools.map((tool) => ({ id: identifier("capability"), ...tool })) };
    return { scope: "registered_tools", revision: capabilityObservation.revision, complete: true, items: capabilityObservation.items, preview: true };
  };
  const capabilityResponse = (value) => encoder.encode(JSON.stringify(value)).byteLength <= state.settings.limits.max_output_chars * 4
    ? response(value) : failure({ code: "capability_payload_too_large" }, 413);

  // This local demo backend reads only its bounded in-memory text blobs. It does
  // not pretend to extract PDF/DOCX or use these digests to authorize real work.
  const sha256 = async (raw) => {
    if (!globalThis.crypto?.subtle) throw new Error("当前演示环境无法核对资料版本。");
    return [...new Uint8Array(await globalThis.crypto.subtle.digest("SHA-256", raw))].map((value) => value.toString(16).padStart(2, "0")).join("");
  };
  const collectionFits = (value) => Array.from(JSON.stringify(value)).length <= state.settings.limits.max_output_chars;
  async function collectionSnapshot(id) {
    const file = state.uploads.find((entry) => entry.id === id), blob = state.files.get(id);
    if (!file || !blob || !/^[a-f0-9]{32}$/u.test(id)) throw new Error("没有这份演示附件。");
    if (!blob.type.startsWith("text/") || blob.size > Math.min(state.settings.uploads.max_file_bytes, state.settings.limits.max_file_bytes)) throw new Error("演示仅提供限额内 UTF-8 文本收藏；文档提取请在真实服务中使用。");
    const raw = new Uint8Array(await blob.arrayBuffer()), content = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(raw);
    if (!content.trim() || content.includes("\0")) throw new Error("演示附件正文无效。");
    const sourceRevision = await sha256(raw), sourceId = `attachment:${id}`, marker = `${sourceId}:${sourceRevision}`;
    const source = { content, source: marker, session_id: "", role: "tool", principal: "external:attachment", trusted_owner: false };
    // Match the existing source_digest field order and JSON spacing for text.
    const packed = "{" + Object.entries(source).map(([key, value]) => `${JSON.stringify(key)}: ${JSON.stringify(value)}`).join(", ") + "}";
    const identity = { attachment_id: id, source_revision: sourceRevision, format: "text", scope: "utf8_text", document_revision: null, source_id: sourceId, registered_revision: await sha256(encoder.encode(packed)), source: marker, session_id: "", role: "tool", principal: "external:attachment", trusted_owner: false };
    return { file, content, identity };
  }

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
    if (path === "/capabilities" && method === "GET") return capabilityResponse(capabilityDirectory());
    const capabilityMatch = /^\/capabilities\/tools\/([^/]+)$/u.exec(path);
    if (capabilityMatch && method === "GET") {
      const catalog = capabilityDirectory(), revision = url.searchParams.get("revision");
      if (!revision) return failure({ code: "capability_revision_required" }, 422);
      if (revision !== catalog.revision) return failure({ code: "capability_catalog_stale" }, 409);
      const tool = catalog.items.find((item) => item.id === decodeURIComponent(capabilityMatch[1]));
      if (!tool) return failure({ code: "capability_not_found" }, 404);
      // These are explicitly simulated contracts, without a real tool executor.
      return capabilityResponse({ scope: catalog.scope, revision, tool: { ...tool, parameters: { type: "object", properties: {}, additionalProperties: true } }, preview: true });
    }
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
    if (["/commitments", "/commitment-directory"].includes(path) && method === "GET") {
      const status = url.searchParams.get("status") ?? "active";
      if (![...commitmentStates, "active", "all"].includes(status)) return failure("未知承诺状态。", 400);
      const records = state.commitments.filter((item) => commitmentMatches(item, status));
      if (path === "/commitments") return response({ items: records.sort((left, right) => right.updated_at - left.updated_at).slice(0, 100).map(commitmentMetadata) });
      const limit = Number(url.searchParams.get("limit") ?? 20), cursor = url.searchParams.get("cursor");
      if (!Number.isSafeInteger(limit) || limit < 1 || limit > 100 || cursor === "") return failure("演示任务目录分页参数无效。", 422);
      let before = null;
      if (cursor !== null) {
        try {
          const token = JSON.parse(cursor);
          if (!Array.isArray(token) || token.length !== 3 || token[0] !== "preview-commitments" || token[1] !== status || typeof token[2] !== "string" || !token[2]) throw new Error("cursor");
          before = token[2];
        } catch { return failure("演示任务目录游标无效或不属于当前筛选。", 400); }
      }
      const candidates = records.filter((item) => before === null || item.id < before).sort((left, right) => left.id < right.id ? 1 : left.id > right.id ? -1 : 0);
      const selected = candidates.slice(0, limit), more = candidates.length > selected.length;
      return response({ items: selected.map(commitmentMetadata), has_more: more, next_cursor: more ? JSON.stringify(["preview-commitments", status, selected.at(-1).id]) : null, scanned: selected.length, json_bytes: selected.reduce((total, item) => total + encoder.encode(JSON.stringify(item)).length, 0), omitted: { oversized: 0, invalid: 0, missing: 0 }, omitted_total: 0, stopped: more ? "limit" : "end", consistency: "bounded_live" });
    }
    if (parts[0] === "commitments" && parts.length === 2 && method === "GET") {
      const item = state.commitments.find((entry) => entry.id === id);
      if (!item) return failure("没有这条演示任务。", 404);
      const control = { epoch: 0, approved_epoch: 0, replan_request_id: null, plan: null, ...item.control };
      const scope = { member_count: state.members.filter((member) => member.commitment_id === id).length, active_workflow_count: 0, can_pause: true, blocked_reason: null };
      return response({ ...item, control: { ...control, paused: control.epoch > control.approved_epoch, has_plan: control.plan != null, plan_epoch: control.plan?.epoch ?? null }, scope });
    }
    if (parts[0] === "commitments" && parts.length === 3 && parts[2] === "inspection" && method === "GET" && url.searchParams.get("section") === "owner_questions") {
      const params = url.searchParams, fail = (code, message, status = ({ evidence_not_found: 404, evidence_changed: 409 })[code] ?? 400) => failure({ code, message }, status);
      const allowed = ["section", "question_status", "cursor", "limit", "revision", "record_id", "record_revision", "offset_bytes", "length_bytes"];
      if ([...params.keys()].some((key) => !allowed.includes(key))) return fail("invalid_arguments", "持久问题不接受其他核查参数。");
      if ([...params.keys()].some((key) => params.getAll(key).length !== 1)) return fail("invalid_arguments", "演示问题参数不能重复。", 422);
      for (const key of ["cursor", "revision", "record_id", "record_revision"]) if (params.has(key) && !params.get(key)) return fail("invalid_arguments", "问题修订与坐标不能为空。", 422);
      const integer = (key, fallback) => params.has(key) && !/^\d+$/u.test(params.get(key)) ? NaN : Number(params.get(key) ?? fallback);
      const limit = integer("limit", 20), offset = integer("offset_bytes", 0), length = integer("length_bytes", state.settings.limits.max_context_chars * 4);
      if (!Number.isSafeInteger(limit) || limit < 1 || limit > 100 || !Number.isSafeInteger(offset) || offset < 0 || !Number.isSafeInteger(length) || length < 1) return fail("invalid_arguments", "问题分页或字节参数无效。", 422);
      const questionStatus = params.get("question_status") || "open", recordId = params.get("record_id"), recordRevision = params.get("record_revision");
      if (!["open", "answered", "cancelled", "stale", "all"].includes(questionStatus)) return fail("invalid_arguments", "问题状态筛选无效。");
      if (recordId !== null && !/^oq:[a-f0-9]{64}:[a-f0-9]{64}$/u.test(recordId)) return fail("invalid_arguments", "问题 ID 必须是完整宿主 ID。");
      if (recordId !== null ? params.has("cursor") || params.has("question_status") : recordRevision !== null || offset !== 0 || params.has("length_bytes")) return fail("invalid_arguments", "原记录坐标不能混合目录筛选，续读需要准确 record_id。");
      const task = clone(state.commitments.find((entry) => entry.id === id));
      if (!task) return fail("evidence_not_found", "没有这条演示任务。");
      const records = clone((state.ownerQuestions ?? []).filter((entry) => entry.task_id === id)).sort((left, right) => left.id < right.id ? -1 : left.id > right.id ? 1 : 0);
      const revision = await sha256(encoder.encode(JSON.stringify([task, records, state.settings.limits.max_context_chars, state.settings.limits.max_output_chars])));
      if (params.has("revision") && params.get("revision") !== revision) return fail("evidence_changed", "问题目录已改变，请从首页重读。");
      const base = { id, section: "owner_questions", authority: "coordination_data" };
      if (recordId !== null) {
        const item = records.find((entry) => entry.id === recordId);
        if (!item) return fail("evidence_not_found", "问题不属于该演示任务。");
        if (item.preview_unavailable || encoder.encode(JSON.stringify(item)).length > state.settings.limits.max_context_chars * 4) return fail("content_unavailable", "这条演示原记录当前不可核查。");
        const token = await sha256(encoder.encode(JSON.stringify(item)));
        if (recordRevision !== null && recordRevision !== token) return fail("evidence_changed", "问题原记录已改变，请从首字节重读。");
        if (offset && recordRevision === null) return fail("invalid_arguments", "续读原文须保持 record_revision。");
        const fields = ["id", "version", "task_id", "main_id", "worker_id", "message", "attachment_ids", "status", "notice", "created_at", "updated_at", "author", "answer", "delivery", "cancellation"];
        const view = Object.fromEntries(fields.map((key) => [key, item[key]])), raw = encoder.encode(JSON.stringify(view));
        if (offset > raw.length || raw[offset] >= 0x80 && raw[offset] <= 0xbf) return fail("invalid_offset", "请使用上一页 next_offset_bytes 核查原文。");
        let end = Math.min(raw.length, offset + Math.min(length, state.settings.limits.max_context_chars * 4));
        while (end > offset && raw[end] >= 0x80 && raw[end] <= 0xbf) end--;
        const text = Array.from(new TextDecoder("utf-8", { fatal: true }).decode(raw.slice(offset, end)));
        const select = (count) => {
          const content = text.slice(0, count).join(""), size = encoder.encode(content).length;
          return { ...base, record_id: recordId, record_revision: token, field: "content", content, offset_bytes: offset, total_bytes: raw.length, length_bytes: size, next_offset_bytes: offset + size, has_more: offset + size < raw.length };
        };
        let low = 0, high = text.length;
        while (low < high) { const middle = Math.ceil((low + high) / 2); if (collectionFits(select(middle))) low = middle; else high = middle - 1; }
        const result = select(low);
        return !collectionFits(result) || !low && result.has_more ? fail("output_budget", "预算不足以返回完整字符与原文坐标。") : response(result);
      }
      let after = "";
      if (params.has("cursor")) {
        try {
          if (params.get("cursor").length > state.settings.limits.max_output_chars) throw new Error("cursor");
          const cursor = JSON.parse(params.get("cursor"));
          if (!Array.isArray(cursor) || cursor.length !== 5 || cursor[0] !== "preview-owner-questions" || cursor[1] !== id || cursor[2] !== revision || cursor[3] !== questionStatus || typeof cursor[4] !== "string" || !records.some((item) => item.id === cursor[4])) throw new Error("cursor");
          after = cursor[4];
        } catch { return fail("evidence_changed", "问题游标失效或筛选已改变。"); }
      }
      const candidates = records.filter((item) => item.id > after), maximum = Math.min(limit, state.settings.limits.max_search_results ?? limit), budget = state.settings.limits.max_context_chars * 4;
      let remaining = budget, result = { ...base, revision, question_status: questionStatus, source_completeness: "candidate_window_only", items: [], scanned: 0, omitted: 0, unavailable: 0, has_more: false, next_cursor: null };
      for (const [index, item] of candidates.slice(0, maximum).entries()) {
        const candidate = { ...result, items: [...result.items], scanned: result.scanned + 1 }, size = encoder.encode(JSON.stringify(item)).length;
        const withinBudget = size <= remaining;
        if (withinBudget) remaining -= size;
        if (item.preview_unavailable || !withinBudget) { candidate.unavailable++; candidate.omitted++; }
        else {
          const paused = (task.control?.epoch ?? 0) > (task.control?.approved_epoch ?? 0), observed = item.preview_binding ?? "current", binding = ["done", "cancelled"].includes(task.status) ? "stale" : observed !== "current" ? observed : paused ? "paused" : "current";
          const status = item.status !== "cancelled" && !["current", "paused"].includes(binding) ? "stale" : item.status;
          if (binding === "unavailable") candidate.unavailable++;
          if (questionStatus !== "all" && status !== questionStatus) candidate.omitted++;
          else candidate.items.push({ question_id: item.id, record_id: item.id, record_revision: await sha256(encoder.encode(JSON.stringify(item))), task_id: id, worker_id: item.worker_id, status, recorded_status: item.status, binding, dispatch_allowed: binding === "current" && item.status === "answered" && item.delivery === null, answered: item.answer !== null, answer_recorded: item.answer !== null, delivery_state: item.delivery?.state ?? "not_dispatched", queued: item.delivery?.state === "queued", claimed: item.delivery?.state === "claimed", cancellation_effect: item.status === "cancelled" ? item.delivery?.state === "claimed" ? "claimed_answer_cannot_be_retracted_task_not_stopped" : "question_closed" : "not_cancelled", task_completion: "separate_evidence_required" });
        }
        candidate.has_more = index + 1 < candidates.length;
        candidate.next_cursor = candidate.has_more ? JSON.stringify(["preview-owner-questions", id, revision, questionStatus, item.id]) : null;
        if (!collectionFits(candidate)) { if (!result.scanned) return fail("output_budget", "预算不足以返回问题目录首项与续页坐标。"); break; }
        result = candidate;
      }
      return collectionFits(result) ? response(result) : fail("output_budget", "问题目录超过输出预算。");
    }
    if (parts[0] === "sessions") {
      if (parts.length === 1 && method === "GET") return response({ items: state.sessions });
      if (parts.length === 2 && method === "GET") return session(id) ? response(detail(id)) : failure("没有这条演示会话。", 404);
      if (parts.length === 2 && method === "PATCH" && session(id)) { merge(session(id), { mode: body.mode ?? session(id).mode }); emit("session.updated", session(id), id); return response(session(id)); }
      if (parts.length === 2 && method === "DELETE" && id !== state.sessions[0].id) { state.sessions = state.sessions.filter((item) => item.id !== id); return response({ deleted: true }); }
      if (parts[2] === "stop" && method === "POST") { for (const run of Object.values(state.runs).filter((entry) => entry.session_id === id && ["running", "approval", "queued"].includes(entry.status))) cancelRun(run); return response({ stopped: true }); }
      if (parts.length === 5 && parts[2] === "messages" && parts[4] === "speech" && method === "POST") {
        if (Object.keys(body).sort().join(",") !== "confirmed,revision,run_id" || body.confirmed !== true || typeof body.run_id !== "string" || typeof body.revision !== "string") return failure("请确认这条准确版本的完整演示答复。", 422);
        const message = state.messages[id]?.find((item) => item.id === parts[3]), run = state.runs[body.run_id];
        if (id !== state.sessions[0].id || !message || message.role !== "assistant" || message.interrupted || message.tool_calls?.length || !message.content.trim()
          || message.evidence_revision !== body.revision || run?.session_id !== id || run.status !== "completed" || run.result_message_id !== message.id || run.result !== message.content) return failure("这条演示答复的完整来源已变化。", 409);
        if (state.settings.disabled_tools.includes("speech_synthesize")) return failure("语音生成工具未启用。");
        const blob = previewSpeechAudio(), totalBytes = [...state.files.values()].reduce((total, file) => total + file.size, 0);
        if (blob.size > state.settings.uploads.max_file_bytes || totalBytes + blob.size > state.previewLimits.max_upload_bytes) return failure("演示附件容量已达上限。", 413);
        if (state.files.size >= state.previewLimits.max_uploads) return failure("演示附件数量已达上限。", 429);
        const fileId = (++sequence).toString(16).padStart(32, "0");
        const attachment = { id: fileId, name: "演示提示音.wav", mime_type: blob.type, size: blob.size, source: "tts", created_at: timestamp(), url: `/api/uploads/${fileId}/download`, preview_url: `/api/uploads/${fileId}/content`, kind: "audio" };
        state.uploads.unshift(attachment); state.files.set(fileId, blob); emit("upload.created", attachment);
        return response({ session_id: id, run_id: run.id, message_id: message.id, revision: body.revision, attachment, preview: true });
      }
      if (parts.length === 3 && parts[2] === "messages" && method === "POST" && session(id)) {
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
      const reminderKind = (item) => item.kind === undefined ? "reminder" : item.kind;
      if (parts.length === 1 && method === "GET") return response({ items: state.reminders.map((item) => ({ ...item, kind: reminderKind(item) })) });
      if (parts.length === 1 && method === "POST") { if (atCapacity(state.reminders)) return failure("演示提醒数量已达上限。", 429); const item = { ...body, kind: "reminder", id: identifier("reminder"), enabled: true, fired_count: 0, created_at: timestamp() }; state.reminders.unshift(item); emit("reminder.updated", item); return response(item); }
      const kind = url.searchParams.get("kind") ?? "reminder";
      if (!["reminder", "calendar_reminder"].includes(kind)) return failure("提醒类别无效。", 422);
      const item = state.reminders.find((entry) => entry.id === id && reminderKind(entry) === kind);
      if (item && method === "PATCH") {
        const fields = ["title", "content", "due_at", "enabled", ...(kind === "reminder" ? ["interval_seconds"] : [])];
        if (Object.keys(body).some((key) => !fields.includes(key))) return failure("提醒更新字段无效。");
        merge(item, body); item.kind = kind; emit("reminder.updated", item); return response(item);
      }
      if (item && method === "DELETE") { state.reminders = state.reminders.filter((entry) => entry.id !== id || reminderKind(entry) !== kind); return response({ deleted: true }); }
      return failure("没有这条演示提醒。", 404);
    }
    if (path === "/memory/watches" && method === "GET") {
      if (state.settings.disabled_tools.includes("memory_watch_list")) return failure("资料关注工具未启用。");
      const after = url.searchParams.get("after_id") ?? "", limit = Number(url.searchParams.get("limit") ?? 20);
      if (!Number.isInteger(limit) || limit < 1 || limit > 100 || !/^(?:[a-f0-9]{64})?$/u.test(after)) return failure("清单分页参数无效。", 422);
      const items = state.memoryWatches.filter((item) => item.id > after).sort((left, right) => left.id.localeCompare(right.id)).slice(0, limit).map((item) => Object.fromEntries(["id", "question", "match_terms", "state", "revision", "reply_preference"].map((field) => [field, item[field]])));
      const maxChars = state.settings.limits.max_output_chars;
      const processing = { enabled: state.settings.memory.enabled && state.settings.attention.enabled && state.settings.attention.events.includes("memory"), paused: false };
      const page = (count) => {
        const selected = items.slice(0, count), more = count < items.length || items.length === limit;
        return { items: selected, after_id: after, has_more: more, next_after_id: more ? selected.at(-1).id : null, processing };
      };
      const fits = (value) => {
        const packed = JSON.stringify(value);
        return Array.from(packed).length <= maxChars && encoder.encode(packed).length <= watchOutputBytes;
      };
      const complete = page(items.length);
      if (fits(complete)) return response(complete);
      let fitted = null;
      for (let count = 1; count < items.length; count++) {
        const candidate = page(count);
        if (!fits(candidate)) break;
        fitted = candidate;
      }
      return fitted ? response(fitted) : failure({ code: "output_budget", message: "完整长期留意条目及分页信息超过当前输出预算。" });
    }
    if (path === "/memory/watches/cancel" && method === "POST") {
      if (state.settings.disabled_tools.includes("memory_watch_cancel")) return failure("资料关注工具未启用。");
      if (Object.keys(body).length !== 1 || typeof body.id !== "string" || !/^[a-f0-9]{64}$/u.test(body.id)) return failure("只接受准确关注 id。", 422);
      const item = state.memoryWatches.find((entry) => entry.id === body.id);
      if (!item) return failure("演示关注不存在。", 400);
      if (item.state === "active") { item.state = "cancelled"; item.revision++; }
      return response({ id: item.id, state: item.state, revision: item.revision });
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
    if (path === "/memory/sources/compare" && method === "POST") {
      if (Object.keys(body).some((key) => !["left_id", "left_revision", "right_id", "right_revision", "cursor"].includes(key))) return failure("比较参数无效。", 422);
      const left = state.memorySources.find((item) => item.source_id === body.left_id && item.comparisonDemo);
      const right = state.memorySources.find((item) => item.source_id === body.right_id && item.comparisonDemo);
      if (!left || !right) return failure("没有这份演示比较资料。", 404);
      if (body.left_revision !== left.revision || body.right_revision !== right.revision) return failure("演示来源版本已经变化。", 409);
      if (body.cursor != null) return failure("这组演示变化只有一页，请从头核对。");
      const identity = ({ content, job, comparisonDemo, ...item }) => item;
      const side = (item) => ({ offset_bytes: 0, length_bytes: item.total_bytes, content: item.content, read_arguments: { id: item.source_id, revision: item.revision, offset_bytes: 0, length_bytes: item.total_bytes } });
      const equal = left.source_id === right.source_id;
      return response({ status: equal ? "equal" : "changed", algorithm: "line_sequence_matcher_v1", left: identity(left), right: identity(right), items: equal ? [] : [{ kind: "replace", left: side(left), right: side(right) }], has_more: false, next_cursor: null });
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
    if (path === "/workflow-schedules" && method === "GET") {
      const after = url.searchParams.get("after_id") ?? "", afterKind = url.searchParams.get("after_kind");
      if (afterKind !== null && !["workflow_schedule", "calendar_workflow_schedule"].includes(afterKind)) return response({ detail: "委托类别无效" }, 422);
      const kindOf = (item) => item.kind === undefined ? "workflow_schedule" : item.kind;
      const sorted = state.schedules.map((item) => ({ ...item, kind: kindOf(item) })).sort((a, b) => a.id < b.id ? -1 : a.id > b.id ? 1 : a.kind < b.kind ? -1 : a.kind > b.kind ? 1 : 0);
      const candidates = sorted.filter((item) => item.id > after || (afterKind !== null && item.id === after && item.kind > afterKind));
      const limit = Math.min(50, Math.max(1, Number(url.searchParams.get("limit")) || 20));
      const items = candidates.slice(0, limit), hasMore = candidates.length > items.length;
      return response({ items, has_more: hasMore, next_after: hasMore ? items.at(-1).id : null, next_after_kind: hasMore ? items.at(-1).kind : null });
    }
    if (parts[0] === "workflow-schedules" && parts[2] === "cancel" && method === "POST") {
      const kind = url.searchParams.get("kind") ?? "workflow_schedule";
      if (!["workflow_schedule", "calendar_workflow_schedule"].includes(kind)) return response({ detail: "委托类别无效" }, 422);
      const item = state.schedules.find((entry) => entry.id === id && (entry.kind === undefined ? "workflow_schedule" : entry.kind) === kind);
      if (!item) return response({ detail: "委托不存在" }, 404);
      item.enabled = false; item.active = false; item.due_at = null; item.next_due_at = null;
      emit("workflow_schedule.updated", { ...item, kind }); return response({ ...item, kind });
    }
    if (parts[0] === "uploads") {
      if (parts.length === 1 && method === "GET") return response({ items: state.uploads });
      if (parts.length === 3 && ((parts[2] === "memory-preview" && method === "GET") || (parts[2] === "memory-collection" && method === "POST"))) {
        if (state.settings.disabled_tools.includes("memory_import_attachment")) return failure("附件收藏工具未启用。");
        if (method === "POST" && (Object.keys(body).sort().join(",") !== "document_revision,registered_revision,source_revision" || ![body.source_revision, body.registered_revision].every((value) => typeof value === "string" && /^[a-f0-9]{64}$/u.test(value)) || body.document_revision !== null)) return failure("请提交这次准备的准确资料版本。", 422);
        let snapshot;
        try { snapshot = await collectionSnapshot(id); } catch (error) { return failure(error.message); }
        if (signal?.aborted) throw new DOMException("请求已取消", "AbortError");
        if (disposed || state.settings.disabled_tools.includes("memory_import_attachment")) return failure("本次收藏已不可用。");
        const { file, content, identity } = snapshot;
        if (method === "GET") {
          const text = Array.from(content).slice(0, 64).join(""), length = encoder.encode(text).length, total = encoder.encode(content).length;
          const preview = { ...identity, filename: file.name, read_only: true, processing_notice: "本次只核对资料；确认时使用当时设置，后台整理可能向已配置服务发送正文。", field: "content", content: text, offset_bytes: 0, length_bytes: length, next_offset_bytes: length, total_bytes: total, has_more: length < total };
          return collectionFits(preview) ? response(preview) : failure({ code: "output_budget", message: "完整资料身份及预览超过当前输出预算。" });
        }
        if (["source_revision", "document_revision", "registered_revision"].some((key) => body[key] !== identity[key])) return failure("资料版本已变化，请重新准备。", 409);
        const existing = state.memorySources.find((item) => item.source_id === identity.source_id);
        if (existing && existing.revision !== identity.registered_revision) return failure("这份资料已登记其它版本，请先核查。", 409);
        const receipt = { ...identity, state: !state.settings.memory.enabled ? "disabled" : existing?.job.state ?? "queued", created: state.settings.memory.enabled && !existing };
        if (!collectionFits(receipt)) return failure({ code: "output_budget", message: "完整收藏回执超过当前输出预算。" });
        if (receipt.created) state.memorySources.unshift({ ...identity, revision: identity.registered_revision, content, total_bytes: encoder.encode(content).length, created_at: timestamp(), job: { state: "queued", attempts: 0, last_error: "" } });
        return response(receipt);
      }
      if (parts.length === 1 && method === "POST") {
        const blob = options.body instanceof Blob ? options.body : new Blob([options.body ?? ""]);
        if (blob.size > state.settings.uploads.max_file_bytes) return failure("文件超过预览附件大小上限。", 413);
        if (state.files.size >= state.previewLimits.max_uploads) return failure("预览附件数量已达上限，请刷新页面重置演示数据。", 429);
        const totalBytes = [...state.files.values()].reduce((total, file) => total + file.size, 0);
        if (totalBytes + blob.size > state.previewLimits.max_upload_bytes) return failure("预览附件总容量已达上限，请刷新页面重置演示数据。", 413);
        const headers = new Headers(options.headers); let name = headers.get("X-File-Name") || "附件"; try { name = decodeURIComponent(name); } catch { /* Keep the visible original name. */ }
        const item = { id: (++sequence).toString(16).padStart(32, "0"), name, mime_type: blob.type || "application/octet-stream", size: blob.size, source: "preview", created_at: timestamp() };
        state.uploads.unshift(item); state.files.set(item.id, blob); emit("upload.created", item); return response(item);
      }
      if (["content", "download"].includes(parts[2]) && method === "GET") return state.files.has(id) ? new Response(state.files.get(id)) : failure("演示附件不存在。", 404);
    }
    if (path === "/media/transcribe" && method === "POST") return response({ text: "这是一段演示转写。你可以编辑文字，再决定是否发送。", preview: true });
    if (parts[0] === "channels" && parts[1] === "deliveries" && method === "GET") {
      const channel = url.searchParams.get("channel"), ownerId = url.searchParams.get("owner_id"), profile = state.settings.channels[channel];
      if (!profile?.enabled || !profile.owner_ids?.includes(ownerId)) return failure("当前演示渠道或主人绑定已不可读取。", 403);
      const header = { source: "local", coverage: "saved_by_muli", consistency: "live", channel, account_id: profile.account_id ?? "preview-bot", owner_id: ownerId, session_id: state.sessions[0].id, preview: true };
      const records = state.channelDeliveries.filter((item) => item.channel === channel && item.account_id === header.account_id && item.owner_id === ownerId);
      const metadata = (item) => ({ state: item.state, confirmed_parts: item.confirmed_parts, total_parts: item.parts.length, created_at: item.created_at });
      if (parts.length === 2) {
        const limit = Number(url.searchParams.get("limit") ?? 20), cursor = url.searchParams.get("cursor"), prefix = ["preview-deliveries", channel, header.account_id, ownerId, header.session_id];
        if (!Number.isSafeInteger(limit) || limit < 1 || limit > 100) return failure("演示记录分页参数无效。", 422);
        let start = 0;
        if (cursor !== null) {
          try {
            const value = JSON.parse(cursor);
            if (!Array.isArray(value) || value.length !== 6 || !prefix.every((field, index) => field === value[index]) || !Number.isSafeInteger(value[5]) || value[5] < 0) throw new Error("cursor");
            start = value[5];
          } catch { return failure("演示游标不属于当前渠道与主人。", 400); }
        }
        const selected = records.slice(start, start + limit).map((item) => ({ id: item.id, kind: "delivery", role: "assistant", ...metadata(item), attachment_ids: item.parts.flatMap((part) => part.attachment_id ? [part.attachment_id] : []) }));
        const result = () => ({ ...header, items: selected, omitted: 0, next_cursor: start + selected.length < records.length ? JSON.stringify([...prefix, start + selected.length]) : null, has_more: start + selected.length < records.length });
        while (selected.length && !collectionFits(result())) selected.pop();
        if (!collectionFits(result()) || !selected.length && start < records.length) return failure("演示发送记录超过当前输出预算。", 400);
        return response(result());
      }
      if (parts.length === 4 && parts[3] === "content") {
        const item = records.find((record) => record.id === parts[2]), part = Number(url.searchParams.get("part") ?? 0), offset = Number(url.searchParams.get("offset_bytes") ?? 0);
        if (!Number.isSafeInteger(part) || part < 0 || !Number.isSafeInteger(offset) || offset < 0) return failure("演示分段或字节位置无效。", 422);
        if (!item || part >= item.parts.length) return failure("没有这个演示发送分段。", 404);
        const selected = item.parts[part], attachmentIds = selected.attachment_id ? [selected.attachment_id] : [], raw = encoder.encode(selected.text ?? "");
        const revision = `sha256:${await sha256(encoder.encode(JSON.stringify([item.id, part, selected.text ?? "", attachmentIds])))}`, requestedRevision = url.searchParams.get("revision");
        if (requestedRevision !== null && requestedRevision !== revision) return failure("演示发送分段已经变化。", 409);
        if (offset > raw.length || offset && !requestedRevision || raw[offset] >= 0x80 && raw[offset] <= 0xbf) return failure("演示发送正文位置无效。", 400);
        const base = { ...header, delivery_id: item.id, part, ...metadata(item), attachment_ids: attachmentIds, revision, field: "content", offset_bytes: offset, total_bytes: raw.length };
        let end = Math.min(raw.length, offset + state.settings.limits.max_output_chars), content = "", value;
        for (; end >= offset; end--) {
          try { content = new TextDecoder("utf-8", { fatal: true }).decode(raw.slice(offset, end)); } catch { continue; }
          value = { ...base, content, length_bytes: end - offset, next_offset_bytes: end, has_more: end < raw.length };
          if (collectionFits(value)) break;
        }
        if (end < offset || end === offset && end < raw.length) return failure("演示发送正文超过当前输出预算。", 400);
        return response(value);
      }
    }
    if (path === "/channels" && method === "GET") return response({ preview: true, channels: Object.fromEntries(Object.entries(state.settings.channels).filter(([name]) => name !== "proactive").map(([name, item]) => [name, { enabled: item.enabled, available: true, running: item.enabled, ...(["telegram", "weixin"].includes(name) ? { receive_health: { state: "idle", code: null, occurrence_id: null } } : {}), owner_count: item.owner_ids?.length ?? 0, group_count: item.allowed_groups?.length ?? 0, account_id: item.account_id ?? "preview-bot" }])), outbox: { sent: 0, pending: 0, unknown: state.channelDeliveries.length }, deliveries: state.channelDeliveries.map((item) => ({ id: item.id, name: item.channel, target: item.owner_id, state: item.state, created_at: item.created_at })), proactive: { attention_enabled: true, model_configured: true, memory_status: { reason: "not_configured" } } });
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
