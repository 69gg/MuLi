# 网页录音草稿与本地语音

## 网页点击录音

`web_voice.enabled` 默认 `false`，与后台 `voice.enabled` 独立。工作台“语音与采集”设置开启后，还需配置 `media.asr` 的模型、基础地址和凭据。网页不需要安装录音库、本地模型或 FFmpeg；音频只发往当前配置的 OpenAI 兼容 ASR 服务。

```json
{
  "web_voice": {
    "enabled": false,
    "max_seconds": 60,
    "max_file_bytes": 8388608,
    "max_concurrent": 1,
    "timeout": 60,
    "max_text_chars": 64000
  },
  "media": {
    "asr": {
      "model": "填写转写模型ID",
      "base_url": "https://provider.example/v1",
      "api_key_env": "MULI_ASR_KEY"
    }
  }
}
```

在对话输入框点击麦克风，再由浏览器申请权限；点击“停止并转写”才上传并转写。转写只返回可编辑草稿，检查后仍需点击发送，才成为当前登录用户的消息。接口不会调用主模型、提交主人输入、触发记忆提炼或登记语音身份。浏览器权限和这段录音都不构成声纹验证，也不会启用后台唤醒。

录音开始时保留输入框和会话快照。期间发生输入、发送或会话切换时，转写保存在独立草稿区，原输入不变；可编辑后选择追加或丢弃。即使文字后来被改回原值，也不会覆盖期间的编辑。取消、切换至其它功能页、页面隐藏或离开页面会停止所有已取得的轨道并中止上传/转写；取消后迟到的结果被丢弃。设备意外结束或权限撤销时丢弃本次未上传录音并提示重试。显式退出登录会取消当前操作并清空内存中的输入和转写草稿，已保存的音频仍在附件库。浏览器的权限请求无法由标准 API 撤回，忽略权限提示时最多保留一个未决请求；若后来批准，已取消录音的轨道会立即关闭。

网页需要 HTTPS 或 localhost 等安全上下文及浏览器的 `getUserMedia` / `MediaRecorder` 支持。远程 HTTP 地址通常不能使用麦克风；通过局域网访问时需提供 HTTPS。格式按浏览器探测选择 WebM/Opus、MP4 或 Ogg/Opus，不要求所有浏览器支持同一种格式。格式不可用或权限拒绝会显示原因，不尝试后台采集。

`max_seconds` 为 1–300 秒，浏览器使用单调时钟到期停止；浏览器暂停或延迟计时器时无法保证精确音频时长，服务端不解码音频验证时长。服务端硬限制为文件字节、总转写时间和并发数：文件上限取本节、`uploads.max_file_bytes`、`media.asr.max_input_bytes` 三者最小值，并受附件总配额约束；本节可设 1 KiB–32 MiB。`max_concurrent` 为 1–4，满额立即返回 429，不建立等待队列。`timeout` 为有限正数，最多 300 秒，限制整个服务端读取与 ASR 请求，也用于网页权限、停止、上传和转写各阶段的等待上限。`max_text_chars` 为 1–64000，超限明确失败，不静默截断。

停止后通过既有 `POST /api/uploads` 保存原音频，再以 `POST /api/media/transcribe`、JSON `{"attachment_id":"附件ID"}` 取得 `{"text":"转写文字","attachment_id":"附件ID","draft":true}`。两个接口沿用登录与同源限制；新接口只接受本地音频附件。请求断开、配置修改及关闭服务会取消正在转写的 HTTP 请求，但取消不能保证已接收音频的上游不计费。音频仍保留在附件目录 `data/attachments/`，转写失败或丢弃文字不会删除音频；可在附件库手动删除。录音不会自动附到聊天消息，只有用户另行选择附件或点击发送文字才进入对话。NDJSON 导出不包含音频文件，备份时应另存附件目录。

网页状态机、ASR 接口和断开处理均以假录音器、假轨道及 HTTP MockTransport 验证；开发验证没有打开实际麦克风或调用付费转写。浏览器行为依据 [getUserMedia](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia)、[MediaRecorder.stop](https://developer.mozilla.org/en-US/docs/Web/API/MediaRecorder/stop)、[MediaStreamTrack.stop](https://developer.mozilla.org/en-US/docs/Web/API/MediaStreamTrack/stop) 官方文档核对。

## AI 转录、合成与渠道语音

AI 已有 `audio_transcribe(id)` 按需转录音频附件，以及 `speech_synthesize(text, voice?)` 调用当前配置的 TTS、保存合成音频并返回本地附件 ID。合成不会自动播放或发出；无需为发送功能打开后台麦克风或唤醒。

需要发送原生语音时，再显式调用 `channel_send`，传 `attachment_ids=[合成返回的ID]`、`media_mode="voice"` 和从可信入站取得的已绑定目标/群路由。同一逻辑发送可带稳定 `idempotency_key`；返回排队 ID 后以 `channel_status` 检查状态，不把入队说成已送达。结果 unknown 不自动重发，不能通过改键绕过重复风险。

当前显式 voice 支持 Telegram 的 MP3、M4A、Ogg/Opus，以及远端确认 `can_send_record` 的 OneBot；QQ 与微信的这一显式模式尚未支持，会明确拒绝。默认 file 保持各平台原有附件行为。TTS 默认 MP3 可直接走 Telegram 语音；若服务配置生成 WAV、FLAC、PCM 等 Telegram 不支持的格式，需显式选择兼容的 TTS 输出或文件发送，不会自动安装转码器或降级。详见[渠道语音合同](channels.md#显式发送语音)。没有真实平台收发或付费 TTS 验收记录。

## 本地后台唤醒

voice.enabled 默认 false。未启用时，start 不创建采集任务，listen_once 和 enroll 拒绝录音。模块导入、状态探测与设置读取都不会打开麦克风、下载模型或加载 GPU 推理框架。本轮使用模拟音频与模拟进程验证，未实际录音或播放开发机音频。

## 处理流程

启用后，需要已安装的录音工具、用户提供的真实声学唤醒命令和已配置的 media.asr。默认唤醒词是“沐璃”，它只是传给声学组件的配置，不代表已经内置中文唤醒模型。没有声学组件时状态明确报告不可用，不把 ASR 字符串匹配当作声学唤醒。

后台 worker 先录制有限 PCM16 单声道 WAV 片段并交给唤醒组件。未命中立即删除，不调用 ASR。命中后优先通过已开启的 TTS 播放“请说”，未配或失败则尝试短提示音；没有播放组件会发出 voice.prompt 状态事件并记录 played=false。播放进程有 20 秒上限，失败不会伪装成成功。

然后补录有界指令音频，保留唤醒片段作为前置音频，避免丢掉“沐璃，……”之后紧接的指令。配置声纹组件时，先核对显式录制的参考音频，未通过不调用 ASR；未配置声纹时，回调仍明确 speaker_verified=false。转写复用已配置的 MediaService OpenAI 兼容 ASR，不发送未唤醒片段或声纹参考录音。业务层必须按该身份字段处理权限，唤醒成功本身不等于可信主人身份。

ASR 后回调 `{text,wake_word,speaker_verified,source:"voice"}`。原始唤醒片段和本次指令音频在成功、失败、拒绝及取消后删除；参考录音与显式合成文件留在数据目录 voice/，需要单独备份或删除。合成文件不自动播放；唤醒提示的短音频播放后删除。

## 配置与设备

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| enabled | false | 允许语音采集 |
| wake_word | 沐璃 | 交给声学组件的目标词 |
| device | auto | auto / cpu / rocm / cuda |
| input_device | 空字符串 | Linux 使用默认设备；macOS / Windows 必须显式指定 |
| sample_rate | 16000 | 采样率，8000 至 48000 |
| chunk_seconds | 2 | 唤醒片段长度，最多 10 秒 |
| max_utterance_seconds | 15 | 包括前置片段的指令录音上限 |
| cooldown_seconds | 3 | 重复唤醒冷却 |
| wake_command / speaker_command | [] | 声学唤醒 / 声纹命令 argv |
| wake_threshold / speaker_threshold | 0.6 / 0.75 | 有限置信度阈值 |
| max_file_bytes / max_total_bytes | 2000000 / 20000000 | 单文件与目录配额 |
| tts_enabled | false | 允许调用已配置 TTS；用于“请说”和显式语音合成 |

auto 只探测主机可见的 /dev/kfd、/dev/dri、NVIDIA 设备或工具，优先传递可见加速设备，否则传 CPU。显式请求的 GPU 不可见时报告 CPU 回退。设备可见不证明推理框架或模型能使用它，status.device.runtime_verified=false；桥接组件必须验证支持并处理自身的 CPU 回退。不会探测时装载大模型或安装 CUDA / ROCm。

Linux 录音优先 arecord，其次 ffmpeg PulseAudio；macOS 使用 ffmpeg avfoundation 音频设备，Windows 使用 ffmpeg dshow 音频设备。input_device 以独立参数传递，不经 shell。Linux 播放探测 pw-play、aplay（WAV）、ffplay；macOS 探测 afplay、ffplay；Windows 探测 ffplay 或 PowerShell/.NET WAV 播放。工具存在只表示候选可用，实际权限和编解码支持以执行结果为准。

## 声学桥接协议

wake_command / speaker_command 是完整 argv 数组，不是 shell 命令字符串。MuLi 通过 stdin 发送单个 JSON 对象，组件必须在有限时间内向 stdout 返回一个 JSON 对象，诊断信息写 stderr。请求示例：

```json
{
  "operation": "wake",
  "audio_path": "/private/data/voice/random.wav",
  "sample_rate": 16000,
  "input_device": "",
  "device": "cpu",
  "requested_device": "auto",
  "wake_word": "沐璃",
  "reference_path": null
}
```

唤醒返回 `{"detected":true,"score":0.91}`；声纹 operation 为 verify，reference_path 指向已录制参考 WAV，返回 `{"verified":true,"score":0.93}`。结果必须包含真正的布尔值，以及 0 至 1 的有限数值置信度；仅返回 text、字符串 "true"、NaN 或超范围得分都会拒绝。MuLi 检查退出状态、JSON 格式与阈值；这不替代对用户选择的声学模型效果和可信度的验证。

`await worker.enroll(seconds=5)` 是显式录制参考音频入口，要求已启用语音，1 至 15 秒且不超过录音上限。返回本地音频 id/path/name/size/mime_type 和 acoustic_verification_available。它只保存参考 WAV，不宣称已经训练或实现声纹识别；必须配置真正的 speaker_command 才能比对。参考索引保存在 voice/enrollment.txt，重启后读取，不会自动重新录制。

## 模块接线

`VoiceWorker(config, data_dir, media, on_text, *, recorder=None, wake=None, speaker=None, player=None, on_status=None)` 支持小型本地组件替换。status 同步读取能力；start / stop / aclose 管理后台任务；listen_once 执行一次有限监听；enroll 录制参考；synthesize 保存合成文件，不播放。on_status 可接 SSE，当前提示事件为 `{type:"voice.prompt",prompt:"请说",played,kind,reason}`，kind 是 speech / tone / status；最近结果也在 status.last_prompt 中。

录音与声学处理使用同一异步锁，避免后台监听和录制参考争用麦克风。后台监听、手动 listen_once、enroll 及 synthesize 都登记为当前操作；stop 会取消正在执行或排队的操作，等待录音进程退出和临时文件清理后返回，随后允许再次 start。aclose 是最终关闭，会拒绝新的监听、录制、合成或启动，status.closed=true；即使等待关闭的调用方被取消，也先完成已有操作的清理再传播取消。文件与进程都有限额；取消会终止子进程、删除未完成文件。失败后至少等待两秒再重试，能力缺失时 start 不进入轮询。

转写回调在采集与文件锁释放后执行，可以合成回复语音。整个回调仍属于当前可取消操作；回调内的嵌套 TTS 不会让关闭流程遗漏尚未结束的录音任务。

录音和播放参数依据 [FFmpeg Devices](https://ffmpeg.org/ffmpeg-devices.html) 与 [Arch arecord 手册](https://man.archlinux.org/man/aplay.1.en)。ASR/TTS 的 HTTP 接线、接口兼容性与请求预算复用 [媒体服务](providers.md#视觉语音和检索服务)。
