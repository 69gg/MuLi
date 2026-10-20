# 主动通知的默认目的地与明确记忆偏好

状态：A 已实现并完成定向离线验证，B 显式记忆引用仍为未实现设计。基于 `9bf9dab` 的 Team `reply_origin` 合同，操作说明见[渠道文档](channels.md#无发令会话时的默认通知)；本稿不代表真实平台收发已验证。两片可独立验证，A 不等待 B。

目标是一个持续主身份在没有发令会话时仍能按用户设置通知。已经知道任务来自 A，就继续回 A；明确来自 WebUI，就继续留在 WebUI。只有宿主能证明是独立产生的事项，才选择默认目的地。默认仍为 WebUI。模型整理内容，不选择地址、不解释投递偏好、不获取新的发送工具权限。

## 现有链路与真实缺口

目前 `AttentionInbox` 用 source adapter 回查 reminder/update/management/system_log 的持久原记录，再等待前台工作、团队与工作流报告交接结束。`Runtime.submit_attention()` 将资料以 `principal=source=system:attention` 放入既有 ingress；模型只得到只读工具。Team 已把来源沿 agent/report/ingress 保存，`Store.claim_inputs` 在原有工具安全边界按完整目的地分段；`ChannelManager` 已有稳定 outbox、`blocked`、`unknown` 和明确重试。

主动事项还缺三个必要事实：提醒与管理计划没有持久原始 `reply_origin`；没有默认目的地设置；记忆只有自然语言事实、版本、来源与冲突状态，没有已经启用的结构化通知地址。不能把“没有 origin 字段”当作独立事项，也不能把 `trusted_owner=true` 当作其正文中的收件地址已经获准启用。

`DesktopNotifier` 是独立、默认关闭的 Linux 桌面副本，启动只读新事件、队满可丢弃；它不是渠道投递账本。本片不把它改成可靠发送队列。工作台原通知/主会话始终保留，是本地可回查事实；不是发送失败后又创建的一次替代投递。

选择复用 Attention 的模型说明作为唯一自动外发路径。相比另加一个原通知直发消费者，这样不会让同一提醒先发原文、再发模型说明两遍。相应限制也要显示：Attention 关闭、主模型未配置、模型失败或前台仍忙时，不承诺渠道已收到；本地记录仍可立即查看。无模型直接发送原始提醒属于另一片，不能悄悄混入本片。

## A：可配置默认目的地

沿用 `channels` 配置与现有设置/管理入口，不增加第二份通知配置文件：

```json
{
  "channels": {
    "proactive": {
      "external_enabled": false,
      "default_destination": {"kind": "web"}
    }
  }
}
```

外部目的地使用同一个渠道 route 结构，必须明确到具体账号与收件对象：

```json
{
  "kind": "channel",
  "route": {
    "name": "telegram", "account_id": "123456",
    "kind": "private", "target": "主人已绑定的ID", "sender_id": "同一个主人ID"
  }
}
```

WebUI/channel 用 `kind` 判别联合，复用 `Section(extra="forbid")`、有限字符串预算和既有路由校验；不允许 token、URL、`context_token`、固定 `reply_to` 或任意扩展字段。群目的地必须另外明确选择已允许的群和已绑定主人 sender，不能默认选择第一个用户或群。没有完整且唯一的对象就明确拒绝该配置，不从最近会话补齐。账号改变后不能把原 ID 套到新账号上。

路由语法与 owner/account/group 授权需要提取现有校验以共享调用，不能复制一份容易漂移的权限逻辑。配置校验面对的是完整候选 Settings，包括同次修改的账号、主人与群绑定；不能调用仍持有旧配置的 manager 去判断新目的地。发送前则以当前已发布渠道配置重新授权。

设置页在现有渠道页提供“无发令会话时通知到”选择，默认 WebUI，展示具体账号、对象及当前可用性。`external_enabled` 只控制独立事项的外发，默认 false。用户选定外部默认目的地后，一次保存同时写入目的地与 `external_enabled=true`，不增加第二次启用确认或重复配置步骤；自然语言 `self_configure` 同样可用一个 changes 补丁完成。渠道 transport 单独启用不会自动开启主动外发。暂停仍可只写 `external_enabled=false`，保留已选地址。

保存沿现有配置 schema、校验、配置锁和失败回滚路径。WebUI 的设置 PATCH 保持当前忙时 409，agent 管理计划继续等空闲应用；计划已排队与配置已应用分别显示，不宣称立即生效。agent 复用 `self_config`/`self_configure`，不新增任意收件人发送工具；现有工具权限、任务守卫和配置授权照常执行，后台系统资料没有自我修改这项设置的权限。

接线需要同步覆盖以下现有边界，而非只给 Settings 加字段：

- `ChannelSettings` 增加有默认值的类型字段，`Settings.model_json_schema()` 的既有接口直接生成判别联合与默认值，不维护第二份手写 schema。`ConfigFile.public()` 当前只对四个已知渠道 profile 隐藏凭据，新增策略只含非秘密元数据，不能把真实 transport 凭据复制进去。
- `ChannelApplication._manager()` 当前把 `channels.model_dump()` 直接传入仅接受 transport 字段的 `ChannelsConfig`；必须在该边界显式取出策略，不能给 dataclass 传入未知 `proactive` 参数。第一期沿用完整配置更新重建 manager、递增 generation 的生命周期；不为这项策略引入单独热切换，从而保持现有入站 admission_guard 的配置快照与当前 manager 一致。
- `ConfigFile._merge()` 和前端 `settingsPatch()` 当前逐层合并对象，单改 `kind` 会遗留旧 route，单改 route 也可能只产生残缺目的地补丁。`channels.proactive.default_destination` 必须作为完整值替换，B 的 `memory_preference` 也完整替换或置 null；仅在这两个明确配置路径采用此规则，不能改变其它任意对象的补丁语义。WebUI、agent 和直接 API 使用同一合同；channel→web 后不残留 route，暂停开关不意外清除已选目的地。
- 渠道页现有“发送队列/更多参数”会收集全部非 profile 的 channels 字段。增加专用选择器后需从该 JSON 区排除 proactive，避免同一次保存有两个可互相覆盖的编辑来源；高级补丁仍受同一 schema 与完整值替换规则校验。

“暂停主动外发”设置 `external_enabled=false`；新独立事项留 WebUI，尚未开始的独立渠道投递 blocked。它不会停用工作台，也不会改变已绑定 A 的正常 Team 任务报告；关闭某渠道仍用该渠道原有开关。`attention.enabled=false` 则沿原合同暂停全部注意事项处理。只是把 default 从 A 改成 B 属于以后新事项的选路变更，不等于撤销已经冻结给 A 的投递。

## 来源的分类必须由生产者证明

每个可投递事项有以下三种宿主分类；这不是可由模型正文或工具参数提供的枚举。

| 分类 | 足够的证据 | 使用规则 |
| --- | --- | --- |
| `bound` | canonical 任务/提醒/管理记录中由宿主捕获的 `reply_origin`，且 adapter 回查一致 | channel 和明确 web 都是终点；不再套默认或记忆 |
| `independent` | 被明确登记的宿主生产入口持久写入的独立来源标记，adapter 同时验证原记录、事件引用及生产类别 | 才允许读取当前默认/显式记忆策略 |
| `legacy_local` | 旧记录缺来源、来源损坏、来源无法证明，或未纳入合同的入口 | 留在 WebUI，不把 unknown 当作 independent |

具体入口必须写清，不能只检查字段不存在：

- `reminder_schedule` 在有效 PreparedRequest 中从宿主捕获来源；authenticated WebUI 创建提醒固定为 web。提醒修改、重复触发与恢复保留初始绑定，正文和模型参数不能改绑。直接 Python 调用没有请求上下文时默认 `legacy_local`。原提醒没有 origin 的升级记录也留本地；第一期不凭“定时触发时没人说话”将其改为独立。
- `SelfManagement.stage` 保存有效请求的来源。后续 configure/restart/update 结果继承该计划，不能因为执行时主模型空闲就改走全局默认。旧计划和没有已登记独立入口的直接宿主调用留本地。当前 WebUI 只有读取/取消管理计划，没有新增创建入口；以后若新增，须明确绑定 web。
- 更新检查由启动时的自主检查入口明确标记 independent。adapter 继续核验当前 checked/available 的版本记录和对应通知；只伪造一个 `notification.created` 不够。以后的自主检查若增加其它调用点，须逐个登记，不由 `Updates.check` 缺少参数反推。当前没有新增 WebUI 手动检查入口；以后若新增，须明确绑定 web。
- 系统日志继续核验 canonical `system_log` 与精确 event ID。当前仅有 run/session 引用或仅仅缺少引用，都不足以证明 independent。第一期现有日志默认本地；以后仅对明确的服务维护调用点增加独立标记，或在任务执行入口保存其冻结来源。`unknown` component/code、普通 logging 调用和带 run 但无法关联原来源的记录不能选默认渠道。handler 线程不读记忆、不查 SQLite、不决定路由。
- `source=assistant` 的 `notify`、外部/chiliarch 通知和一般 EventBus 文本保持原合同，不自动提升成合格来源。这样也不会把模型自己的通知再次交回 Attention，形成外发循环。

新入口的原始来源、独立标记和选路结果进入 adapter 的 canonical 一致性核验。不能只验证通知正文却允许事件中的路由覆盖原记录。这里沿用本地可信 Python 插件的信任边界，不宣称抵御可直接改库的恶意本机代码。

## 冻结时机、增量合并与原子写入

任务来源在任务/提醒/管理计划建立时冻结。独立事件的默认目的地在它第一次持久产生时冻结，发生在事件循环上、任何后续 await 之前；日志若以后支持独立选路，则在队列消费者首次 `_persist` 时冻结，不在 logging handler 中冻结。之后的 coalesce、前台等待、模型排队、重启、默认设置变更都不能重新选一个地址。重复提醒每次新的触发各自产生新事件，仍继承原提醒分类与绑定；重复消费同一次触发不重新解析配置。

复用原记录中的小型 `routing` 元数据保存：格式版本、分类、选定 destination、原来源 input ID（若有）、`selected_by=origin|default|memory|legacy_local`、规范化策略 fingerprint，以及可选记忆 ID/版本/原来源 digest。字段只存固定元数据，不复制记忆正文、通知正文或凭据。最终入队时，在生成稳定 ingress ID 后封装为现有不可变 `ReplyOrigin`；独立事项的 input ID 指向这次系统 ingress，绝不能伪装成旧 owner 输入。`principal` 始终为 `system:attention`，不因外发目的地成为 owner。

这里有两个现有写入间隙不能隐瞒：`Reminders._fire` 和 `Updates.check` 先提交通知，再 `bus.emit`；Runtime 完整 assistant 消息也先保存，再另行发 completed 事件。只加路由字段不能保证崩溃后还能消费这些消息。A 需要在所触达的通知生产入口复用 `Store.event(objects=...)` 或调用方事务内的 `_event/_put`，一起提交 canonical 记录、routing 和事件，再 `bus.notify()`。不嵌套 `with db` 提前提交。

对本片主动回复，同样把最终 assistant 消息与 completed 事件在原 Store 事务中一次保存，再 notify；`_put(message)` 已维护 message_order。不要用启动全库扫描去弥补这两个间隙，也不要重新生成已存在的模型答案。模型若在产生持久终答前失败或进程中断，沿现有运行终态留下未完成状态，不自动再付费运行。

Attention 先校验每项 canonical routing，再只取连续同一完整目的地、同一授权/撤销依据的一段合并，遇不同 key 就停，游标不越过它；bound 与 independent 即使都发 A 也不混批，避免暂停独立外发时连任务来源的回复一起阻塞。key 包含分类、完整目的地、选路方式及实际使用的记忆证据；同 key 可共用一个有限 decision，另保留各 canonical 引用，不能只留最后一项。原字符/条数/待批次预算与 idle-only 条件不变。读工具中间轮次仅留 WebUI；该注意事项回合没有 tool_calls 的完整终答才附自动外发目的地，避免一批资料变成多段推理说明外发。正常 owner/Team 回复的既有行为不借此重构，token delta 继续走 SSE。

ingress、原 attention_receipt 和 cursor 仍使用现有单事务。receipt 增加对应 routing 与 `accepted` 含义，不把它称作渠道回执。最终 assistant 消息把这些有限证据传到既有 outbox；不新增第二份发送账本。

## 默认改变、关闭与 fallback

默认改变只影响之后新产生的独立事项，不重新定向或仅因 fingerprint 不同而阻塞旧的冻结项。规范化策略 fingerprint 用于说明当时依据，不是要求旧通知永远等于当前默认。安全撤销另有明确条件：独立外发开关关闭、相关记忆绑定被移除/替换/失效、Attention 关闭或渠道授权撤销。

初次选择与选好之后是两个不同阶段：独立外发关闭时直接选择 WebUI；开启时，没有记忆绑定，或绑定的事实在新事件产生时已无效，使用用户明确设置的 default，default 缺省为 WebUI。可是，已经选到了 A，只因 A 未启用、未就绪、账号/owner/group 撤销、协议缺少上下文或平台拒绝，不能转发到另一个默认 B。沿原 outbox 保持 A 的稳定 ID，记录 blocked/failed/unknown 与原因；WebUI 原记录继续可见。“回到 WebUI”只表示不新增外部投递，不是生成第二条替代消息。

为支持安全撤销，policy/memory 选出的尚未开始发送项，在入 outbox 和每个分段真正发送前，都复核独立外发开关、Attention 开关、实际使用的记忆绑定/状态及渠道授权。满足上述撤销条件时改为 `blocked`，不改 destination；只有 default 改变则仍沿原 A 继续。恢复旧设置也不会自动重发 blocked，显式 retry 仍须复核当前证据；需要新目的地时由主人发起一条明确的新发送，不通过改 ID 自动补发。bound 来源不受全局默认/独立外发开关影响，仍受原渠道授权检查。

HTTP 已开始之后的安全撤销不能声称撤回已送达内容。已有成功分段不重发；在途取消/崩溃遵循原 unknown 判定。unknown 不自动重试、不转默认、不把超时解释成未发送；必须使用原有明确确认可能重复的人工重试。新策略、通知已读或相同逻辑 receipt 再次出现，都不能复位 unknown/sent/blocked。

默认 route 不缓存一次入站的 `reply_to` 或微信上下文秘密。发送时仍走已有适配器；QQ 没有 reply_to 时现适配器会请求平台发送，是否具备主动权限要由平台结果确定，不能宣称已验证。微信缺少当前账号/目标的有效 context_token 会明确失败。配置页展示这些前提，不编造永久有效的被动回复窗口、不借最新陌生人输入取得上下文、不为了“好用”静默换对象。

## B：显式记忆引用，不做语义选路

在 A 稳定后，`channels.proactive` 再允许一个可选绑定：

```json
{
  "memory_preference": {
    "memory_id": "已存在的主人偏好事实ID",
    "version": 3,
    "destination": {"kind": "web"}
  }
}
```

非空表示明确启用这条偏好，优先于 default；空值表示只用 default。destination 与默认使用同一结构和授权校验，不能从记忆正文直接提取 target。绑定本身就是经既有配置入口启用的结构化选择，不另建“偏好账本”、更改记忆正文或新增目的地关系语言。

启用时校验一条确切 ID：`layer=preference`、`status=active`、指定版本、trusted_owner，且存在未忘记的原始 source，原 source 的 role=user/principal=owner、digest 一致。逐字 quote 只证明原文出处；明确启用的配置操作才承担选路语义。UI 显示引用、版本和绑定对象，可选“使用此偏好/取消使用”；agent 可以在真实 owner 任务授权内读证据后通过 self_configure 建立绑定。不会仅因后台抽取或检索命中一条偏好就更改默认设置。

现有 `memory_remember` 不允许模型提交 trusted_owner/source_id，手工写入也默认不可信；不能让它伪装成来源已核验的偏好。新原文尚未被 pipeline 提炼出合格事实时，记忆绑定明确返回未就绪，用户仍可立即使用 A 的普通默认配置，不等待后台提炼才能聊天或设通知。

新独立事件只点查所绑定的 fact/source 元数据与状态；不跑全库 owner_profile、embedding、图查询、reranker 或模型。记忆处于 conflict/superseded、版本已改变、原文已遗忘、memory 功能关闭时，该绑定对新事件无效，明确显示原因，再用配置 default。已经冻结的旧项则按前述撤销规则 blocked。现有 memory_meta 的 status/version 和 source digest 足够，不需要另建长期 seen 集合或复制一份记忆事实。修订后即便仍选择同一事实，也需要显式绑定当前版本，不能猜测改写后的含义。

## 迁移、可观测性与最小交付

缺少 channels.proactive 的旧配置加载为独立外发关闭、默认 WebUI。原有 reminder/management/system_log/notification 没有新版 canonical routing 的项全部按 legacy_local 服务，不做首屏/启动全库回填，不重新投递历史。不存在“升级后要手动迁移才能聊天”的前提。保持原 Attention 首次启用跳过历史、停用期间不补发的规则。

设置/状态至少显示：当前默认、记忆绑定是否有效及原因、Attention/模型条件、下一条新独立事项将如何处理。每条 outbox 继续显示原对象、选路依据、阻塞/未知原因与原有重试入口。`attention_receipt=accepted`、assistant 已生成、outbox pending、平台 API 确认 sent 和用户已读分别陈述；不把排队或本地已读当作送达。

A 的交付范围为配置/共享路由校验、已登记生产入口的来源与原子事件、Attention 分组及主动终答/outbox 证据、现有设置与状态展示。第一期唯一启用 independent 的入口是启动时自主更新检查；用户发起的新提醒/管理操作保存来源，现有日志全部留本地。不同时实现 Workflow 自动汇报、所有日志调用点的来源追溯、另一个原文通知消费者或新主动收件人发现。B 只增加一个显式记忆绑定和点查校验，尚未实现；A 验证见下文。

验证必须使用真实 Store 与离线门控：默认 WebUI 零外发；A 任务与明确 WebUI 压过全局 B；旧无 origin 记录仍本地；只有登记的独立入口可选 default；默认改变不重定向旧项；不同目的地不合批；记录/事件、ingress/receipt/cursor 在注入失败时一起回滚；重开重复事件不复位 blocked/unknown/sent；安全撤销发生在等待、入队和分段发送前；模型未配置/失败时保持未完成与可查记录。配置另测一次保存启用、channel→web 无残留、外部 A→B 完整替换、仅暂停保留地址、同次账号/授权变更按候选校验、manager 能构造并按原生命周期重建、schema/公开配置一致且不出现凭据。B 另测伪 trusted 事件、自由文本/引用、冲突/版本/忘记/关闭、手工 memory_remember、配置排队后证据变化，以及没有召回/外部模型调用。

所有未来运行继续通过统一 `scripts/check_local.py`，与其它片串行。真实账号主动权限、QQ 平台时限、微信上下文寿命与跨真实进程发送后崩溃只能在单独授权的集成实验中验证，本稿不作通过声明。

配置方法已通过 Context7 核实：[Pydantic 判别联合](https://docs.pydantic.dev/latest/concepts/unions/#discriminated-unions)、[extra=forbid](https://docs.pydantic.dev/latest/api/config/#pydantic.config.ConfigDict.extra)。事务沿用既有 `Store._event`/`EventBus.notify`；[sqlite3 Connection context manager](https://docs.python.org/3.13/library/sqlite3.html#how-to-use-the-connection-context-manager) 不提供嵌套事务。这些来源说明方法，不是本稿已实现或通过测试的证据。

### A 的离线验证

经统一资源 guard 串行通过 218 项定向 Python 回归（包含 30 项新增）、19 项前端回归，峰值 160.4 MiB、swap 0。覆盖实际 Runtime/Store 的终答与事件同事务、通知/管理注入失败回滚、冻结来源与默认变更、连续目的地分组、工具中间轮不外发且保留 token 流、原路撤销 blocked、未知结果不复位、QQ 分段回执与预算保留、附件读取 await 期间撤销，以及真实设置表单→PATCH 的暂停语义。14 份源码 mypy 与 16 份源码/测试 Ruff 通过，`git diff --check` 通过。独立只读复审已收敛，未运行完整仓库测试、真实账号或平台集成；这些定向证据不代替后续整合验证。
