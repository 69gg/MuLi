/** Active content is downloadable but never embedded in the application origin. */
export function attachmentKind(attachment) {
  const mime = String(attachment.mime_type ?? "").toLowerCase().split(";", 1)[0];
  if (["image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/bmp"].includes(mime)) return "image";
  if (mime.startsWith("audio/")) return "audio";
  if (mime.startsWith("video/")) return "video";
  return "file";
}

export function attachmentURL(attachment, download = false) {
  if (!attachment?.id) return null;
  return `/api/uploads/${encodeURIComponent(attachment.id)}/${download ? "download" : "content"}`;
}

export function formatBytes(bytes) {
  const size = Number(bytes);
  if (!Number.isFinite(size) || size < 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const exponent = Math.min(Math.floor(Math.log(Math.max(size, 1)) / Math.log(1024)), units.length - 1);
  return `${Number((size / 1024 ** exponent).toFixed(exponent ? 1 : 0))} ${units[exponent]}`;
}
