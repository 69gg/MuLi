const REDACTED_FIELDS = new Set(["api_key_set", "credential_set", "key_set", "api_key_env_set", "password_set", "password_hash", "token_set", "webhook_secret_set", "client_secret_set"]);
const SECRET_FIELDS = new Set(["api_key", "password", "credential", "token", "webhook_secret", "client_secret"]);
const ATOMIC_OBJECTS = new Set(["reasoning", "thinking", "extra_body", "extra_headers", "mcp"]);

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
export function settingsPatch(original, draft) {
  const patch = {};
  for (const [key, value] of Object.entries(draft ?? {})) {
    if (REDACTED_FIELDS.has(key)) continue;
    if (key === "mcp" && value && typeof value === "object" && "configured" in value) continue;
    if (SECRET_FIELDS.has(key) && !value) continue;
    if (key.startsWith("clear_") && SECRET_FIELDS.has(key.slice(6)) && !value) continue;
    if (ATOMIC_OBJECTS.has(key)) {
      if (JSON.stringify(value) !== JSON.stringify(original?.[key] ?? {})) patch[key] = value;
    } else if (value && typeof value === "object" && !Array.isArray(value)) {
      const nested = settingsPatch(original?.[key] ?? {}, value);
      if (Object.keys(nested).length) patch[key] = nested;
    } else if (JSON.stringify(value) !== JSON.stringify(original?.[key])) {
      patch[key] = value;
    }
  }
  return patch;
}
