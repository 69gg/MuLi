# 微信 iLink 入站语音文字与原始附件

本片只接收平台已经提供的 `voice_item.text`，并识别附件中的 Tencent SILK 格式。它不调用 ASR、不解码或转码，也不增加原生语音发送能力。

## 输入合同

- 沿现有 `item_list` 前 64 项顺序处理。`voice_item.text` 仅接受非空白字符串，保留原始内容；缺失、空白和其他 JSON 类型不被转成文字。普通 `text_item` 沿用原有转换行为。
- 连续普通文本仍直接拼接。有效语音转写与前后文字片段之间加入换行；即使两个内容完全相同也都保留，不假设它们是平台镜像或做语义去重。一个平台消息仍只提交一次入站信封。
- 整个 HTTP 响应继续受渠道 `max_response_bytes` 限制；合并文本沿用 `ChannelManager._receive` 的 64,000 字符上限，超限拒绝，不静默截断。`max_message_chars` 仍只控制出站文字分片。
- 平台转写与普通渠道文字一样经过现有主人身份、路由和持久幂等校验，不提供新权限。`receive_files=false` 不丢弃已收到的转写，但不会为此下载录音；非机主也不会自动下载附件，其文字仍走现有隔离路径。

## 附件与能力边界

`attachments.detected_media` 仅检查现有最多 512 字节文件头中的 Tencent SILK 标识 `\x02#!SILK_V3`，标识后需有数据，再登记为 `audio/silk`。这只是 MIME 识别，不证明帧完整、时长、音频可解码或采样参数可信。伪造 MIME、其他二进制和截断标识仍作为普通文件。

SILK 不加入 `SAFE_INLINE`，浏览器保持下载文件展示。原始录音仍走既有有界 CDN 下载、AES 解密与附件配额，不因转写存在而额外外传音频。`audio_transcribe` 仍需显式调用已配置 ASR，服务是否接受 SILK 取决于服务本身；本片不承诺自动识别或自动重试。TTS 和出站 `media_mode=voice` 的支持范围不变，微信仍明确不支持该发送模式。

既有入站记录继续使用原消息 ID 和规范化内容指纹。升级后重放的同 ID 信封若内容不同，仍按现有规则拒绝，不为本片放宽幂等校验或重新提交历史内容。

## 固定来源与验证

- 腾讯官方 [`v2.4.6/src/api/types.ts`](https://github.com/Tencent/openclaw-weixin/blob/v2.4.6/src/api/types.ts) 定义 `VoiceItem.text`、编码和时长字段；协议版本不因本片升级。
- 独立客户端 [`weixin-ilink-client v0.1.3/src/weixin_ilink_client/types.py`](https://github.com/69gg/weixin-ilink-client/blob/v0.1.3/src/weixin_ilink_client/types.py) 已读取平台转写；其 [`constants.py`](https://github.com/69gg/weixin-ilink-client/blob/v0.1.3/src/weixin_ilink_client/constants.py) 提供 Tencent SILK 标识。该项目不是腾讯官方客户端。

本地检查均通过统一 `check_local.py --seconds 30 run` 资源守卫串行执行：

- 新增 `tests/test_weixin_voice_ingress.py` 共 32 项通过，0.16 秒，峰值 60.5 MiB。覆盖顺序与有意重复、无效转写、64 项及 64,000 字符边界、HTTP 响应预算、主人/隔离与下载开关，以及模拟长轮询到 AES 解密、附件留存和原指纹重投冲突的实际调用链。
- 既有 `test_channels.py`、`test_channel_voice.py`、`test_attachment_records.py`、`test_auth_uploads.py` 共 96 项通过，1.44 秒，峰值 156.4 MiB。
- 修改的 3 份 Python 文件通过 Ruff 与格式检查，2 份源码通过严格 mypy；类型检查峰值 127 MiB。全部检查 swap 为 0，未运行全仓检查。

测试只使用小型合成信封、HTTP MockTransport、临时附件库和伪造的 SILK 标识数据。未连接真实账户或调用模型、ASR，也没有录音、真实客户端播放、编解码有效性及 Windows/macOS 运行验收。
