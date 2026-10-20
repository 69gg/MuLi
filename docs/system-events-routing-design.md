# 系统事件、日志与汇报路由

历史设计快照：下文记录 2026-10-20 的 2.14 分片起点及当时缺口，不代表最新发行状态。任务来源、默认渠道和明确记忆偏好已由后续版本实现；当前合同以[系统事件](system-events.md)、[主动通知](channels.md#无发令会话时的默认通知)和[错误来源](system-error-origin-design.md)为准。原设计和当时验证证据保留如下。

当时 2.14 候选已实现结构化系统日志与统一来源，以及 [Telegram/OneBot 显式语音发送](channel-voice-design.md)；整合验证见[评审记录](review-v2.14.md)。任务来源绑定、主动默认路由及记忆偏好选路在当时尚未实现。`self_logs` 是独立的按需服务日志读取，不扫描日志来驱动模型。

## 现有接口与缺口

`AttentionInbox._record` 已逐一验证 reminder/update/management 的原始持久证据，再通过 `Runtime.submit_attention` 交给同一个主身份。空闲等待事件总线；资料在当前工作与团队报告交接结束后进入，不通过模型轮询。系统输入没有主人授权，也不会自动提炼为主人偏好。

`Runtime._delivery_routes` 目前读取最近的主人或注意事项触发：主人渠道请求使用其 route，系统资料不使用旧渠道。它没有为一个完整任务绑定独立汇报目的地；另一个渠道后来发来新要求时，不能直接用“最近一条”替代较早任务的来源。因此新增默认渠道必须先建立任务来源合同。

`ChannelManager` 已有入站信任与账号绑定、出站幂等、发送不确定状态及附件引用，可继续复用。发送排队不等于送达；结果未知不能靠切换渠道再次发送来掩盖。

## 统一内部来源

保留现有 EventBus / AttentionInbox，提取有明确来源证明的 source adapter，而非增加第二套聊天或消息账本。内部来源使用 `system:*` 身份，与主人渠道不同；每个 adapter 负责验证原记录、生成有限摘要、稳定去重键和原记录引用。

- 更新、提醒、自身管理首先迁入同一注册入口，原校验与回执不减弱。
- 应用日志增加明确的结构化来源，字段包含时间、severity、logger/component、固定错误代码、有限脱敏说明、可选当前任务/运行引用；原始 traceback 或请求正文不直接进入模型。
- 策略可配置启用类别与日志阈值，默认 `error`，可选 `warning` 等；录入、保留、提醒的阈值分开，关闭提醒不意味着删除日志。
- 复用当前单批、字符、待收件与事件合并预算，另为重复错误规定时间窗和计数；只在实际事件到来时工作。通知设施自身失败不反复生产同类事件。

Python logging handler 可能从其它线程调用，不能直接操作 Runtime 的 SQLite 连接或在 handler 锁内重配 logging。使用有界队列与一次已排队的 loop 唤醒；溢出只累计固定计数，不能排入无界 callback。后台消费者负责持久化和发事件，关闭先卸载 handler 再清理消费者。现有被捕获错误须在明确业务失败边界记录，不能指望仅加一个 handler 就覆盖此前没有写日志的代码。

systemd 的 stdout/stderr 原生 priority 不一定对应 Python 文本中的 ERROR；应用级阈值依据结构化 severity。原服务 journal、macOS/Windows 启动日志继续可用，但不扫描或解释所有宿主日志来冒充应用告警。`self_logs` 读取服务尾部；后续结构化日志查询另提供来源、级别、游标、截断和保留边界。

## 路由优先级

1. 与主人任务相关的汇报优先使用该任务已登记的可信来源会话，包括明确的 WebUI 来源。不能把 WebUI 解释成“无来源”然后发到其它平台。
2. 独立主动事件没有任务来源时，采用用户配置的默认主动渠道；默认值为 WebUI。
3. 若启用记忆偏好选路，只接受可回查主人来源的明确偏好，并解析到已经绑定的具体渠道/账号/目标。模型生成的普通摘要、引用、群里其它人的文字不能直接成为新收件地址。

来源记录应跟随任务说明或工作代保存，包含原 ingress ID、渠道账号代次、kind/target/sender/reply_to 和适用的任务范围。子 agent/工作流报告携带原任务引用；主助手面向多个来源汇总时明确拆分投递，不能默认把一份私人结果广播给所有曾联系过的渠道。

选择后把具体路由与理由、来源修订一起固定到持久投递项；发送前仍由 ChannelManager 校验当前配置、账号与目标授权。配置停用、账号换绑、QQ 被动回复额度/时效或微信上下文缺失均保留明确失败。原路由存在但发送结果未知时，不自动转发到默认渠道；WebUI 可以显示投递状态，用户或 agent 依据确定证据处理。

## 语音复用

现有 `audio_transcribe` 已按附件 ID 调用 ASR；`speech_synthesize` 已生成音频附件并保存。`file_publish` 和 `channel_send` 复用附件登记及持久出站队列。2.14 的 `media_mode=file|voice` 已明确区分发送表现形式，不能仅凭 audio MIME 宣称所有渠道都产生语音气泡。

默认文件模式保留 Telegram sendDocument 和 QQ 按 audio MIME 上传的行为；显式语音支持 Telegram sendVoice 与 OneBot record，不支持的渠道或格式明确拒绝，不自动转码、安装大库或降级。协议替身验收与真实账号收发分别记录。

## 验收顺序

先完成 `self_logs` 小片和现有来源 adapter 等价迁移，再添加结构化日志阈值与有界消费，最后接任务来源/default/preference 路由和原生语音发送。每步单独审查，避免把诊断、权限和发送副作用藏在同一次配置重载中。

关键回归包括：日志线程突发/队列满/关闭/凭据轮转/无递归通知；既有工作不被内部资料打断；同一主身份跨渠道并发输入时，各任务报告仍回原会话；明确 WebUI 来源不被默认渠道覆盖；偏好遗忘或账号换绑后旧路由失效；发送未知不自动重试/跨渠道补发；语音与文件模式的真实协议回执分别验证。全部使用本地资源保护入口和假服务，不发送真实账号消息。

接口核实：通过 Context7 查阅 [Python logging.Handler](https://github.com/python/cpython/blob/main/Doc/library/logging.rst)、[QueueHandler](https://github.com/python/cpython/blob/main/Doc/library/logging.handlers.rst) 及 [systemd journalctl](https://github.com/systemd/systemd/blob/main/src/journal/journalctl.c)。这些来源说明调用约束，不构成本稿已经实现或通过测试的证据。
