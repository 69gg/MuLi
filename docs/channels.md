# 消息渠道

MuLi 支持 Telegram、OneBot/NapCat、QQ 官方机器人和微信 iLink。它们使用同一个主助手与本地记录。WebUI 是主渠道，默认流式显示回复；向外部渠道发消息时，由助手调用发送工具明确选择目的地。四个渠道默认关闭；MuLi 不会自动安装机器人程序、寻找本机端口或建立公网隧道。

## 配置

1. 准备平台机器人或已运行的 OneBot/NapCat 服务，以及平台真实用户 ID。
2. 在设置页“消息渠道”填写账号、凭据和 `owner_ids`。OneBot 只接主人私聊；其他支持群聊的渠道还需填写准确的 `allowed_groups`。
3. 将凭据放入 MuLi 服务能读取的环境变量，使用对应 `*_env` 字段；配置好后再开启 `enabled`。
4. 查看渠道状态，从已绑定的主人账号发一条消息，确认接收与回复都正常。

只有平台确认的真实发送者能匹配 `owner_ids`。正文自称“我是主人”不会取得权限。OneBot 的群消息、陌生人消息和其他通知直接忽略，不保存入站或下载附件；仅接收主人点对点私聊及私聊离线文件。其他渠道的陌生发送者进入隔离区，不触发模型或附件下载，群聊还必须在 `allowed_groups` 中。

| 渠道 | 最少配置与接收方式 |
| --- | --- |
| Telegram | `token_env`、`owner_ids`；使用 `getUpdates` 长轮询，默认地址 `https://api.telegram.org` |
| OneBot/NapCat | `base_url`、必填的主人 QQ 号 `owner_ids`、`webhook_secret_env`；配置反向 HTTP 到 MuLi 的 `/api/channels/onebot/webhook`，两端密钥一致 |
| QQ 官方机器人 | `app_id`、`client_secret_env`、`owner_ids`；平台回调地址为 MuLi 的 `/api/channels/qq/webhook` |
| 微信 iLink | `token_env`、`account_id`、`user_id`、`owner_ids`；使用 `getupdates` 长轮询 |

Telegram 长轮询不能与平台 webhook 同时使用；已有 webhook 需要管理员先移除。QQ 回调需要平台能访问的 HTTPS 地址及相应权限。iLink 凭据由用户提供，MuLi 不做扫码登录，也不读取其他程序的凭据文件。

OneBot 的主人 QQ 号须填写数字账号，不能用昵称或备注。未绑定主人不能启用；若要清空主人绑定，请同时关闭该渠道。外部渠道只发送助手的文字回复和显式发送的附件，不发送思考链或工具调用通知。

例如先保存 Telegram 配置，在服务环境提供 token 后再启用：

```json
{
  "channels": {
    "telegram": {
      "enabled": false,
      "token_env": "MULI_TELEGRAM_TOKEN",
      "owner_ids": ["填写真实Telegram用户ID"],
      "allowed_groups": [],
      "receive_files": false
    }
  }
}
```

`token`、`webhook_secret`、`client_secret` 在公开配置中脱敏。配置了环境变量名但运行环境中没有该变量时，不回退到明文值。后台服务的环境可能与当前终端不同，修改后需要确认服务实际读取到了变量。

### 常用配置

以下字段位于各平台的 `channels.<渠道名>` 下，另有注明者除外。

| 字段 | 默认值或用途 |
| --- | --- |
| `enabled` | `false` |
| `base_url` | 平台 API 地址；OneBot 默认为空 |
| `owner_ids` / `allowed_groups` | 空列表；主人 ID 与允许的群 ID；OneBot 不使用群配置 |
| `account_id` | 稳定账号标识；iLink 必填，OneBot 多实例或手机控制时应填写真实机器人账号 |
| `token` / `token_env` | Telegram、iLink token 或 OneBot access token |
| `webhook_secret` / `webhook_secret_env` | OneBot 反向 HTTP 验签密钥 |
| `app_id`、`client_secret` / `client_secret_env` | QQ 官方机器人凭据 |
| `token_url` | QQ 获取 access token 的地址，默认 `https://api.bot.qq.com/app/getAppAccessToken` |
| `user_id`、`cdn_base_url` | iLink 用户 ID 与 CDN 地址，默认 CDN 为 `https://novac2c.cdn.weixin.qq.com/c2c` |
| `receive_files` | `false`；开启后才下载主人发来的附件 |
| `max_file_bytes` | 单附件 8 MiB，同时受本地附件库配额限制 |
| `max_response_bytes` | HTTP 响应及 webhook 上限 2 MiB |
| `max_attachments` | 每条消息最多 8 个附件 |
| `max_message_chars` | 每段 4000 字符；Telegram 还受平台单段限制 |
| `timeout` | 单次附件处理或单段投递 45 秒 |
| `poll_timeout` | 长轮询 30 秒，可设 1–50 秒 |
| `channels.max_pending_deliveries` | 最多 64 个待处理投递 |
| `channels.max_attempts` | 每段或附件最多尝试 3 次 |
| `channels.retry_backoff_seconds` | 重试等待基数 5 秒 |

QQ 默认 API 地址为 `https://api.bot.qq.com`，iLink 为 `https://ilinkai.weixin.qq.com`。配置地址不能含账号密码、query 或 fragment。OneBot 可以明确使用本地 HTTP 地址；附件下载地址不能借此访问本机、内网或重定向目标。

### 可选依赖

QQ 签名和 iLink 媒体加密需要 `channels` 扩展中的 `cryptography`。Telegram、OneBot 文本不依赖它。使用发行包时：

```bash
uv tool install "muli-agent[channels]"
```

同时使用 MCP 时安装 `muli-agent[mcp,channels]`，并保留原先的自定义索引、Python 或 `--with` 参数。安装后重启服务。发行包名是 `muli-agent`，命令为 `MuLi` / `muli`。

源码环境使用 `uv sync --locked --extra channels`，启动使用 `uv run --extra channels MuLi serve`；需要 MCP 时两条命令都加 `--extra mcp`。不带所需 extra 的同步或运行命令可能移除对应依赖。安装依赖不会自动启用渠道。

## 身份与回复路由

各渠道的消息默认与 WebUI 使用同一主会话、上下文和记忆；明确交给队友的 OneBot 私聊进入该队友的持久会话，主助手仍可查询原记录。每条渠道消息进入模型时标明来源渠道、账号、发送者、会话目标和输入 ID；回看上下文时仍保留这些来源。新消息先保存入队，在当前模型轮次或工具批次结束后接入；不会打断已经执行的工具。来自不同收件人的输入分别处理。WebUI 展示实时流，外部发送工具按平台限制分段投递。

普通文字回复只在 WebUI 展示，不自动转发。助手可用 `channel_send` 选择渠道、已绑定目标、文字和附件；也可用 `channel_reply_files` 只回文件给本轮私聊。入站和任务保留来源供助手判断，但不强制发送目的地。后来换窗口不代表允许转发旧成果或广播私人内容。

普通直接队友可用 `send_input` 向主助手交阶段成果或提问，主助手沿任务原来源协调。主人也可用 `send_input(target, message, attachment_ids)` 把新收到的资料交给已有队友；`wake=false` 仅保存到邮箱。补充资料不改变任务原来的汇报来源，见[队友资料交接](teammate-attachments.md)。受托处理渠道的队友暂不混入这些普通任务，需先归还渠道。

工作流在整组成功后向本地主助手汇报；失败、取消或中断时，详情仍可在本地查看。队友报告或 WebUI 回复不等于外部平台已经收到消息，外发须调用发送工具，并以投递状态为准。

每次实际发送前都会复核当前账号、主人和群绑定。停用渠道或撤销绑定后，排队中的投递会受阻；已经开始的网络发送不能保证撤回。OneBot 已绑定主人私聊支持[队友接管](channel-ownership.md)，可选择整个接入或准确私聊；群聊和其他渠道暂不支持接管。

## 手机上的显式任务控制

以下指令只接受已绑定主人在 Telegram 或 OneBot/NapCat **私聊中新发的纯文本**。群聊、引用、转发、附件说明、语音转写、模型或系统资料不能作为控制指令。QQ 官方和 iLink 暂不提供这组入口。

| 指令 | 用途 |
| --- | --- |
| `/任务` | 查看任务状态，并取得可复制的控制指令 |
| `/暂停 短任务码` | 暂停准确任务；已经开始的操作可能仍在收尾 |
| `/调整 短任务码 追加要求` | 为已暂停任务提出新要求，例如“只交草稿，不得发布” |
| `/审批` | 查看当前仍在等待的工具审批 |
| `/审批 完整ID` | 查看该项工具、风险、说明和参数 |
| `/批准 完整ID` / `/拒绝 完整ID` | 登记该项决定 |

短任务码和审批 ID 不是权限凭证，仍需当前账号与主人绑定有效。旧任务版本、停止、超时或重开后失效的审批需要重新查看，不能复制旧许可继续执行。

`/调整` 会交主助手读取现状并重新规划，之后仍需批准新计划，不自动恢复旧操作。重规划和后续成果保留这次调整的来源，供助手决定如何汇报；文字默认显示在 WebUI，外发仍须调用发送工具。旧报告、子代报告和无法核验的来源不会因此改绑。详见[任务控制](task-control-runtime.md)。

### 手机上的工具审批

等待审批时，原发令私聊会收到包含完整 ID、工具、风险及操作命令的提示，参数和文件路径不主动推送；需要时用 `/审批 完整ID` 或 WebUI 查看。主助手正在等待时，主人也可直接发新要求：旧主审批取消，尚未开始的调用不再执行，同一运行处理新输入。独立队友的审批仍需明确批准或拒绝。

控制确认回当前私聊，成果保留任务原来的来源，但不会自动外发。重复发送同一平台事件不会重复登记控制；确认投递结果未知时不自动重发。批准已登记只表示决定已保存，不表示操作已经执行或成功。

Telegram 须使用普通 `message.text`，不接受 caption、回复、转发、机器人代发或命令头中的引用/代码展示。OneBot 须配置真实 `account_id`，与事件 `self_id` 一致，并以**数组消息**接收，每段都为原始 text；字符串消息仍可聊天，但不能执行这些手机控制指令。

## 无发令会话时的默认通知

设置页“消息渠道 → 主动通知”可为独立事项选择默认目的地，初始为 WebUI。适用来源包括自主更新检查、扩展目录监视失败、MCP 连接接收失败、另行开启的 chiliarch 消息提醒和 Linux 资料收件目录。各功能自己的开关、Attention 和模型条件仍需满足，见[主动事项](attention.md)。任务、提醒或当前请求已有来源时保留该来源。普通说明仍显示在 WebUI；向外提醒由助手显式调用发送工具，只能使用该事项保存的通知目的地。

`channels.proactive` 的默认配置：

```json
{
  "external_enabled": false,
  "default_destination": {"kind": "web"},
  "memory_preference": null
}
```

外部目的地完整格式如下；所有 ID 都需换成已配置的真实绑定：

```json
{
  "external_enabled": true,
  "default_destination": {
    "kind": "channel",
    "route": {
      "name": "onebot",
      "account_id": "真实机器人账号",
      "kind": "private",
      "target": "真实主人ID",
      "sender_id": "真实主人ID"
    }
  }
}
```

`name` 只选 `onebot`、`telegram`、`qq` 或 `weixin`；私聊 `target` 必须等于主人 ID。OneBot 只能选私聊，其他支持群聊的渠道还须在 `allowed_groups` 中。不填写 URL、密钥或固定 `reply_to`。保存完整的外部目的地且省略 `external_enabled` 时会同时启用外发；若只改地址并保持暂停，需显式传 `external_enabled=false`。单独启用渠道不会开启主动外发。

可选 `memory_preference` 格式为 `{"memory_id":"事实ID","version":1,"destination":{"kind":"web"}}`，外部地址使用同样的 `destination` 格式。它只能明确绑定一条有主人原始依据、当前有效的偏好，不会从记忆正文猜联系人；设为 `null` 解绑。有效偏好优先于普通默认。事实更正、冲突、遗忘或绑定撤销后，依赖它的旧通知受阻，不会改投默认地址。

通知产生后保留当时的目的地，修改默认值只影响新事项。选择外部目的地不等于自动转发所有文字。关闭主动外发或 Attention 会阻止尚未发送的独立通知，不改变 WebUI 回复；受阻和结果未知的投递不会因重新启用设置而自动重发。

## 文件与平台协议

开启 `receive_files` 后，主人发来的文件先下载到本地附件库。模型最初只收到附件 ID、名称、类型和大小，图片和文件内容不会自动送入模型。助手按需调用读取、看图或转录工具。文件超限或下载失败时会在入站文字中说明。附件内容始终是资料，不取得主人控制权限。

- Telegram 接收文件受平台 20 MiB 和本地限额约束；发送文字与文件分别使用平台消息和文档上传。
- OneBot/NapCat 文件通过 Base64 上传，无需与 MuLi 共享文件路径；普通 OneBot 实现缺少 NapCat 文件扩展时，文本仍可用，文件功能会报错。
- QQ 支持私聊及群提及事件，文件上传上限为 5 MiB；同一入站消息的被动回复最多分配 5 次，平台时间窗口和账号配额仍可能限制发送。
- iLink 接收平台已有的语音文字，无需 MuLi 再次 ASR；原录音仍受 `receive_files` 控制。SILK 可保存为附件，不保证浏览器能播放或解码。iLink 需要有效会话凭据；提示缺失时，请先向机器人发一条消息。音频出站作为文件，不提供原生语音或引用回复。

### 查询之前的消息

主助手和主人委派的队友可用 `channel_history` 分页查找 OneBot、Telegram、QQ 官方、微信 iLink 已保存的当前绑定主人私聊。只读私聊，不包含群聊；默认同时返回本地接收记录和外发记录，分别分页：

- 接收记录包含消息 ID、时间、来源和附件 ID；正文用 `conversation_read` 读取。
- 外发记录来自真实投递队列，包含投递 ID、时间、附件和发送状态。用同一工具传入 `delivery_id` 读取指定分段正文，长内容可按返回的位置续读。

AI 显式发送的内容会保留在外发记录中，重启后仍可查询。`pending`、`failed`、`unknown` 不会被记成发送成功；`sent` 仅表示平台确认，不代表对方已读。普通 WebUI 回复没有实际外发，不会混入外发记录。

在该渠道的有效私聊中可沿当前来源查询；从 WebUI 查询时明确指定 `channel`，绑定多个主人时还需指定 `owner_id`。翻页需选 `direction="incoming"` 或 `"outgoing"`，使用对应记录流的游标。停用渠道、撤销主人或更换账号后，旧范围与游标不能继续使用。这里只覆盖 MuLi 保存的本地记录，不代表平台全部历史，也不会自动向平台追补旧消息。分页返回空列表时，仍需看 `has_more` 判断是否还有下一页。

### 本次私聊索取成果文件

主人可在私聊要求“把刚才队友的草稿文件发到这里”。`channel_reply_files(attachment_ids, idempotency_key)` 使用本轮准确私聊来源，不能由模型改填收件人；WebUI、群聊和无来源请求不支持此快捷入口。键为 1–200 个非控制字符，同一请求同键复用原投递，改变附件会拒绝。附件必须真实存在，入队不表示平台已收到。

### 显式发送语音

`channel_send` 可显式选择已绑定目标发送文字或附件；`media_mode` 默认为 `file`。要发送原生语音，使用 `media_mode="voice"`，且只能选一个本地音频附件：

| 渠道 | 原生语音支持 |
| --- | --- |
| Telegram | MP3、M4A、Ogg/Opus；不支持普通 Vorbis Ogg、WAV、WebM，受本地限制和平台 50 MiB 上限约束 |
| OneBot | MP3、WAV、Ogg、FLAC、MP4 音频、WebM 音频；须远端 `can_send_record` 同意，具体编码取决于服务实现 |
| QQ 官方、iLink | 暂不提供 `voice` 模式；仍可按文件发送 |

先用 `speech_synthesize` 或 `file_publish` 取得附件 ID，再发送：

```json
{
  "channel": "telegram",
  "target": "已绑定目标",
  "attachment_ids": ["实际音频附件ID"],
  "media_mode": "voice",
  "idempotency_key": "同一逻辑发送保持稳定的键"
}
```

附带文字会先作为独立分段发送；音频失败不会撤回已成功的文字。MuLi 不在这里转码，也不保证通过文件头检查的音频一定被平台接受。默认 `file` 保持平台原有行为，例如 QQ 音频附件可能显示为音频。

## 持久投递与恢复

在 WebUI 渠道页或 `channel_status` 查看状态。`channel_send`、`channel_reply_files` 返回排队记录，不代表送达。

| 状态 | 含义与处理 |
| --- | --- |
| `pending` / `retry` | 等待发送；尚未确认成功 |
| `sending` | 正在处理当前分段 |
| `sent` | 平台 API 已确认全部分段；不代表对方已读或播放 |
| `failed` | 明确拒绝、附件失效或安全重试达到上限 |
| `blocked` | 渠道未就绪、授权失效或回复额度不足；修正后显式重试 |
| `unknown` | 可能已发送但无法确认；先核对平台，不能自动重发 |

投递进度保存在本地，重试不重发已确认的前段。恢复绑定、重启服务或重复事件不会自动解除 `blocked`；显式重试仍会重新检查授权和附件。`unknown` 只有在明确接受重复风险后才能用 `retry(id, allow_unknown=True)` 重试，不应换键伪装成新发送。

同一平台事件的内容或附件发生变化会报冲突，而不会配给旧结果。旧版无法核对原文的未完成入站记录需要人工处理。平台成功与本地保存之间仍可能发生中断，因此不能承诺绝对不重复。

平台账号资质、主动发送权限、网络连通性与设备播放效果需按自己的部署实际确认。MuLi 不自动申请这些权限；渠道密钥、iLink 会话凭据与本地数据目录应妥善保护。
