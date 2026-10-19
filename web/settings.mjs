const REDACTED_FIELDS = new Set(["api_key_set", "credential_set", "key_set", "api_key_env_set", "password_set", "password_hash"]);
const ATOMIC_OBJECTS = new Set(["reasoning", "thinking", "extra_body", "extra_headers", "mcp"]);

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
    if ((key === "api_key" || key === "password") && !value) continue;
    if ((key === "clear_api_key" || key === "clear_password") && !value) continue;
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
