import test from "node:test";
import assert from "node:assert/strict";
import { VoiceInput, mergeTranscript, recordingSupport } from "../../web/voice-input.mjs";

const LIMITS = { available: true, max_file_bytes: 1024, max_seconds: 60, timeout: 30 };
const CONTEXT = { session: "main", revision: 1, text: "原草稿" };
const tick = () => new Promise((resolve) => setImmediate(resolve));

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function harness(options = {}) {
  const tracks = [{ stops: 0, stop() { this.stops += 1; } }, { stops: 0, stop() { this.stops += 1; } }];
  const stream = { getTracks: () => tracks };
  const calls = { permissions: [], uploads: [], transcriptions: [], drafts: [], errors: [], states: [] };
  const timers = new Map(); let timerId = 0, clock = 0;
  class Recorder {
    static instances = [];
    static isTypeSupported(type) { return options.mp4 ? type === "audio/mp4" : type === "audio/webm;codecs=opus"; }
    constructor(input, settings) {
      if (options.constructorError) throw new Error("constructor failed");
      this.stream = input; this.mimeType = settings.mimeType; this.state = "inactive";
      Recorder.instances.push(this);
    }
    start() {
      if (options.startError) throw new Error("start failed");
      this.state = "recording";
    }
    data(text) { this.ondataavailable?.({ data: new Blob([text], { type: this.mimeType }) }); }
    stop() {
      this.state = "inactive";
      if (options.noStopEvent) return;
      // Real MediaRecorder emits its final dataavailable before stop.
      queueMicrotask(() => { this.data("final audio"); this.onstop?.(); });
    }
  }
  const devices = { getUserMedia(settings) {
    calls.permissions.push(settings);
    return options.permission ? options.permission.promise : Promise.resolve(stream);
  } };
  const voice = new VoiceInput({
    secure: true, devices, Recorder,
    upload: async (file, settings) => {
      calls.uploads.push({ file, settings });
      return options.upload ? options.upload(file, settings) : { id: "uploaded", name: file.name };
    },
    transcribe: async (id, settings) => {
      calls.transcriptions.push({ id, settings });
      return options.transcribe ? options.transcribe(id, settings) : { text: "转写草稿", draft: true };
    },
    onDraft: (draft) => calls.drafts.push(draft),
    onError: (error) => calls.errors.push(error),
    onChange: (state) => calls.states.push(state),
    now: () => clock,
    setTimer: (callback, delay) => { const id = ++timerId; timers.set(id, { callback, delay }); return id; },
    clearTimer: (id) => timers.delete(id),
  });
  return { voice, stream, tracks, calls, timers, Recorder,
    advance(value) { clock = value; },
    fire() {
      const [id, timer] = [...timers.entries()].at(-1);
      timers.delete(id); timer.callback(); return timer;
    },
  };
}

test("capability checks never request permission and reject insecure or unsupported pages", () => {
  const h = harness();
  assert.equal(h.voice.support(), "");
  assert.match(recordingSupport({ secure: false }), /HTTPS/);
  assert.match(recordingSupport({ secure: true, devices: {}, Recorder: h.Recorder }), /不支持/);
  assert.match(recordingSupport({ secure: true, devices: {}, Recorder: {} }), /不支持/);
  assert.match(recordingSupport({ secure: true, devices: { getUserMedia() {} }, Recorder: { isTypeSupported: () => false } }), /录音格式/);
  assert.deepEqual(h.calls.permissions, []);
});

test("explicit recording waits for final data and produces only an editable draft", async () => {
  const h = harness();
  await h.voice.start(LIMITS, CONTEXT);
  assert.deepEqual(h.calls.permissions, [{ audio: true, video: false }]);
  assert.equal(h.voice.status().phase, "recording");
  h.Recorder.instances[0].data("first audio");
  await h.voice.stop();
  assert.equal(await h.calls.uploads[0].file.text(), "first audiofinal audio");
  assert.equal(h.calls.uploads[0].file.type, "audio/webm");
  assert.equal(h.calls.transcriptions[0].id, "uploaded");
  assert.deepEqual(h.calls.drafts[0].context, CONTEXT);
  assert.equal(h.calls.drafts[0].text, "转写草稿");
  assert.equal(h.voice.status().phase, "idle");
  assert.ok(h.tracks.every((track) => track.stops > 0));
  assert.equal(h.timers.size, 0);
  assert.deepEqual(h.calls.errors, []);
});

test("Safari-style MP4 recording keeps an audio MIME and m4a filename", async () => {
  const h = harness({ mp4: true });
  await h.voice.start(LIMITS, CONTEXT); await h.voice.stop();
  assert.equal(h.calls.uploads[0].file.type, "audio/mp4");
  assert.match(h.calls.uploads[0].file.name, /\.m4a$/);
});

test("disabled recording rejects without opening the microphone", async () => {
  const h = harness();
  await assert.rejects(h.voice.start({ ...LIMITS, available: false }, CONTEXT), /启用/);
  assert.equal(h.calls.permissions.length, 0);
});

test("permission denial leaves drafts untouched and releases the controller", async () => {
  const permission = deferred();
  const h = harness({ permission });
  const start = h.voice.start(LIMITS, CONTEXT);
  permission.reject(new DOMException("denied", "NotAllowedError"));
  await start; await tick();
  assert.match(h.calls.errors[0].message, /权限被拒绝/);
  assert.equal(h.voice.status().phase, "idle");
  assert.deepEqual(h.calls.drafts, []);
  assert.equal(h.timers.size, 0);
});

test("cancelling pending permission settles immediately and closes every late track", async () => {
  const permission = deferred();
  const h = harness({ permission });
  const first = h.voice.start(LIMITS, CONTEXT);
  h.voice.cancel();
  await first;
  assert.equal(h.voice.status().pendingPermission, true);
  await assert.rejects(h.voice.start(LIMITS, CONTEXT), /权限请求/);
  assert.equal(h.calls.permissions.length, 1);
  permission.resolve(h.stream); await tick();
  assert.ok(h.tracks.every((track) => track.stops === 1));
  assert.equal(h.voice.status().pendingPermission, false);
  assert.equal(h.Recorder.instances.length, 0);
  assert.equal(h.timers.size, 0);
});

test("permission timeout cancels without repeatedly asking for a microphone", async () => {
  const permission = deferred();
  const h = harness({ permission });
  const first = h.voice.start(LIMITS, CONTEXT);
  h.fire(); await first;
  assert.match(h.calls.errors[0].message, /权限超时/);
  await assert.rejects(h.voice.start(LIMITS, CONTEXT), /权限请求/);
  permission.resolve(h.stream); await tick();
  assert.ok(h.tracks.every((track) => track.stops > 0));
});

for (const stage of ["constructorError", "startError"]) {
  test(`${stage} releases all acquired tracks without uploading`, async () => {
    const h = harness({ [stage]: true });
    await h.voice.start(LIMITS, CONTEXT); await tick();
    assert.ok(h.tracks.every((track) => track.stops > 0));
    assert.equal(h.calls.uploads.length, 0);
    assert.equal(h.voice.status().phase, "idle");
    assert.equal(h.calls.errors.length, 1);
    assert.equal(h.timers.size, 0);
  });
}

test("byte limits cancel immediately, discard chunks and never upload", async () => {
  const h = harness();
  await h.voice.start({ ...LIMITS, max_file_bytes: 3 }, CONTEXT);
  h.Recorder.instances[0].data("oversize"); await tick();
  assert.match(h.calls.errors[0].message, /大小上限/);
  assert.equal(h.calls.uploads.length, 0);
  assert.equal(h.voice.current, null);
  assert.ok(h.tracks.every((track) => track.stops > 0));
});

test("unexpected recorder stop cancels the capture instead of uploading partial audio", async () => {
  const h = harness();
  await h.voice.start(LIMITS, CONTEXT);
  const recorder = h.Recorder.instances[0];
  recorder.data("partial audio"); recorder.state = "inactive"; recorder.onstop();
  await h.voice.stop();
  assert.equal(h.voice.status().phase, "idle");
  assert.equal(h.voice.current, null);
  assert.ok(h.tracks.every((track) => track.stops > 0));
  assert.match(h.calls.errors[0].message, /设备已停止或权限已撤销/);
  assert.deepEqual(h.calls.uploads, []);
  assert.deepEqual(h.calls.drafts, []);
  assert.equal(h.timers.size, 0);
});

test("recording deadline uses a monotonic clock and rechecks an early timer", async () => {
  const h = harness();
  await h.voice.start({ ...LIMITS, max_seconds: 2 }, CONTEXT);
  h.advance(1900); h.fire();
  assert.equal(h.voice.status().phase, "recording");
  assert.equal([...h.timers.values()][0].delay, 100);
  h.advance(2000); h.fire(); await tick();
  assert.equal(h.voice.status().phase, "idle");
  assert.equal(h.calls.drafts.length, 1);
});

test("a missing stop event has a deadline and stops its tracks", async () => {
  const h = harness({ noStopEvent: true });
  await h.voice.start(LIMITS, CONTEXT);
  const finishing = h.voice.stop();
  assert.equal(h.voice.status().phase, "stopping");
  h.fire(); await finishing;
  assert.equal(h.calls.uploads.length, 0);
  assert.match(h.calls.errors[0].message, /停止超时/);
  assert.ok(h.tracks.every((track) => track.stops > 0));
});

for (const stage of ["upload", "transcribe"]) {
  test(`cancelling ${stage} aborts HTTP and ignores a late successful response`, async () => {
    const remote = deferred(), started = deferred();
    let signal;
    const h = harness({ [stage]: async (input, settings) => {
      signal = settings.signal; started.resolve(); return remote.promise;
    } });
    await h.voice.start(LIMITS, CONTEXT);
    const finishing = h.voice.stop();
    await started.promise;
    h.voice.cancel();
    assert.equal(signal.aborted, true);
    remote.resolve(stage === "upload" ? { id: "late" } : { text: "late" });
    await finishing;
    assert.deepEqual(h.calls.drafts, []);
    assert.equal(h.voice.status().phase, "idle");
    assert.ok(h.tracks.every((track) => track.stops > 0));
    assert.equal(h.timers.size, 0);
    if (stage === "upload") assert.equal(h.calls.transcriptions.length, 0);
  });
}

test("an errored track does not prevent release of the remaining tracks", async () => {
  const h = harness();
  h.tracks[0].stop = () => { throw new Error("already detached"); };
  await h.voice.start(LIMITS, CONTEXT);
  h.voice.cancel(); await tick();
  assert.ok(h.tracks[1].stops > 0);
  assert.equal(h.voice.status().phase, "idle");
});

test("a stale result cannot overwrite typing, a replacement draft, or another session", () => {
  assert.equal(mergeTranscript(CONTEXT, { ...CONTEXT }, "转写"), "原草稿\n转写");
  assert.equal(mergeTranscript({ ...CONTEXT, text: "" }, { ...CONTEXT, text: "" }, "转写"), "转写");
  assert.equal(mergeTranscript(CONTEXT, { ...CONTEXT, revision: 2 }, "转写"), null);
  assert.equal(mergeTranscript(CONTEXT, { ...CONTEXT, text: "新输入" }, "转写"), null);
  assert.equal(mergeTranscript(CONTEXT, { ...CONTEXT, session: "other" }, "转写"), null);
  assert.equal(mergeTranscript({ ...CONTEXT, text: "" }, { ...CONTEXT, text: "" }, "<script>plain text</script>"), "<script>plain text</script>");
});
