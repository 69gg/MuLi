# 有界系统日志事件

状态：核心模块已接入 Runtime、配置、Attention 与应用状态；早期整版验证记录见[评审](review-v2.14.md)。本轮增加主会话真实请求失败的原路归属与扩展目录自主 watch 的独立失败通知，见[来源设计](system-error-origin-design.md)。自动生产边界是主助手、队友和工作流实际 Runtime 执行失败，以及自主 watch 的失败状态转移；不宣称已经收集所有第三方日志或所有组件的被捕获异常。

配置位于 `system_events`，可通过设置 API 或 `self_configure` 更新：`enabled=true`、`notify_enabled=true`、`capture_level="error"`、`notify_level="error"`、`queue_capacity=128`、`max_records=1000`、`dedup_seconds=60`。级别支持 warning/error/critical。`attention.enabled=false` 是全部系统注意事项的总开关，`attention.events=[]` 只关闭既有 reminder/update/management 三类。

Runtime 在其它组件启动前建立私有 logger，在关闭 Store 前移除采集器，不修改全局 logging 配置。运行失败记录携带固定 runtime/runtime_failed 与真实 run/session 引用，自主 watch 使用固定 extensions/extension_reload_failed；不保存异常正文。首次 await 前已识别的系统输入禁止再次通知，覆盖 ingress 尚未关联 run_id 时的启动失败；消费后的归属仍由精确 run_id 查询验证。配置恢复同时恢复采集策略。API 状态中的 `system_events` 返回运行、队列、丢弃、去重和持久化计数。

## 宿主接口

- `SystemEventsSettings` 是不依赖 `config.py` 的独立 Pydantic 模型。默认 enabled / notify_enabled 为 true，录入与通知阈值均 error，可设 warning 或 critical；待处理队列、保留记录、重复时间窗均有界。
- `SystemEvents(store, bus, settings_callable, independent_routing=optional_callable)` 复用宿主 Store 与 EventBus；`await start(instance_logger)` 返回绑定本采集实例的 logger adapter。同步选择器仅在明确自主 watch 的新记录首次持久化时调用，不在 Handler 线程读配置或 SQLite。旧三参数构造仍保持本地兼容。宿主须使用该 adapter；无归属的原始第三方日志不自动认领，多个 Runtime 共用基础 logger 乃至同一 Store 也不会交叉捕获。Logger 的原等级仍然生效，例如宿主设为 critical 时不能声称捕获了 error。
- `status()` 返回有限计数与固定错误码；`reconfigure()` 重新读取策略；`close()` 先卸载自己的 handler，再关闭接收、统计丢弃队列并停消费者，不删除其它实例的处理器，不调整 logger 级别或全局 logging 配置。
- `AttentionInbox(runtime, system_settings=optional_callable)` 保持旧构造兼容。未传时不消费日志来源；传入后仍受 attention.enabled 总开关约束，旧 attention.events 三项保持原含义。

宿主调用示例为 `logger.error("服务日志中的详情", extra={"muli_component": "runtime", "muli_code": "runtime_failed", "muli_run_id": run_id, "muli_session_id": session_id})`；logger 为返回的 adapter。组件白名单是 runtime / update / management / channels / media / plugins / extensions / attention / system_events / unknown；代码白名单是 runtime_failed / update_failed / management_failed / channel_failed / media_failed / plugin_failed / extension_reload_failed / unknown。不接受任意 logger 名或 extra 作为新枚举。`system_log_context(run_id=..., session_id=..., notify=False)` 可标注当前异步调用链；内层不能解除外层通知抑制。

## 失败来源与投递

只有主会话非 Workflow 的 `_drive` 可以使用当前 `PreparedRequest.reply_origin`。每次准备入口及领取下一批输入之前清空旧来源，构造成功后、首个 hook await 前绑定同一个不可变来源；新 claim/构造前失败保持本地，不能回退到上一请求或最近渠道。请求正常返回后清空，后续子工作流等待失败仍本地。复制上下文的子任务或其它 Runtime 不继承父请求的运行/路由；显式 notify=false 的抑制保留。Team、工作流已有终态报告不新增外发。

扩展自主 fingerprint 失败或目录变更对应的候选发布失败，只在新的失败状态转移通知一次。有限失败签名不因 busy/queued/preparing 重置，成功恢复后可再次通知；每个真实转移有独立 occurrence ID，不被普通去重窗口吞掉。手动重载和 catalog 回调虽由 watch 执行，但没有目录请求代号的证明，不能使用独立默认。启动/配置加载失败及普通 logger extra 也不能声明这一生产者。

新增 producer/occurrence/routing 与 system.log 原事件同事务保存并校验；旧记录缺少这些字段仍留本地，不回填。主请求来源在捕获时固定，独立默认/记忆证据在首次 `_persist` 固定，之后不按当前默认重新选址。路由不进入 Attention 资料正文，通知仍使用只读模型整理、既有回执与 outbox；撤销后 blocked，unknown 不自动重发，accepted 不等于平台送达。

## 证据、队列与恢复

Handler 不格式化 LogRecord，不读取 msg / args / traceback。它只接受原生整数级别、有限 host 组件和固定代码、捕获时间及可选运行引用，进入有界跨线程队列。一个非空队列最多挂起一个 loop 唤醒；溢出只计数，不为每条日志增加 callback。未知组件和代码变为固定 unknown。

重配时只有当前 handler 身份可入队，已卸载但仍在其它线程执行的旧 handler 不会穿过新一代阈值。停止明确丢弃尚在队列中的记录并累计 dropped_on_close；已经同步提交的记录保留，发总线通知被取消也不会回滚证据。

事件循环消费者先在同一 SQLite 事务写 system_log 原记录与 system.log 事件，再唤醒总线。相同时间窗、组件、代码、级别和归属的重复仅增加 count，不为每次重复新建通知。保留清理只处理本模块记录/对应事件，分有限批，绝不改 Attention 全局游标。来源验证要求 event ID 与当前原记录一致；过期记录或旧事件不能借相同标识复活。

队列默认 128（上限 4096），原记录默认 1000（上限 10000），去重窗口默认 60 秒（1–3600）。消费者每批至多 32 条，正常提交后清理至保留上限；清理失败暂停采集并保留固定错误码，已有记录可能暂超出当前上限至多本批 32 条，等待显式重配恢复。降低上限时分批清理并让出事件循环。保留可以丢弃尚未转为注意事项的旧日志；已有 ingress、对话及 attention_receipt 仍遵循原持久历史政策，本片不承诺整个数据库的磁盘上界。SQLite 删除提供逻辑保留，不自动压缩已有数据库文件。

运行归属使用精确 `ingress.run_id` 查询，不以近期消息推断。宿主 Store 维护 `ingress_system_log_run ON ingress(run_id) WHERE run_id IS NOT NULL`；首次给历史库建索引需扫描已有 ingress，之后按 run 查询。该普通索引不改变数据格式，旧版本仍可读取；本模块不自行迁移 schema。无法验证运行引用时不通知，也不宣称无归属第三方错误已经免疫自激活。

Attention 使用固定 adapter 注册表，reminder、update、management 的原校验、logical key 与回执保持等价。日志摘要只含可信固定字段与计数，详情请显式用 self_logs 查询；不把异常正文或配置快照送进模型。

system_events / attention 组件不能再次通知；明确 notify=false 的上下文只保留证据。真实 run 引用对应 ingress principal=system:attention 时也只记录，防止一次通知的模型失败持续自我激活。宿主在相关失败边界应提供真实运行归属或上下文。无法归因的第三方线程错误不承诺全部闭环，模块也不扫描宿主 journal 来猜测原因。

## 验证

### 按需回查

`self_events` 为只读工具，查询已保留的固定错误元数据；`self_logs` 继续按需读取服务日志。可按精确 `level`、`component`、`code`、`run_id`、`session_id` 过滤，默认 20 条，最多 100 条。`before_event_id` 使用规范十进制字符串，按创建事件 ID 降序翻页，避免大整数精度丢失。重复计数更新不会改变位置，计数反映本次读取时的值。

结果提供 `items`、`next_before_event_id`、`has_more`、`truncated`、保留 ID 范围及无效/无法关联证据计数，只返回白名单字段。每条必须与 canonical `system.log` 事件一致；无效页仍推进游标，不回显畸形内容。完整 JSON 信封按工具输出预算截断，不跨过放不下的下一条；连首条都放不下时明确报 `output_budget`。

每条原记录及 canonical 事件的读取上限为 32 KiB，容纳合法的完整 route、来源 input ID 和记忆证据的多字节/JSON 转义形式；该内部证据上限不扩大返回字段或工具输出预算，路由和记忆证据不会出现在查询响应中。

`self_events` 复用宿主 `preserve_output` 合同：最新完整工具批次的分页结果在紧邻的下一次模型请求中保持完整，不会只留下游标与头尾节选。当前上下文确实容纳不下时明确报 `ContextLimitError`，完整已存结果仍可核查；不会发送半条 JSON 或重做查询之外的操作。模型读取后，旧页按既有规则节选或移出，不永久占据上下文；保护标记不进入工具 schema 或厂商请求。

关闭采集或通知不会隐藏已有历史，`notify=false` 也可读取。查询不推进 Attention 游标，不新增通知、回执或运行。`coverage="retained_only"` 表示仅覆盖当前保留集合，缺口不一定是连续前缀，空页不能证明从未发生错误。不会查询全部会话事件，也不会把异常正文当作指令。设计与尚未实施的原生日志留存增强见[查询设计](system-events-query-design.md)。

只使用临时数据库、独立 logger、假模型与有限线程验证：阈值、突发/队列满、单唤醒、重复计数、保留/旧事件、停止/配置更新、实例隔离、持久化失败无递归、通知失败抑制、旧来源等价，以及主/团队工作结束后的原有 Attention 等待合同。所有检查经根目录 scripts/check_local.py 串行进行，不访问真实服务或模型。

`tests/test_system_events.py` 与原 `tests/test_attention.py` 合计 59 项通过，峰值 73.9 MiB、swap 0；两份产品源码严格 mypy 及相关 Ruff/format 通过。宿主接线另覆盖真实执行失败、注意事项自身失败不再唤醒、claim 前失败、跨运行归属、热改策略、启动/关闭顺序、旧运行被较新历史遮挡及精确查询索引。已纳入整版 1,879 项通过、1 项平台跳过的 Linux 检查；后续最终候选及跨平台结果见[评审记录](review-v2.14.md)。

本轮来源切片新增 24 项离线回归通过，系统事件、运行时接线、按需查询、扩展与 Attention 的既有相关 134 项通过；6 份 Python 文件 Ruff/format、4 份源码 mypy 通过，最高检查峰值 138.2 MiB、swap 0。覆盖换来源前后、claim/构造前失败、hook await、子 Task 及显式抑制、非主会话/Workflow、同窗来源去重、写入回滚、记忆证据冻结、长路由查询、watch 持续失败/忙碌排队/恢复与手动重载隔离。未访问真实账号、平台或模型，未以本轮定向结果替代整版和跨平台验证。

依据：[logging.Handler.emit](https://docs.python.org/3.13/library/logging.html#logging.Handler.emit)、[call_soon_threadsafe](https://docs.python.org/3.13/library/asyncio-eventloop.html#asyncio.loop.call_soon_threadsafe)、[contextvars](https://docs.python.org/3.13/library/contextvars.html)。已用 Context7 核对 Handler 锁顺序与跨线程调度；接口核对不等于功能验证。
