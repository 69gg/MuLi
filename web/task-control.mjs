/** Owner control state. Network and DOM work stay in app.mjs. */
const ACTIONS = new Set(["pause", "request_replan"]);
const integer = (value) => Number.isSafeInteger(value) && value >= 0;
const record = (value) => value && typeof value === "object" && !Array.isArray(value);

export function taskControlRequestId() {
  if (globalThis.crypto.randomUUID) return globalThis.crypto.randomUUID();
  return Array.from(globalThis.crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/** Only a previously submitted body is restored; arbitrary drafts are not stored. */
export function restoreTaskControlRequest(value) {
  if (!record(value) || !ACTIONS.has(value.action) || !integer(value.expected_epoch) || typeof value.request_id !== "string" || !value.request_id) return null;
  if (value.action === "request_replan" && typeof value.additional_requirements !== "string") return null;
  return Object.freeze({ action: value.action, expected_epoch: value.expected_epoch, request_id: value.request_id, ...(value.action === "request_replan" ? { additional_requirements: value.additional_requirements } : {}) });
}

function validCurrent(value, identifier) {
  return record(value) && value.id === identifier && integer(value.revision)
    && (value.control == null || record(value.control) && integer(value.control.epoch) && integer(value.control.approved_epoch));
}

export function taskControlView(state) {
  const current = state.current;
  const control = current?.control;
  const capable = !!control && typeof current?.scope?.can_pause === "boolean" && !!state.detail?.brief;
  const paused = !!control && control.epoch > control.approved_epoch;
  const busy = state.phase === "submitting";
  const unknown = state.phase === "unknown";
  const fresh = !!state.detail && state.detail.revision === current?.revision;
  let label = "";
  if (capable) label = paused ? control.replan_request_id ? "已交沐璃 · 仍暂停" : "已暂停后续操作" : control.epoch ? "新计划已确认" : "尚未暂停";
  return {
    capable, paused, busy, unknown, fresh, label,
    canPause: capable && fresh && current.scope.can_pause && !busy && !unknown,
    canReplan: capable && fresh && paused && !busy && !unknown,
    workflowBlocked: capable && current.scope.blocked_reason === "workflow_active",
    plan: control?.has_plan !== false && control?.plan && (control.plan_epoch == null || control.plan.epoch === control.plan_epoch) ? control.plan : null,
  };
}

export function createTaskControl(identifier, { pending = null, createRequestId = taskControlRequestId } = {}) {
  const restored = restoreTaskControlRequest(pending);
  const initial = () => ({ id: identifier, current: null, detail: null, draft: "", editing: false, phase: "ready", submission: null, receipt: null, error: "", errorCode: "", conflict: false });
  let state = initial();
  if (restored) state = { ...state, submission: restored, phase: "unknown", draft: restored.additional_requirements ?? "", editing: restored.action === "request_replan" };
  const change = (patch) => { state = { ...state, ...patch }; };
  const acceptCurrent = (value) => {
    if (!validCurrent(value, identifier) || state.current && value.revision < state.current.revision) return false;
    const control = value.control == null ? null : { ...(state.current?.control ?? {}), ...value.control };
    change({ current: { id: identifier, revision: value.revision, control, scope: value.scope ?? null } });
    return true;
  };
  return {
    get state() { return state; },
    acceptCurrent,
    acceptDetail(value) {
      const changedBrief = state.editing && state.detail?.brief && JSON.stringify(state.detail.brief) !== JSON.stringify(value?.brief);
      if (!acceptCurrent(value)) return false;
      change({ detail: value, conflict: state.conflict || !!changedBrief });
      return true;
    },
    edit() { if (!state.submission && taskControlView(state).canReplan) change({ editing: true, error: "", errorCode: "" }); },
    cancelEdit() { if (!state.submission) change({ editing: false, draft: "", conflict: false, error: "", errorCode: "" }); },
    setDraft(value) { if (!state.submission) change({ draft: String(value) }); },
    begin(action) {
      if (state.submission) {
        if (state.phase === "unknown" && action === state.submission.action) {
          change({ phase: "submitting", error: "", errorCode: "" });
          return state.submission;
        }
        throw new Error("请先确认上次提交结果。");
      }
      const view = taskControlView(state);
      if (!ACTIONS.has(action) || !(action === "pause" ? view.canPause : view.canReplan)) throw new Error(view.workflowBlocked ? "先停止工作流或等待完成，再暂停。" : "请刷新并核对任务状态。");
      const submission = Object.freeze({ action, expected_epoch: state.current.control.epoch, request_id: createRequestId(), ...(action === "request_replan" ? { additional_requirements: state.draft } : {}) });
      change({ submission, phase: "submitting", error: "", errorCode: "", conflict: false });
      return submission;
    },
    complete(value) {
      if (!state.submission || !record(value?.receipt) || !validCurrent(value.current, identifier) || !value.current.control) throw new Error("提交结果待确认，请重试原操作。");
      const receipt = value.receipt;
      if (receipt.request_id !== state.submission.request_id || receipt.action !== state.submission.action) throw new Error("提交回执不匹配，请重试原操作。");
      const action = state.submission.action;
      acceptCurrent(value.current); // An old receipt must not roll the current view back.
      change({ submission: null, phase: "ready", receipt, error: "", errorCode: "", ...(action === "request_replan" ? { editing: false, draft: "" } : {}) });
    },
    fail(error) {
      if (error?.status === 401) { state = initial(); return; }
      const detail = error?.details?.detail;
      const code = record(detail) ? detail.code ?? "" : "";
      if (record(detail?.current)) acceptCurrent(detail.current);
      const admissionRejected = error?.status === 503 && code === "model_unconfigured";
      const unknown = !!state.submission && (!error?.status || error.status >= 500 && !admissionRejected);
      change({ phase: unknown && state.submission ? "unknown" : "ready", submission: unknown ? state.submission : null, error: unknown ? "提交结果待确认，请重试原操作。" : code === "workflow_active" ? "先停止工作流或等待完成，再暂停。" : admissionRejected ? detail.message || "尚未配置主模型，请先在设置中配置。" : error?.message ?? "请求未完成，请重试。", errorCode: code, conflict: error?.status === 409 });
    },
    clear() { state = initial(); },
  };
}
