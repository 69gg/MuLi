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
