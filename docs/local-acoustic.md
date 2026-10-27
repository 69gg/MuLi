# 本地声学引擎

如果已经准备好 sherpa-onnx 及其本地模型，可以使用 MuLi 提供的适配器完成唤醒和声纹比对，不必自己编写桥接程序。此功能默认关闭，**不会安装依赖或下载模型**。

普通网页录音、ASR、TTS 不需要这个引擎。先阅读[语音设置](voice.md)，配置 ASR 和录音设备；不要仅为转写音频打开后台监听。

## 准备配置

在 MuLi 所用的 Python 环境中准备兼容的 `sherpa_onnx` 安装，以及中文关键词唤醒模型、关键词文件和声纹模型。适配依据是官方 [KeywordSpotter API](https://github.com/k2-fsa/sherpa-onnx/blob/v1.13.8/sherpa-onnx/python/sherpa_onnx/keyword_spotter.py) 和[声纹示例](https://github.com/k2-fsa/sherpa-onnx/blob/v1.13.8/python-api-examples/speaker-identification.py)。模型文件并不随 MuLi 分发。

例如将以下内容保存为 `~/MuLi/acoustic.json`，路径替换为自己的模型文件。相对路径从该配置文件所在目录计算：

```json
{
  "provider": "cpu",
  "wake": {
    "tokens": "models/wake/tokens.txt",
    "encoder": "models/wake/encoder.onnx",
    "decoder": "models/wake/decoder.onnx",
    "joiner": "models/wake/joiner.onnx",
    "keywords_file": "models/wake/keywords.txt",
    "sample_rate": 16000
  },
  "speaker": {
    "model": "models/speaker/model.onnx"
  }
}
```

关键词文件必须按所选模型的分词格式编写，并为目标词提供准确结果标签，如 `@沐璃`；不能把任意汉字文件当作所有模型通用的关键词文件。适配器只接受与 `voice.wake_word` 完全一致的结果，不把检测到其它词当作成功。

`wake` 和 `speaker` 可分别配置；只配置唤醒不能取得主人身份。默认音频限制为单文件 2,000,000 字节、最长 120 秒，可通过 `max_audio_bytes` 和 `max_audio_seconds` 调低。录音仍受主配置中更早的语音配额约束。

## 检查并接入

用 **MuLi 实际使用的解释器**运行，路径仅作示例：

```sh
/实际路径/python -m muli.acoustic_bridge --config ~/MuLi/acoustic.json --check
```

这只检查配置、本地文件和当前解释器能否发现引擎，不加载模型、不录音。`available: true` 不是声学识别或硬件测试成功；`runtime_verified` 仍为 `false`。文件缺失或格式错误会失败，缺库时显示不可用。

然后请沐璃将主配置中的两个桥接命令设置为同一数组，替换解释器和配置的绝对路径：

```json
{
  "voice": {
    "enabled": false,
    "wake_word": "沐璃",
    "device": "auto",
    "sample_rate": 16000,
    "wake_command": ["/实际路径/python", "-m", "muli.acoustic_bridge", "--config", "/实际用户目录/MuLi/acoustic.json"],
    "speaker_command": ["/实际路径/python", "-m", "muli.acoustic_bridge", "--config", "/实际用户目录/MuLi/acoustic.json"]
  }
}
```

命令数组中的 `~` 不会由 shell 展开，应使用绝对路径。使用同一解释器和上述标准命令形式时，`media_capabilities` 会在录音前检查依赖、模型文件、采样率和明确选择的设备。其它外部桥接仍按原协议工作。

确认配置后再开启语音，并明确让沐璃使用 `voice_enroll` 录制参考音频，再用 `voice_listen` 做一次监听。登记只保存参考 WAV，不训练模型。声纹拒绝时不发送 ASR；未配置声纹的转写不能作为主人命令。参考音频留在本地比对。

## 设备和使用限制

默认使用 CPU，每个桥接进程的模型推理线程数为 1。CUDA 需同时在该文件中设置 `provider: "cuda"`，并准备支持 CUDA 的引擎安装。明确选择的设备必须与配置一致；`auto` 使用该文件配置，不根据显卡可见性推断支持。

**此适配器目前不支持 ROCm。** 可继续通过[外部桥接](voice.md#声学桥接协议)接入支持它的引擎。返回的 `configured_provider` 只是配置值，不能证明实际在 GPU 执行。

录音必须是单声道 PCM16 WAV，采样率与配置相符。每片录音使用一次受管进程，沿用 15 秒期限；小片段间的模型加载成本、跨片段唤醒效果以及实际模型准确度需在你确认允许录音后验证。本项目的离线回归不证明真实模型、CUDA 或三平台音频设备可用，也不提供抗录音重放保证。

唤醒使用 `voice.wake_threshold` 作为引擎的关键词阈值。由于引擎不提供本次命中的概率，结果是附带实际阈值的布尔决定，不显示虚构置信度。声纹使用真实向量余弦相似度，与 `voice.speaker_threshold` 比较；需要按自己的录音环境验证阈值。
