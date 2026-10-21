# V2 托管安装的启动准入

本模块提供 `muli.managed_boot.ManagedBootPermit` 和 `ConfigFile.from_bytes`，已连接
Runtime、Maintenance、CLI、systemd probe 和服务生命周期。完整入口的整合状态见
[本地托管切换](managed-operations.md)。准入本身不创建数据库、补格式标记、
迁移数据、恢复备份、发布 selector 或执行服务操作。

## 调用与所有权

宿主先用真实 systemd/proc/cgroup 观察证明当前调用者 PID、InvocationID、固定 unit
与实际 slot argv，再将这个实际 boot 和已获得的同数据库 writer lease 交给：

```python
permit = ManagedBootPermit.acquire(journal, boot=boot, plan=entry_plan, lease=lease)
config = permit.config
store = permit.open_store()
# 宿主在此后才允许构造 Runtime 的写入组件和执行恢复。
```

这段代码是宿主接线顺序，不是现成的服务启动命令。`EntryPlan` 来自固定 launcher，
只是路由建议；本模块自身不能证明传入的 boot 来自 systemd，也不把路径或 DTO
当成安装授权。不能跳过外层真实进程观察直接向此 API 自报 boot。

`acquire` 接管 lease，包括普通准入失败时释放它；journal 及其目录描述符始终由外层
session 持有。成功的 `open_store` 仅把 Store 所有权交给宿主，许可本身不关闭该业务
连接。宿主须等全部 writer 关闭后才调用 `close_after_writers()`，再关闭 journal。
`open_store` 每个许可只允许尝试一次，失败也不重置该次数。

普通元数据/格式失败在自身只读连接关闭后可以释放许可。若只读连接、失败候选 Store
的关闭本身失败，或 Store 工厂抛出不能确认内部关闭的未知异常，许可进入固定的
`managed_boot_cleanup_failed` 状态：保留连接引用、DB 文件描述符和 writer lease，
`close_after_writers` 也不能假称已回收。错误对象保留该 owner，宿主必须保留它并使
进程退出，不能捕获错误后另开 writer。测试只在临时连接上直接清理这些失败夹具。

公开宿主属性为 `journal/config_path/boot/installation/record/role/probation/config`，
另提供已核 `anchor`、`check_paths` 与 `close_after_writers`。它没有 V1 的 `service`
摘要字段；后续健康证明必须明确分派协议，不能伪造 V1 身份。

## 当前记录决定唯一角色

在真实 writer lease 下重新严格读取 V2 状态、按摘要寻址的 anchor 和所选 slot。
没有 V2 状态、协议不明、anchor 缺失或安装不符均拒绝，不降为 legacy。
每次信任状态前，先有界读取原始记录、同步 journal 目录、再复读核对同一内容。
目录同步失败或此窗口记录变化均拒绝，不争控制者的 journal flock；不能将先前
rename 已可见、目录同步却失败的 `released` 当作已持久放行。

未解决代仅允许 `record.boot_role` 指定的启动窗口，且实际 boot 必须不同于
`origin_boot`。`committed` 的终态只选 target，`rolled_back/aborted` 的终态只选 base；
已解决代不再处于 probation。迁入失败后的已决 legacy base 允许 selector 明确缺席，
不能以空 JSON 代替。未决代仍需宿主在后续被动启动、健康证明和持久决定后激活。

控制者可能在启动子进程取得 lease 之前，将 `activating/start_target` 推进到
`verifying`。因此允许同 generation/deployment/role/installation、revision 不倒退的
合法推进；同 revision 必须与计划的原始 state 摘要完全一致。最终权威来自新读取的
记录、实际安装及格式证据，并不声称计划保存了它没有携带的旧 role binding。
在本次准入读取期间，记录的身份、两侧 binding、观察格式、期限和 challenge 等
不可变字段仍必须一致；只接受可变阶段/健康/状态字段的合法推进。

## 实际文件与同连接证据

所选 slot 使用现有 `verify_current_slot`，核实际当前 Python 词法入口、prefix、
真实导入路径及静态文件。固定 controller 的静态身份也必须仍可验证。未选中 slot
不执行文件核验，损坏 target 不能因此阻止健康 base 回退；这不证明另一侧仍可启动。

actual config、selector、unit 使用已有有界 nofollow 文件读取和精确快照合同。
unit 按原始字节而非 JSON 校验；managed role 的 unit 必须对应 anchor 冻结的 unit。
固定 launcher、journal 根及预算也重新核验。文件系统核验复用现有 fdinfo/mountinfo
实现，覆盖 config、DB、journal、writer 与 unit 的父目录。本片不宣称锁住挂载拓扑，
也不提供抵御忽略合作锁的同用户恶意进程的沙箱。

实际配置从已经核过的 bytes 经 `ConfigFile.from_bytes` 解析，使用既有 Settings
转换和未知字段拒绝规则，不重读路径，也不采用缺文件的默认配置分支。

未决代以及尚未在实际 Store 精确投影的终态仍冻结三份文件。只有记录已 resolved，
且同一个已核 DB 连接读取到固定 `management_projection/transaction-v1` 对象，其
generation/revision/store_instance 与该终态精确相符，启动才允许采用与历史快照不同
的当前配置。复用 `read_projection_connection` 的有界回执解析和真实 Store/main、
路径、WAL/FULL 合同，不构造临时 Store，也不凭 DTO 或 resolved 字段单独放行。
这使已完成事务后的普通配置热更能在下次启动保留；旧 journal 和快照不被改写。
selector、unit、slot、anchor 和完整格式始终保持原合同，未决代不因同形回执放宽。

一次启动中，`acquire` 已核配置 bytes 必须保持到 `open_store`，包括原样字节与解析后
设置；中途变化在业务 Store 工厂前拒绝，不静默更换调用方已用来确定监听参数的配置。
初次准入内部前后观察之间也要求相同 bytes；返回 Store 前再次从该实际连接核回执。
该规则只处理启动准入，不允许旧 nonce 健康证明在配置已变后继续报告历史配置摘要；
运行期健康与配置门的宿主接线须独立保留严格判定。

DB 的 parent 和普通文件均保留 nofollow 描述符，拒绝缺文件、软链接、非普通文件、
外来 owner、多硬链接及 inode/path 变化。只比较稳定的文件身份，不用主 DB 文件
摘要代替 SQLite WAL。初次读取采用专用 `mode=ro&cache=private` 连接，复用
`read_format_evidence` 的预算和同一显式 snapshot；其只读 SQL 不承诺 SQLite 不产生
WAL/共享内存的底层 bookkeeping 文件。

必须观察到完整七领域 `FormatState`，与当前记录的 `observed_format` 精确一致；
Store/main 身份及主对象 `id/kind=main/principal=owner` 也必须一致。所选 slot 实际
兼容声明的摘要必须符合该 role，且用 `assess_compatibility(slot, slot, state,
handoff_protocol=2)` 验证本角色可读、写入同一合同且无需迁移。对另一侧的准备期
证据不冒称此刻仍新鲜。marker 的来源认证边界仍见[格式证据](format-evidence.md)。

`open_store` 使用真实 `Store.open_existing(durable=True)`，由它核三组既有结构，禁止
DDL/backfill/新身份；返回业务 Store 前，在该同一连接上再次读取完整格式/主身份，
有限读取 `PRAGMA database_list`，核实际 main 路径、WAL、FULL/EXTRA 同步策略与事务
模式。前后仍复核 DB 文件身份、最新允许角色、安装及三个实际文件。marker 删除、
坏数据、同身份复制到新 inode 或路径替换均在交给 Runtime 业务恢复之前失败。
通过后正常业务仍可写；这不是只读运行模式。

## 验证边界

终态已投影后的配置准入尾片新增 16 项参数化回归，同连接回执接口新增 1 项并强化
外层事务保持断言。当前 boot 63 项、投影 42 项和运行绑定 33 项合计 138 项通过
（pytest 2.36 秒，service 2.848 秒，峰值 121.1 MiB、swap 0）；四份 Python 文件
Ruff/格式和两处源码严格 mypy 通过，静态检查最高 122.8 MiB。覆盖四种终态、缺失/
错误/畸形/超大回执、未决代同形回执不放宽、acquire→open_store 配置变化、准入观察
中变化、实际返回 Store 连接再次核回执，以及不改写历史或创建额外 Store。

新增单文件 47 项回归通过（pytest 0.94 秒，受限 service 1.436 秒，峰值 85.8 MiB、
swap 0）；两处源码严格 mypy、三份 Python 文件 Ruff 和格式检查通过，静态检查最高
127.2 MiB。全部经统一守卫，单次 1 GiB、共享 2 GiB、30 秒硬限。

回归只使用临时数据库、合成安装声明、模块局部的进程身份替身及协议 fixture，
不启动真实 systemd、模型、候选解释器或用户数据库。包含实际 Store 无迁移打开后
业务可写、合法 revision 推进与本次读取倒退拒绝、坏 target 不妨碍 base 准入、实际
配置/selector/unit/slot 改变、同内容 DB inode 替换、marker/主身份变化及失败关闭
仍保留 writer lease。两个目录同步失败用例先在旧链均得到 `DID NOT RAISE`：真实
state rename 已可见、随后目录同步持续 EIO，修后在 acquire 或 open_store 阶段均
拒绝，且不进入 SQLite/Store 工厂。
既有 launcher 的验证证据见[固定入口](managed-launcher.md)，不计作本片测试结果。

Python 的 [SQLite URI](https://docs.python.org/3/library/sqlite3.html#how-to-work-with-sqlite-uris)
用于禁止缺文件创建，Pydantic 的
[JSON 校验](https://docs.pydantic.dev/latest/concepts/json/)用于解析已核字节；二者均不替代
宿主的真实 boot、lease、安装来源或跨进程事务权威。
