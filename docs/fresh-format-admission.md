# 新建数据库的完整格式声明与启动准入

本片将完整格式声明接入真实 Runtime 构造。只有 `Store.open_runtime` 在本次调用中以 `O_CREAT|O_EXCL` 创建的新数据库，才有发布 `runtime_state.format_state_v1` 的资格。已有空文件、历史库、普通 `Store(path)` 创建的库，以及上次初始化中断留下的无标记库，都不会因为结构相同而获得认证。

这是本版本持久编码与解释行为的合同。它不证明实际 uv 环境、ResolverContext、安装来源或现有配置已经认证，也不代替随包兼容 manifest、protocol 2 或 A/B 切换的独立核验。正常 Runtime 只接受当前完整格式；已有无标记库与旧格式都在恢复业务前拒绝，局部迁移标记不能替代完整声明。非托管 Linux 接纳[停机升级指南](channel-ownership-format-upgrade.md)列出的七种精确前序；非托管 macOS 接纳其中完整 `(1,3,2,4,3,2,1)`、`(1,3,2,4,3,3,1)` 或 `(1,3,2,4,3,4,1)`，并须满足既有物理路径、权限与无托管痕迹条件。Windows、其它 macOS 旧格式与托管跨格式更新仍拒绝；其他旧库继续使用原发行版。

## 真实构造顺序

`Runtime.__init__` 将 `ConfigFile.settings.default_mode` 交给内部 `Store.open_runtime` 工厂。三种默认模式 `ask/auto/yolo` 沿用配置验证结果，创建对应的 owner 主会话；重开时不因默认模式变化而替换既有主身份或模式。

1. 普通 `Store(path)` 与工厂先取得相同规范数据库路径的构造锁。已有文件在锁内读取当前标记分类，不沿用锁外判断。
2. 本次独占创建的文件在 SQLite 连接前和初始化写入前，还须确认同一创建句柄仍为零字节，且 `-wal`、`-shm`、`-journal` 均不存在。悬空链接和无法核实的状态也拒绝，不删除或截断原文件。只有这份一次性空白证明可以省去首次 legacy 分类；普通已有空文件没有该资格。各准备阶段及最终发布前继续核对 `dev/ino`、普通文件和单链接身份，最终发布前的标记复查不变。
3. 无标记新库复用实际 Store 初始化、`Memory.__init__`、`Store.prepare_channels` 和 `main_session(default_mode)`。渠道只准备共享 DDL，不创建 HTTP client，不运行渠道恢复。随后只读核齐 core、memory、channel 三组与 Store/main 身份。
4. 再次确认路径、构造锁和无完整标记，设置并核验 `synchronous=FULL`，在显式 `BEGIN IMMEDIATE` / `COMMIT` 中插入完整七领域声明。此前准备允许分多个事务提交；不把 `with db` 或 `executescript` 当作整个初始化的原子事务。
5. 发布后重读格式和身份，通过后返回 `migration_allowed=False` 的 Store。此时才进入 Runtime 的 Memory 构造、`Runtime._recover`，以及之后的 `ChannelApplication._manager` / `ChannelManager.recover`。后两者可执行正常恢复业务写入，不能先迁移再宣称准入。

普通 `Store(path)` 仍可创建仅 core 的未标记库，避免轻量内部用途被强制变成完整 Runtime。但它打开已经标记的库时，与工厂一样先准入并禁止迁移。`Store.open_existing` 只打开已有文件，默认不新建构造锁或调整权限；内部 Runtime 使用 `runtime_owner=True` 时会持有同一构造锁直到连接关闭。该入口不迁移，路径与 writer 所有权仍由可信宿主核验。

### 短锁与失败边界

锁使用数据库规范路径同目录下的固定 `.<数据库名>-initialization.lock`，POSIX 使用 `flock(LOCK_EX|LOCK_NB)`，Windows 使用 `msvcrt.locking(LK_NBLCK, 1)`。Windows 加解锁前均 seek 到 byte 0，锁区允许位于 EOF 后，无需写入占位字节。竞争立即以固定 `store_initialization_busy` 失败，不轮询，不退回无保护初始化；释放只关闭/解锁，不删除固定 inode。POSIX 还核验锁文件归属及私有权限，不 chmod 已有锁。[Python fcntl](https://docs.python.org/3/library/fcntl.html#fcntl.flock)、[Python msvcrt](https://docs.python.org/3/library/msvcrt.html#msvcrt.locking)

macOS 停机升级复用此固定锁，借用已锚定的既有父目录 fd，不 resolve、mkdir 或关闭所借 fd；进入及提交前后重核词法父路径、数据库、权限与固定锁身份。普通 Runtime 的规范路径和建目录默认行为不变。升级器在确认 SQLite 关闭后才释放独占；关闭失败保留资源，由显式 `retry_close()` 只重试清理。

文件描述符不可被 exec/subprocess 继承；这不等于对纯 fork 隔离描述符。构造链不使用 fork。普通 `Store(path)` 只在构造期持锁；合作 Runtime 持有同一固定锁直到连接关闭，停机升级因此会拒绝仍在运行的合作实例。该锁不能阻止不合作的外部程序改库、换路径或改安装环境。规范路径可归并普通路径别名，不提供任意硬链接发现或恶意 ABA 防护。

准备、核验或 marker 提交之前失败，连接和创建句柄关闭，文件不删除，已提交的准备事实可以保留为无标记 legacy。下一次打开已有文件没有独占创建资格，不自动补证，正常 Runtime 拒绝该库。若 COMMIT 或其后核验报错，调用者得不到可用 Store；不宣称能撤销可能已经提交的 marker。重开按实际持久事实重新分类：当前有效标记走禁止迁移，旧格式和无标记库不能进入正常 Runtime，坏标记直接拒绝。

SQLite WAL/SHM 与短锁文件可以发生文件系统变化。“准入拒绝前无业务写入”不等于目录完全没有变化；不复制运行中的单个 DB 文件、不忽略 WAL，也不使用 `immutable=1`。

## 当前七领域合同

完整值由 `format_admission.CURRENT_FORMAT_STATE` 声明：`metadata_version=1`，`database=1`、`objects=3`、`configuration=2`、`ingress=5`、`outbox=3`、`memory=4`、`install_profile=1`。此前七域完整前序保留为显式停机升级源；本次项目资料复核只将 ingress 从 `4` 扩为 `5`，原完整 `(1,3,2,4,3,4,1)` 保存为 `PRE_PROJECT_INBOX_FORMAT_STATE`。记忆来源、任务、未知回执和主配置的解释方式保持原合同。ID 不是大小可比较的 schema 版本；不能仅靠表结构相同沿用一个已经改变的解释合同。

| 领域 | 持久事实与解释边界 | 当前主要写入者 / 读取者 |
| --- | --- | --- |
| `database` | 共享 core、memory、channel DDL 与索引；WAL；消息插入序号；`message_sequence_v1='done'`；1–96 UTF-8 bytes 的 Store/main 身份；主对象 `id` 对应、`kind='main'`、`principal='owner'`。不扫描所有业务行或替缺失派生行回填。 | `Store._initialize/_put/main_session`、`validate_existing_schema`、`read_format_evidence`；共享声明见 `store_schema` 与 `memory_schema`。 |
| `objects` | `(kind,id)` 主键下的 JSON 对象、事件与消息顺序；run/session/approval/任务快照的原有状态语义；持久 approval 不等于当前活跃 future。执行身份、权限快照与 ReplyOrigin 分离；unknown 外部效果不能被重启或合成错误收据伪装成安全重试。压缩保留原始证据与来源边界。 | `Store._put/event/messages`、`Runtime._recover/_execute`、Permissions、TeamManager、WorkflowEngine、Compactor 与 Conversation。 |
| `configuration` | 缺文件采用 `Settings` 默认；Pydantic 沿用现有类型转换，未知字段拒绝，并非 `strict=True` JSON 合同。包含长期问题关注的 `memory_watches` 设置，完整序列化会写出其默认值。`validate_changes` 递归合并，渠道 destination/memory preference 等既有整体替换规则继续；候选配置仍需原有来源、授权及生命周期核验。外部 JSON 配置的原始 bytes/路径真实性不由数据库 marker 证明。 | `ConfigFile.__init__/validate_changes/_merge/replace`、Settings、server 的 `apply_configuration` 与 `Extensions.apply_locked`。 |
| `ingress` | 持久输入 ID、原 payload 比较、有限队列及 pending/delivered/cancelled/quarantined 状态；完整 route/proof 与 principal 独立冻结；同来源前缀合批而不混入另一来源，领取与恢复维持已有原子性。`attention_memory_watch` 只允许本机宿主关注证明，按证明独立合批，在领取、模型请求及输出前复核当前关注、来源和生命周期；取消或遗忘后旧证明失效。来源文本与模型输出不能自行提升权限或猜收件人。 | `Store._enqueue_input/claim_inputs`、`Runtime.submit/_claim_inputs/_recover`、Conversation、AttentionInbox、渠道入站。 |
| `outbox` | `channel_outbox` 的原 payload、media mode、route、proactive/proof、分片游标及收据；相同幂等键不同 payload 拒绝。`sending` 恢复为 unknown，unknown 不自动重发，blocked 需显式现有重试/重验。默认附件行为保留，voice 仅显式选择；发送时继续核当前授权与冻结来源。 | `ChannelManager.enqueue/recover`、发送循环及重试、`ChannelApplication`、`FrozenRouting/guard_delivery`、原平台 adapter。 |
| `memory` | 原始 source/revision、记忆事实及版本、别名证据、关系与派生检索结构，以及文本、文档、ASR 和网页快照来源的明确解释范围；`revision` 为非负 INTEGER，`alias_evidence_version=1`。记忆绑定以 ID/version/source 元数据证明核验，不能由记忆正文自动授予身份、授权或渠道地址权限。禁止迁移重开不重建 meta、别名、词索引或关系。 | `Memory.__init__/_migrate_alias_evidence/register_source/save/edit/resolve_conflict`、MemoryPipeline、`attachment_memory_source`、`proactive_memory.check_preference`。 |
| `install_profile` | 接受尚未准备任何实例 profile/receipt 的缺省状态；本片不制造持久实例 profile。已有安装意图仅按 pinned uv adapter 有界解析，保留主 requirement/extras/with/constraints/overrides/python/entrypoint 与已支持 options；结果仍是 `intent_parsed/context_required`。索引/约束/凭据来源及 ResolverContext 未核齐时不能变成可安装证明。 | `parse_install_intent`、`InstallationReceipt/read_install_receipt`，以及现有安装 manifest 的独立核验；这些证据不由七 ID 自动生成。 |

以上声明绑定当前可信应用代码的解释合同，不声称审计数据库内所有额外 SQL、插件或历史手工写入。额外插件私有表/其私有索引可保留；这不是插件完整性或任意触发器认证。正常配置和安装真实性仍由各自宿主核验负责。

### 项目资料复核的格式边界

`ingress=5` 为 `attention_project` 的持久来源、准确 Web 路由、按证明独立领取及项目段纯读提供准入边界。项目段从本运行实际首次领取的输入建立；原始路由及 reply origin 坏值不得通过归一化变成合法 Web 证明。新主人输入立即持久排队，项目在当前工具结算后结束，由独立下一运行处理，不在项目续轮重新解释为主人授权。

冻结的[2.136 格式 reader](../tests/fixtures/format_admission_v2_136.py)与[声明](../tests/fixtures/compatibility_v2_136.json)来自签名 `e9813d711e21e38b6f5104e5aaed3ce2f9a09e4d` 原字节。[格式回归](../tests/test_project_inbox_format.py)核原字节摘要，并用旧实际准入函数拒绝新库；它是源码 oracle，不冒称安装了旧 wheel。旧运行的独立离线反证另确认 marker 丢失与同 run 认领新主人消息，不据此宣称首轮 Web 项目可以向平台外发。

已有 ingress4 库须在既有排他、完整状态校验和 marker 原子提交边界升级。迁移保留原身份和业务行，不重放输入、来源或 unknown 投递；托管跨格式切换与 Windows 旧库升级仍不支持。旧 reader 与原 A/B 同格式判定拒绝升级后的库，不因候选启动失败而放宽格式。

### 长期问题关注的格式边界

最初的 `memory_watch`、`memory_watch_match`、`memory_source_completion` 是独立 `version=1` 功能记录；`memory_jobs.checkpoint.source_completion` 是可选的完成证明。真实后台 job 进入 `done` 与完成事件、canonical object 和 checkpoint 证明同事务保存，发现服务随后核对准确来源及修订。旧 `done` 没有完成证明不会补造事件，也不会扫描此前已整理完的资料。该功能加入时不重解释原有 source、revision、forget、run 或 unknown 状态，当时保留 `objects=3`、`memory=1`；后续 ASR 来源合同改为 `memory=2`、网页来源合同改为 `memory=3`，均不改这些证明字段。

本次 `memory=4` 的新 `memory_watch/version=2` 增加严格规范化的 `routing`：只允许规范 Web-local，或从登记轮真实主人输入冻结的 bound 原来源；完整字段、输入 ID、主会话、principal 和已保存原来源必须一致，不能容错回落后接纳坏路由。旧 v1 继续按 Web 解释，match/completion/checkpoint、ingress 与 outbox 的编码不变。事件仅携带原 match proof，选路回查 canonical watch；相同路线的不同 proof 仍独立合批。Web attention 内容整体预算包含原身份和非授权说明；普通文字与异常 partial 的回复路线为空，AI 显式发送入队前仍核当前渠道、原审批和当前 proof，不把偏好当成新权限。

原运输配置、主人绑定与 manager 代次在入队及每段实际发送前核对，配置对象漂移时拒绝继续；已完成分段的收据保留。主动通知默认目的地变化不会重定向已冻结的原来源，当前主动通知权限与记忆依据继续由原 `guard_delivery` 核验。

已认证网页管理使用 `GET /api/memory/watches`（`after_id`、`limit`，最多100个候选）及 `POST /api/memory/watches/cancel`（正文只含准确 `id`）。读取完整校验全部有限候选后，仅返回准确 `id`、问题、短语、状态、修订和有限来源偏好的完整条目前缀，不返回原路由或地址；整个页遵守配置字符预算和65536字节上限。`has_more` 为真时，以最后实际返回条目的 `next_after_id` 继续；短页也可继续，满窗口可能还需读取空末页。单条或分页元数据放不下仍报错；模型列表保持原整列表预算。取消继续沿原准入、事务及通知失效链，重复取消同一条已停止登记返回原状态，不自动重发未确认的请求。这些管理读取不改变持久记录格式。

[`format_admission_v2_120.py`](../tests/fixtures/format_admission_v2_120.py) 和 [`compatibility_v2_120.json`](../tests/fixtures/compatibility_v2_120.json) 保留签名 P `c55546bbc1687c7e183f1e5ba7c671a7443dfb2a` 的原字节；[来源偏好格式回归](../tests/test_memory_watch_origin_format.py) 用其源码 reader 证明 memory4 被拒。它复用当前未变的模型与格式证据读取器，不是旧 wheel 或旧发行环境的运行证明。精确 Linux/macOS 独立安装停机升级只改允许的完整 marker，保留原 v1 关注、来源和业务行；托管及 Windows 旧库升级范围不扩大。

主配置新增字段会让拒绝未知字段的旧版无法读取保存后的完整配置；新输入 proof 也不能被旧版当普通 `system:attention` 消费。2.112 的旧读取路径只识别整理失败证明，既不按 watch proof 独立合批，也不在取消或遗忘后核验它。因此新合同必须显式拒绝旧 reader；即使本机尚未登记关注，也不沿用旧完整声明。

### ASR 转写来源的格式边界

ASR 转写收藏将当时的 `memory=1` 扩为 `memory=2`，新增 `attachment:<转写ID>:transcript:v1:asr_text:<工件SHA256>`，明确来源是已保存的派生转写，原文保持工件字节。普通 SHA marker 和 `document:v1` 的旧编码不变；当前 `memory=4` 继续在升级后的库中恢复这些来源。

2.114 只恢复纯摘要或六段文档 marker。让新 marker 继续声明 `memory=1` 会使旧版接纳整库，却拒绝缺 episode/job 的来源恢复、失败证明和关注核验。把新来源伪装成普通文本则会丢失转写身份。因此无 DDL 变化也需要新的来源解释合同。对象/输入 proof 字段、版本、遗忘、授权和 unknown 语义均不变，其余六领域保持原值；不新增 `objects=4`。

冻结的 2.114 format reader、attachment source reader 与 manifest 及原字节出处见 [fixture 说明](../tests/fixtures/format-v2.114.md)。它们用于源码 oracle，不表示实际安装或运行了旧发行包。已有库必须经固定停机升级，不在正常启动时写 marker。

### 网页快照来源的格式边界

`memory=3` 新增 `attachment:<id>:webpage:v1:<scope>:<sha256>`，范围仅为 `http_utf8_text_v1`、`html_static_text_v1`。摘要绑定已保存快照的完整 UTF-8 字节，包含请求网址、最终网址、宿主采集时间、读取范围、不可信资料声明及全文。恢复时核对原摘要、来源头与 marker 范围；保留稳定 `attachment:<id>` 来源身份和外部资料身份，不把网页内容视为主人、当前事实或执行授权。旧文本、文档和 ASR 编码不变。

主人明确选定已留存快照后，主助手才可调用 `memory_import_webpage(id, revision)`，其中 `revision` 是该快照的完整附件摘要。只核验和登记本地全文，不重抓网站，也不只收藏阅读页、预览或摘录。默认捕获、阅读、书签、摘录、压缩和重开均不自动收藏；后台沿用既有提炼、分块、embedding 与遗忘边界，可能按已配置服务发送正文。收藏回执仅报告实际后台状态，不证明网页真实或当前有效。

网页收藏前的 `memory=2` reader 只识别文本、文档和 ASR 来源，拒绝网页 marker；继续沿用旧完整声明会让整库准入与来源恢复的解释不一致。因此即使 DDL 没变，也须使用新完整合同。网页收藏当时的目标为 `(1,3,2,4,3,3,1)`；本次来源偏好继续扩为 `(1,3,2,4,3,4,1)`，符合停机入口条件的非托管 Linux、macOS 旧库按[当前升级说明](channel-ownership-format-upgrade.md)直接升到当前合同。新建库直接发布当前合同；托管同格式协议不支持这次跨格式切换，Windows 的旧库仍拒绝升级，不能用旧版回退打开已升级数据。

[`format_admission_v2_118.py`](../tests/fixtures/format_admission_v2_118.py)、[`attachment_memory_source_v2_118.py`](../tests/fixtures/attachment_memory_source_v2_118.py) 与 [`compatibility_v2_118.json`](../tests/fixtures/compatibility_v2_118.json) 来自签名基线 `4c069a12cfe2bcbab062db44105c10de669861f3`，已由宿主核对签名 Git 对象和其冻结源码的原始字节；预期摘要固定在[格式回归](../tests/test_webpage_memory_format.py)。回归执行冻结旧 reader，并复用当前未变的共享模型与证据读取器；这不是旧 wheel、旧发行环境或任意历史版本的运行验证。

固定升级只在既有独占保护下原子转换精确 marker，不改 DDL 或业务行，也不改外部配置文件。旧配置缺少 `memory_watches` 时新版采用默认值；这不证明配置文件来源真实，更不表示旧库可跳过升级直接准入。回归保留 2.112 的冻结准入源码和 manifest，执行旧准入函数核对新 marker 被拒；它使用当前未变的格式模型和证据读取器，不是运行旧发行包的完整验证。

### 可淘汰的 shell 回执记录

`objects.kind='shell_receipt'`、`version=1` 是正常关闭时生成的独立证据记录，内部键由真实会话与命令编号组成；`shell.receipt_stored` 事件仅含内部键及来源，其 `events.id AUTOINCREMENT` 定义跨关闭和批内保留次序。回执不包含命令正文、环境、shell secret、PID 连接或执行授权，不改变 run/approval/unknown、渠道或主配置语义，不恢复进程，也不自动重放。因此沿用当前七领域 ID，独立版本不能替代相应语义改变时的合同审查。

格式固定上界集中在 `shell_receipts`：原始 retained bytes 最多 100000，非空段数不超过 retained bytes，完整 JSON envelope 最多 8 MiB，非 output 元数据最多 512 KiB，计数不超过 SQLite 最大整数。写入先核最坏编码开销和实际完整大小；读取在 SQL 内先核 envelope 和元数据，再点读单条、拒绝重复 JSON key、未来版本、非法 Base64、不连续偏移或计数不等式，最后构造 `_Output`。旧大记录的读取门不随当前页帽缩小；验证后按当前输出额度裁剪，分页保持原始 byte 游标和 UTF-8 终态规则。

只有当前真实调用资格下、本会话 live 查找的确切 `not_found` 才能回退读取，其他游标、预算或执行错误保持原拒绝。正常收口成功与归档成功独立记账，归档失败回滚并传播，已收口的 Store/writer 仍正常释放；未收口时不伪造成功。普通会话删除与回执清理处于同一同步事务，归档前再次确认会话存在。

当前格式的真实 Runtime 重开与通用 Store 保留未知功能记录分别有回归。通用 Store 回归使用当前代码，不是基线旧发行包的实际运行证据；旧读取器忽略该 kind/事件的判断还依赖原有读取路径的源码审查，不能据此宣称任意历史包已验证兼容。敏感内容保留与崩溃边界见[连续终端会话](shell-sessions.md#正常重开后查回执)。

## 已标记库的持续约束

`read_optional_format_evidence` 在有界快照中区分真正缺失、有效完整标记与错误。缺少 `runtime_state` 表可视为 legacy；目录按 SQLite 的 NOCASE 名称规则定位，再拒绝非 canonical 名称，避免 `RUNTIME_STATE` 被误当缺表。不可读/错误类型的表、未知命名族、坏 JSON、部分领域、未知 metadata 或当前不支持的合同 ID 都拒绝。`runtime_state.name` 非 STRICT TEXT 仍可存 BLOB，所以 TEXT 与 BLOB 的 `format_state_` 前缀分别用主键范围查询；BLOB-only 和合法 TEXT 与 BLOB 冲突均不能降为 legacy。最多三次分类查询加五次完整证据查询，沿用默认八次查询预算。

有效标记必须匹配当前七 ID，并再次核齐三组受控结构、局部 marker 和 owner 主身份，才返回 Store。Memory 不运行迁移分支；ChannelManager 在创建自有 HTTP client 前调用共享准备入口并只读复核，重新配置继续继承。结构相同不能生成格式 ID，具名 partial/表达式索引仍按已有保守声明核验，不仅比较名称。

`Store.format_state` 是最后已 admitted 的不可变合同观察值。效果前调用 `revalidate_format()`；一旦本 Store 已 admitted，marker 后来删除、损坏、换合同或 Store/main 身份改变均失败，状态不清空、不变成 `migration_allowed=True`。该方法不会把原先无标记 legacy 自动升级为 marked。

Runtime 与 ChannelApplication 重配置入口先核；server 在关闭可用组件之前核一次，在全部 close awaits 后、保存候选配置之前再核；Extensions 在最后一次异步 fingerprint 后、无 await 的 persist/publish 前核，失败丢弃未发布候选连接。预先坏状态不会先关闭组件；关闭途中发生变化时不保存 target，仍传播失败并沿用原有恢复策略，不承诺所有组件一定可恢复。

与本片配套的更新能力检查会重核 admitted 状态，已标记库暂拒 Runtime 内原位自动升级，直到完整候选切换接通；legacy 沿用原安装检查。版本查询与通知仍可用，不许诺批准即可安装。`format_state` 不等于 `migration_allowed`：已有禁止迁移入口也可打开无标记 legacy。跨 data-dir 共享同一 uv 环境、手工外部升级或直接运行不识别 marker 的旧程序，不在当前 Runtime 封口的全局控制范围内。

## 回归范围

使用小型临时数据库验证独占创建、同文件内容变化、SQLite 辅助文件、首次写入前的持久级别、格式发布、失败重开、已有数据只读拒绝和合作构造器互斥。格式读取器仍验证原期限、隔离、WAL 快照和连接状态恢复。执行方式与资源保护见[开发与验证](development.md)；离线替身不代替跨平台 CI 或真实 A/B 切换验收。
