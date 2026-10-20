# Linux 同版本配置／重启事务：首片接口设计

状态：2026-10-21，整体设计与第一块纯 journal／单步控制者已通过独立复审及 60 项离线回归。第二块已接入真实 Runtime、应用生命周期、维护门和固定 nonce 路由；第三块的 Linux backend、独立 helper／ensure 已有替身回归，详见[Linux 控制者与 helper](linux-controller-helper-design.md)。CLI bootstrap 与管理事务提交／投影仍未接通，因此尚不能通过现有 self_restart 取得可恢复重启，也未实测 systemd、中断或恢复成功率。最初基于 `368efc5` 研究，第二块已合入主动通知 A，保留其来源和投递语义。总目标参见[跨进程恢复总设计](recoverable-management-design.md)。不包含 A/B 安装、uv 下载、程序升级或数据库回滚。

## 第一块的实际边界

新增 `management_transaction.py` 与 `management_controller.py`，没有改动现有管理工具或服务路径。journal 提供 Linux 私有目录、不同固定 inode 的 journal flock／规范化 DB 路径的 writer lease、有界精确字节快照、严格不可变记录、generation/revision CAS 和不可覆盖未解决代的 prepare。JSON 对象及摘要校验已实现；本块 semantic_digest 只是严格 JSON 的规范化摘要，不代表 Settings 校验。Settings、安装摘要、实际 DB 文件身份和真实 Store 投影回执仍须由后续宿主校验，不能把 typed DTO 当成这些事实的证明。

持锁读取在执行任何效果前成功同步 journal 目录，作为接管可见记录的持久化屏障；写入或该屏障失败后，同一控制者拒绝继续。新控制者也必须先成功完成屏障。这个约束专门处理 rename 已可见而目录 fsync 失败的窗口，不会因此自动修复文件或重放效果。普通无锁 `journal.read()` 仅用于观察，不确认持久化。

控制者只提供 `await step()`，由调用方持有 journal 锁并注入时钟和 `ControllerBackend`。第一块仅用内存替身验收；后续 Linux backend 已独立实现 owner、配置 CAS、cgroup 与 lease 复核，helper 复用此状态机而不重建阶段。上述产品模块仍未构成已接通的管理／恢复命令，真实服务交接尚未验证。

`phase` 表示持久决定，`intent` 仅区分需要先记录的停服／替换／启动步骤；`activation` 区分决定已落盘与执行门确已放行。`rolled_back` 与 `committed` 都必须另有同代放行证明才是 resolved。原始失败码与当前失败码分开，避免回退自身失败掩盖最初原因。一次回退标记和独立 challenge 限制自动回退次数并隔离目标／原配置的健康证明；未引入升级、组件级子事务或逐组件恢复状态。

单步推进在观察前后使用同一 deadline 检查，总期限耗尽不得补写成功。时钟倒拨无法确认预算时，提交前进入 recovery_required，决定已持久时标 blocked，不自动回退。准备／排空只针对仍使用 base、running 且 boot 为 origin 的原实例；其它观察进入 recovery_required，不向它发 resume。提交或回退决定后若实例更换，可在原 deadline 内用同代 nonce 的新 boot 证明重新绑定并确认放行，不重置预算、不重复启动、不撤销提交。显式当前实例失败优先于同次观察中矛盾的 released proof，不能据此解决事务。主动取消驱动只留下已持久 intent，后继先观察；启动命令是否执行不明确时不盲目重发。

第一块验证使用临时目录、注入时钟、内存服务和文件错误，不证明掉电后的文件系统行为、真实进程被杀后的接管或 systemd 生命周期。第二块新增的本地握手与执行门证据见下，仍不能将两块标作“已实现可恢复重启”。

已通过根 check_local 串行执行四个新 Python 文件的 Ruff format/check、两个新源码的严格 mypy（follow-imports=silent），以及 `tests/test_management_transaction.py tests/test_management_controller.py` 首轮 56 项回归。独立复审补充失败优先级四项后，只复验变更的 controller 文件，最终 29 项通过；未变化的 journal 首轮 31 项结果继续有效，共 60 项。pytest 峰 60.6 MiB，mypy 峰 193.5 MiB，swap 均为 0；单次 1 GiB／共享 2 GiB／单 CPU／180 秒保护已由内核核验。第一块没有运行整库或真实 Runtime／服务验收。

## 第二块：已实现的维护门与被动启动

`maintenance.py` 提供 `BootPermit`、`Maintenance` 和单事件循环的 `Admission`；`maintenance_api.py` 只安装四个固定握手路由。未带 permit 的既有服务保持原启动行为，私有事务接口返回 transaction_unavailable。第三块必须在读取 ConfigFile、构造 Store 之前取得 permit，不能给已经打开真实库的进程补发一份许可：

```python
BootPermit.acquire(journal, config_path, *, boot, installation, service)
Runtime(config, data_dir, *, boot_permit=permit)
create_app(config, data_dir, *, boot_permit=permit, maintenance_clock=clock)
```

这几个签名是替代构造入口：给 create_app 传入现成 Runtime 时，不再重复传 permit。journal 的目录 FD 与 writer lease 保留整个实例寿命；应用实际组件／Store 收尾之后才释放 lease。安装、服务摘要和可信 ClockReading 来自尚待接通的宿主核验，不能由 HTTP 请求自报。

BootPermit 先取得规范化 DB 路径的固定 writer lease，再读当前 journal。未解决代仅接受明确 start_target／verifying／committed 或 start_base／verify_base／rolled_back 许可，校验安装、服务、精确配置字节摘要和只读 SQLite 的 store_instance/main_session；拒绝复用 origin boot、错误库、错误配置或未发启动意图。Runtime 在 Store 构造前复核路径。被动启动只做本地建模、既有恢复校正和 DB 身份核验；不加载 MCP／插件、不启动通知、提醒、记忆提炼、渠道 poll、语音、chiliarch、版本检查或模型。渠道旧 sending 本地标 unknown；记忆旧 processing 本地回 queued，不自动推断已执行工具、重发消息或回滚 DB。

执行准入采用自然空闲边界。prepared／waiting 阶段只冻结普通配置写入，现有服务仍可开始正常工作，**waiting 不表示已暂停新 claim**。drain 在既有 configuration_lock 内同步检查全部实际 owner；只有前台、团队、工作流与 finalizer、compaction、管理应用／版本检查、记忆回填、onboarding、Portal probe 和已领取外部效果都空闲，才在无 await 的同一步关闭门并返回 drained。持续输入／语音等流量可能始终无法自然空闲；控制者必须遵循原 deadline，超时保留原服务，不强杀工作或伪称排空。没有在持配置锁期间等待工作，也没有增加 parent→child 准入继承机制。

drained 之后才阻止新模型／工具运行和新发送领取。已进入的实际 Task 以 finally 收尾，不把计数写零代替排空证据：

| 拥有者 | 实际边界 |
| --- | --- |
| Runtime／team／workflow／compaction | 运行 Task、会话 finalizer、团队 stop task、工作流 driver 与取消 fallback；终态已存但通知未完成仍不空闲 |
| Admission effects | HTTP 效果 handler、summary、记忆提炼 job、通知发送、提醒交付、chiliarch 业务 sender、渠道文件下载／一段发送、一次语音采集至 ASR／提示／回调 TTS |
| PersonalIO | shield 的 Portal probe 由 capture 持有，观察者取消不丢失句柄；关闭等待实际回收 |
| 应用本地任务 | 记忆回填、管理应用与启动版本检查、延后的 onboarding 采用真实 Task，而非常驻 worker 的存活计数 |

主人文字和审计过的本地查询继续可用：输入持久排队而不启动模型。正常旧实例允许签名渠道文字入站，渠道附件下载等待门；等待不消耗下载 attempt。只读长轮询等待及 chiliarch ACK／heartbeat 属于协议控制流，不被宣称为业务执行已清零；drained 不代表 HTTP 连接或控制帧为零。它们仍由原组件 close 强拥有收尾。被动实例在本代实际 activation 成功前不启动这些外部接收器，也不接受渠道 webhook；成功后保留可靠入站许可，随后暂停执行仍可持久接收主人文字，不启动模型。外发 pending／retry、内存通知与管理候选保留；门释放后由事件一次唤醒，Attention 不依赖新 bus 事件才能处理暂停期间的事实。

无可核验生命周期／idle 合同的 Python legacy 插件不参与事务：检测到已加载／失败插件，或启用插件功能且发现未在 settings.plugins.disabled 中排除的带 plugin.json 包候选，返回 maintenance_unsupported/legacy_plugins。即使 manifest 内 enabled=false，也保守拒绝这类未显式 disabled 的候选；不导入来探测、不卸载原插件，旧服务保持运行。这不是插件后台任务的通用生命周期引擎。

### 固定内部握手与放行

| 方法／路径 | 请求与限制 |
| --- | --- |
| POST /api/internal/management/drain | JSON generation、boot；仅本代 origin + draining；返回 waiting 或带真实 owner receipt 的 drained |
| POST /api/internal/management/resume | 同样两个字段；仅 origin + prepared／aborted 且 base 配置未切换；释放一次 |
| GET /api/internal/management/readiness | query generation、boot；已 seal 的被动 target／base 返回 core_ready 与 paused／blocked／released 证明；原 boot 也可在准备／排空／停止原实例的精确窗口只读观察实际门状态 |
| POST /api/internal/management/activate | JSON generation、boot；target 必须 committed，base 必须 rolled_back，health 绑定当前 boot，blocked 不自动重试 |

四条精确 method/path 用 X-MuLi-Nonce 代替 Cookie；其他 API 不豁免。保留 Host、同源、JSON 类型、请求体上限，schema 拒绝 path、command、配置正文等额外字段。nonce 与 proof 只在私有握手中传输，不进入公开管理对象、日志或 SSE。

原实例的 readiness 只接受 prepared／draining／aborted、activating+stop_base 或 rolling_back+stop_target，使用原 challenge、base 摘要和 origin boot；返回 paused 前同步复核全部 owner 与 effects 确实空闲。控制者仅在 prepared／draining 把原实例证明解释为 resumed／drained，其余停止窗口只用于重新核对原实例身份，不缓存先前 POST 响应。原 boot 始终不能通过 TransactionRecord.accepts 成为目标／回退新实例的健康证明；drain／resume 动作的阶段限制不变。

激活前先成功 fsync journal 目录并重读同一记录；强拥有 activation Task 在首次外部 await 之前、组件启动之后和放行前再次校验阶段、当前 boot、配置、blocked 与原 deadline。观察者取消不终止它；同 boot 失败不会重复执行启动。release 回调失败同样使本地证明 blocked 并重新关门；已发生的外部效果不能撤销，不能据此承诺回滚。持久 blocked 只限制其 health.boot，本实例 activation 失败始终 blocked；新 boot 本地仍暂停，必须先由控制者按第一块合同持久重绑 pending，重绑前 activate 拒绝，不接受宿主自解 blocked。target／base 两角色均有真实 Maintenance→Controller 的重绑与一次放行回归。

组件在 committed／rolled_back 后启动，放行 receipt 另行持久后才 resolved。onboarding 可能修改配置，其任务延后到 resolved 再执行，避免启动过程中改变目标摘要；它保留既有幂等输入 ID，并纳入后续 drain 的真实 owner。配置与扩展写入从 prepared 到 resolved 都返回 transaction_in_progress；已排队管理计划保留 target，扩展 watcher 保留 dirty。

Maintenance.reconcile 只对齐进程内唤醒提示，配置权威始终是当前 journal。宿主 prepare 后调用一次，正常控制者持久 released／resolved 后再请求一次 readiness 立即通知。若控制者正好在最后 GET 前退出，已有配置准入、主会话唤醒或下次已认证 HTTP 入口会重读 journal、对齐提示并唤醒等待者；不产生另一种持久未完成状态，也没有文件轮询框架。私有握手先验证并形成 proof，再触发 reconcile，避免提示任务先改动配置。普通自配置与旧 self_restart 的用户接口尚未切换到事务入口，这是第三块的接线范围。

第二块新增四个 maintenance 测试文件，49 项离线用例已有 guard 通过证据。临时 Runtime + ASGITransport 覆盖完整 commit→activate→finalize、nonce／原实例重入观察、被动无外部启动、配置门、真实尾任务、取消与队列保留；真实激活回归修复了 private readiness 前提前 reconcile 的调度窗。首次新 HTTP fake 路由误置静态 mount 后已中止该检查并修正 fixture，没有循环重跑。

本轮按相关文件串行验证，49 项新增及 524 项既有用例均取得通过证据，共 573 个唯一用例：第一组 359 项全过，第二组 204 项通过；集成替身补齐新增 Maintenance 合同后，该文件 13 项复验全过；原实例观察新增两项及受影响 API 合计 31 项另组全过。26 个 Python 文件 Ruff format/check、21 个受影响源码严格 mypy 与 diff 检查通过，pytest 峰 419 MiB、mypy 峰 150 MiB，swap 0，未提高 guard 限制。本片未跑整库，未启动 systemd、设备、外部模型或真实集成，未取得真实可恢复重启证据。

## 交付范围与入口

三个实现块全部完成后的目标是让一个已识别的 Linux 用户服务，使用**同一个受支持安装、解释器、资料库和服务入口**，把配置变更或单纯重启推进到“目标本地就绪并已放行执行”，失败时恢复本事务保存的原配置并重新证明旧实例就绪。保留最新数据库事实；恢复程序不重做未知工具或发送。以下未特别标为已实现的宿主行为仍是整体设计。

根任务已确认增加显式 `restart_transaction` 应用方式：

- `self_restart` 与新增 `POST /api/management/transactions` 使用跨进程事务，返回事务 accepted/phase，不将 `systemctl` accepted 记为 completed。
- `self_configure` 增加 `apply_mode: in_process | restart_transaction`，默认保留现有进程内应用；配置 API `PATCH /api/settings`、扩展目录换代继续原有路径。这样修改扩展或称呼不强制重启整个应用。
- 请求 `restart_transaction` 而 capability 不满足时明确 `transaction_unsupported`，不会悄悄改走进程内应用或直接服务 restart。保留原 CLI `service start/stop/restart` 的 accepted 语义，文档继续区分它与可恢复事务。
- `GET /api/management/transactions/current` 返回安全摘要；`muli management recover/status` 是不依赖正常应用启动的诊断／接管入口。控制者不通过一次模型请求验证健康。

首片不把现有 `self_update` 的原位安装包装成可回退更新。

新事务带独立 protocol/transaction ID，不能进入旧 `SelfManagement.start` 的“配置指纹相同就 completed”分支。既有 pending restart 计划需重新校验私有目标与 base 后交给新事务路径；旧 applying restart 只能标 interrupted，没有新协议的健康证据就不能补称成功。旧进程内 configure 历史按原语义显示。CLI `service` 命令在无事务时保留 accepted 语义；有未解决事务时，install/start/stop/restart 等冲突写操作明确 transaction_in_progress，status/print 可读。本轮不借 raw service 命令在事务中插入另一位控制者；直接外部 systemctl 或安装修改按外部干预核对，不宣称能强制隔离它们。

## 当前代码证据

| 现有位置 | 对首片的影响 |
| --- | --- |
| `management.py:stage/start/_apply` | 私有快照没有完整 fsync 协议；扫描最近 100 条不能跨进程互斥；重启 accepted 后直接 completed；启动时配置相同会推断 completed，不能沿用到新事务 |
| `service.py:UserService.action` | Linux `--no-block` 仅提交操作；已有 `render/_arguments/quote_unit` 和有限 `run_process` 可复用，不再造通用命令执行器 |
| `__main__.py:main` | 先构造 ConfigFile；目标配置损坏时会在恢复入口之前失败。恢复子命令与 serve 的 boot guard 必须移到 ConfigFile/Store 构造前 |
| `runtime.py:startup` 与 `server.py:lifespan` | 会立即读扩展、启动通知／提醒、唤醒 pending、加载插件并启动各集成；必须拆出被动准备与提交后的激活 |
| `runtime.py:_wake_main`、`team.py:_deliver_report` | 内部唤醒不都受 configuration_lock 约束，必须检查明确的维护准入状态；不能把锁是否 locked 当作禁止执行标记 |
| `runtime.py:busy`、`_session_finalizers` | busy 之外还有 driver 最后通知和报告交接；首片复用真实拥有者，不用 session.status 或最近事件猜空闲 |
| `channels/manager.py:_outbox/start` | 发送前有独立 sending claim；启动将旧 sending 标 unknown。等待全部 owner 自然空闲后原子关门并确认 drained，随后停止新 claim；不能关闭后盲重发 |
| `notifications.py:_send` 与 `reminders.py:_fire` | 新通知、提醒会产生外部效果或新的宿主事件，probation 不启动，drained 后停止领取新的发送；waiting 仍可正常领取 |
| `store.py:Store.__init__/instance_id` | 构造会建表、索引与迁移；已有 store_instance_v1 可作资料库身份，不能把构造当无副作用预检 |

根任务已修复 2.14 的进程内 apply/rollback 强拥有与 lifespan 关闭屏障；首片保留这些路径，不重新实现一遍。

## 能力判定与旧安装

新增 `UserService.transaction_capability()`，返回固定 capability/reason 与有效安装摘要。只有全部成立才接受事务：

1. Linux、当前用户 systemd manager 可用、已安装并正在使用受支持的 MuLi unit；不是任意独立 `serve` 进程，也不是第三方 wrapper。
2. 识别服务文件、有效 drop-in、ExecStart、配置与 data_dir；核对当前 InvocationID、MainPID 与运行进程身份。首片只支持已测试的标准生成格式；自定义 ExecStart、未知 drop-in、并行 worker、不同 data_dir 或无法证明入口一致时拒绝。仅凭 `muli.service` 这个名字不够。
3. 解释器／安装摘要与运行实例一致，具备同一管理协议。当前 unit 原本的 `python -m muli --config ... --data-dir ... serve` 可继续使用，不需切换部署路径或改 uv receipt。是否为 editable 本身不是拒绝条件；依赖当前工作目录才能导入、实际模块来源无法识别、入口或协议不一致才拒绝。控制者与新实例均回报同一安装摘要；发现事务期间入口／安装外部变化即停止推进。首片不安装程序，也不阻止外部管理器修改安装。
4. 可创建并核验独立控制者的资源硬限；未满足时保持旧服务并给出明确原因，不静默创建系统资源配置。控制者使用单独 transient service 的 MemoryMax、MemorySwapMax、CPUQuota、TasksMax、RuntimeMaxSec 等限制；不要求现有应用新增 slice 模板。本机研发验证仍走既有 check_local，不拿生产 capability 代替研发 guard。
5. 使用已验证的本地文件系统、目录 fsync 与 flock；未知网络文件系统等语义不清的布局不给跨进程持久保证。记录／配置路径由已有参数发现，不硬编码 HOME 或安装目录。

首片不安装／修改真实 unit，也不新增 slice 依赖。已有可核验的标准 unit 运行本版本 CLI 时，由 serve 启动前的 bootstrap/lease 识别事务，继续沿用它的服务入口。独立控制者的硬限制只约束控制者，不把两个 sibling unit 各自的 MemoryMax 描述成共同内存预算；研发检查继续受现有 shared slice 的总额限制。

## 控制者与文件权威

新增轻量 `management_transaction.py`，只使用配置 schema、标准库和已有 process runner；导入／运行控制者时不构造 Runtime、Store、插件或任何集成。

每个安装范围使用一个固定的私有恢复根和**不替换的 journal lock 文件**。安装 identity 来源为规范化 config/data/unit 定位的摘要；controller 的 systemd unit 名也由该 identity 产生，不接受调用方任意指定服务名。journal lock 与实例 writer lease 是两个不同的固定 inode：前者串行事务阶段，后者按规范化资料库路径排它，不能包含 config/unit 差异而容许两个配置同时打开同一库。控制者持 journal lock 期间可以等待旧 writer 释放 lease；主服务不在持 writer lease 时阻塞等待 journal lock，只做非阻塞准备或无锁原子读取，避免锁序死锁。

建议布局：

```text
data_dir/management/transaction/
  lock                       # 固定 inode；fcntl.flock，fd不传给子进程
  state.json                 # 唯一权威当前记录，含 generation / revision
  <generation>-base.json     # 原文件精确字节
  <generation>-target.json   # 校验过的候选配置
  <generation>-cancel.json   # 仅取消请求，不能提交或改目标
```

目录 0700、文件 0600，固定格式／版本、单文件大小与总保留数有界。旧快照保存精确原字节和语义指纹；候选同时保存精确字节摘要与 Settings 指纹。控制者接管必须核验两类摘要，不能仅解析后等价就覆盖人工编辑。所有读取拒绝非普通文件，复用已有非阻塞、不跟随链接与前后身份检查思路。

持久写顺序：同目录新文件 → flush → fsync(file) → 原子 replace → fsync(parent)。快照及目录先落盘，再把引用它们的 prepared 记录落盘；发生任何 fsync/replace 错误都不得前进阶段。原子可见不等于持久；不能只复用 `_write_private` 目前“文件 fsync 后 replace、未同步父目录”的保证。

持有 flock 的控制者是唯一阶段写入者。阶段更新带 expected generation/revision，任何旧控制者／旧 observer 都不能推进新代。控制者运行期间网页取消只写有界、含事务 ID/generation 的取消请求；进入 activating 之后它只取消观察，不中断确认／回退。prepared 阶段无控制者时，cancel/recover 先取得同一锁再处理。锁文件不 unlink、不 replace；PID 数字不作为互斥依据。

`state.json` 至少包含协议版本、事务／计划 ID、action、phase、revision、原发起 session ID、安装／服务摘要、base/target 原字节及语义摘要、Store instance/main_session、old/new InvocationID、nonce 摘要／私有 challenge、deadline、activation intent、回退是否已尝试、健康证据与固定结果码。密钥、完整服务环境、配置正文、nonce 不进入公开对象或事件。

SQLite management 行是文件状态的显示副本：主进程读取已经持久的 revision，在一次 Store 事务内写 management 对象与对应事件；记录 projected_revision 防重复。控制者从不为“显示进度”在主服务之外打开同一 Store。不能把文件 replace 与 SQLite put 声称为一个事务。

准备者仅在尚无控制者时短暂持同一锁创建 prepared 记录，释放锁后才请求 manager 启动 helper。启动失败保留可辨认的 prepared/launch_failed，不删除恢复材料，也不假称已经切换。创建目录和最初 lock inode 同样纳入父目录同步。首片要求已有可读取、校验通过的普通配置文件；缺失配置返回 configuration_missing，不把 ConfigFile 的内存默认值当成原文件字节。以后若支持“原文件不存在”，必须另加缺失标记与恢复 unlink/fsync 协议，不能悄悄创建一份默认文件充当回退。

prepare 必须在持锁后读取当前记录：任一未解决代（含 prepared、draining、recovery_required、committed 但尚未放行／受阻）均拒绝新代，只有同 request ID 和同目标的重放返回原 accepted 摘要。advance CAS 不能代替这条入口互斥。终态覆盖／清理只在锁内、前控制者已交出所有权、旧 boot permit 不再在途时进行；新 prepare 由持有 writer lease 的主实例发起，并先确认旧终态完整 revision 已投影到同一 Store。未投影或无法确认时不覆盖唯一 state，先恢复投影。引用中的快照永不按保留数清掉。

## 建议窄接口

第一块已实现的调用边界为：

```python
with TransactionJournal(root, database, limits) as journal:
    with journal.writer_lease() as writer, journal.locked() as control:
        record = control.prepare(
            request_id=request_id, action=action, identity=identity,
            base=base_bytes, target=target_bytes, deadline=deadline,
            writer=writer, projected=prior_projection,
        )
    with journal.locked() as control:
        controller = TransactionController(control, backend, clock)
        record = await controller.step()
```

writer 与 journal 锁均为非阻塞获取；准备者原本拥有的 writer lease 不因本片的 helper 自动转交。`control.read()` 做接管同步，`control.advance(expected, transition)` 是持锁 CAS。生产端不能按示例脱离现有实例 lease 再临时打开第二个 Store。后续 Linux backend、boot guard 与执行准入仍是设计签名：

```python
class LinuxTransactionBackend:
    async def capability(self) -> TransactionCapability: ...
    async def launch_controller(self, identity: TransactionIdentity) -> Accepted: ...
    async def observe_service(self) -> ServiceObservation: ...
    async def stop(self, expected: ServiceIdentity) -> OperationAccepted: ...
    async def start(self, expected: ServiceIdentity) -> OperationAccepted: ...

class BootGuard:
    def inspect_before_config(self, paths: ServicePaths) -> BootPermit: ...

class MaintenanceAdmission:
    async def request_drain(self, expected: TransactionIdentity) -> DrainObservation: ...
    def observation(self, expected: TransactionIdentity) -> DrainObservation: ...
    async def resume_unchanged(self, expected: TransactionIdentity) -> None: ...
    async def activate_decided(self, expected: TransactionIdentity, role: Role) -> ActivationReceipt: ...
```

记录与 DTO 使用明确类型／Literal／严格 Pydantic schema，拒绝未知协议和未知阶段；不是一组任意 dict 状态。`advance` 是持锁、校验旧阶段与 revision、持久新记录的唯一入口。服务操作沿用 argv 形式的有限 run_process，输出只作内部固定原因判定，不公开 stderr。

## 一次事务的握手

1. 应用校验候选并采集 unit、实际绑定地址、安装、config/data/Store/main 身份；在配置锁内复核 base 与当前设置一致，短暂取得 journal 锁落 prepared。返回同一事务的 accepted 摘要；重复请求以 plan/request ID 幂等，不创建第二个并行事务。控制者接管后才成为阶段唯一写者，主进程只投影和处理受限命令。
2. 控制者确认自身硬限、持锁身份和剩余总时限，先落 draining intent，再用私有 nonce 请求旧实例检查自然空闲。请求快速返回 waiting/drained；waiting 时原服务继续执行，不承诺最终能自然空闲。每次请求只同步核对实际 owner，不用一条长 HTTP 请求拥有整个等待，也不另造 drain task。
3. 排空到达安全边界后，旧实例确认配置／启动身份仍匹配并关闭准入，给出带本次 boot 身份的 receipt。只有当前运行族群、finalizer、handoff 与已领取发送均收尾，receipt 才是 drained。正常路径须确认发起管理工具的结果已经持久化；若先前进程已硬退出，只能保留其 unknown 历史，不凭事务结果伪造原工具回执。
4. 控制者落 stop intent，停止旧 unit，观察 job、InvocationID、cgroup 与 writer lease；确认旧 writer 已退出后，持久目标配置与对应 boot permit，再启动同一 unit。stop/start 的 accepted 与实际退出／就绪分开记录。旧进程自然崩溃、目标自动重启或 controller 重入都先观察事实再决定下一步。
5. 新实例启动前的 guard 验证本代许可，进入 probation。正确 nonce 的 HTTP 本地健康证明通过后，控制者持久 committed，再发幂等 activate 提示或让 probation 的有界观察任务读取到提交；实际放行另有 receipt。
6. 放行确认持久后，主实例在一个 DB 事务内投影最终管理对象与事件。网页或原发起会话离线不影响推进；下一次读取按 revision 补投影。未提交失败走一次回退；提交后不做数据库或外部效果回滚。

内部接口建议为固定的 `POST /api/internal/management/drain`、`POST .../resume`、`GET .../readiness`、`POST .../activate`，请求只带事务/generation/预期 boot，不接受调用方路径、服务名、配置正文或任意命令。所有调用均验证当前私有 nonce、journal 阶段与实例归属；保留 Host/Origin/请求体上限，只对这些精确路径使用 nonce 身份而非 Cookie。resume 仅接受 journal 已回 prepared/中止且 base 未切换；activate 在 committed 时只允许 target、rolled_back 时只允许 base，分别校验该代独立 challenge、对应配置和当前 boot。两条路径均须实际放行 receipt 后才能 resolved；不能靠调用者自报阶段开门。

从 prepared 到本代解决，普通配置写入、进程内 SelfManagement._apply/apply_configuration、显式／自动扩展发布和第二项管理操作均返回固定 transaction_in_progress；不在 pending/draining/probation 中热重建组件。检查在既有 configuration_lock 内并在任何 close/start/最终发布之前执行；不只给 HTTP 路由加表层判断。自动 watcher 保持 dirty，事务解除后再按既有安全边界重试。无未解决事务时，普通 self_configure 与设置 API 保留既有热改。观察、可持久输入与取消观察继续可用。

硬退出后的 journal 尚未终结时，正常服务启动先执行 bootstrap；可核验的同代恢复启动保持 probation，若 controller 不在则请求同名 helper 接管。helper 名绑定安装和 generation，重复 ensure 观察现有单元，不创建另一位控制者。

## 独立控制者寿命与硬退出

控制者通过 `systemd-run --user` 建立独立 `.service`，由用户 manager 作父；不使用 `--scope`、shell 后台、主服务中的普通 asyncio/subprocess task，也不设置使其随 muli.service 停止的 PartOf/BindsTo 关系。使用同一绝对解释器执行轻量 `muli.management_controller`，给私有事务定位，不在 argv/env 放配置密钥或 nonce。

使用 Type=exec、KillMode=control-group、明确 UMask、硬 RuntimeMaxSec/TimeoutStopSec、控制者内存／swap／Tasks／CPU 上限，并验证实际 cgroup。systemd-run 的同步返回只确认 helper 已 exec；随后仍需私有 controller-ready 事实或阶段推进确认。`--no-block` 若保留，只能返回 queued，不能直接确认交接。

控制者使用有限 `Restart=on-failure` 与 StartLimit；每次重入先取得 flock、读已持久 deadline/尝试计数，再核对实际服务、配置和 nonce。RuntimeMaxSec 可能随一次 helper 重启重新计时，因此总事务时限必须来自记录，不能靠每次重新给 150 秒无限延长。有效时限与预算在 prepare 固定，候选配置中的新预算不影响本次事务；预留有限回退确认时间。同 boot 使用 monotonic 截止，跨 boot 结合持久 wall 截止且不扩展原预算，时钟倒拨／无法确认剩余额度时明确 recovery_required。停旧或启动目标的 intent 先落盘；resume 先观察现存 job/InvocationID，不盲发第二次 stop/restart。

同一 user manager 仍存活时，控制者 SIGKILL/OOM 后可由有限重启接管；主服务意外重新启动也先检查 journal 并保持维护门。用户 manager／机器整体重启会丢失 transient controller；只有已启用服务启动时 boot guard，或显式 `management recover`，才会重新启动接管。**未启用开机服务且无人调用恢复入口时，不承诺机器断电后自动恢复。** 不新增永久轮询守护进程，也不启用 linger。

重复外部取消、页面断开只停止观察；独立控制者继续到 committed/rolled_back/recovery_required。有限重启耗尽、锁一直被未知持有者占用、截止时间不足以证明旧实例健康时明确 recovery_required，不无限重启。

## 执行准入与排空

维护门是独立于 `_closing` 与 configuration_lock 的明确状态。旧实例准备期间继续工作；接近空闲时先取得配置锁，复核 base、实际拥有者与服务摘要，再关闭**新执行**准入。从关门到确认门状态无 await。锁随后释放，不能持锁等待现有工作结束。

首片的保守实现是在当前运行族群已结束后才建立排空门，避免关门后让旧父任务等待一个禁止启动的子任务。需显式等待／核对 runtime.tasks、queued/running agent、unfinished workflow、`_session_finalizers`、team handoff、正在发送的 channel/desktop notification；不能只看 bool(runtime.tasks)。pending ingress、pending outbox、未读通知和未来 reminder 不是排空必须清零的对象。

准入检查至少覆盖 Runtime.start/submit 的启动分支、`_launch/_wake_main`、team scheduler、workflow launch、attention、reminder、channel outbox claim、桌面通知发送 claim，以及宿主扩展 watcher 的配置换代。维护中的主人输入仍按已有容量／幂等规则持久接收，但不启动模型；不能可靠持久接收的入口不给成功 ACK。停止外发领取后，已有 sending 在原超时内完成收尾；不会先 close/cancel 发送再把它当作排空成功。

各组件提供小型 `pause_admission/idle_receipt/resume` 或等价宿主回调，供 server 统一聚合，不让事务模块直接遍历其私有任务集合。报告交接可保存为 pending ingress；维护解除后统一唤醒，不能丢掉 `_wake_main` 的信号。drain receipt 包含同一 boot/nonce、所有纳入的 owner 计数和收尾完成事实，而非一条“idle”字符串。

排空超时：原服务与原配置未停止／未替换，先持久回到 prepared，再解除本代维护门；总 deadline 尚有余量时，私有目标可等待下一次既有 idle 事件重排，不能因重排延长 deadline。耗尽时确认原实例已放行，记录 aborted/drain_timeout 后解决本代，保留结果；无法确认则 recovery_required。没有定时强杀用户运行。外部 stop/restart 另有现有明确语义，不伪装成这条安全排空路径。

## 被动启动、健康证明与提交

`serve` 在 ConfigFile/Store 前取得实例 writer lease，并检查 journal/安装摘要。发现 verifying/rolling_back 等有效记录时只取得对应 BootPermit，未知或损坏状态 fail closed；不能因为正常启动失败就跳过 journal。lease 从 Store 打开前持有到所有 DB writer 关闭后；controller 激活窗口先证明旧 cgroup 空且能独占该 lease，发布配置与启动许可后才释放给新进程。它是本应用合作协议，不能阻止无视锁的外部程序。

新实例 probation 仅构造本地核心、恢复持久事实、确认 Store instance/main_session 与支持的同版本格式；不加载任意插件，不启动 MCP、模型、渠道发送／轮询、通知、提醒、采集、onboarding 或 pending wake。channels 的 sending→unknown 恢复等纯本地步骤要从 start 拆成被动恢复，不能为执行恢复而顺带启动 sender。外部扩展能力只标为未激活／未验证。

probation 中的网页只提供必要只读状态和能可靠持久接收的主人输入；直接外部工具／插件调用、审批放行、测试连接、采集及配置写入同样受准入门约束。仅不调用 Runtime.startup 还不够：所有具有外部效果的 API 都必须经宿主门，避免从旁路触发。首片不承诺把内存中的桌面通知队列迁移为持久队列或实现外部效果 exactly-once；已持久 notice/ingress 保留，已发送但无确认的事实继续按现有 unknown 语义处理。

新增有界只读 `/api/internal/management/readiness`，由本代私有随机 nonce 认证，且只在有效 boot permit 存在时可用；使用 constant-time 比较，与常规 Cookie 登录分离。nonce 不放查询参数、普通日志或公开状态。返回 transaction/generation、intent challenge、本次 boot nonce、InvocationID/PID、实际 version/interpreter 摘要、配置摘要、Store instance、main_session、恢复完成、本地核心就绪及 maintenance=true。

控制者使用已有 HTTPX、trust_env=False、禁止重定向、单连接、小响应上限、短单次超时和整体确认期限，访问候选实际绑定地址（wildcard 转本机 loopback；其它只允许已验证本机 IP）。成功响应同时核对最新 systemd MainPID/InvocationID 与 `/proc` start identity、资料库和配置摘要；迟到 nonce、旧进程 200、抢占端口的其它服务、配置相同但错误 data_dir 都失败。不能只在 lifespan 写 ready 文件：锁定 Uvicorn 源码先等 lifespan.startup，再执行 socket bind；一次正确 nonce 的 HTTP 响应才同时证明监听入口可达。

目标健康后：先持久 committed，再允许目标按同一 generation 激活。目标可通过单个只读 journal 观察任务或受认证激活通知得知提交；该任务只在 probation 期间存在、有总期限，不是永久守护进程。激活任务由宿主强拥有、只执行一次；重复通知返回当前事实。收到 committed 但还未解除维护门时进程死亡，重启继续该提交代的本地确认与激活，不倒退成未提交配置。

对外 completed 要有持久 committed 与执行门已放行的确认。ActivationReceipt 区分 released、released_degraded 与 blocked；可选组件启动失败独立标 degraded，核心激活失败则保持门关闭并记 activation_failed，不能只凭 committed 对外 completed。提交后的失败不得自动把已发生的外部效果“回滚”。本地健康确认不包含真实 MCP、模型、渠道账号或任意插件工作能力。

## 失败回退与重入表

| 持久阶段 | 接管时先核对的事实 | 最小重入行为 |
| --- | --- | --- |
| preparing/prepared | base 快照／当前原文件／安装摘要；原服务身份 | 没有切换；完成有限准备或给 stale/prepare_failed；不重启正在工作的旧实例 |
| draining | 原配置仍为 base；旧服务与维护门身份 | 继续有限排空；超时回 prepared 并放行旧服务 |
| activating | stop/start intent、实际 job、cgroup、writer lease、当前文件 raw digest | 只有确认旧 writer 已退出才能替换；若已是 target 不重复替换；其它摘要不覆盖 |
| verifying | 当前目标摘要、目标 InvocationID、健康 nonce、deadline | 存在同一目标则继续确认；失败进入一次 rolling_back，不盲目多次启动目标 |
| rolling_back | rollback_attempted、目标已退出、当前文件属于本事务、旧快照 | 停目标，确认空 cgroup/lease后恢复精确原文件，生成独立回退 nonce，启动旧服务并重新确认 |
| committed | 已提交摘要、当前实例与放行确认 | 不恢复数据库或回退配置；续接该代激活／投影，异常标 degraded |
| rolled_back | 原配置、新的 base boot、独立回退 challenge 与放行证据 | 先完成 base 激活和放行 receipt 再 resolved；原管理请求仍 failed，之后投影一次原发起会话 |
| aborted | 从未替换配置／停止旧服务，已解除本代维护门 | 取消／准备失败／排空超时的安全终态；只投影结果，不重启 |
| recovery_required | 证据不足或外部修改 | 已关闭的维护门保持；未关门时旧服务保持原状。保留未解决代、配置／日志，不覆盖、不无限重启 |

失败回退只在尚未 committed 时自动发生，且本事务最多一次。当前配置必须是本事务写入的 target 精确摘要或尚未修改的 base；人工改动、路径替换、安装／服务描述变化、无法确认旧 writer 退出均 recovery_required。若恢复 fsync 失败，即使新字节已可见也不能声称回退成功。

同版本路径不执行 schema 降级或数据库备份恢复；继续使用包含最新 ingress/outbox/tool unknown 的原库。原始错误与回退结果分开记录，恢复原文件本身不是 rolled_back 的充分条件。普通重启也保存 base/target（相同）和两次健康 challenge，不能用“没有配置变化”跳过交接协议。

## 文件范围与分片顺序

| 文件 | 允许的最小职责 |
| --- | --- |
| 新 `management_transaction.py` | 严格协议、私有持久日志、短状态摘要、锁与同代 CAS；不导入 Runtime |
| 新 `management_controller.py` | Linux 独立控制者状态机、观察／激活／确认／一次回退、恢复 CLI 主函数 |
| `service.py` 或新 `platforms/linux_transaction.py` | 受控 unit 安装身份、transient helper argv、有限 manager 观察、cgroup 与资源核验；Linux 专用 import/lease 不破坏 Windows/macOS 原启动与服务后端 |
| 新 `maintenance.py` + `runtime.py` 必要行 | 明确准入门、writer lease、被动 core prepare 与提交后激活窄回调 |
| `server.py` | 接入根已有配置关闭屏障；被动 lifespan／一次性激活、nonce readiness/drain、事务 API；进程内配置入口保留 |
| `management.py` | stage 模式选择、accepted/最终结果分离、文件 revision 投影与 target 保留；旧 plans 历史兼容 |
| `__main__.py` | 恢复子命令、ConfigFile 前 boot guard/lease、同版本原服务入口兼容 |
| `team.py/workflows.py/channels/manager.py/notifications.py/reminders.py` 等 | 只补必要准入／排空小接口，不重写执行器、重试策略或 channel 协议 |
| `config.py` | 单一 ManagementTransactionSettings 字段；时限／文件／controller 预算全部 typed、严格上界，根任务协调归属 |
| `tests/test_management_transaction.py`、`test_management_controller.py`、`test_maintenance.py` | 临时目录／SQLite、假服务／时钟、失效注入与真实 Runtime 入口回归 |
| `docs/self-management.md`、`operations.md`、服务说明 | capability、模式、accepted/committed/rolled_back、恢复 CLI、未验证边界 |

建议分成可独立审查的三个实现块：先纯日志／控制者 fake backend，再维护门／被动启动与 nonce 证明，最后现有管理工具/API/文档接线。三个块都通过才宣传 Linux 同版本可恢复重启；只合纯状态机不能称成品。

## 有限验证与不作的承诺

先经统一 guard 做窄单测：每次 fsync/replace 前后崩溃重入、两把锁的独立归属／锁序、prepare 不覆盖未解决代、旧终态未投影不换代、PID复用、record unknown/corrupt、外部手工配置变更、启动 accepted 但无健康、假／旧 nonce、旧 writer 残留、一次回退及回退失败、committed 后未激活死亡。再用真实 Runtime 与 fake 模型／服务证明新输入持久不重复、`_wake_main`/team/workflow 不绕门、driver finalizer 与 sending 尚在时不 stop、prepared/draining/probation 中 settings/apply/extensions 不能绕门启动组件、probation 没有外部组件调用，管理工具结果先落盘。最后仅用受 guard 的有限假服务子进程证明控制者与主进程分别退出后的接管。

第一块仅执行两个新增 journal／controller 测试文件及对应静态检查，不执行上述真实 Runtime／子进程验收；不启动真实用户 service、设备、外部 MCP 或模型，不下载或修改系统设置。所有检查在目标 worktree 使用根 check_local，单次 1 GiB／合计 2 GiB／swap 0／单 CPU／180 秒，与其它 agent 串行。没有真实 Linux 服务恢复证据前，只报告替身验证，不承诺真实停机时间；Windows/macOS、A/B 版本安装与持久格式升级另片交付。

## 查证来源

已通过 Context7 查阅 systemd、Python 3.12 标准库与 Uvicorn，并核对锁定环境 `uvicorn/server.py:startup` 的顺序；这里的接口是依据来源推导的设计，尚非已交付能力。

- [systemd-run](https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html)：transient service 由 manager 管理；scope 的父进程／环境不同；Type=exec 与 no-block 的事实边界。
- [systemd.service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)：有限 Restart、RuntimeMaxSec 及停止后的行为；不将 exec 当 readiness。
- [Python os](https://docs.python.org/3.12/library/os.html#os.fsync)、[fsync(2)](https://man7.org/linux/man-pages/man2/fsync.2.html)：flush、文件与父目录同步，错误必须阻止阶段前进。
- [Python fcntl](https://docs.python.org/3.12/library/fcntl.html#fcntl.flock)、[flock(2)](https://man7.org/linux/man-pages/man2/flock.2.html)：固定 fd 所有权、关闭后释放、advisory 边界。
- [Uvicorn lifespan](https://uvicorn.dev/concepts/lifespan/)：每实例生命周期；具体 bind 在 lifespan 后的结论另由本地锁定源码确认。
