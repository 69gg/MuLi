# 持续会话与分层上下文压缩

所有已授权的本机 owner 渠道使用 `Runtime.main_session()` 返回的同一个持久 session。渠道 adapter 必须先验证白名单，再把身份归一为 `owner`；任意外部联系人即使使用相同渠道也不能共享主助手权限。`submit(content, mode=None, source='web', attachment_ids=None, principal='owner', route=None, event_id=None, admission_guard=None)` 先写入 SQLite 入站表，再返回接受结果。可选同步 `admission_guard: Callable[[], None]` 在取得配置锁后、任何入站落盘前重新核对渠道实例与配置代次；等待锁期间撤销白名单或禁用渠道时抛错拒绝，不留下消息或启动模型。渠道外层不得持同一把锁再调用 `submit`。`source` 应含渠道账户命名空间；同一 session/source/event_id 的重投去重，内容或身份不同则拒绝。非 owner 输入进入 quarantine，不进入模型历史、不调用工具。

收到输入不等于已处理：响应中的 `queued` 和 `message.delivery_status` 描述入站状态。`Conversation.pending(session_id, after=0, limit=1000)` 供界面刷新时显示持久队列，不把排队消息插到半个工具批次中；其 `seq` 是 ingress 序列，不能与历史 message seq 混比。附件仅保存 `attachment_ids`；空正文加附件使用“请查看附件”，不会自动读取附件全文。

运行中的新消息在当前完整模型响应结束、或正在执行的工具返回后进入下一次模型请求。当前请求对象不被修改，流式输出不中断；尚未开始的旧工具调用写入 `not_executed` 结果，再一次性落盘补充输入。审批或独立审查期间出现新消息，同样在检查返回后先重新规划。显式 stop 取消活动任务、未完成后代和待处理输入；服务关闭保留尚未消费的输入，启动恢复由事件队列触发，不依赖 AI 轮询。中断而没有落盘结果的工具写明 `unknown`，后续须先检查实际状态，不能自动重做副作用。

消息按 SQLite 单调 seq 排序，系统时间回退不改变工具调用/结果顺序。主身份、队列与原始消息均落盘；旧归档初次迁移保留原先顺序，此后使用新序列。

渠道回复也在每次模型请求开始时取快照：只有稳定主会话会从最新已消费的 owner 输入取得 `source='channel:<name>:<account>'` 的单一路由，写入 assistant 消息及 `message.completed` 的 `delivery_routes`。流式期间新渠道消息不改变原回复去向，下一安全边界才更新；最新输入来自 web、系统注意事项或 route 为空时停止出站。队友和其它会话不自动发渠道，禁止遍历历史路由广播。实际发送前渠道层还要重新校验当前 owner/群组白名单，路由不得携带 token。

`system:attention` 是本机提醒与维护事件的专用主体，不是 owner。消费器先等待活动主任务、队友、工作流及其可交接报告完成，主队列中的 owner/agent 输入也优先消费，再通过内部原子入站接口交付独立的系统资料回合。主助手先回复而队友尚未结束时仍等待，保留报告回主会话后的协调能力；明确暂停身份及其暂缓报告不算活动，不会永久拦住注意事项，系统资料也不能解除暂停。提醒不会触发原工具批次的 `not_executed` 或中断 owner 已授权工作。配置锁内再次核对同一活动条件，必要时继续等待真实任务或事件。关闭消费者只取消等待，不取消被观察的执行任务。系统资料保留 principal/source 标记与空路由，不调用 owner hooks；以它为最新触发输入的模型轮次只开放只读工具，真实 owner 下一次输入才恢复正常协调工具。管理页面可查看策略和消费者状态，详见 [系统注意事项](attention.md)。

## 后台语义压缩

`Compactor` 对主助手、独立会话、动态队友及工作流节点使用同一机制。默认 `compaction.enabled=true, mode='auto', trigger_ratio=0.75, target_ratio=0.4, max_summary_tokens=1600`。达到字符阈值才启动一次后台摘要，使用 `models.compactor`；未配置该角色时回退主模型。每个会话最多一个候选，全局未完成候选受未完成任务预算约束。后台摘要使用独立的单请求名额，不占前台 `max_parallel_agents`：即使该值为 1，主对话和队友也无需等待其它会话的摘要。模型请求总并发上限因此是前台名额加一个后台摘要名额；短会话与空闲没有周期性模型请求。`mode='extractive'` 只做本地摘录，`enabled=false` 关闭该层。

摘要要求保留用户目标与约束、完成证据、待办承诺与下一步、未解问题/执行结果未知事项，以及文件、队友 ID、原始 seq。输入包括上一层摘要及新完成的历史前缀。摘要存为 `compaction_snapshot`，带 `parent_snapshot_id`、`level`、`covered_seq`、前缀 digest 和 generation，形成可追溯层次；原始消息和原生协议数据始终保留。语义摘要是模型生成的资料，可能不完整，不能充当新授权或证明操作成功。需要原证据时用 `conversation_read(session_id, after, limit)` 分页核查；该工具不展开附件或内联图片。

assistant 工具调用批次与其全部结果是不可分割单位，未闭合批次不会进入摘要。安装时再次验证配置指纹、基础 generation、原始前缀数量与 SHA-256；前缀被更正、另一份摘要先安装或配置发生变化时丢弃旧候选。新追加的后缀不会被覆盖。摘要只在安全边界安装，已结束会话可在后台完成后安装；若摘要在最后一次流式回复期间准备好，运行结束转为空闲时会再次安装并释放候选预算，无需用户再发送消息。当前仍必要的最近用户输入保留为锚点。摘要作为明确标记的历史资料固定在上下文中，不能被普通旧轮次清理静默丢弃。

请求失败或超时，当前前缀回退为包含 seq 的明确摘录，不后台无限重试。正常请求不等待摘要；只有完整原生回放已无法放入预算时，前台最多等待已经触发的压缩一次，再次核查仍超限则如实报错。关闭或重配置会取消后台压缩。单次原始投影和快照最多读取 1000 条，摘要前缀另受配置预算约束。回放尾窗若切入旧用户轮次，会整体跳过该轮，直到下一个完整用户轮次，并明确提示省略的 seq 区间；不会以孤立工具结果开始回放。当前轮次自身超过条数上限时明确失败，不静默遗漏用户请求或发送残缺原生工具批次。归档与分页读取接口仍保留全部原文。

## 最终请求打包

`pack_context(messages: Sequence[Message], max_chars: int) -> list[Message]` 使用确定性规则整理发给模型的消息，不调用额外模型、不执行工具、不修改会话持久记录。返回值是深拷贝，嵌套的 `provider_data` 也独立。

文本字符预算包含消息结构、工具参数、思考、原生协议数据和 JSON 转义开销。没有内联图片时精确等于 `len(json.dumps([asdict(message) for message in messages], ensure_ascii=False))`；`Message.images` 的 base64 数据在计量副本中替换为含长度的图片标记，保留 MIME/detail 等元数据，原图不变。图片字节由媒体入口的独立限制约束，不能把图片 base64 当成文字 token。此限制不是厂商 tokenizer 或实际 HTTP 字节数。预算必须是正整数；最终返回前再次核对文本计量长度。

打包首先验证 assistant 工具调用和工具结果的配对。每个调用必须有且仅有一个对应结果；同一批调用的 id 不得重复，结果必须紧随该批调用，不能跨越后续用户或 assistant 消息。缺失、重复、孤立结果会报错。中断且执行结果未知的调用，应由运行时先补明确的“结果未知、先核查状态”结果，再交给打包器；打包器不会推断执行成功或重做操作。

运行时在持久化新的 assistant 工具批次和执行任何调用前，核对整批调用数量是否超过本次运行剩余的 `max_tool_steps`。超出时整批明确失败，不执行其中一部分，不截断原生思考或工具调用数据，也不为每个超额调用追加错误行挤掉当前用户输入。此前已完成的批次及结果仍完整保存。

预算足够时，消息内容和顺序保持不变。超限时，从较早内容起压缩工具结果文本，以及最新用户输入之前、不含工具调用和原生数据的普通 assistant 文本。摘录保留头尾并插入 `[结果已节选；原始记录保存在会话]`；按包含 JSON 转义后的真实长度寻找可容纳的最长摘录。当前用户输入之后的工具结果也可以节选，以便长任务继续进行。

仅修改副本的 `tool.content` 和旧普通 `assistant.content`。系统与 developer 提示、最新用户输入、`reasoning`、`tool_calls` 及其参数、`provider_data` 始终不被截断或改写。带思考签名的 Anthropic blocks、Responses encrypted reasoning、Chat 原始消息等原生回放保持完整。

如果文本摘录仍不足，按最旧顺序整体移出已经结束的用户轮次及其 assistant/tool 消息，保留系统与 developer 消息。最新用户轮次始终保留；轮次中的调用及其结果一起移出，避免形成孤立工具结果。原始历史仍在会话存储中，可供界面查阅。

最新用户输入、系统提示、当前轮的工具参数或原生数据仍然超限时，抛出 `ContextLimitError`（`ValueError` 的子类），明确提示拆分任务或提高上限；错误不包含消息正文。此时不会发送不完整的原生回放，也不自动执行任何工具。

运行时在修复中断历史、采用可用摘要后，将投影交给打包器；每次模型请求前对新增工具结果重新打包。打包结果仅用于请求，存储继续保存完整模型回放和原始工具结果，不能用摘录覆盖持久历史。

## 扩展接口

`context_hooks` 接收异步 `(session_id, messages) -> messages`，每个安全边界使用新历史投影调用，之后再统一检查预算和工具配对。`memory_context_provider(query) -> str` 是可选同步缓存入口，替代默认本地检索，调用方不应在这里联网；运行时统一标注资料性质并限制长度。

`message_hooks` 是同步 `(message, trusted_owner) -> None` 列表，只在 `submit` 新信封持久成功后触发，重投不重复。适合短事务入队；不能在钩子中执行模型或慢操作。信任标记仅来自已认证的 `principal == 'owner'`，队友消息不是新的用户授权。钩子失败产生 `message.hook_failed`，不撤销已接收输入；后台消费者应按输入 ID 去重并用持久 ingress 补偿崩溃窗口。

`add_tool_provider(factory, replace=False, coordinator=True, session_scoped=False)` 注册本机受信任工具，配置重载保留 factory。默认名称冲突拒绝；`replace=True` 显式覆盖，`coordinator=False` 不把写工具加入主助手可直接操作范围。扩展仍经过统一权限检查；带 `requires_approval=True` 的工具始终走 ask，即使当前为 yolo。用户自己的 Python 插件是 full-trust 本地代码，不是沙箱。

## 完整任务说明

绑定队友的 task brief 以独立 system 资料参与最终预算，完整目标、限制和证据引用不经过摘要或头尾摘录；明确标注为协调资料，不能成为新的主人授权。必要说明与最新输入/原生回放放不下时清楚失败。每次上下文构建读取一个当前修订快照，等待模型并发额度期间不自动刷新，也不取消已有工具调用。主会话多任务时只保留账本索引，通过只读分页工具按需取正文，详见 [task-briefs.md](task-briefs.md)。

启用任务控制后，该快照同时冻结完整 brief、epoch 和已确认方案；异步 hooks 或容量等待不能给旧正文贴新 epoch。主助手通过只读工具选择的重规划快照与 worker 的执行 scope 分开，读取只选择下一轮，确认必须使用模型该轮实际见到的快照。暂停在工具最后一次 await 后阻挡尚未进入的副作用，先完整配齐原生批次，再持久停靠为 paused；已进入的 handler 不声称撤销。详见 [任务控制的运行边界](task-control-runtime.md)。

显式开启 session_scoped 的本地工具 factory 会按实际执行 session 生成独立 schema，不改变共享注册表或覆盖后注册的插件替换。运行时在 handler 调用期间用 ContextVar 记录宿主 caller，并在正常、异常与取消路径恢复；派生任务继承的上下文不能替代其真实 session。正文写权限仍须在 handler 内检查，不能仅依赖 schema 隐藏字段。本机 Python 扩展保持 full-trust，不是沙箱。

## Codex 参考与差异

实现前只读检查了 `/data1/codex/codex-rs/core/src/session/turn.rs` 的 pending input 安全注入、`session/async_compaction.rs` 的不可变快照与 generation/config 检查、`compact_remote_v2_installation.rs` 的候选安装和前台压力兜底。MuLi 使用 SQLite seq/digest、普通可配模型和中文摘要，不依赖 Codex 专有压缩端点、Rust 运行时或授权系统；没有复制大段源码。团队生命周期的对应关系见 [workflows.md](workflows.md)。

`tests/test_context.py`、`tests/test_compaction.py`、`tests/test_conversation.py`、`tests/test_store_replay.py`、`tests/test_runtime.py` 离线验证工具配对、原生回放、图片预算、深拷贝、压缩前缀验证/新后缀保留、多层来源、失败降级、关闭取消、持续身份、排队恢复及流式/工具边界注入。专用回归还覆盖单前台名额下主对话和队友不被摘要阻塞、最终回复后收养候选、超额工具批次无副作用，以及历史尾窗保留完整当前轮次。没有调用真实模型。
