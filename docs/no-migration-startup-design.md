# A/B 候选的无迁移启动边界

状态：[连接内格式证据读取器](format-evidence.md)与覆盖三个构造器的[禁止迁移入口](store-existing.md)已独立实现，完整准入与实际启动接线仍为设计。本文补充[可恢复管理](recoverable-management-design.md)与[兼容声明](upgrade-compatibility.md)，不改变 2.19 的同版本恢复入口，也不为已有数据库补发格式证明。

## 为什么现有 probation 还不够

`linux_bootstrap.open_boot_session` 已在 ConfigFile/Store 之前取得 writer lease，并核验 protocol 1 的单安装身份。这个约束使同版本恢复有明确边界，但不允许把另一套候选代码当成同一安装启动。

当前构造路径含有下列真实写入，不能用“启动后再看 readiness”替代启动前检查：

| 位置 | 当前行为 | 候选需要的变化 |
| --- | --- | --- |
| `Store.__init__/_initialize` | 创建目录和数据库、chmod、设置 WAL、建表/索引、补消息序号与实例 ID | 单独提供只打开已存在库的入口；禁止缺库创建、DDL、序号回填或生成新实例 ID |
| `Memory.__init__` | 建表、补 memory_meta、迁移别名证据、补词索引与关系边 | 同一启动策略必须传入记忆层，禁止它自行迁移或回填 |
| `server._create_app` → `ChannelApplication` → `ChannelManager` | 即使渠道关闭也建收件/出站表；缺 fingerprint 列时执行 ALTER TABLE | 渠道构造与重新配置都必须遵守同一策略，不能把 lifespan 尚未开始当作无迁移证据 |
| `Runtime._initialize` | 构造记忆后执行 `_recover`、配置团队、取得主会话等 | 只有通过格式准入后，才能进入已审查的同格式恢复写入；外部效果仍受 probation 门控制 |
| `ConfigFile` | 读取 JSON，缺文件采用默认值 | 候选必须使用已核摘要的有界配置 bytes；缺配置不能变成默认配置后继续 |

数据库初始化、崩溃恢复与外部激活是三个阶段。无迁移不等于永远只读：格式已核验后，恢复旧运行状态、记录新收件等合法业务写入仍需要进行；禁止的是在兼容判定前或判定之外悄悄改变持久格式。

## 选择与不采用的捷径

采用显式的“打开已有数据且禁止迁移”启动策略，覆盖 Store、Memory 与 ChannelManager；普通首次初始化仍保留现有流程。只增加一个 Store 布尔开关、让其它构造器继续回填是不完整的。实际接口应随准入证据一起评审，不能暴露一个由模型自报 `compatible=true` 的绕过参数。

不通过 SQL authorizer 阻止少数 ALTER 语句来定义兼容：INSERT 回填同样会改变事实，objects/ingress/outbox 的 JSON 解释也可能改变而 SQL 完全相同。authorizer 可作为测试断言或附加约束，不能替代七领域格式合同。

不在真实库上运行旧构造器，再比较修改前后摘要。失败发生在写入之后，已经违反候选准入边界。也不复制运行中的单个 SQLite 文件或使用旧备份来“消除”新格式和 unknown 效果。

## 准入顺序

1. 由旧控制者验证候选构件中的静态兼容声明、双方安装身份与协议；候选包尚不导入或执行。缺少声明/当前格式证据时返回明确不支持。
2. 获取既有 writer lease，核对配置、数据库、journal 与选择记录的路径和身份；需要停写的最终核验必须在旧 writer 退出后进行，不能沿用准备阶段的陈旧证据。
3. 宿主为独立有限读取器提供已存在数据库的专用连接，只读取必要的完整格式标记、实例 ID 与有界 schema 事实。必须显式开启读事务，在同一 SQLite 快照内完成查询；`with db` 不会替 SELECT 开启快照，也不负责关闭连接。读取器只结束自己开启的事务，连接和路径身份由宿主负责。schema 相同只能作为结构一致性证据，不能从中推导七领域格式 ID。
4. 运行 `assess_compatibility`。读取器得到的结果绑定本次 boot/generation、数据库和配置身份；它不是可跨重启复用的通行证。跨版本 handoff 使用独立 protocol 2，不能修改 protocol 1 的单安装比较以便通过。
5. 候选用禁止迁移入口打开同一数据库，复核其连接所见的必要事实，再进入本地恢复和被动 readiness。检查失败即关闭连接并保持准入门关闭；关闭不能确认时保留 writer lease，延续现有所有权规则。
6. 只有旧控制者确认健康并耐久提交后才激活外部组件。回退继续使用包含最新事实的当前数据库；不存在自动还原旧库的分支。

格式准入失败必须没有业务写入；准入通过后，Runtime、团队、工作流、渠道和记忆任务的合法崩溃恢复可能已经持久更新状态。因此，后续 readiness 或激活失败不能继续声称“数据库完全没变”。protocol 2 的 Store/main 身份核验也不能弱于现有 BootPermit 与终态投影：除标记值外，还需证明对应主会话对象的身份和归属。

用户同权限进程可以绕过应用锁或同时改路径；writer lease 不是防恶意本机代码沙箱。准入声明只涵盖合作写入者与已核身份，不能宣传能抵御任意外部数据库篡改。

## 读取预算与 SQLite 限制

读取器不构造 Runtime、Store、Memory，不加载插件，不运行模型，也不扫描全部消息/记忆来猜版本。先复用现有私有路径与文件系统核验；连接使用编码后的 file URI 和 `mode=ro`，禁止缺库创建。连接设置短 busy timeout、SQL/结果长度限制及基于单调时钟的 progress handler，查询数量、schema 行数、标记 bytes 和总时间均有显式上界；超限返回固定错误，不打印 SQL 数据。

通过 Context7 核对 [Python sqlite3 文档](https://docs.python.org/3/library/sqlite3.html)：`set_progress_handler` 可中断过长语句，`setlimit` 约束连接资源；`executescript` 在传统事务模式下可能先隐式提交，因此只读准入不用它承载快照协议。限制不能保证底层阻塞 I/O 的硬时限，执行宿主仍需要现有进程资源与期限保护。

SQLite 的只读连接不等于文件系统完全零变动：WAL 模式可能涉及 `-wal`/`-shm` 辅助文件。验收应证明业务页、schema 与业务事实没有被迁移，并明确辅助文件行为；不能只凭 `mode=ro` 宣称目录未变。[SQLite WAL 说明](https://sqlite.org/wal.html#read_only_databases)

不能给仍会变化的业务库设置 `immutable=1` 来规避锁或辅助文件；该参数会关闭部分锁与变更检测，必须以真正不变为前提。[SQLite URI 参数](https://sqlite.org/uri.html#uriimmutable)

完整格式标记的首次建立是另一项交付：需要明确各领域合同、旧数据适配范围和跨版本 fixture，不能因为两项局部迁移 marker 存在就自动生成。读取器首期对没有完整标记的历史库保持不支持；普通启动的历史兼容能力不受其替代。

## 最小实现与验收顺序

先独立实现有界只读格式证据读取与拒绝分支，再实现覆盖 Store/Memory/ChannelManager 的禁止迁移入口，最后接 protocol 2。先不改普通启动默认行为，也不新增跨平台服务控制器或数据库降级工具。

合成临时 SQLite 测试至少覆盖：缺库/缺 marker 不创建；未知/重复/类型错误/超大标记固定拒绝；schema 变化与身份变化；真实 WAL 中最新已提交事实；锁竞争和查询预算；禁止入口缺表/缺索引/缺实例 ID 不修补；Memory 不迁移别名或回填索引；禁用渠道的构造与重新配置也不补 fingerprint；格式准入失败前后业务事实相同；两个已认证同格式实现对 ingress/outbox/unknown、审批与路由 proof 的读取一致。

读取器已在宿主提供的合成连接上通过单快照、WAL、身份/标记、锁竞争、读取预算与清理测试，并拒绝非零 `read_uncommitted`，避免共享缓存连接读取未提交事实。连接内新回归与纯判定共 200 项通过，详情见[验证记录](format-evidence.md#验证边界)。

`Store.open_existing` 已实现已有文件的禁止创建打开和三组结构、局部身份的有界核验，Memory 与 ChannelManager 继承同一策略；普通初始化不变。独立测试覆盖零迁移、失败不修复、成功后的业务写入与 Runtime/渠道重新配置，详情见[构造验证](store-existing.md#验证边界)。宿主路径认证、完整七领域准入、实际 bootstrap 接线和跨版本语义验收仍未完成；没有访问用户真实数据库。
