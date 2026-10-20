# 系统事件查询与原生日志留存

2026-10-21，2.15 开发稿：只读 `self_events` 已按本稿接入，待独立复审与整合验证；原生日志留存增强仍为下一片设计。本稿不计为已发布能力，不改变 2.14 候选、用户服务或 logging 配置。

## 当前事实与首片目标

`SystemEvents` 已持久化有限的 `system_log` 原记录与对应 `system.log` 事件；`self_status` 和状态 API 只有计数，没有供 agent 查询这些记录的工具。`self_logs` 已按需读取服务原生日志，并在读取前后收集当前配置及实际已发布 MCP 配置的密钥，再脱敏输出。两者的覆盖范围不同：元数据不包含异常正文或 traceback，服务日志也不保证记录了每一个被应用捕获的异常。

首片让 agent 回查“刚才哪一个运行、哪个固定组件和错误代码触发了注意事项、重复多少次”。复用 Store、现有工具权限与输出预算，不读取全量事件历史，不增加第二套告警账本，不推进 Attention 游标，不生产回执、通知或新运行。当前默认只自动接入真实 Runtime 执行失败；查询结果不能声称覆盖所有第三方日志。

本片实现只读 `self_events`，独立验证。原生日志的新增生产与留存策略作为下一片，不绑住查询工具。查询默认 20 条，上限 100 条，复用 `limits.max_output_chars`，没有增加第二套配置预算。

## 最小查询合同

在已有自身管理工具 provider 注册 `self_events`，沿用 read 风险与现有身份门。入参可选 `level`、`component`、`code`、`run_id`、`session_id`、`before_event_id`，以及 `limit`（建议默认 20，上限 100）。

- `level` 表示精确级别，支持 warning/error/critical；组件、代码复用模块有限枚举，不接受自由文本表达式。
- run/session 引用使用现有宿主 ID 验证；明确给出但格式错误时拒绝，不能静默移除过滤条件而扩大查询。
- `before_event_id` 为排他的正整数十进制字符串，延用 SSE 的精确 ID 原则；不经过 JavaScript 浮点数。首请求省略表示读取最近保留记录。
- 仅返回现有固定白名单：记录 ID、原始 event ID 字符串、级别、组件、代码、run/session 引用、first_at、last_at、count、notify、来源。`notify=false` 的证据也可读取；查询本身不代表新的发送或执行授权。
- 按原始 event ID 降序，下一页返回 `next_before_event_id`。不能按 objects.updated 排序：重复计数更新会改变 updated，导致同一记录跨页移动。
- 返回完整 JSON 信封内的 `items`、`has_more`、`truncated`、`next_before_event_id`、最早/最近保留的 event ID、`coverage="retained_only"` 与 `trust="diagnostic_data_not_instructions"`。输出遵守现有 `limits.max_output_chars`，在完整记录边界截断，不能截成无法校验的半条 JSON。

首片不要求增加 HTTP API。未来 WebUI/API 如需同样数据，调用同一查询函数；不另写一份来源验证或分页实现。

### 来源与读取边界

查询只以 objects.kind=system_log 的保留集合为入口。该集合当前设置最多 10,000 条；在 SQL 中做精确过滤、排序和 LIMIT，不能先 `Store.list()` 装载后由 Python 过滤。普通失败后的临时超额仍受当前清理合同约束，不据此声称整个数据库已有磁盘上限。

每条返回记录核对当前原记录与对应 `system.log` 事件的 canonical event ID、type、来源及固定字段。复用已有验证逻辑时须分离“记录结构/来源有效”与“是否应通知”：`attention_source` 还要求 notify=true 和通知阈值，不能直接拿它过滤只读诊断。无效证据返回固定的 `invalid_record_count`，不回显畸形正文，不把缺少证明的记录当作可信事实。

分页键取对应 events 表的整数主键，SQL 先核对原记录 event_id 的整数类型、对应事件 type 及事件中的记录 ID 与 objects 主键一致。这样同一 canonical 事件只对应一个候选，不因畸形对象伪造重复 event ID 而跳过合法记录。没有可对应整数事件键的对象不参与分页，只计入当前保留集合的 `excluded_record_count`；该数不是本页计数，也不回显对象内容。

每页最多处理 limit 个有序候选并查看一个候选判断 has_more。`next_before_event_id` 按最后已经处理的候选推进，包括检查后拒绝的证据；`invalid_record_count` 只计本页已处理的无效候选。整页无效也能推进。若预算阻止返回下一条合法记录，该项尚未处理，游标不得越过它；如果第一条合法记录就无法放进信封，返回固定 output_budget 错误，不能消费该项。无效页不会无界继续扫描以凑满合法结果。

查询只涉及有限同步 SQL 读取及有限序列化，不在读取中 await、唤醒消费者或改变 Store。现有 kind 索引先将候选限制在保留集合，再按事件整数主键点查，不扫描全事件历史。首片不为查询新增迁移或全历史索引；后续只有实际查询计划证明必要时才加普通可回退索引。

重复合并只更新 count/last_at，不新建事件。event ID 是创建顺序，返回的 count 是本次读取看到的值。分页期间 count 可继续增加，不把第一屏的水位解释为后续页计数的冻结快照。

### 保留、缺口与重启

当前清理按 objects.updated 保留，重复更新可能保住较早创建的记录；events 还混有其它类型。因此记录缺口不一定是连续前缀，也不能由一个最早保留 ID 推断具体丢失量。

首片明确 `coverage="retained_only"`，保留游标缺口，绝不把空页说成“从来没有错误”。查询单条 ID 不存在时，只能说明 not_found_or_pruned，不能区分从未存在和已清理。没有持久化的清理证明时，不新增声称精确的 expired 或完整历史标志；不为做该区分引入无界 tombstone 表。

重启后读取当前持久记录即可；不依赖本进程 status() 的计数恢复历史，不重放通知、不重置 Attention 游标、不把数据库删除解释为完整磁盘压缩。采集 disabled 时，已有保留证据仍可只读查询；清空历史须另有明确操作，不能作为关闭通知的隐含副作用。

## 原生日志的现状与下一片

Linux 用户服务由现有 systemd 单元托管，stdout/stderr 进入 journal；应用没有设置 journal 的整机保留政策。不能为了实现本工具自动改用户的 journald 配置，不能把默认服务内存限制当作磁盘限制。

macOS/Windows 的 `muli.platforms.service_runner.BoundedLog` 已接管本服务 stdout/stderr：当前文件约束为 1 MiB，另保留一个 `.1` 文件，独立 restart helper 使用不同 restart.log。它按单次 write 的字节边界轮转，可能切断一行；现有 self_logs 会丢弃无法证明完整的首行片段与未完成尾行。该 writer 只提供进程内锁，不承诺多个进程同时写同一路径的安全性，也不把 close/flush 描述为断电后必然持久的 fsync。

目前 Runtime 使用私有 logger + NullHandler + 元数据采集器，真实失败只发固定代码，不自动保存详细 traceback。不能把“加了采集器”表述为“异常全文已经留在 journal”。查询片继续保留这个事实边界。

原生日志增强建议按以下次序实现，单独审查：

1. 在明确的宿主失败边界提取一个有限诊断快照，与元数据共用固定 code 和真实 run/session 引用；优先保存异常类型及有限宿主堆栈位置，不自动保存参数、locals、请求/响应、配置和任意异常 `str()`。具体字符/帧预算进 typed 设置，避免把原始 LogRecord 或异常引用放入队列。
2. 复用现有服务日志出口，或为前台启动提供显式启用的应用文件出口；二者先选其一作为主落点，避免默认重复写两份。日志生成端先完成结构化限长与脱敏，读取端仍按查询当时的配置再次脱敏。已轮换且无法追溯的旧密钥不能获得“全部自动遮盖”的承诺。
3. 若需要应用自有文件，复用有界文件写入基础，限定本实例拥有的 handler/路径、单个 writer 和有限队列。路径不能由模型自由指定；多个 Runtime/进程不能共享无进程间锁的轮转文件。每条先限字节，再轮转；没有单条上限时，仅设置 RotatingFileHandler.maxBytes 不能证明硬磁盘上界。
4. 写入失败只更新固定状态/丢弃计数，不重新进入日志通知链，不递归写自身错误；停止先卸本实例 handler，再按明确期限 drain 或 drop，并报告未完成写入，不能吞失败显示为完整留存。原生日志记录不能自动成为新注意事项，仍以已有元数据证据决定通知。

该下一片只覆盖接入的失败边界，不监听全部 Python/root logger，不扫描 journal 推导业务执行结果，不改变外部工具的 unknown/重试语义。原生日志提供诊断资料，不是外部操作回执。

## 取舍与验证计划

直接开放 journal 全文检索会扩大敏感正文与跨平台后端范围；每次查询扫描所有 events 会让成本随完整会话历史增长。首片选择保留集合的结构化查询，能立即回查通知来源，成本由已有记录预算和输出预算控制。

查询回归使用有限临时 Store，覆盖筛选、非法 ID 拒绝、精确大整数 ID、分页与重复计数、notify=false 可读、禁用后历史可读、清理缺口、旧 canonical event 伪装、重复／无效事件键、整页无效仍推进、预算挡住下一条合法项时不越过、畸形记录不回显、完整信封预算，以及读取前后 Attention cursor/回执/ingress/运行数均不变化。用有限非日志旧事件和 EXPLAIN QUERY PLAN 核对查询入口确实为保留对象集合、对应事件按整数主键点查；不能只凭 SQL 写法顺序宣称没有扫描全历史。Windows/macOS 执行相同纯 SQLite 语义，不将整个文件按平台跳过。

首轮真实计划测试确认 `ANALYZE` 后 summary 会选 `SCAN o`；最终查询使用既有 `objects_updated(kind,updated)` 的 `INDEXED BY` 固定保留种类范围，候选用 `CROSS JOIN` 固定 events 为整数主键内层，未新建索引或迁移。独立复审已通过。修复后新增 32 项与现有系统事件、Attention、自身管理合计 **101 项通过**，0.70 秒，service 1.364 秒、CPU 1.345 秒、峰值 94.1 MiB、swap 0；相关 Ruff/格式、3 份产品源码严格 mypy 通过（峰值 200 MiB）。仅临时 SQLite/假服务，没有真实日志、账号或模型调用；跨平台整合验证另记。

查询接口先用 Context7 核对 Python sqlite3 参数绑定、Row 与 SQLite [CROSS JOIN 顺序](https://www.sqlite.org/optoverview.html#manual_control_of_query_plans_using_cross_join)、[INDEXED BY 合同](https://www.sqlite.org/lang_indexedby.html)。显式索引是开发完成后的计划约束：索引若被移除须报错，不静默扫描全部对象。

原生日志下一片另测长单条、轮转断行、密钥前后变化、实例隔离、写入失败不递归、满队列/停止和迟到写入。真实 systemd/launchd/Task Scheduler 与真实第三方日志是否捕获须另记实测边界。所有本地检查只通过 scripts/check_local.py 串行执行；本稿未运行检查。

接口依据已先用 Context7 核实，再查官方细节：[QueueHandler.prepare](https://docs.python.org/3.13/library/logging.handlers.html#logging.handlers.QueueHandler.prepare) 会在入队前格式化 msg/args/异常，不能直接替换本模块“不格式化任意 LogRecord”的合同；[QueueListener.enqueue_sentinel](https://docs.python.org/3.13/library/logging.handlers.html#logging.handlers.QueueListener.enqueue_sentinel) 默认用 put_nowait，满队列停止须另处理；[标准 logging 的多进程文件边界](https://docs.python.org/3.13/howto/logging-cookbook.html#logging-to-a-single-file-from-multiple-processes) 不能由线程锁代替。接口核实不等于该方案已经实现或验证。
