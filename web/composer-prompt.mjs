/** Compose an owner prompt into the current draft without sending or touching attachments. */
export const REPLACE_CONFIRMATION = "将替换现有文字，附件与语音保留。";

const modes = new Set(["append", "replace"]);

export function applyComposerPrompt(prompt, {
  mode = "append",
  session,
  mainSession,
  expectedSession,
  revision,
  expectedRevision,
  text = "",
  expectedText,
  maxChars = 64000,
  confirmed = false,
} = {}) {
  if (typeof prompt !== "string" || !prompt.trim()) return { ok: false, reason: "empty" };
  if (!modes.has(mode)) return { ok: false, reason: "mode" };
  if (!session || session !== mainSession) return { ok: false, reason: "not_main" };
  if (expectedSession !== undefined && session !== expectedSession) return { ok: false, reason: "stale" };
  if (expectedRevision !== undefined && revision !== expectedRevision) return { ok: false, reason: "stale" };
  if (typeof text !== "string") return { ok: false, reason: "text" };
  if (expectedText !== undefined && text !== expectedText) return { ok: false, reason: "stale" };
  if (!Number.isSafeInteger(maxChars) || maxChars < 1) return { ok: false, reason: "limit" };
  if (mode === "replace" && text && !confirmed) return { ok: false, reason: "confirmation_required" };
  if (mode === "append" && text.includes(prompt)) return { ok: true, mode, text, previousText: text, unchanged: true };
  const merged = mode === "append" && text ? `${text}\n\n${prompt}` : prompt;
  if (merged.length > maxChars) return { ok: false, reason: "too_long" };
  return { ok: true, mode, text: merged, previousText: text };
}
