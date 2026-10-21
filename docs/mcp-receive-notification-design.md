# MCP 接收故障进入统一系统通知

状态：实现、本地离线回归与独立静态尾审已完成，签名 `39a71fb7a5298f1eab7f57865212f1fa6b02ab1a` 已整合，根定向回归与全仓静态通过，远端验收待完成。代码依据为 `1cf078dbd31c4f71054328de9cf02a46f2805d8f`，加已独立审查的 MCP wire 切片 `ca439096b66293448f5afd7a7b37f39079558770`；不代表真实 MCP 服务、渠道或远端矩阵已验收。

## 目标与范围

当前接收守卫会锁存固定失败码、撤销未派发工具并回收连接；SDK 后台 GET 的异常可能仅被 SDK 记为 debug。主人需要在统一系统事项中知道连接已经不可用，不能必须先主动查询 `self_status`。

首片只把**当前已发布 HTTP/SSE 连接的首次接收失败**接入现有 `SystemEvents → system.log → Attention → FrozenRouting/outbox`。不新增通知引擎、持久队列、轮询、自动重连、自动重试或 MCP 操作权限，不采集响应正文、异常原文、URL、服务器名、header 或凭据。

普通 SDK 工具异常、stdio 错误、初始化失败和目录重载失败不借此变成新的独立通知；原有请求回执、状态和 `extensions_watch` 合同保持各自含义。

## 为什么采用独立连接事实

工具调用的前台失败继续通过原工具结果返回，保留已派发/未派发和未知结果的原有语义。新通知说明的是连接进入不可用状态，不是给原工具另造回复或证明远端没有执行。

接收守卫可能在 SDK 的长期后台 Task 中触发，也可能在 POST 响应流中触发。不能用当前 `ContextVar`、`active_calls > 0`、最近 run 或服务器名反推发令人；SDK Task 在创建时复制上下文，不等于故障所属请求。即使同时存在前台错误回执，也只产生一次连接故障事实。

该事实明确登记为独立宿主来源，使用首次持久化时的默认/显式记忆通知策略；不沿用任意正在运行的请求路由。Attention 只能只读整理，不获得 MCP 调用、配置、重连或自动修复能力。

## 最小接口与发布门

在 `MCPManager` 增加独立宿主回调，与 catalog 的 `_changed` 回调分开：

```python
def set_receive_failed_callback(
    self, callback: Callable[[str, str], None] | None
) -> None: ...

def mcp_receive_failed(self, code: str, occurrence_id: str) -> None: ...
```

第二个方法属于 `SystemEvents`。参数只接受 wire 已有的六个固定码，以及宿主生成的 32 位十六进制 occurrence ID；不接受模型参数、任意 logger extra 或外部来源字符串作为授权。

每个实际 `_Connection` 保有一个 occurrence ID，跨 schema generation 复用同一连接时不更换。新连接使用新 ID。复用现 `ReceiveState.fail()` 的 first-latch，重复失败不重复调用，这是一次实际连接故障只提交一次回调的保证；意外重复入队时，只在原记录仍被保留期间复用同一条 canonical 事件。

`MCPManager._receive_failed()` 先保持原锁存、`usable=False`、状态码和强拥有 retirement，再尽力同步调用宿主回调。回调异常不能解除 latch、阻断清理、替换原失败码或触发 SDK 重放。

发通知前必须同时满足：manager 未关闭、该连接仍是 `_connections[name]` 的同一对象、失败属于已发布连接。正在 prepare 的新候选、已退休旧连接、正常取消/EOF 和关机后的失败不发新独立通知。旧在途调用仍按原 owner 收尾，不借默认渠道报告一条新的当前故障。

`Runtime.configure()` 给每个新 manager 绑定本 Runtime 的薄回调，再委托当前 `SystemEvents`；完整重建后重新绑定。薄回调同步检查 `Runtime._closing`，封住 Runtime 已开始关闭、尚未执行到 `MCPManager.close()` 的等待窗口。使用独立回调，不调用 `request_reload()`，不伪造目录请求，不让通知改变 desired/effective 代次。

## 复用现有采集与持久化

`SystemEvents` 新增精确 `LogProducer="mcp_connection"`，复用现有组件 `extensions`，把六个 wire 固定码纳入有限 `_CODES`。码集合应与接收守卫共用一个不依赖可选 SDK 的定义，避免各自维护任意字符串规则。

宿主方法建立本实例、当前 Task 拥有的临时上下文，清空 run/session/请求 routing，只设置该 producer 和 occurrence。保留外层 `notify=False` 的单向抑制；继承上下文只能抑制，不能授予请求归属或收件人。finally 还原原 token。

通过现私有 adapter 发固定 error 日志；Handler 仍只捕获有限纯值，不格式化 msg/args/exception，不读 SQLite、Settings、记忆或网络。关闭后的 handler、错误实例及不合法码/ID不能获得独立分类。

复用现有限 deque、单消费者、公平批次、队列计数、保留清理与总线唤醒。`mcp_connection` 记录键以组件、固定码、producer、occurrence 区分真实转移，不叠加时间桶或可变化的 notify/routing；原记录仍保留时，同一事实不因重复回调的上下文不同而新建通知，首次通知资格与路由保持不变。原 runtime/watch/普通日志的去重规则不改。

消费者首次持久化时，在原 Store 事务内写入：

- `system_log` 原记录及现有固定字段；
- 同一 canonical `system.log` 事件与精确 event ID；
- producer、occurrence 和首次冻结的 routing。

同一 occurrence 的重复在原记录保留期内只增加 count/last_at，保留第一次路由，不新建事件。`max_records` 清除原记录及事件后不保证永久去重；不新增账本来保存已清理的 occurrence，也不把 producer 回调变成同步数据库写入。

## 路由、关闭与防循环

`_persist()` 仅对通过白名单的 `mcp_connection` 调用现有 `FrozenRouting.independent(settings, store.db)`；与 watch 共用同一窄分支，不能把所有无 run 的日志都升级为独立来源。

独立外发默认关闭时仍在 WebUI；开启后按现有有效显式记忆绑定或配置默认选择。记忆必须经原 `check_preference` 真实证据核验，不接受模型给地址或 source_ref。选择发生在首次持久化，不是在流回调、Attention 消费或模型回答时重选。

后续只复用原授权/撤销守卫：默认 A 改 B 不改投旧项；记忆撤销、独立外发暂停等按既有规则 blocked；unknown 不自动重发。`accepted` 只证明 Attention 已收资料，不代表模型完成或平台送达。

capture_level、notify_level、SystemEvents enabled/notify_enabled、Attention enabled 及队列/模型/待运行预算照旧生效。error 低于用户设定的 critical 时不越过阈值；关闭和重新开启沿用现有消费/跳过历史规则，不承诺撤销已发 HTTP 或补发所有关闭期间记录。

保留三道现有防循环：显式 `notify=False` 不可被内层解除；Attention run 以真实 ingress principal 禁止自身错误再次唤醒；`attention`/`system_events` 组件自身失败只保留固定状态，不能递归记通知日志。连接 first-latch 及 Attention 的只读工具集合共同防止故障触发自重连循环。

## Canonical 校验与有限输出

`_valid_log_routing()` 只增加一个精确分支：组件 extensions、六码之一、run/session 均空、合法 occurrence、independent/legacy_local routing，且对象与事件所有来源字段完全相等。未知 producer、畸形路由或单独伪造事件不能外发。

Attention 正文与 `self_events` 继续复用已有有限白名单，显示固定码、时间、计数和 canonical 记录 ID，不加入 routing、收件人、记忆证据或服务配置。具体哪条连接失败仍按需查 `self_status` 的现有受保护状态；首片不承诺跨重载精确映射某个服务器，也不为此扩一套诊断账本。

`ChannelApplication.status().proactive.independent_sources` 追加 `mcp_connection`，同步当前系统日志/Attention/MCP 文档，避免宣称只有 watch 才是独立日志来源。

## 格式 1 与旧 slot

不改主 Settings、`Attention.events` Literal、数据库 DDL 或配置字节。继续使用已有 `system_log`/`system.log` 字段与 `FrozenRouting` version 1；只增加新包可验证的有限 producer/code。

旧 slot 不认识新 producer/code 时，按其已有 canonical 校验跳过，不能降级为 legacy_local 或据缺少理解重新广播。旧 Attention 可能推进共享 cursor，因此不能承诺之后升级会补发；不回填、不回扫历史、不为通知修改事务/controller 的兼容声明。

这属于新包新增的通知能力，不意味着旧 controller/回退 slot 已具有 wire 防护或该 producer。新包读取原 runtime/watch/legacy 记录的行为保持兼容。

## 最小完整实施面与验收

生产修改限于 `tools/mcp.py` 回调/连接 ID、wire 固定码共用定义、`system_events.py` 新宿主入口与 canonical 分支、`runtime.py` 绑定、`channel_application.py` 来源状态。沿用 Attention/Store/outbox，不改它们的调度或授权逻辑。

新增临时 Store + 真实 SystemEvents/Attention 的门控回归，MCP、模型和渠道用假实现：

1. 实际 HTTPX MockTransport 流让当前连接 first-latch，固定事实仅一次入原记录/事件，后台消费器吞异常后仍唤醒；共享连接换代不换 occurrence，保留期内的重复失败和跨去重时间窗不增加事件，新连接新故障可以再次通知。
2. 候选、retired、closing、正常取消不通知；旧调用继续 drain，未派发旧 token 仍拒绝，零重连/重放；通知 callback 抛错仍保留 latch 和原清理。
3. 前台结果仍返回原会话；故障独立事实不借用该会话/复制 Task 的渠道。显式 notify=false、Attention 自身失败不循环，真实只读过滤不授予 MCP 外部工具。
4. 默认/记忆在首次持久前冻结；默认变更不改旧路由，撤销阻止出站；关闭、阈值、队满、无模型/后台繁忙使用原队列与状态，不另建任务循环。
5. 事件/对象不一致、未知 producer、坏 occurrence 均不能通知；当前 validator 继续接受缺少来源字段的 legacy 记录。旧 slot 的结论来自精确 `1cf078d` 源码只读核对：其 `_CODES` 无这六码，`_valid_log_routing()` 仅识别 legacy/runtime/watch，未知 producer 返回 false；没有执行旧包或用当前 validator 冒充旧二进制验收。
6. 正文/状态通知资料不含 URL、凭据、响应原文、异常对象或路由证据；恶意 `__str__`/异常链不被格式化，完整输出仍遵守已有预算。

只在获共享 guard 后执行小组；远端 MCP extra job 必须显式收集真实 HTTPX/SDK 用例，普通六矩阵的纯 Store/Runtime 用例不因可选 SDK 整文件 skip。不据 Mock 声称真实外部送达。

## 三项实际边界

- 现 SystemEvents 是有界、尽力采集：队满、首次持久失败、崩溃或关机清空未提交队列可能丢通知；latch/status 仍可按需查，不新增自动重试保证。
- 因 SDK 任务不能可靠归属具体请求，独立连接事实与前台回执可能同时可见；禁止用活动调用数消除其中一个，也不把它说成两个远端执行。
- 旧 slot 可能忽略并越过新 producer；首片只保证新包运行期间的合同，不许诺回退期间补发或自动恢复故障连接。

依据：已用 Context7 核对 [asyncio Task 的上下文复制](https://docs.python.org/3.13/library/asyncio-task.html#asyncio.Task)、[ContextVar token 还原](https://docs.python.org/3.13/library/contextvars.html#contextvars.ContextVar.reset)，以及会格式化记录的 [QueueHandler.prepare](https://docs.python.org/3.13/library/logging.handlers.html#logging.handlers.QueueHandler.prepare)。实现继续复用项目已有不格式化 Handler；没有新第三方依赖。

## 本片本地验证

真实 `Runtime.close()` 停在早期 maintenance await 时，旧接线仍生成一条故障通知，单例稳定失败；增加 `_closing` 薄回调后通过，同时断言 latch、不可用状态、retirement 与最终客户端关闭仍成立。首次启动、完整 Runtime 重建、实际 HTTPX 流、Attention 成功及模型失败防循环均使用现有 Store/Runtime/Attention，只有 MCP client、HTTP 传输、模型和渠道是替身。

新增通知文件 **27 项通过**；接收/代次四文件 67 项、系统事件三文件 58 项、路由/Attention/MCP 四文件 124 项通过，合计 **12 文件、276 项唯一回归**。MCP 的一个既有真实 in-process server 用例主动不选；未运行 stdio、真实 CLI、网络或外部服务。7 份 Python 的 Ruff/format、6 份产品源码及新测试的限定严格 mypy 通过。首次测试类型检查只发现通用 Awaitable 不能直接传 `create_task`，改为本例显式 async 包装，相关单例复验通过，不重复计数。

所有检查经根绝对 `scripts/check_local.py --seconds 30` 串行执行，pytest 峰 173.9 MiB、整轮 mypy 峰 344.7 MiB，swap 0。队满、关闭丢队、首次写失败、原记录被保留清理及进程崩溃仍受上述尽力采集边界约束。MCP extras CI 新文件接线由根整合，本片不修改 CI；跨平台和真实渠道送达仍待验证。
