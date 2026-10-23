# 连接内的只读格式证据

状态：连接读取器已用于[本版本新建库声明与已标记库启动准入](fresh-format-admission.md)，并接入 2.22 开发分支的 V2 prepare 与 boot；当前整合范围见[本地托管切换](managed-operations.md)。本文原始读取器测试的格式声明仍是合成 fixture，不代表历史数据库或双方发布版本已经兼容，也不替代未发布候选的真实服务验收；不为旧库补发格式标记。

## 调用与所有权

`read_format_evidence(db, *, limits=FormatReadLimits())` 只接收宿主提供的 `sqlite3.Connection`，返回不可变 `FormatEvidence`。读取器不发现路径、不开文件、不创建数据库，不构造 Store、Memory 或 Runtime，也不关闭调用者的连接。

`read_optional_format_evidence` 复用相同快照/预算；仅在 `runtime_state` 表或保留格式命名族真正缺失时返回 `None`。目录按 SQLite 的 NOCASE 名称规则定位，非 canonical 大小写也固定拒绝；缺表与不可读/错误类型的对象明确区分。TEXT/BLOB 的 `format_state_` 前缀各用一个有界主键范围查询；未知 TEXT 名、BLOB-only、TEXT 与 BLOB 冲突都固定拒绝。最多三次分类查询加五次完整读取，仍在默认八次查询内；没有全库文本扫描或根据旧局部 marker 猜测版本。

宿主必须交付专用、独占、没有活跃事务的连接，负责真实只读打开、路径/数据库/lease 身份和连接生命周期。连接使用默认 `str` 文本解码。游标独立使用普通 tuple 行，不改变连接的 row factory。宿主明确转交本次读取的 progress callback 与连接限制设置权；此专用连接此前不能装有其它 progress handler 或 authorizer，也不能与其它线程/任务同时使用。Python 没有读取旧 progress callback 的接口，代码无法自动检测或恢复未知回调，不能将任意共享连接传进来。

入口还只读检查 `PRAGMA read_uncommitted=0`；非零一律以 `format_isolation_unsupported` 拒绝，不修改该设置。独占一个连接句柄并不能排除另一个 shared-cache 连接；启用 read-uncommitted 时，后者未提交的数据也可能可见，显式 `BEGIN` 无法补上这个隔离缺口。不检测或改变进程全局共享缓存配置。[SQLite 隔离说明](https://sqlite.org/isolation.html)、[共享缓存读锁](https://sqlite.org/sharedcache.html#read_uncommitted_isolation_mode)

读取器临时启用 `query_only`、缩短 busy timeout、降低 SQLite 的 SQL/值长度限制，并安装自己的 progress handler。结束时清空自己的 handler，恢复读取得到的原限制和 PRAGMA；未安装或替换 authorizer。`query_only` 只是附加防写措施，不能证明连接以 `mode=ro` 打开，更不认证路径或来源。清理失败返回固定错误，宿主应丢弃连接；连接始终由宿主关闭。

先显式 `BEGIN`，在第一次读取时形成快照；所有 schema、格式和身份查询都在该事务内完成。`finally` 用显式 `ROLLBACK` 结束自己的读事务；不提交、不回滚调用前已有事务。`with db` 既不替 SELECT 开启读事务，也不关闭连接，不能替代此协议。SQL `ROLLBACK` 也适用于 `autocommit=True`，此时 `Connection.rollback()` 本身没有效果。[Python sqlite3](https://docs.python.org/3/library/sqlite3.html#transaction-control)、[SQLite 快照](https://sqlite.org/isolation.html)

## 固定证据来源

- 完整格式标记只能来自 `main.runtime_state` 中 `name='format_state_v1'` 的单条 TEXT 值，按有界 UTF-8 bytes 交给现有 `FormatState.from_json`。缺失、重复、非文本、坏 JSON、未知 metadata_version/字段或超限均拒绝。合法但尚未认证的格式 ID 仍可作为观察值返回；双方是否支持交给 `assess_compatibility`，读取器不验证其语义。
- `message_sequence_v1`、记忆别名迁移标记、建表 SQL、文件摘要均不能替代完整标记。七领域版本是各自的合同 ID；SQL 结构观察不能推导版本。
- `store_instance_v1`、`main_session` 均须为 1–96 UTF-8 bytes 的 TEXT。必须存在对应 `objects(kind='session', id=main_session)`，且其有界 JSON 的 `id` 相同、`kind='main'`、`principal='owner'`。不生成身份、不创建主会话；重复 JSON key 同样拒绝。
- schema 只观察 `runtime_state`、`objects` 及其相关索引/触发器的有限定义；两者必须是表。返回这些事实供后续独立审查，不把它们当作完整 schema 认证或兼容授权。

读取成功仅说明该连接同一快照中的有限事实可解析。完整标记的发布认证与来源真实性、实际配置和 install profile 是否对应其声明，仍由宿主负责。结果没有 `compatible` 或启动许可；不能跨 boot/generation 当作通行证。宿主将证据绑定所选 DB/config/安装身份，候选自己的新连接仍须在任何初始化或恢复写入前重核。

`SchemaFact.sql` 可能含有 DDL 中的字面值；完整证据是宿主私有资料，不能直接作为 `self_status`、事件或模型输出，也不声称已脱敏。对外只使用固定错误码；未来若需要公开结构摘要，应另行定义白名单投影。

## 预算和失败

预算覆盖查询次数、schema 行数、标记/对象/schema 定义 bytes、SQLite 单值/SQL 长度、VM progress 次数、busy timeout 与总单调时钟期限。默认最多 8 次证据查询、32 条 schema 事实、单条 SQL 定义 16 KiB、全部 schema 字段 64 KiB；累计 bytes 包含 kind/name/table/SQL 的 UTF-8 内容。每条与总量分别受限，内部一次取出的候选行仍受行数及单行 SQLite 长度上界约束。固定查询使用 `main` 来源和参数，不执行数据库提供的 SQL。先由 SQL 检查类型、长度并只返回有限内容，再解析 JSON；返回行数也有上界。

VM progress 按至多 100 条指令回调，`vm_steps` 限定累计回调覆盖的指令；每条语句不足一个回调间隔的尾部另有小于 100 条指令的余量，语句数量本身固定且受查询预算限制。快语句可能不触发回调，查询前后仍核期限。底层阻塞 I/O 不受 Python progress handler 的硬期限保证，宿主仍需已有进程级资源/期限保护。SQLite 的只读连接可能涉及 WAL/SHM 辅助文件；本文仅保证读取器不迁移业务事实，不声称文件系统完全零变动，也不使用 `immutable=1` 跳过 WAL。

错误只暴露 `FormatEvidenceError.code` 固定代码，不包含数据库正文、SQL 参数或底层异常。格式准入失败没有业务写入；这不承诺未来格式通过后的恢复/readiness 失败也能恢复原事实。

## 验证边界

### 失败阶段观测合同

读取失败时，仅在最终 `FormatEvidenceError` 上附一条 `format_read_observation=` note；成功不添加观察结果。note 只含固定阶段 `setup/begin/optional/schema/marker/identity/main_object/cleanup` 的累计整数毫秒、总毫秒、失败阶段和本次查询/VM progress 计数，不含 SQL、路径、身份、正文、参数或底层异常。毫秒向下取整并饱和到有符号 32 位非负整数，单条 note 最多 1024 UTF-8 bytes。阶段累计包含清理，分别取整可能使各阶段之和略小于总量；这是同进程墙钟观察，不能据此断言 I/O 或调度根因。

共享原入口单调期限，不续期，不增加查询或重试；默认 1 秒、8 查询及所有 VM/bytes/busy 限制不变。只在固定阶段边界采样，progress callback 不增加采样或格式化。先记录失败阶段再清理；清理失败仍优先返回 `format_cleanup_failed` 并记失败阶段为 cleanup，不能将准入失败改为成功。底层阻塞 I/O 仍不承诺 Python 硬期限。

`Store.open_existing` 的固定错误映射只转发此读取器本次生成的 note，不复制任意 `__notes__`，不改变异常 code/str、公开响应或持久数据。此观测用于下一次失败同次定位，不代表已修复期限失败的根因。真实 Windows 期限边界未验证。

本片经统一守卫每项 30 秒串行验证：`test_format_evidence.py` 与 `test_store_existing.py` 合计 124 项通过（含新增 17 项，0.64 秒；守卫运行 1.189 秒、峰值 95.6 MiB、swap 0）；四 Python 文件 Ruff/format 与两源码严格 mypy 通过，整轮最高峰值 188.3 MiB。首次测试 122 通过、2 失败，原因是 pytest 的 `match` 同时匹配新增 note；改为直接精确断言异常 code/str 后全部通过。首次格式检查发现两处排版，格式化后复核通过；未扩大读取预算，未运行真实服务或访问用户数据。

回归只使用小型临时 SQLite 数据库，覆盖真实 WAL 最新提交、读取期间另一 writer 提交仍不拼接快照、缺/坏/超大/重复标记、旧 marker 不替代、身份与主对象、schema 观察、锁竞争、预算中止、事务/回调/限制清理及无业务写入。共享缓存反证仅通过两个临时 URI 的 `cache=shared` 启用，旧实现确实返回另一连接未提交的格式/实例，修复后拒绝且保留调用者 read-uncommitted 设置；未修改全局 SQLite 选项。

已通过统一 `scripts/check_local.py` 的 30 秒受限检查：原始新回归与既有 compatibility 合计 200 项通过（0.22 秒、峰值 58.7 MiB、swap 0），两文件 Ruff/format 与单源码严格 mypy 通过；静态检查最高峰值 194.3 MiB。后续 optional 分类与真实启动的定向数据见[新片验证](fresh-format-admission.md#验证记录)。未读取用户数据库，未验证真实 A/B 更新，读取器本身仍不认证格式来源。
