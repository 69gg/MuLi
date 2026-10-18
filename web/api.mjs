/** Same-origin API client and an incremental, standards-shaped SSE reader. */
export class ApiError extends Error {
  constructor(message, status = 0, details = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.details = details;
  }
}

export async function request(path, options = {}) {
  const headers = { Accept: "application/json", ...options.headers };
  if (!["GET", "HEAD"].includes((options.method ?? "GET").toUpperCase())) headers["Content-Type"] = "application/json";
  let body = options.body;
  if (body !== undefined && typeof body !== "string") {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(`/api${path}`, { ...options, headers, body });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ApiError("暂时无法连接工作台，请检查服务后重试。");
  }
  const raw = await response.text();
  let data;
  try { data = raw ? JSON.parse(raw) : null; } catch { data = null; }
  if (!response.ok) {
    const detail = data?.detail ?? data?.error ?? data?.message;
    const message = typeof detail === "string" ? detail : detail?.message;
    throw new ApiError(message || `请求未完成（${response.status}）`, response.status, data);
  }
  return data;
}

/** CR, LF, split CRLF, comments and multiline data are handled incrementally. */
export function createSSEParser(onEvent) {
  let buffer = "";
  let lastId = "";
  let frame = { data: [], type: "message", id: "" };
  const consumeLine = (line) => {
    if (!line) {
      if (frame.data.length) {
        const raw = frame.data.join("\n");
        let payload;
        try { payload = JSON.parse(raw); } catch { payload = raw; }
        onEvent(payload && typeof payload === "object" && payload.type
          ? { ...payload, id: payload.id ?? (frame.id || lastId) }
          : { id: frame.id || lastId, type: frame.type, data: payload });
      }
      frame = { data: [], type: "message", id: "" };
      return;
    }
    if (line.startsWith(":")) return;
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "data") frame.data.push(value);
    if (field === "event") frame.type = value;
    if (field === "id" && !value.includes("\0")) { frame.id = value; lastId = value; }
  };
  return {
    feed(text, final = false) {
      buffer += text;
      let start = 0;
      for (let index = 0; index < buffer.length; index += 1) {
        const char = buffer[index];
        if (char !== "\r" && char !== "\n") continue;
        if (char === "\r" && index === buffer.length - 1 && !final) break;
        consumeLine(buffer.slice(start, index));
        if (char === "\r" && buffer[index + 1] === "\n") index += 1;
        start = index + 1;
      }
      buffer = buffer.slice(start);
    },
  };
}

export async function readEvents(path, { signal, after = "", onEvent, onOpen }) {
  const separator = path.includes("?") ? "&" : "?";
  const suffix = after ? `${separator}after=${encodeURIComponent(after)}` : "";
  const response = await fetch(`/api${path}${suffix}`, {
    headers: { Accept: "text/event-stream", ...(after ? { "Last-Event-ID": String(after) } : {}) },
    signal,
  });
  if (!response.ok || !response.body) {
    throw new ApiError(`实时连接暂不可用（${response.status}）`, response.status);
  }
  onOpen?.();
  const parser = createSSEParser(onEvent);
  const decoder = new TextDecoder();
  const reader = response.body.getReader();
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      parser.feed(decoder.decode(value, { stream: true }));
    }
    parser.feed(decoder.decode(), true);
  } finally {
    reader.releaseLock();
  }
}
