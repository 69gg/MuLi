export const CHANNELS = [["onebot", "OneBot"], ["telegram", "Telegram"], ["qq", "QQ 官方机器人"], ["weixin", "微信"]];

/** Read-only capability requests fail independently; never invent an inactive status. */
export async function loadSettingsResources(request) {
  const resources = { status: "/status", service: "/service", management: "/management", channels: "/channels", voice: "/voice", capture: "/capture" };
  const keys = Object.keys(resources);
  const results = await Promise.allSettled(keys.map((key) => request(resources[key])));
  const values = {}; const errors = {};
  results.forEach((result, index) => {
    const key = keys[index];
    values[key] = result.status === "fulfilled" ? result.value : null;
    if (result.status === "rejected") errors[key] = result.reason?.message ?? "状态读取失败";
  });
  return { values, errors };
}

export function capabilityLabel(status) {
  if (!status) return "状态未知";
  if (status.enabled === false) return "未启用";
  if (status.running || status.active) return "运行中";
  if (status.available === true) return "可用";
  if (status.available === false) return "暂不可用";
  return status.enabled ? "已启用" : "状态未知";
}

export function managementLabel(value) {
  return ({ pending: "待应用", applying: "正在应用", completed: "已完成", cancelled: "已取消", failed: "失败", interrupted: "已中断", sent: "已发送", sending: "发送中", unknown: "结果未知", loaded: "已加载", degraded: "运行异常", disabled: "未启用", error: "加载失败" })[value] ?? value ?? "状态未知";
}

export function managementActionLabel(plan) {
  if (plan?.action === "switch" && plan.operation === "update") return "版本更新";
  return ({ configure: "配置变更", restart: "重启服务", update: "版本更新", switch: "切换版本", adopt: "托管服务" })[plan?.action] ?? plan?.action ?? "管理操作";
}

export function managementPlanLabel(plan) {
  if (plan?.action === "switch" && plan.operation === "update") {
    if (plan.status === "applying") {
      const label = ({ candidate_cleanup_required: "等待恢复", candidate_preparing: "准备更新", candidate_ready: "等待切换" })[plan.phase];
      if (label) return label;
    }
    if (plan.status === "interrupted" && plan.phase === "candidate_cleaned") return "更新中断";
  }
  return managementLabel(plan?.status);
}

const recoveryPhases = { prepared: "准备就绪", draining: "正在排空", activating: "正在切换", verifying: "正在核查", rolling_back: "正在回退", committed: "切换已提交", rolled_back: "已回退", aborted: "已中止", recovery_required: "需要人工核查" };
const recoveryActivations = { pending: "等待启用", released: "已启用", released_degraded: "已启用，部分能力异常", blocked: "启用受阻" };
const recoveryCodes = { cancelled: "已取消", drain_timeout: "排空超时", deadline_exceeded: "操作期限已过", clock_invalid: "时钟状态异常", foreign_state: "当前状态与记录不符", health_failed: "健康核查未通过", backend_failed: "服务操作失败", ambiguous_start: "启动结果不明确", activation_failed: "启用失败", snapshot_invalid: "保存的状态无效" };
const recoveryIdentifier = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}$/;

/** Interpret only public, known recovery fields; never render arbitrary payloads. */
export function managementRecoveryView(management) {
  const unknown = { key: null, canRecover: false, entries: [["当前恢复", "状态未知"]], hint: "暂时无法确认当前操作，请刷新状态核对。" };
  const recovery = management?.recovery;
  if (!recovery || typeof recovery.available !== "boolean" || !("transaction" in recovery)) return unknown;
  const record = recovery.transaction;
  let transaction = null;
  if (record !== null) {
    if (!record || ![undefined, 2].includes(record.protocol) || !Object.hasOwn(recoveryPhases, record.phase) || !Object.hasOwn(recoveryActivations, record.activation)
      || (record.protocol === 2 && !["adopt", "switch", "configure", "restart"].includes(record.action))
      || typeof record.generation !== "string" || !recoveryIdentifier.test(record.generation) || !Number.isSafeInteger(record.revision) || record.revision < 1
      || typeof record.resolved !== "boolean" || [record.code, record.failure_code].some((code) => code != null && !Object.hasOwn(recoveryCodes, code))) return unknown;
    const finalPhase = ["committed", "rolled_back"].includes(record.phase);
    const resolved = record.phase === "aborted" || (finalPhase && ["released", "released_degraded"].includes(record.activation));
    if (record.resolved !== resolved || (!finalPhase && record.activation !== "pending")) return unknown;
    const manual = record.phase === "recovery_required" || record.activation === "blocked";
    transaction = {
      key: `transaction:${record.generation}`, canRecover: recovery.available && !resolved && !manual,
      entries: [["当前恢复", manual ? "需要人工核查" : resolved ? "当前操作已结束" : "当前操作未结束"], ["阶段", recoveryPhases[record.phase]], ["启用状态", recoveryActivations[record.activation]], ["原因", recoveryCodes[record.code] ?? "—"], ...(record.failure_code ? [["失败原因", recoveryCodes[record.failure_code]]] : [])],
      hint: manual ? "请通过 MuLi management status 核查；此处不提供强制恢复。" : resolved ? "以上为最近一次读取的实际状态。" : "接续已保存的当前操作；请求返回后仍需刷新状态核对。",
    };
  }
  if (!recovery.available) return {
    ...(transaction ?? unknown), canRecover: false,
    entries: [["恢复入口", "当前不可用"], ...(transaction?.entries ?? [])],
    hint: recovery.reason === "managed_boot_required" ? "当前启动不支持此恢复入口，请使用受支持的托管服务。" : "请通过 MuLi management status 核查当前服务与恢复条件。",
  };
  if (transaction && !record.resolved) return transaction;
  if (!Array.isArray(management.plans) || management.plans.some((plan) => !["pending", "applying", "completed", "cancelled", "failed", "interrupted"].includes(plan?.status))) return unknown;
  const candidates = management.plans.filter((plan) => plan?.operation === "update" && plan.status === "applying");
  if (candidates.length) {
    const plan = candidates[0];
    if (candidates.length !== 1 || plan.action !== "switch" || plan.candidate_protocol !== 1 || typeof plan.id !== "string" || !recoveryIdentifier.test(plan.id)
      || !["candidate_preparing", "candidate_cleanup_required", "candidate_ready"].includes(plan.phase)) return unknown;
    return {
      key: `candidate:${plan.id}`, canRecover: plan.phase !== "candidate_ready",
      entries: [["当前恢复", "版本更新准备"], ["阶段", managementPlanLabel(plan)]],
      hint: plan.phase === "candidate_ready" ? "候选已准备，等待原操作继续切换。" : "上次更新的准备过程尚未确认结束，可接续当前清理。",
    };
  }
  return transaction ?? { key: null, canRecover: false, entries: [["当前恢复", "暂无可接续的当前操作"]], hint: "待应用及历史操作见下方记录。" };
}

export function managementRecoveryReplyLabel(reply) {
  if (["controller_busy", "candidate_cleanup_busy"].includes(reply?.helper)) return "当前操作正忙，请刷新状态核对。";
  if (["accepted", "running"].includes(reply?.helper)) return "接续请求已接受，请刷新状态核对。";
  if (reply?.helper === "unavailable") return "接续暂不可用，请刷新状态或通过命令行核查。";
  if (reply && typeof reply.available === "boolean" && "transaction" in reply && reply.helper === undefined) return "请求已返回，请刷新状态确认当前操作。";
  return "接续结果未知，请刷新状态核对，不要重复提交。";
}

export function updateLabel(management) {
  if (!management) return "状态未知";
  const update = management.update;
  if (!update) return "尚未检查";
  if (update.status === "unavailable") return "暂时无法检查";
  if (update.status !== "checked") return "状态未知";
  return update.available ? "有可用更新" : "已是最新";
}

export async function retryChannelDelivery(request, delivery, { allowUnknown = false } = {}) {
  if (!["failed", "unknown", "blocked"].includes(delivery?.state)) throw new Error("这条发送记录当前不能重试。");
  if (delivery.state === "unknown" && !allowUnknown) throw new Error("请先确认可能重复的发送。");
  return request(`/channels/deliveries/${encodeURIComponent(delivery.id)}/retry`, { method: "POST", body: { allow_unknown: delivery.state === "unknown" && allowUnknown } });
}
