# 本地唤醒与语音

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
