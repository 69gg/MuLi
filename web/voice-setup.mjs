/** Finite, explicitly confirmed setup; stale requests never update another view. */
export class VoiceSetup {
  constructor({ request, active, onChange = () => {} }) {
    this.request = request; this.active = active; this.onChange = onChange;
    this.generation = 0; this.controller = null; this.identifier = null; this.state = { phase: "idle", result: null, error: "" };
  }
  status() { return this.state; }
  publish(value) { this.state = value; this.onChange(value); }
  release() {
    this.generation += 1; this.controller?.abort(); this.controller = null; this.identifier = null;
    this.state = { phase: "idle", result: null, error: "" };
  }
  async run(operation, seconds = 5) {
    if (!this.active()) return;
    if (!["enroll", "verify"].includes(operation)) throw new Error("未知语音设置操作。");
    if (this.controller || this.state.phase === "stopping") throw new Error("请等待本次语音设置结束。");
    const controller = new AbortController(), generation = ++this.generation, id = crypto.randomUUID();
    this.identifier = id;
    this.controller = controller;
    const current = () => this.generation === generation && this.active() && !controller.signal.aborted;
    this.publish({ phase: "recording", operation, result: null, error: "" });
    try {
      const result = await this.request("/voice/setup", { method: "POST", body: { id, operation, seconds, confirmed: true }, signal: controller.signal });
      if (!["completed", "cancelled"].includes(result?.state)) throw new Error("本次设置未完成，请查看语音状态。");
      if (current()) this.publish({ phase: result.state === "cancelled" ? "cancelled" : "completed", operation, result, error: "" });
      return current() ? result : undefined;
    } catch (error) {
      if (current()) this.publish({ phase: "error", operation, result: null, error: error.message || "语音设置未完成。" });
    } finally {
      if (this.generation === generation) this.controller = null;
    }
  }
  async stop() {
    if (!this.controller || !this.active()) return;
    const generation = ++this.generation, id = this.identifier;
    this.controller.abort(); this.controller = null;
    this.publish({ phase: "stopping", result: null, error: "" });
    try {
      const result = await this.request("/voice/setup/cancel", { method: "POST", body: { id } });
      if (!["completed", "cancelled", "idle"].includes(result?.state)) throw new Error("取消结果尚未确认。");
      if (this.generation === generation && this.active()) this.publish({ phase: result.state, result, error: "" });
    } catch (error) {
      if (this.generation === generation && this.active()) this.publish({ phase: "error", result: null, error: error.message || "取消结果尚未确认。" });
    }
  }
}

export function voiceSetupLabel(state) {
  if (state.phase === "completed") {
    const result = state.result ?? {};
    const prefix = result.preview ? "演示：" : "";
    if (result.operation === "enroll") return prefix + (result.enrolled ? "参考声纹已保存。" : "参考声纹未保存。");
    return `${prefix}唤醒${result.wake_detected ? "命中" : "未命中"} · 声纹${!result.speaker_configured ? "未配置" : result.speaker_verified ? "通过" : "未通过"}`;
  }
  return ({ recording: "正在录制，请说出唤醒词和一句短话…", stopping: "正在停止…", cancelled: "本次设置已取消。", error: state.error, idle: state.result?.state === "idle" ? "当前没有进行中的设置操作。" : "本次录音只用于设置，不发送消息。" })[state.phase] ?? "";
}
