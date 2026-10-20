# 可选消息渠道

MuLi 可把 Telegram、OneBot/NapCat、QQ 官方机器人及微信 iLink 的消息送入统一主会话，并把该次请求的回复投回原渠道。四个渠道默认都关闭；启用后才访问明确配置的平台地址，不探测本机端口，不下载或启动机器人程序。

## 身份与回复路由

`owner_ids` 必须填写平台真实用户 ID。只有验签后的事件或已鉴权长轮询结果中的真实发送者能匹配机主身份；消息正文自称“我是机主”无效。其他发送者进入隔离区，不触发模型，不下载附件，也不保存 iLink 的会话凭据。群消息还必须精确匹配 `allowed_groups`，未绑定的群直接忽略。

渠道消息使用 `source=channel:<name>:<account>`，路由为：

```json
{
  "name": "telegram",
  "account_id": "机器人账号标识",
  "target": "会话ID",
  "kind": "private",
  "sender_id": "真实用户ID",
  "reply_to": "入站消息ID"
}
```

`name` 为 `onebot`、`telegram`、`qq` 或 `weixin`；群聊的 `kind` 为 `group`。私聊的 `target` 必须与 `sender_id` 相同。账号优先取 `account_id`、`app_id`、`user_id`；Telegram 还可取 token 中公开的机器人数字 ID；其他情况为逻辑账号 `bot`。OneBot 配置多个实例或切换账号时应明确设置稳定的 `account_id`。

每次模型请求在进入上下文钩子前冻结宿主来源；主会话完整 assistant 消息携带一个 `delivery_routes` 时才自动出站，取消后的不完整回复跳过。网页输入对应本地回复；模型流式生成期间新到的渠道输入只影响下一次请求。发送前再次检查当前机主、群和账号绑定；撤销绑定后旧的排队消息不能继续发给该对象。来源记录缺少账号时，从原输入的 `source` 取得账号，不套用后来配置的新账号。

Team 队友保存派工时的来源，并由子队友、恢复、普通补充消息和结果报告继承。从 A 渠道派工后切到 B 或 WebUI，主助手整理该任务后台报告时仍投回 A。输入队列只在既有工具回放安全边界领取连续同目的地的一段；完整 route（含账号及 `reply_to`）不同的输入不混成一段外发回复。普通 `send_message` 和 `resume` 不改绑；本片没有改绑工具，新任务需要新建队友。报告仍是资料，不取得主人身份或新的执行授权。

旧队友或旧报告没有原始来源证据时，回复留在 WebUI。队友自身消息不直接自动外发；Workflow 自动汇报尚未接入本合同。记忆偏好仅用于下节的独立事项，不改变 Team 已固定的来源。已有 A 目的地不可用时不会换发到 B、最近渠道或默认渠道；设计与边界见 [Team 原路汇报](report-origin-v2.15-design.md)。

路由、投递记录和状态接口不含 token、client secret 或微信 `context_token`。iLink 的 `context_token` 只单独保存在本地渠道状态中，发送时按当前账号与目标查找。它需要本地数据目录的同等保护。

## 无发令会话时的默认通知

设置页的“消息渠道 → 主动通知”可选择无发令会话时的默认目的地，默认 WebUI。第一期只有**启动时自主检查发现新版本**可以使用默认渠道。新建的提醒和自身管理计划从有效请求保存原始来源：渠道请求仍回原渠道，网页新建提醒留在 WebUI。旧提醒、旧管理计划、没有宿主请求来源的直接调用及系统日志保持本地。没有来源字段不等于独立事项，模型也不能从记忆或最近聊天猜收件人。

一次选择外部目的地并保存，即启用独立事项外发；不需要额外确认。配置 API 与 `self_configure` 可提交以下一个补丁，使用同次候选中的渠道账号、`owner_ids`、`allowed_groups` 校验，不能借用旧账号权限。以下数字仅为示例，须替换成真实绑定：

```json
{
  "channels": {
    "onebot": {
      "enabled": true,
      "base_url": "http://127.0.0.1:3000",
      "account_id": "bot",
      "owner_ids": ["7"]
    },
    "proactive": {
      "external_enabled": true,
      "default_destination": {
        "kind": "channel",
        "route": {
          "name": "onebot",
          "account_id": "bot",
          "kind": "private",
          "target": "7",
          "sender_id": "7"
        }
      }
    }
  }
}
```

完整的外部 `default_destination` 或 `memory_preference.destination` 补丁省略 `external_enabled` 时也同次启用；若要更改地址并继续暂停，必须同时明确给 `external_enabled=false`。单独启用渠道 transport 不会开启主动外发。暂停只需该 false，原地址仍保留。默认值为 `{"external_enabled":false,"default_destination":{"kind":"web"},"memory_preference":null}`；两个目的地配置均按完整对象替换，切回 `{"kind":"web"}` 不残留 route，也不会隐式暂停已冻结的外部项。

可选 `channels.proactive.memory_preference` 明确引用一条主人的已保存偏好：`{"memory_id":"事实ID","version":1,"destination":{"kind":"web"}}`；外部 `destination` 使用上例相同结构。在设置页填写确切 ID、版本及地址，或让 `self_configure` 一次提交完整对象；设为 `null` 取消。记忆页提供详情和按需原始证据核查。设置输入不能提供 `trusted_owner/source_id/digest` 代替来源校验，也不会根据记忆正文猜出账号、群组或收件人。

新建、更换或重新启用绑定时，宿主只按主键读取当前事实与来源的有限元数据：记忆已开启、active 的 preference、版本一致、事实及来源受主人信任、来源确为 user/owner，且具有有效摘要。排队应用和关闭组件的等待结束后再次核对，证据变化则保留原配置。后台记忆提炼不改此设置；尚未提炼出合格事实时可先使用普通默认。未变更的旧绑定失效不阻止无关设置、关闭记忆、撤销渠道权限、暂停外发或显式解绑。

产生新的独立事项时，有效绑定优先于普通默认；否则使用普通默认并在渠道状态显示原因。绑定所用事实版本、来源 ID 和摘要随目的地一同冻结，不读取全文或运行搜索/模型。取消绑定、记忆关闭、事实更正/冲突/遗忘、来源变化或更换该绑定地址后，旧的依赖项受阻，不能自动改投普通默认；冲突解决后的新版本也需重新明确绑定。恢复相同依据不会自动重发 blocked，unknown 同样保留原规则。

外部地址需要明确四种渠道之一、稳定账号、私聊或已允许群组、目标及已绑定主人。私聊目标与主人 ID 必须相同；群聊还要在 `allowed_groups`。不自动挑第一个主人或群，不允许 URL、token、`context_token` 或固定 `reply_to`。QQ/iLink 等平台仍有自己的主动发送权限、窗口与会话凭据限制；配置通过不等于平台已经允许或送达。

目的地在通知第一次持久产生时冻结；之后等待前台任务、模型排队、重启或 default A→B 都不会把旧 A 改发到 B。绑定任务及 WebUI 来源不套用默认配置。Attention 只合并连续同一目的地、同类授权依据的事项，bound 与 independent 即使同发 A 也不混批。系统资料仍为 `principal=system:attention`、`route=null`，只开放读取工具；小型路由元数据由宿主处理，不进入模型资料正文。工具读取期间的 token 继续在工作台流式显示，仅完整无工具调用的最终回复自动外发。

独立外发暂停或 Attention 关闭时，尚未发送的 independent 外发持久变为 `blocked`，不影响 bound/Team 的原路回复。账号/主人/群绑定撤销、渠道停用或未就绪则阻塞所有自动回复，保留原目的地与已成功分段。恢复设置不会自动重发 blocked；需使用既有“重试”入口，复核通过后从未完成部分继续。`unknown` 永不自动重试，仍需明确确认可能重复。改默认、重复事件和通知已读都不会复位 sent/blocked/unknown。HTTP 已开始的发送不能承诺撤回。

需要开启 Attention、订阅 update 并配置主模型才会整理自主更新通知；模型未配置可保留一批待处理输入，失败则保留运行失败，不自动再生成。`/api/channels` 与 `channel_status` 返回当前策略、记忆依据短状态、下一事项目的地及 Attention/模型条件；accepted、排队、已生成回复和 sent 各有不同含义。WebUI 始终保留通知与回复；失败后不会再创建第二条所谓 fallback 发送。记忆失效的 fallback 只适用于新事项。尚未连接真实账号验证本片，其它独立来源仍未加入。

## 配置

配置位于 `channels`。可通过设置页或配置 API 编辑；`token`、`webhook_secret`、`client_secret` 在公开配置中脱敏。优先使用对应的 `*_env` 字段；配置了环境变量名但运行环境缺少它时，不回退到明文值。

| 字段 | 默认值或作用 |
| --- | --- |
| `enabled` | `false`；显式启用 |
| `base_url` | 下表的平台地址；OneBot 默认为空 |
| `owner_ids` / `allowed_groups` | 空列表；必须显式绑定真实用户及允许的群 |
| `account_id` | 可选逻辑账号，iLink 必填 |
| `token` / `token_env` | Telegram、iLink 的机器人 token；OneBot 的 access token |
| `webhook_secret` / `webhook_secret_env` | OneBot 反向 HTTP 的共享验签密钥 |
| `app_id`、`client_secret` / `client_secret_env` | QQ 官方机器人凭据 |
| `token_url` | QQ 获取 access token 的地址 |
| `user_id`、`cdn_base_url` | iLink 机器人用户 ID 与 CDN 地址 |
| `receive_files` | `false`；仅绑定机主可自动接收文件 |
| `max_file_bytes` | 8 MiB；本地附件配额仍同时生效 |
| `max_response_bytes` | 2 MiB；解压后的 HTTP 响应及 webhook 字节限制 |
| `max_attachments` | 每条消息最多 8 个附件 |
| `max_message_chars` | 4000；按段发送，Telegram 每段还限制为 4096 字符 |
| `timeout` | 45 秒；每次入站附件处理或单段投递有总时间限制 |
| `poll_timeout` | 30 秒，允许 1–50；Telegram 使用平台长轮询等待 |
| `max_pending_deliveries` | `channels` 根字段，默认最多 64 个待处理投递 |
| `max_attempts` | 根字段，默认每段或每个附件最多尝试 3 次 |
| `retry_backoff_seconds` | 根字段，默认重试等待基数 5 秒 |

| 平台 | 默认地址 | 最少配置及收取方式 |
| --- | --- | --- |
| OneBot/NapCat | 无 | `base_url`、`owner_ids`、`webhook_secret_env`；在 NapCat 配置反向 HTTP 到 `/api/channels/onebot/webhook`，密钥两端一致 |
| Telegram | `https://api.telegram.org` | `token_env`、`owner_ids`；`getUpdates` 长轮询 |
| QQ 官方 | `https://api.bot.qq.com` | `app_id`、`client_secret_env`、`owner_ids`；回调 `/api/channels/qq/webhook` |
| 微信 iLink | `https://ilinkai.weixin.qq.com` | `token_env`、`account_id`、`user_id`、`owner_ids`；`getupdates` 长轮询 |

QQ 默认 `token_url` 为 `https://api.bot.qq.com/app/getAppAccessToken`；iLink 默认 CDN 为 `https://novac2c.cdn.weixin.qq.com/c2c`。配置 URL 拒绝账号密码、query 和 fragment。平台返回的 CDN 下载地址可携带 query，但不写入错误或状态。OneBot 的配置地址允许明确的本地 HTTP 服务；入站文件的任意下载 URL 则只允许公开地址，并校验 DNS 后固定连接目标，禁止本机、内网、保留地址与重定向，且不向 CDN 转发机器人认证头。

例如只配置 Telegram，先保留关闭状态，填写真实 ID 并在服务环境中提供 token 后再启用：

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

Telegram 的 `getUpdates` 与远端 webhook 互斥。本实现使用长轮询，不自动设置或删除 Telegram webhook；已经配置过 webhook 的机器人需要管理员先在平台侧移除。QQ 回调需要外部平台能访问的 HTTPS 地址及相应平台权限，MuLi 不自动开公网端口或建立隧道。微信需要用户自行取得有效 iLink 凭据；本模块不做扫码登录，不读取其他程序的凭据文件。

## 文件与平台协议

接收的文件先进入现有附件仓库。图片、音频和视频只保存为附件，随后由显式看图、转录等工具按需读取；不会把图片盲目注入主提示词。非机主、未绑定群或关闭 `receive_files` 时不下载。附件超限、格式不允许或多次下载失败会在入站文本中标明，仍提交原文字。已落盘的附件有独立进度记录；提交消息失败后重放不会重复导入它们。

- Telegram 使用 `sendMessage` 与 multipart `sendDocument`；显式语音使用 multipart `sendVoice`，不启用 HTML/Markdown 解析。接收先 `getFile`，再从该机器人配置的文件端点获取内容；接收上限同时受官方 20 MiB 和本地配置限制。
- OneBot 发送数组形式的 text segment，不把文字解释为 CQ code。文件使用 NapCat 的 `upload_private_file` / `upload_group_file`，通过 `base64://` 提交，无需共享本机文件路径。显式语音先查询 `can_send_record`，再发送包含 Base64 的 `record` 数组段。接收使用 `get_image`、`get_record`、`get_private_file_url` 或 `get_group_file_url` 获取 URL；不读取 NapCat 返回的本地路径。通用 OneBot 服务若没有 NapCat 文件扩展，文本仍可用，文件操作明确失败。
- QQ 支持 `C2C_MESSAGE_CREATE` 与 `GROUP_AT_MESSAGE_CREATE`；校验 `X-Bot-Appid`、时间戳及 Ed25519 原始请求签名，并实现官方 `op=13` 回调验证。access token 有效期必须为有限正数并提前刷新。文件先上传到目标用户或群的 `/files`，然后以 `msg_type=7` 和原样 `file_info` 发送；本轮直接上传限制为 5 MiB，不实现大文件分块。被动回复的 `msg_id/msg_seq` 在 SQLite 中跨投递分配，同一入站消息最多分配 5 次；预算不足会在发送前拒绝。平台的时间窗口、主动消息配额及资质仍由 QQ 决定。
- iLink 按只读参考项目验证的 2.4.6 协议发送文本、图片、视频和普通文件；媒体使用协议规定的 AES-128-ECB/PKCS#7 与 CDN。音频出站作为文件发送，此版本不提供原生语音消息发送或引用回复。消息使用稳定的 `client_id`；失效 token 的 `-14` 响应暂停重试一小时。没有有效 `context_token` 时明确要求用户先向机器人发消息。

QQ 签名与 iLink 媒体加密按需使用可选 `channels` 依赖中的 `cryptography`；本模块不自动安装依赖。缺少时相关能力明确报告不可用，Telegram 和 OneBot 文本不依赖它。

使用已发布的发行包时，安装渠道加密依赖：

```bash
uv tool install "muli-agent[channels]"
```

同时使用 MCP 时，应完整保留两个扩展：`uv tool install "muli-agent[mcp,channels]"`。这会调整该工具的安装要求；原先使用自定义索引、Python 或 `--with` 依赖时，调整安装时也应保留相应参数。安装后重启 MuLi 服务。发行包名称是 `muli-agent`，命令仍为 `MuLi` / `muli`。

源码开发环境使用 `uv sync --locked --extra channels`，之后用 `uv run --extra channels MuLi serve`；同时需要 MCP 时，这两条命令都增加 `--extra mcp`。单独运行没有 extra 参数的 `uv sync` / `uv run` 可能移除未选择的扩展依赖。安装只提供库，渠道仍需显式配置和启用。

## 持久投递与恢复

`ChannelManager` 使用同一个本地 SQLite 的独立渠道表，保存入站去重、平台游标、出站分段进度及安全错误。长轮询和消息事件触发处理；空响应或错误有退避，不调用 AI 定时轮询。首次启动把 assistant 事件游标定位到当前末尾，不补发启用前的历史回复；以后重启恢复持久游标。每个已处理入站事件和每条明确 `event_id` 的出站投递都有稳定去重 ID。

入站还对规范化后的完整消息信封保存 SHA-256 指纹，覆盖正文、真实发送者/目标、附件描述与协议上下文；只保存摘要，不把 CDN URL 或上下文密钥明文写进指纹列。同一渠道、账号、事件 ID 重投时，先核对指纹再返回原接收结果或复用附件进度；正文、发送者或附件改变会报 409，不能把新正文错配给旧附件。旧版数据库自动增加指纹列，但不凭重投内容猜测历史原文：旧已完成项仅返回原 receipt 并明确 `fingerprint_verified=false`，旧未完成 checkpoint 需人工核对并恢复，自动接续会报 409。状态中的 `inbound.legacy_unverified/legacy_incomplete` 公开这些历史数量。

出站状态如下：

| 状态 | 含义 |
| --- | --- |
| `pending` / `retry` | 等待工作器；尚未确认成功 |
| `sending` | 正在处理当前分段 |
| `sent` | 所有分段均收到平台 API 成功确认；不代表对方已读 |
| `failed` | 明确拒绝、文件失效或安全重试达到上限 |
| `blocked` | 自动回复的原渠道未启用、尚未就绪、绑定失效或被动回复预算不足；已持久记录，尚未发送，不自动重试 |
| `unknown` | 请求可能已被平台接受，无法确认；不自动重发 |

每段成功后立即持久记录下一段位置。后续分段失败再重试时，不重发前面已确认的部分。连接尚未建立或明确限流可有限重试；读取/写入超时、发送后无效确认、服务器错误以及发送途中退出会保守标记 `unknown`。进程重启看到遗留 `sending` 也转为 `unknown`。

`retry(id, allow_unknown=True)` 才能人工重试未知结果，调用方须明确接受可能重复。HTTP 成功后、本地落盘前进程崩溃仍可能留下歧义，系统不承诺跨平台 exactly-once。QQ 的消息序号和 iLink 的稳定 `client_id` 会尽量复用平台去重能力；不把它等同于已验证的绝对不重复。

`blocked` 也使用同一个 outbox 和稳定去重 ID；重新启用渠道、重启或重复消费该 assistant 事件不会把它改回 pending。工作台将其显示为“原路投递受阻”，附原因和既有“重试发送”按钮。显式 `retry(id)` 只有在原账号/owner/group 重新通过验证、渠道已就绪后才继续；QQ 在此时才为尚未发送的 blocked 项预留被动回复序号。阻塞项的附件在真正发送时仍核验存在、大小及内容摘要，记录阻塞不等于附件当前可用。`team_report.delivered` 只证明父会话收件成功，实际外部送达状态以这里的 outbox 为准。

工作台 `/api/channels` 与 `channel_status` 可查看状态和投递记录。`channel_send` 通过统一 `external` 权限门显式发送本地附件或文字，目标仍须符合绑定。工具返回排队 ID 后应检查投递状态，不能把入队说成已送达。

## 显式发送语音

`channel_send` 新增可选 `media_mode="file"|"voice"`，默认 file 保持已有附件发送行为，不按扩展名把所有音频自动切到新语音路径。file 的含义是原有传输路径，不承诺每个平台都显示文档：QQ 原有音频附件仍沿用平台富媒体 `file_type=3`，本片不改变它。

voice 只接受一个本地音频附件 ID，可附文字；文字仍作为先行独立分段，不作为音频 caption。所有本地格式与大小校验在整条投递入队前完成，发送时再次核验文件摘要；若后续远端拒绝或附件失效，已确认的文字分段不会撤回或重发。

| 渠道 | 当前显式 voice 能力 |
| --- | --- |
| Telegram | MP3、M4A、Ogg/Opus；普通 Vorbis Ogg、WAV、WebM 等会明确拒绝。上传名按 MIME 使用 `.mp3` / `.m4a` / `.ogg`，已保存附件的文件名和字节不变。Opus 只核对有限文件头中的标记，不做完整解码验证；平台仍可能拒绝无效音频。受本地 `max_file_bytes`、附件库配额和 Telegram 50 MiB 上限约束。 |
| OneBot | 当前适配器接收附件库识别的 MP3、WAV、Ogg、FLAC、MP4 音频与 WebM 音频。实际发送前须由远端 `can_send_record` 返回 `yes=true`；不保证每个实现都支持所有编码，远端拒绝会明确失败，不在 MuLi 内转码。 |
| QQ 官方、微信 iLink | 本片尚未提供显式 voice，直接拒绝；默认附件发送继续可用。这里说明的是当前 MuLi 适配器能力，不是对上游协议未来能力的限制。 |

先用现有 `speech_synthesize` 获得保存的语音附件 ID，再显式发送；也可用 `file_publish` 将已有文件导入附件库：

```json
{
  "channel": "telegram",
  "target": "从可信路由取得的已绑定目标",
  "attachment_ids": ["speech_synthesize返回的附件ID"],
  "media_mode": "voice",
  "idempotency_key": "同一逻辑发送保持稳定的键"
}
```

`idempotency_key` 可选，限制为 1–200 字符、不能包含控制字符。它复用持久出站去重：同一键、渠道及可信路由返回原投递；正文、附件或模式改变会报冲突。同键重试仍会先做附件预检，附件必须存在且可核验；删除附件后应从 `channel_status` 的持久记录查原投递，不能靠重新调用发送获取收据。省略键表示新的发送，不能用换键将 unknown 伪装成安全重试。mode 保存在附件分段中，重启不丢；旧分段无该字段时继续原 file 语义。OneBot 能力查询未发送消息，查询取消保持 pending；实际发送后的无效回执、超时、取消及重启遗留 sending 保持 unknown，不自动重试。`channel_status` 的 `voice_supported` / `voice_mime_types` 只描述本适配器的协议路径，不是对真实账号或设备的验收。

## 模块接口与测试

```python
manager = ChannelManager(store, bus, config, submit, attachments, client=optional_client)
await manager.start()
await manager.webhook("onebot", raw_body, headers)
delivery = await manager.enqueue("telegram", target, text, attachment_ids)
result = await manager.wait(delivery["id"])
await manager.close()
```

`submit` 为异步回调，接收 `content/source/principal/route/event_id/attachment_ids`。关闭时取消长轮询、事件消费者与出站工作器，完成正在发送任务的状态收口后唤醒等待者；若管理器关闭，`wait` 返回当时的持久状态，尚未发送的项目仍可能是 `pending`。

离线验证命令：`python scripts/check_local.py run -- .venv/bin/python -m pytest -q tests/test_channels.py tests/test_channel_voice.py`。测试使用 `httpx.MockTransport`，覆盖四个平台的实际请求格式、验签/加密、机主隔离、文件收发及大小限制、内网 URL 阻断、持久游标与去重、分段恢复、取消后未知结果、限流有限重试和 QQ 被动回复预算。语音回归覆盖 Telegram multipart、OneBot 私聊/群 record 与能力拒绝、格式/数量预检、默认文件与 QQ 音频兼容、同键模式冲突、持久重开及 unknown 不重发。相关渠道/应用集成/附件服务共 61 项受限测试通过，源码检查记录见[语音设计](channel-voice-design.md#验证)。没有连接真实账号、真实收发消息或安装机器人程序；生产平台的账号资质、权限和端点可达性尚需使用者配置后实际验证。

## 协议依据

- [Telegram Bot API](https://core.telegram.org/bots/api)：`getUpdates`、`sendMessage`、`sendDocument`、`sendVoice`、`getFile`。
- [OneBot 11 语音消息段](https://github.com/botuniverse/onebot-11/blob/master/message/segment.md#语音)、[公开 API](https://github.com/botuniverse/onebot-11/blob/master/api/public.md)：`record` 与 `can_send_record`。
- [OneBot 11 反向 HTTP 通信](https://github.com/botuniverse/onebot-11/blob/master/_autodocs/api-reference/communication-http-post.md)：原始请求体 HMAC-SHA1 签名。
- [NapCat 私聊文件上传](https://napcat.apifox.cn/226658883e0)、[群文件上传](https://napcat.apifox.cn/226658753e0)；[官方 URI 解析源码](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-common/src/file.ts)确认 `base64://` 支持。
- [QQ 调用基础与 access token](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/api-use.html)、[事件回调](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html)、[签名校验](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/sign.html)。
- [QQ 富媒体协议](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/rich-media.html)、[群消息接口与被动回复限制](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html)。
- [weixin-ilink-client](https://github.com/69gg/weixin-ilink-client)：本机参考仓库只读核实的 iLink 2.4.6 协议、媒体与状态码，不依赖或自动安装该客户端。
