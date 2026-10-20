# 有界系统日志事件

状态：独立模块与 Attention 来源验证已实现，尚未发行。配置字段、应用生命周期与真实业务失败边界由宿主另行接线，未接线不能声称日志告警已启用。

## 宿主接口

- `SystemEventsSettings` 是不依赖 `config.py` 的独立 Pydantic 模型。默认 enabled / notify_enabled 为 true，录入与通知阈值均 error，可设 warning 或 critical；待处理队列、保留记录、重复时间窗均有界。
- `SystemEvents(store, bus, settings_callable)` 复用宿主 Store 与 EventBus；`await start(instance_logger)` 返回绑定本采集实例的 logger adapter。宿主须使用该 adapter；无归属的原始第三方日志不自动认领，多个 Runtime 共用基础 logger 乃至同一 Store 也不会交叉捕获。Logger 的原等级仍然生效，例如宿主设为 critical 时不能声称捕获了 error。
- `status()` 返回有限计数与固定错误码；`reconfigure()` 重新读取策略；`close()` 先卸载自己的 handler，再关闭接收、统计丢弃队列并停消费者，不删除其它实例的处理器，不调整 logger 级别或全局 logging 配置。
- `AttentionInbox(runtime, system_settings=optional_callable)` 保持旧构造兼容。未传时不消费日志来源；传入后仍受 attention.enabled 总开关约束，旧 attention.events 三项保持原含义。

宿主调用示例为 `logger.error("服务日志中的详情", extra={"muli_component": "runtime", "muli_code": "runtime_failed", "muli_run_id": run_id, "muli_session_id": session_id})`；logger 为返回的 adapter。组件白名单是 runtime / update / management / channels / media / plugins / extensions / attention / system_events / unknown；代码白名单是 runtime_failed / update_failed / management_failed / channel_failed / media_failed / plugin_failed / extension_reload_failed / unknown。不接受任意 logger 名或 extra 作为新枚举。`system_log_context(run_id=..., session_id=..., notify=False)` 可标注当前异步调用链；内层不能解除外层通知抑制。

## 证据、队列与恢复

Handler 不格式化 LogRecord，不读取 msg / args / traceback。它只接受原生整数级别、有限 host 组件和固定代码、捕获时间及可选运行引用，进入有界跨线程队列。一个非空队列最多挂起一个 loop 唤醒；溢出只计数，不为每条日志增加 callback。未知组件和代码变为固定 unknown。

重配时只有当前 handler 身份可入队，已卸载但仍在其它线程执行的旧 handler 不会穿过新一代阈值。停止明确丢弃尚在队列中的记录并累计 dropped_on_close；已经同步提交的记录保留，发总线通知被取消也不会回滚证据。

事件循环消费者先在同一 SQLite 事务写 system_log 原记录与 system.log 事件，再唤醒总线。相同时间窗、组件、代码、级别和归属的重复仅增加 count，不为每次重复新建通知。保留清理只处理本模块记录/对应事件，分有限批，绝不改 Attention 全局游标。来源验证要求 event ID 与当前原记录一致；过期记录或旧事件不能借相同标识复活。

队列默认 128（上限 4096），原记录默认 1000（上限 10000），去重窗口默认 60 秒（1–3600）。消费者每批至多 32 条，正常提交后清理至保留上限；清理失败暂停采集并保留固定错误码，已有记录可能暂超出当前上限至多本批 32 条，等待显式重配恢复。降低上限时分批清理并让出事件循环。保留可以丢弃尚未转为注意事项的旧日志；已有 ingress、对话及 attention_receipt 仍遵循原持久历史政策，本片不承诺整个数据库的磁盘上界。SQLite 删除提供逻辑保留，不自动压缩已有数据库文件。

运行归属使用精确 `ingress.run_id` 查询，不以近期消息推断。宿主 Store 维护 `ingress_system_log_run ON ingress(run_id) WHERE run_id IS NOT NULL`；首次给历史库建索引需扫描已有 ingress，之后按 run 查询。该普通索引不改变数据格式，旧版本仍可读取；本模块不自行迁移 schema。无法验证运行引用时不通知，也不宣称无归属第三方错误已经免疫自激活。

Attention 使用固定 adapter 注册表，reminder、update、management 的原校验、logical key 与回执保持等价。日志摘要只含可信固定字段与计数，详情请显式用 self_logs 查询；不把异常正文或配置快照送进模型。

system_events / attention 组件不能再次通知；明确 notify=false 的上下文只保留证据。真实 run 引用对应 ingress principal=system:attention 时也只记录，防止一次通知的模型失败持续自我激活。宿主在相关失败边界应提供真实运行归属或上下文。无法归因的第三方线程错误不承诺全部闭环，模块也不扫描宿主 journal 来猜测原因。

## 验证

只使用临时数据库、独立 logger、假模型与有限线程验证：阈值、突发/队列满、单唤醒、重复计数、保留/旧事件、停止/配置更新、实例隔离、持久化失败无递归、通知失败抑制、旧来源等价，以及主/团队工作结束后的原有 Attention 等待合同。所有检查经根目录 scripts/check_local.py 串行进行，不访问真实服务或模型。

`tests/test_system_events.py` 与原 `tests/test_attention.py` 合计 59 项通过，峰值 73.9 MiB、swap 0；两份产品源码严格 mypy 及相关 Ruff/format 通过。`tests/test_system_events_runtime.py` 的七项宿主接线回归已另行准备并通过静态检查，尚未执行；模块回归通过不等于 Runtime/server 接线已完成验证。

依据：[logging.Handler.emit](https://docs.python.org/3.13/library/logging.html#logging.Handler.emit)、[call_soon_threadsafe](https://docs.python.org/3.13/library/asyncio-eventloop.html#asyncio.loop.call_soon_threadsafe)、[contextvars](https://docs.python.org/3.13/library/contextvars.html)。已用 Context7 核对 Handler 锁顺序与跨线程调度；接口核对不等于功能验证。
