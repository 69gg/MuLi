const REDACTED_FIELDS = new Set(["api_key_set", "credential_set", "key_set", "api_key_env_set", "password_set", "password_hash", "token_set", "webhook_secret_set", "client_secret_set"]);
const SECRET_FIELDS = new Set(["api_key", "password", "credential", "token", "webhook_secret", "client_secret"]);
const ATOMIC_OBJECTS = new Set(["reasoning", "thinking", "extra_body", "query_extra_body", "extra_headers", "mcp"]);

export const MODEL_ROLES = [["main", "主模型"], ["reviewer", "审阅"], ["worker", "执行"], ["vision", "视觉"], ["compactor", "压缩"], ["memory_extractor", "记忆提取"]];
export const SETTINGS_GROUPS = [["overview", "概览"], ["models", "模型"], ["persona", "称呼与提示词"], ["tools", "权限与工具"], ["connections", "连接"], ["channels", "消息渠道"], ["inputs", "语音与采集"], ["media", "媒体与检索"], ["maintenance", "扩展与维护"], ["system", "系统"]];

/** Advanced JSON contains only fields without their own controls or secret placeholders. */
export function settingsExtras(section, excluded = []) {
  const reserved = new Set(excluded);
  return Object.fromEntries(Object.entries(section ?? {}).filter(([key]) => !reserved.has(key) && !REDACTED_FIELDS.has(key) && !SECRET_FIELDS.has(key) && !(key.startsWith("clear_") && SECRET_FIELDS.has(key.slice(6)))));
}

export function mergeSettingsExtras(draft, path, values) {
  setPath(draft, path, { ...getPath(draft, path), ...values });
}

export function getPath(object, path) {
  return path.split(".").reduce((current, key) => current?.[key], object);
}

export function setPath(object, path, value) {
  const keys = path.split(".");
  let current = object;
  for (const key of keys.slice(0, -1)) {
    if (!current[key] || typeof current[key] !== "object") current[key] = {};
    current = current[key];
  }
  current[keys.at(-1)] = value;
}

/** Only edited fields are PATCHed; redacted flags and empty secrets never leak back. */
export function settingsPatch(original, draft, path = "") {
  const patch = {};
  for (const [key, value] of Object.entries(draft ?? {})) {
    if (REDACTED_FIELDS.has(key)) continue;
    if (key === "mcp" && value && typeof value === "object" && "configured" in value) continue;
    if (SECRET_FIELDS.has(key) && !value) continue;
    if (key.startsWith("clear_") && SECRET_FIELDS.has(key.slice(6)) && !value) continue;
    const fieldPath = path ? `${path}.${key}` : key;
    if (ATOMIC_OBJECTS.has(key) || ["channels.proactive.default_destination", "channels.proactive.memory_preference"].includes(fieldPath)) {
      const previous = original?.[key] ?? (fieldPath === "channels.proactive.memory_preference" ? null : {});
      if (JSON.stringify(value) !== JSON.stringify(previous)) patch[key] = value;
    } else if (value && typeof value === "object" && !Array.isArray(value)) {
      const nested = settingsPatch(original?.[key] ?? {}, value, fieldPath);
      if (Object.keys(nested).length) patch[key] = nested;
    } else if (JSON.stringify(value) !== JSON.stringify(original?.[key])) {
      patch[key] = value;
    }
  }
  if (path === "channels.proactive" && (patch.default_destination?.kind === "channel" || patch.memory_preference?.destination?.kind === "channel") && draft.external_enabled === false) {
    // A complete external address enables on the backend unless pause is explicit.
    patch.external_enabled = false;
  }
  return patch;
}

/** Only inspect the edited patch; unchanged listener controls need no restart. */
export function changesListener(patch) {
  return ["host", "port"].some((key) => Object.hasOwn(patch.server ?? {}, key));
}

/** Choosing an external default is one action; recipients always remain explicit. */
export function selectProactiveDestination(draft, name, accountId = "") {
  const proactive = draft.channels?.proactive ?? { external_enabled: false, default_destination: { kind: "web" } };
  proactive.default_destination = selectedDestination(proactive.default_destination, name, accountId);
  if (name !== "web") proactive.external_enabled = true;
  setPath(draft, "channels.proactive", proactive);
}

function selectedDestination(current, name, accountId) {
  if (name === "web") return { kind: "web" };
  if (!["onebot", "telegram", "qq", "weixin"].includes(name)) throw new Error("未知通知渠道。");
  if (current?.kind === "channel" && current.route?.name === name) return current;
  return { kind: "channel", route: { name, account_id: accountId, kind: "private", target: "", sender_id: "" } };
}

export function selectNotificationPreference(draft, enabled) {
  const current = draft.channels?.proactive?.memory_preference;
  setPath(draft, "channels.proactive.memory_preference", enabled ? current ?? { memory_id: "", version: 1, destination: { kind: "web" } } : null);
}

export function selectPreferenceDestination(draft, name, accountId = "") {
  const preference = draft.channels?.proactive?.memory_preference;
  if (!preference) throw new Error("请先选择一条明确的记忆偏好。");
  preference.destination = selectedDestination(preference.destination, name, accountId);
  if (name !== "web") setPath(draft, "channels.proactive.external_enabled", true);
}

export function preferenceStatusLabel(reason) {
  return ({ valid: "依据有效", not_configured: "未绑定，使用普通默认", memory_disabled: "记忆已关闭，使用普通默认", unavailable: "证据暂不可读，使用普通默认", missing: "事实或来源已不存在，使用普通默认", not_preference: "该记录不是偏好，使用普通默认", inactive: "偏好已失效或有冲突，使用普通默认", version_changed: "偏好版本已更正，使用普通默认", untrusted: "缺少主人来源证据，使用普通默认", source_invalid: "来源摘要无效，使用普通默认" })[reason] ?? "依据状态未知";
}
