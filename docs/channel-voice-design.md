# 显式渠道语音发送

状态：已实现并通过相关离线验证，尚未发行，未做真实平台收发。复用现有 ASR、TTS、附件库与渠道 outbox，不安装录音、模型或转码依赖。

## 最小合同

`speech_synthesize` 已通过当前配置的 TTS 保存本地附件并返回 ID；`audio_transcribe` 按需读取附件转录。新增的发送路径是 `channel_send(media_mode="voice", attachment_ids=[id])`，不自动录音、合成、播放或发送其他音频。

`media_mode` 仅有 `file` 与 `voice`，默认 `file`，表示原有附件发送路径。这里的 file 不承诺各平台客户端都显示为普通文档：QQ 原有音频附件的富媒体处理保持不变。voice 必须恰好带一个受支持的本地音频附件；可带文字，文字继续按原规则作为先行独立分段。预先拒绝不支持的渠道与格式，不自动降级或转码。

| 渠道 | 显式 voice | 发送合同 |
| --- | --- | --- |
| Telegram | 支持 | multipart `sendVoice`，MP3、M4A 或 Ogg/Opus；不把 Vorbis Ogg 当 Opus。受本地附件限额及平台 50 MiB 上限约束。 |
| OneBot 11 | 支持 | 先以 `can_send_record` 查询实现能力，再由 `send_private_msg` / `send_group_msg` 发送 `record` 数组段，内容为 `base64://`。远端实现负责其支持的音频编码，拒绝时不改成文件。 |
| QQ 官方 | 本片未支持 | 默认附件路径保持，包括既有音频富媒体行为；不把它当作已验证的新 voice 合同。 |
| 微信 iLink | 当前 MuLi 适配器未支持 | 默认音频仍作为文件；本片不新增 SILK 编码或时长测量。不声称整个 iLink 协议永远不支持出站语音。 |

## 持久化与失败

voice 模式写入 outbox 的附件 part；旧 part 没有模式时沿用 file。复用原事务、分段位置、成功回执和发送中断恢复，不增加新表。工作器在发送前重新读取并验证附件摘要和大小，附件改变或删除明确失败。

`channel_send` 增加可选的有界 `idempotency_key`，映射到现有 `event_id` 去重路径。同一键、渠道与可信路由返回原投递；正文、附件或发送模式改变会拒绝，不产生第二份投递。省略键表示新的显式发送。键应为同一逻辑发送保持稳定，不能通过换键把未知结果当成安全重试。

去重查找沿用现有预检顺序，同键重试也要求附件仍存在且可核验；附件删除后应按投递 ID 或 `channel_status` 查询历史记录，不承诺重新发送调用仍返回收据。

入队不是送达。只有平台明确确认才记为 sent；语音请求发出后的无效回执、读写超时、取消或遗留 sending 均为 unknown，不自动重发。OneBot 能力查询没有发送消息，不把查询失败误称为消息已发出。仍不承诺跨平台恰好一次。

## 验证

以临时 SQLite、假音频和 HTTPX MockTransport 覆盖协议、格式拒绝、默认兼容、同键模式冲突、持久重开、分段位置及 unknown 不重试；均经根目录 `scripts/check_local.py` 串行执行。新增语音、原渠道、应用集成及附件服务共 61 项通过，用时 0.99 秒，service 峰值 121.7 MiB、swap 0。4 份修改源码严格 mypy 通过，相关 Ruff lint 与格式检查通过。独立静态审查无阻断，已据审查补全“同键重试也需附件可核验”的边界说明。

没有连接真实账号、调用 ASR/TTS、下载模型、安装或转码。伪造的短音频只验证有限文件头与协议，不证明真实音频解码、平台账号权限或客户端渲染。

## 协议依据

- [Telegram sendVoice](https://core.telegram.org/bots/api#sendvoice)：支持的上传格式、multipart 参数及平台上限；已用 Context7 与官网核对。
- [OneBot 11 语音段](https://github.com/botuniverse/onebot-11/blob/master/message/segment.md#语音)、[公开 API](https://github.com/botuniverse/onebot-11/blob/master/api/public.md)：record、Base64 与 can_send_record；已用 Context7 核对。
- [QQ 富媒体](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/rich-media.html)：现有 file_type 与 file_info 路径；本片不修改默认行为。
- [weixin-ilink-client](https://github.com/69gg/weixin-ilink-client)、[0.1.3 包说明](https://pypi.org/project/weixin-ilink-client/0.1.3/)：不同来源的语音能力说明并不一致，MuLi 当前实现能力以本地适配器和本片验收为准。
