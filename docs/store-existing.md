# 已有数据库的禁止迁移构造

状态：内部禁止迁移入口已实现，并由[新建库准入](fresh-format-admission.md)复用于已标记库的真实 Runtime 启动与重配置；V2 boot 也使用该入口，2.22 开发分支的整合范围及未验证边界见[本地托管切换](managed-operations.md)。它未暴露公开配置开关。`open_existing` 本身不建立完整格式标记、不认证历史库。无标记 legacy 仍使用原有建表/迁移流程；已有有效完整标记的库从所有 Store 入口进入禁止迁移模式。

## 入口与策略

`Store.open_existing(path, *, durable=False, limits=ExistingStoreLimits())` 以编码的 SQLite URI `mode=rw&cache=private` 打开已有库。缺文件或父目录直接失败，不调用 mkdir、O_CREAT 或 chmod；不将非 WAL 库切换成 WAL。`durable=True` 仅在只读核验通过后设置本连接 `synchronous=FULL`，同时启用连接级 foreign keys 和 3 秒 busy timeout。[SQLite URI 打开模式](https://sqlite.org/uri.html)、[PRAGMA 设置](https://sqlite.org/pragma.html)

成功的 Store 持有只读属性 `migration_allowed=False`。Memory 跳过整个 schema script、meta/别名迁移、词索引和关系边回填；ChannelManager 在创建任何自有 HTTP client 前复核渠道结构，并跳过 CREATE/ALTER。ChannelApplication 重新配置时继续把同一 Store 交给新 manager，所以自动继承策略，无需另一个布尔参数。

Store 在返回之前一次性核齐 core、memory、channel 三组，避免 Runtime 在 Memory 后执行恢复写入，直到渠道构造才发现缺表或 fingerprint。后续 Memory/ChannelManager 构造再复核自身部分；它们失败不修复数据。禁止迁移路径的 `main_session()` 只读已有 marker 和有界 owner 主对象，缺失或归属错误不会新建身份。后续显式业务仍可生成新记录 ID、写记忆、恢复 ingress/outbox 或入队；禁止迁移不是永久 query-only。

本入口由可信宿主使用。跨版本宿主仍须独占 writer lease、核配置/路径/连接身份，并先完成双方安装与七领域格式准入；一个内部方法或可读的布尔属性不是跨版本许可。本入口不把局部 marker 转成 `format_state_v1`，也不替旧库补发认证。后续 Runtime 工厂只有本次独占新建证明才能发布完整声明，具体构造顺序与持续约束见[新建库准入](fresh-format-admission.md)。

## 保守结构合同

Store 与渠道的原始 DDL 提取为共享常量，普通初始化和核验使用同一份声明；记忆继续复用 `memory_schema.SCHEMA`。会话目录索引和读取排序使用同一表达式生成器。没有第二套列/约束模型、临时参考数据库或 SQL 等价解析器。

`sqlite3.complete_statement` 只为仓库内有界声明判断语句边界；它不是语法验证器。读取器仅识别受控 CREATE TABLE/INDEX/UNIQUE INDEX 前缀、去掉 IF NOT EXISTS 和末尾分号，其余正文逐字节与 `main.sqlite_schema.sql` 比较。数据库提供的 SQL 从不执行，表达式、partial 条件、默认值、主键、唯一性和外键不能只靠同名对象蒙混通过。[Python sqlite3](https://docs.python.org/3/library/sqlite3.html#sqlite3.complete_statement)、[SQLite schema 文本存储规则](https://sqlite.org/schematab.html)

这是刻意严格的当前构造前提：不同空白、等价写法或历史 ALTER 留下的不同 SQL 文本也拒绝。例如旧 `channel_inbound` 经 `ALTER TABLE ... ADD COLUMN fingerprint` 后，其存储文本可能不同于当前完整声明；不会为了接受它静默重建。无标记历史库的普通初始化仍可照原流程使用。额外 schema 不等于已获认证，读取器不证明所有触发器、扩展、JSON/ingress/outbox 语义或数据行完整一致；未来协议仍需完整格式审查。

必须已存在 `message_sequence_v1='done'`、1–96 UTF-8 bytes 的 TEXT `store_instance_v1`/`main_session`，以及对应 JSON `id` 一致、`kind='main'`、`principal='owner'` 的主对象。记忆必须有非负 INTEGER revision 和 INTEGER `alias_evidence_version=1`。缺少派生行不触发回填，也不据此保证检索完整；本片不扫描所有消息、记忆或关系。

## 预算、快照与失败

Store 拥有新建连接。首条 SQLite 语句之前就设置 SQL/单值长度和 progress 限制；初始连接 busy timeout 为零。核验临时开启 query-only、将 busy timeout 缩到剩余读取预算内，显式 BEGIN 后在同一读快照核验结构和局部身份，finally 用 SQL ROLLBACK 结束自己的事务，再恢复 limits/PRAGMA 并清空自己的 callback。不关闭成功返回的连接；打开或核验失败会关闭自己创建的连接。

Memory/ChannelManager 的内部复核要求该 Store 连接没有活跃事务、使用默认 str 解码、传统事务控制和非 None isolation_level，且 `read_uncommitted=0`；业务写入继续依赖 Store 原有事务语义，不接受用户改成自动逐条提交的连接。读取器不修改这些设置，不回滚调用前的事务。Store 独占自己的 progress callback；Python 无法获取和恢复未知旧 callback，因此宿主不能在此连接自行安装并期待保留 progress handler。读取器不替换 authorizer。

默认限额为仓库声明总计 32 KiB、96 个必需对象、每条 schema SQL 4 KiB、主对象 16 KiB、累计 20 万 VM 指令与 1 秒期限；SQLite SQL 长度另限 4 KiB，单值长度为最大对象/schema 预算加小量行开销。schema 返回值先在 SQL 内核长再取有限 bytes，非法记忆 marker 不返回其 TEXT/BLOB 正文。

结构核验将仓库声明中的名称作为参数放入一次 `IN` 查询，最多返回必需对象数加一行，逐行比较类型和 SQL；缺名或重复仍拒绝，不再为每个必需对象重复扫描 `sqlite_schema`。不返回无关对象的 SQL，也不把整个目录装入内存。身份/marker 查询仍至多两行，快照和所有默认预算保持原值。该优化减少扫描开销，不保证繁忙宿主不会耗尽期限。

VM 步数按至多 100 条指令回调累计，每条语句末尾还有小于该间隔的余量；快语句可不触发回调，语句数量和源码大小仍有限，最后再次核期限。底层阻塞 I/O 不受 Python callback 的硬期限保证，执行宿主仍需进程级资源和期限保护。WAL/SHM 辅助文件可能被 SQLite 访问或创建，因此“零迁移/业务写入”不等于“目录完全零变化”；不使用 immutable 忽略实时 WAL。错误使用固定 `ExistingStoreError.code`，不回显数据库正文。

结构校验超出预算时，日志额外附上固定的 `existing_store_read_observation`：校验范围、阶段、首次拒绝的时间或指令门限，以及指令计数和上限。它使用 Python 的[异常附注](https://docs.python.org/3.12/library/exceptions.html#BaseException.add_note)，不改变错误码，不包含路径、SQL、数据库内容或底层错误原文。指令门限先命中时保持原短路，不额外读取时钟，因此不推断两项是否同时超限；计数仍是回调累计值。诊断生成失败不改变拒绝，清理失败仍优先报告。没有自动重试或扩大预算；新日志也不能反推旧失败的具体原因。

## 验证边界

新增小型临时数据库回归覆盖从连接创建时就拒绝所有 DDL/DML、三组缺表/索引/marker、同名但不同表达式/partial/unique/主键、渠道缺列、owner 身份、零回填、失败关闭、预算与事务/设置恢复，以及核验后真实记忆和 outbox 业务可写。另用测试内注入让真实 Runtime 和 ChannelApplication 重新配置复用已有入口，禁止 DDL 而保留合法本地恢复写入；这不是正式 Runtime 接线。

已通过统一资源 guard 的 30 秒受限检查：新增 48 项全部通过（0.45 秒、峰值 82.3 MiB、swap 0）；其中先完成的 45 项与既有 Store/Memory/渠道/会话及格式证据 254 项同轮共 299 项通过（2.12 秒、峰值 199.1 MiB）。测试连接显式关闭及三个连接模式尾例补齐后，仅重验新增文件。四个源码严格 mypy（峰值 121.6 MiB）与五个 Python 文件 Ruff/format 均通过。

所有 fixture 都是临时合成数据库；未读取真实用户库、安装候选、迁移或运行真实服务。后续 Runtime 工厂测试使用本次独占创建的新临时库，完整数据见[新片验证](fresh-format-admission.md#验证记录)。通过上述构造回归不代表跨版本 handoff 已实现或历史库已获语义认证。
