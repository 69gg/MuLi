# 系统错误按真实来源投递：C 最小切片

状态：已实现并完成 24 项新增、134 项既有相关离线回归，独立静态复审通过；本文为实现合同。沿用 A 的默认渠道和 B 的显式记忆绑定，不增加模型推断收件人、全局日志采集、轮询或新账本。最终证据见[系统日志验证](system-events.md#验证)，不代表真实平台送达已验证。

本片只增加两个可证明的来源。主会话当前请求执行失败，使用该次 `PreparedRequest.reply_origin`；扩展目录自主 watch 从非同一失败状态进入失败，作为固定 `extensions_watch` 独立生产者。Team、工作流、用户发起的配置/加载操作及其它日志仍保持原有本地行为；已有终态报告不再广播。

## 主会话错误的来源生命周期

`SystemEvents` 在现有有限日志上下文中保存不可变的运行引用、请求来源和拥有它的 asyncio Task。`_drive` 首次 await 前建立全新上下文，不继承父任务的运行、来源或 principal；finally 在清理/唤醒后续任务前还原。仅真实 main 且非 workflow 的驱动允许绑定请求。上下文不是授权：TaskSnapshot、principal、工具风险与 route validator 保持各自边界。

`_prepare_request` 入口先清空旧来源，包括 TaskSnapshot 读取、来源读取和 `PreparedRequest` 构造之前；成功构造后、首个上下文 hook await 前，绑定同一个不可变 `ReplyOrigin`。因此 hook、模型流、工具批次和当前请求的终态写入失败，都能使用它。消费者不查询最新消息或当前渠道来补来源。

切换下一批输入前的两个 `_claim_inputs` await 也先清空；否则该 await 失败时仍可能误用上一请求的来源。新请求构造成功后才绑定新来源。`_execute` 正常返回后同样清空，后续子工作流等待或运行收尾失败保持本地。尚未有 PreparedRequest、准备本身失败、来源无归属、Team/workflow 均为 `legacy_local`。

asyncio Task 会复制 Context；所有绑定、清空和捕获都要求当前 Task 等于上下文拥有者。继承上下文的子任务不能把父请求来源写入自己的日志。新 `_drive` 使用新作用域，不因上个 Attention 运行的继承上下文抑制下一位主人，也不把父 principal 带给新运行。现有显式 `notify=false` 嵌套抑制和精确 `ingress.run_id` 的 Attention 自激活防护保留。

## watch 的独立来源证明

只在 `_watch` 自主 fingerprint 或候选发布失败的调用点发生产者信号；不改共享 `_failure`、`load_initial` 或 `apply_locked` 的默认分类。维护门的 queued/dirty/recheck 逻辑原样保留。

watch 使用一个有限的最近失败签名 `(generation, phase, code, source)`：busy/queued/preparing 不重置同一失败；成功恢复后再次失败，或真实不同失败状态，则各产生一次。启动/手动加载已存在的相同失败也不借首次扫描重发。目录变化调用现有 request_reload 时记录其 `_requested` 代号；只有匹配该代的发布尝试失败才可独立通知。手动重载和 catalog 回调同样会排队到 watch，但它们递增代号，不获得目录自主来源。每次转移分配一个有限宿主 occurrence ID，保证同一秒两次真实转移也不会被普通时间窗去重折叠。状态正文和目录路径不进入日志或模型；只记录固定 extensions/extension_reload_failed。取消与 runtime_busy 排队不算失败转移。

生产者不继承调用者的运行/渠道上下文。Handler 只同步捕获有限标记，不读 SQLite 或配置。首次持久化时由 Runtime 提供的同步选择器调用 `FrozenRouting.independent(settings, db)`，同一 Store 事务保存 system_log 原记录、system.log canonical 事件与冻结 routing（含 B 的证据）。复核/发送沿用既有 outbox：撤销 blocked，unknown 不自动重发。

## 有限元数据与兼容

普通日志仍按现有窗口去重。请求来源的去重包含完整冻结 ReplyOrigin，不能合并不同输入或目的地；watch 用本次唯一 occurrence ID，既不跨失败转移合并，也不因默认设置更改而重定向已有项。重见相同标识只增加计数，保留第一次持久的路由和证据。

新增 producer/occurrence/routing 与已有固定字段一起在 canonical 事件和原记录中核验；来源枚举由宿主 API 写入，logger extra 和模型参数不能自称独立。缺少新字段的旧记录仍可按需查询和本地处理；不回填历史，不扫描全库。畸形或事件/对象不一致的新来源不能外发。

Attention 的资料正文沿用原白名单，只含级别、组件、固定码、时间、计数和真实 run/session 引用，**不含 routing、收件人或记忆证据**。路由只经 AttentionSource → receipt → PreparedRequest → 完整终答 → 既有 outbox 传递。没有新配置项、事件消费者或持久表；队列、批次、保留及 self_events 的有限输出合同不变。

## 独立可验证交付

新增离线回归覆盖：A 请求后收到 B，A 失败仍回 A；新 claim 抛错/构造前异常保持本地；成功准备 B 后失败回 B；hook await 中更换最新输入不重定向；复制 Task 上下文/Team/workflow 不获得父来源；Attention 自身失败不再次唤醒。canonical 事件/对象不一致不能外发，路由不进资料正文，旧记录仍本地。

watch 回归以门控的假 fingerprint/发布器验证：持续同失败只一次、恢复后新失败再次一次、不同真实失败转移不被时间窗合并、普通配置加载失败不自称独立、默认/记忆在首次持久前冻结、撤销 blocked 且 unknown 保留。所有检查只使用临时数据库、假模型/渠道，经过共享 check_local.py 串行执行；不据此声称真实平台已送达。

依据：已用 Context7 核对 [asyncio Task 的上下文复制](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task)和 [ContextVar token 还原](https://docs.python.org/3/library/contextvars.html#contextvars.ContextVar.reset)。保留现有不格式化 LogRecord 的采集器，不采用会格式化 msg/args/exception 的 QueueHandler.prepare。
