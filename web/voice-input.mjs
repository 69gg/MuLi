import { attachmentKind } from "./attachments.mjs";

/** One audio input at a time; cancelled generations can never publish a draft. */
const MIME_TYPES = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus", "audio/webm"];

export function recordingSupport({ secure = globalThis.isSecureContext, devices = globalThis.navigator?.mediaDevices, Recorder = globalThis.MediaRecorder } = {}) {
  if (!secure) return "麦克风需要 HTTPS 或 localhost 安全页面。";
  if (!devices?.getUserMedia || !Recorder?.isTypeSupported) return "当前浏览器不支持网页录音。";
  if (!MIME_TYPES.some((type) => Recorder.isTypeSupported(type))) return "当前浏览器没有支持的录音格式。";
  return "";
}

export function mergeTranscript(captured, current, text) {
  if (captured.session !== current.session || captured.revision !== current.revision || captured.text !== current.text) return null;
  return current.text ? `${current.text}\n${text}` : text;
}

function stopTracks(stream) {
  for (const track of stream?.getTracks?.() ?? []) {
    try { track.stop(); } catch { /* Release the remaining tracks as well. */ }
  }
}

export class VoiceInput {
  constructor({ devices = globalThis.navigator?.mediaDevices, Recorder = globalThis.MediaRecorder, secure = globalThis.isSecureContext, upload, transcribe, onDraft, onUploaded = () => {}, onChange = () => {}, onError = () => {}, now = () => performance.now(), setTimer = setTimeout, clearTimer = clearTimeout }) {
    Object.assign(this, { devices, Recorder, secure, upload, transcribe, onDraft, onUploaded, onChange, onError, now, setTimer, clearTimer });
    this.phase = "idle";
    this.generation = 0;
    this.current = null;
    this.permission = null;
  }

  status() { return { phase: this.phase, pendingPermission: this.permission !== null }; }
  support() { return recordingSupport({ secure: this.secure, devices: this.devices, Recorder: this.Recorder }); }
  change(phase) { this.phase = phase; this.onChange(this.status()); }
  active(session) { return this.current === session && session.generation === this.generation && !session.controller.signal.aborted; }
  clearDeadline(session) { if (session.timer !== undefined) this.clearTimer(session.timer); session.timer = undefined; }

  deadline(session, seconds, message) {
    this.clearDeadline(session);
    session.timer = this.setTimer(() => this.fail(session, new Error(message)), seconds * 1000);
  }

  fail(session, error) {
    if (!this.active(session)) return;
    if (this.phase === "transcribing" && !error.status) error = new Error(`${error.message || "转写结果未确认。"} 转写可能已到 ASR，再次操作可能计费。`);
    this.cancel();
    this.onError(error, session.context);
  }

  cancel() {
    const session = this.current;
    this.current = null;
    this.generation += 1;
    if (session) {
      this.clearDeadline(session);
      session.controller.abort();
      stopTracks(session.stream);
      if (session.recorder && session.recorder.state !== "inactive") {
        try { session.recorder.stop(); } catch { /* Tracks are already stopped. */ }
      }
      session.stopped?.();
      session.chunks = [];
    }
    this.change("idle");
  }

  async start(limits, context) {
    if (!limits?.available) throw new Error("请先启用网页语音输入并配置 ASR。");
    const unsupported = this.support();
    if (unsupported) throw new Error(unsupported);
    if (this.permission) throw new Error("浏览器仍有麦克风权限请求，请先处理权限提示。");
    if (this.phase !== "idle") return;
    const session = { generation: ++this.generation, controller: new AbortController(), context, limits: { ...limits }, chunks: [], size: 0 };
    this.current = session;
    this.change("permission");
    this.deadline(session, limits.timeout, "等待麦克风权限超时，录音已取消。");
    let onAbort;
    try {
      const permission = Promise.resolve(this.devices.getUserMedia({ audio: true, video: false }));
      this.permission = permission;
      // getUserMedia has no AbortSignal. A cancelled prompt can resolve later;
      // close every late track, and permit at most one unresolved permission call.
      void permission.then((stream) => { if (!this.active(session)) stopTracks(stream); }, () => {}).then(() => {
        if (this.permission === permission) this.permission = null;
        this.onChange(this.status());
      });
      const aborted = new Promise((resolve, reject) => {
        onAbort = () => reject(new DOMException("已取消", "AbortError"));
        session.controller.signal.addEventListener("abort", onAbort, { once: true });
      });
      const stream = await Promise.race([permission, aborted]);
      if (!this.active(session)) { stopTracks(stream); return; }
      session.stream = stream;
      this.clearDeadline(session);
      const mimeType = MIME_TYPES.find((type) => this.Recorder.isTypeSupported(type));
      const recorder = new this.Recorder(stream, { mimeType });
      session.recorder = recorder;
      session.finished = new Promise((resolve) => { session.stopped = resolve; });
      recorder.ondataavailable = (event) => {
        if (!this.active(session) || !event.data?.size) return;
        session.size += event.data.size;
        if (session.size > session.limits.max_file_bytes) { this.fail(session, new Error("录音超过大小上限，已取消。")); return; }
        session.chunks.push(event.data);
      };
      recorder.onstop = () => {
        session.stopped();
        if (this.active(session) && this.phase === "recording") this.fail(session, new Error("录音设备已停止或权限已撤销，录音已取消，请重试。"));
      };
      recorder.onerror = () => this.fail(session, new Error("浏览器录音失败，请重试。"));
      recorder.start(250);
      session.startedAt = this.now();
      const until = session.startedAt + limits.max_seconds * 1000;
      const stopAtLimit = () => {
        if (!this.active(session)) return;
        const remaining = until - this.now();
        if (remaining > 0) session.timer = this.setTimer(stopAtLimit, remaining);
        else void this.stop();
      };
      session.timer = this.setTimer(stopAtLimit, limits.max_seconds * 1000);
      this.change("recording");
    } catch (error) {
      if (error.name !== "AbortError") this.fail(session, new Error(error.name === "NotAllowedError" ? "麦克风权限被拒绝，草稿保持不变。" : error.message || "无法开始录音。"));
    } finally {
      if (onAbort) session.controller.signal.removeEventListener("abort", onAbort);
    }
  }

  async startSaved(limits, context, attachment) {
    if (!limits?.available) throw new Error("请先启用网页语音输入并配置 ASR。");
    if (this.permission || this.phase !== "idle") throw new Error("请先结束当前录音或转写，并处理麦克风权限提示。");
    if (!attachment?.id || attachmentKind(attachment) !== "audio") throw new Error("请选择已保存的音频附件。");
    if (!Number.isSafeInteger(attachment.size) || attachment.size <= 0 || attachment.size > limits.max_file_bytes) throw new Error("音频为空或超过网页转写大小上限。");
    const session = { generation: ++this.generation, controller: new AbortController(), context, limits: { ...limits } };
    this.current = session;
    await this.transcribeSaved(session, attachment);
  }

  async transcribeSaved(session, attachment) {
    this.change("transcribing");
    this.deadline(session, session.limits.timeout, "转写超时，草稿保持不变。");
    try {
      const result = await this.transcribe(attachment.id, { signal: session.controller.signal });
      if (!this.active(session)) return;
      if (typeof result?.text !== "string" || !result.text.trim()) throw new Error("没有识别到文字，请重试。");
      if (result.text.length > session.limits.max_text_chars) throw new Error("转写文字超过草稿上限，请拆分录音。");
      this.onDraft({ ...result, context: session.context, attachment });
      this.cancel();
    } catch (error) {
      if (error.name !== "AbortError") this.fail(session, error);
    } finally {
      this.clearDeadline(session);
    }
  }

  async stop() {
    const session = this.current;
    if (!session || this.phase !== "recording") return;
    this.change("stopping");
    this.deadline(session, session.limits.timeout, "录音停止超时，已取消。");
    try {
      session.recorder.stop();
      stopTracks(session.stream);
      await session.finished;
      if (!this.active(session)) return;
      const type = (session.recorder.mimeType || session.chunks[0]?.type || "").split(";", 1)[0].toLowerCase();
      const extension = { "audio/webm": "webm", "audio/mp4": "m4a", "audio/ogg": "ogg" }[type];
      if (!extension || !session.size || session.size > session.limits.max_file_bytes) throw new Error("录音为空、格式不可用或超过大小上限。");
      const file = new File(session.chunks, `语音草稿.${extension}`, { type });
      session.chunks = [];
      this.change("uploading");
      this.deadline(session, session.limits.timeout, "保存录音超时，已取消。");
      const attachment = await this.upload(file, { signal: session.controller.signal });
      if (!this.active(session)) return;
      this.onUploaded(attachment, session.context);
      await this.transcribeSaved(session, attachment);
    } catch (error) {
      if (error.name !== "AbortError") this.fail(session, error);
    } finally {
      this.clearDeadline(session);
      stopTracks(session.stream);
    }
  }
}
