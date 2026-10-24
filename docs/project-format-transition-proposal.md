# 项目复核的格式 1→2 与托管升级前置（待审、未实现）

2026-10-24 发布基线整合：本开发树合并已发布 v2.45.0 的文档检查点 `bcf9aad`，该次合并仅引入文档和测试，没有生产代码改动。合并后的记忆评估、任务续接及项目格式/运行时定向检查实收 **38 passed、6 deselected**（pytest 1.58 秒；守卫 2.331 秒、117.8 MiB、swap 0、exit 0）。这不是新一轮全量后端验证；六项格式 2 正向仍未满足前置，控制权换代和格式迁移仍未发行。

本树 WIP 最终检查补记：全仓 Ruff 通过；新 producer 测试的格式首检 exit 1 保留，仅格式化该文件后 382 文件格式通过。普通收件/API/运行时质量/系统事件与真实格式 1 拒写、普通来源五文件 66 passed（pytest 1.59 秒；守卫 2.403 秒、149.5 MiB、swap 0、exit 0）。与既有 110 项分开记录；九项 producer、六项 Runtime 格式 2 正向仍未具备前置，不改常量或伪造标记以取得通过。

2026-10-24。项目输入的宿主 `project_review` 证明改变了 objects/ingress 的授权与执行含义：旧 Runtime 会忽略它，并可能在工具批后把手机主人输入认领进同一 run，重新取得渠道路由。因此，这不是能沿用完整格式 1 的可选提示字段。项目候选暂不发行、激活或写入真实格式 1；拒绝旧槽只是准入保护，**尚不等于已有托管用户能完成安全更新**。

本文只提出一个已知的 `project_review_1_to_2` 转换。复用现有格式证据、构造锁、controller lock、writer lease、精确文件 CAS、journal 与维护生命周期，不建设通用迁移器，不恢复旧数据库，不增加业务表、worker、轮询或依赖。本文没有运行测试，也没有迁移用户数据。

## 当前代码能证明的事

- `format_admission.CURRENT_FORMAT_STATE` 仍是 metadata 1、七域均 1；`compatibility.json` 的普通写合同也均为 1。真实 `Store.open_runtime` 只给本次 O_EXCL 独占创建、完整初始化并验证的新库发布声明。已有 legacy 库不补证。
- `require_current_format` 精确比较完整状态；`Store._admit_format/revalidate_format` 不把未知/坏 marker 降级成 legacy。单把常量改成 2 会先拒绝现存格式 1，不会替它迁移。
- `format_evidence.read_format_evidence` 有界读取真实 marker、主身份及 schema；不读取项目正文，也不证明在线 writer 已停止。它要求连接不在事务中，不能直接塞到外层 `BEGIN IMMEDIATE` 内。
- `StoreConstructionLock` 是短构造锁。托管的 `BootPermit/ManagedBootPermit` 才持有数据库 writer lease 到所有 writer 关闭之后；迁移必须取得后者，不能只拿构造锁。
- `assess_compatibility` 要求双方都能读当前完整状态且双方 writes 都等于当前状态。现有 switch/adopt/configure/restart 均不授权 1→2，应继续拒绝 `write_format_change`，不能为新功能放宽这个判定。
- `TransactionRecordV2` 只有 base/target 和一个冻结的 `observed_format`。`managed_host._record`、`ManagedBootPermit._format`、`verify_running_binding` 均核它。resolved 的旧记录也仍绑定格式 1。把数据库先升 2、留下旧 journal，连正常重开都会被拒绝。
- 稳定 `launcher.py` 由 anchor 精确绑定，recover 按 `state.json.identity.deployment` 找不可变 anchor 的 `controller_slot`，不按当前 selector 选 controller。普通应用 switch 不会更新这个固定 controller。

## 首片立即采用的准入门

共享同步函数为 `muli.project_review.require_project_review_format(store)`，Runtime 只作同名委托。每次用原有有界 reader 重读实际证据，再核 Store 已接纳状态与身份；缺证据或不是 objects=2、ingress=2、其余当前合同则 `FormatAdmissionError("project_review_format_required")`，不写任何升级标记。

项目 configure、低层 settings replace、来源 start、项目 proof commit、Runtime/Conversation 项目 enqueue 均在产生项目效果前复用此门。恢复 claim 先按原有界 limit/目的地和完整规范 marker 构出实际批次，generic 与项目、不同项目证明均分开；批内有 marker 时在任何归档 DML 前重读格式，再沿原同一事务归档/标 delivered。普通事项在前可以独立归档，项目在前被拒则整批原样保留；不能在外层事务内启动现 evidence reader，也不能先认领后事后拒绝。generic 与解绑不受此新增门影响。CURRENT 仍为 1 时，项目正向用例会明确失败；不得 mock 门、手工伪造格式 2 或跳过用例取绿。

发行时应同步真实新库声明与实际发行 asset：objects=2、ingress=2，其余五域保持 1，metadata 版本保持 1。格式 2 reader 必须接受迁移前仍保留的旧对象/输入形状，同时对新增宿主 marker 执行新隔离合同。新库可直接发布这一真实完整状态；它不替代下面的已有库升级。

## 为什么必须两阶段

正常进程的来源是 **source1**。数据库提交格式 2 后，失败时可启动的必须是已经核验、实现格式 2 的 **fallback2**，不是 source1。候选是 **candidate2**。三者不能冒充现有两角色 switch 的同一 base。

先交付一个仍读写格式 1 的 bridge/controller 安装，项目功能关闭；它能识别下面两种窄动作及其拒绝/恢复规则。先用原有 same-format 路径把实际运行应用换到 bridge，验证主身份、配置、pending 输入与普通工作行为保留。**这一步不会声称固定 controller 已更新**，也不会写项目 marker。

然后完成固定控制权换代，真实确认新的 controller 已被 anchor 选中；最后才准备 candidate2/fallback2 并执行项目格式转换。每个阶段独立保留原 deadline 与回退保留，不能把整个过程伪装成一次原 switch 或让恢复无限续期。

## 阶段一：固定 controller 的受控换代

选择保留现有 protocol 2 路由外壳与同一个 `state.json`，增加严格分派的固定动作 `project_controller_transfer`。它不是旧 `TransactionRecordV2.switch` 的别名；旧完整 reader 不认识该 action 时必须拒绝。新记录只需冻结旧/新 anchor、真实 origin 绑定、新 anchor 下可启动的 bridge 主/备绑定、主身份、格式 1、精确文件快照和 nonce/deadline。具体 schema 和效果实现均尚未编写。

1. 实际运行 bridge 在现有 configuration lock 下验证旧 resolved record 已精确投影、真实进程/boot、旧 anchor/controller、配置、selector、unit 与完整格式 1。按现有本地 slot 登记核验新的 controller 和 bridge 恢复材料，不从候选元数据推测它们可用。
2. 发布一个**新的内容寻址 anchor**，旧 anchor 原样保留；新 anchor 的 config/data/db/unit/journal 路径、限制和 `launcher` 精确指纹不变。更换已核新 controller slot，并用原 `ManagedUserService.render` 按其实际 lexical Python 重新生成、冻结 `managed_unit`。unit 的 ExecStart 本来就引用 controller 的解释器，新旧 retained venv 不同就不能声称 unit bytes 不变。不原地修改旧 anchor，也不替换 launcher.py。这复用稳定 launcher 已有的 recover 路由能力，避免 launcher 文件与 anchor 同时换代的另一组崩溃窗口。
3. 确认新 controller 的实际入口能读 protocol 2 普通记录和这个窄动作，且新 bridge 的启动/维护路径支持该动作。此能力来自发行代码、对应离线进程验收及实际 slot 文件核验；现有 `handoff_protocol=2` 声明本身不证明它。
   当前已审 standalone launcher 的 recover 分支不解释 action，只核 protocol/generation/revision 后按 record 的 deployment 找 controller 并 exec；必须核**实际 retained launcher 指纹及对应代码能力**，并用这份精确 bytes 做新动作的离线真实 exec 验收。不能外推任意历史 launcher；不具备这一路由能力时在 transfer 发布之前拒绝，另审 launcher 升级，不在本动作偷偷替换它。
4. 原 controller lock 下先持久发布 transfer record，`identity.deployment` 从首次发布就指向新 anchor，且 boot role 在停写前为空。这样旧稳定 launcher 的 recover 会核新 anchor 并执行新 controller；它不需要导入 selected 候选，也不依赖尚未修改的 selector。记录另有 frozen origin anchor/绑定，只供认证仍活着的旧 bridge 与 drain，不能把 origin 伪装成新 anchor 下的角色。
5. 只有新 controller 可在该代继续推进。bridge 的维护核验须新增窄 origin-transfer 分支：旧 permit 仍证明旧 anchor/运行安装，只有精确匹配此 transfer 的 origin、boot、nonce、主身份时可 drain；不能普遍允许 permit 与 anchor 不同。原协议记录保持原规则。
6. drain 完整 owners，停止原进程，确认实际 cgroup 空、取得原 writer lease 后，按现有三文件 CAS/耐久前缀切 selector 到新 anchor 的 bridge 角色，再切新 managed_unit。配置不变也必须确认耐久；origin 冻结的旧 unit 与新 anchor 角色的新 unit 分开核验。必要时有界 daemon-reload，并以 fresh loaded unit 属性复验，不能放宽 ManagedSystemdProbe。启动新 boot，核新 anchor/controller/selector/unit、格式 1、主身份与 paused 健康证明，再提交、激活及精确投影。

transfer 一旦持久发布，自动恢复只选择**新 anchor 下已冻结、能读新动作的 bridge 主/备**；不在失败时悄悄把固定控制权退给旧 controller，也不把仍指向旧 anchor 的 origin 当作已提交角色。若新 controller 材料损坏或期限耗尽，保持暂停/recovery_required，不能重捕获故障安装或退回普通启动。需要回旧 controller 时必须另作经新控制者认证的反向控制权换代，数据库仍为 1；这不是本次项目迁移的自动回退。

`JournalControl.read_current` 当前只按协议分派到原 `parse_record_v2`；必须新增按 action 严格解析的 typed 联合，旧动作仍用原模型，不能只凭 launcher 的 raw 路由说恢复已可达。`managed_journal._collect` 当前仅认识 base/target 快照并会回收旧代；origin/fallback 与旧/新 anchor 角色的全部必需快照必须在**本代**完整保存，纳入原 record/entry/临时文件预算与有限回收规则，不能只引用随后会被回收的前代 snapshots。

崩溃前缀：新 anchor/slot 快照已写但 transfer record 未发布，仍由原记录/旧 controller 管理，孤立材料不授予权限；record 发布后 selector 仍旧值，普通启动拒绝，recover 已能经不变 launcher 找新 controller；selector CAS/目录 fsync 丢返回，只接受本代冻结的旧→新前缀，耐久确认后推进；新 bridge 已启动但健康/激活返回丢失，只重观察原 boot/nonce，不重发未知 start 或重复激活。新 action 的宿主材料冻结、allowed/boot/maintenance/projection 分支及上述 origin 认证均是**必要但尚未实现的原语**，不能声称现 V2 已经覆盖。

## 阶段二：真实 managed1→2 转换

固定动作 `project_review_1_to_2` 继续使用唯一 journal、controller lock、writer lease 与原文件快照机制；不新增第二个权威“当前版本”文件。新增严格记录冻结：

- 真实 source1 的安装/兼容声明、配置/selector/unit、boot、完整格式与前代精确投影；source1 用于停写前认证及格式尚为 1 时的安全终止。
- candidate2 与 fallback2 各自实际登记且可启动的安装/声明、精确配置/selector/unit 与健康授权；均在新 anchor 下。两者都必须读写目标完整状态，完整配置/附件/原输入仍兼容。只有候选而无 fallback2 时，不允许停止现 writer 或改证据。
- 固定 source_format、target_format、迁移 ID/版本、Store/main 身份、DB inode、原 marker bytes 引用与目标规范 bytes、nonce、generation/revision、原期限。`observed_format` 仅表示已实际确认的状态，不能在 prepare 时先填 2。
- 供不变 launcher 使用的 base 是经过严格派生验证的字段：格式 1 时对应 source1，确认格式 2 后对应 frozen fallback2；target 始终对应 candidate2。这是一处由该窄动作明文规定的单向切换，不允许任意重写旧 V2 base 或 observed_format。完整 reader 必须同时核不可变三角色及当前阶段，不能只信路由外壳。

执行顺序：

1. 在新 controller/anchor 实际核验成功的前提下，用真实有界 evidence 确认 source_format=已认证完整 1；拒 legacy、未知/半写 marker、坏 schema/身份与非该固定转换。candidate2/fallback2 双方对目标 2 调用原 `assess_compatibility`；source1 对实际 1 自身核验。普通 1↔2 switch 仍拒绝，不能把这种分段校验用作普通切换的例外。
2. 先冻结全部材料并持久 prepared，后经原 Maintenance owner coverage 停止新执行/来源与发送。已认证 origin 可沿原合同继续持久接纳 owner 输入；不把暂停改成权限 403，不先 ACK 未落盘输入。关闭时等待已接纳 handler/Store writer 结算，之后到达的请求明确不可用，依渠道原重投合同处理。
3. 精确停止 source1，核进程/boot/cgroup，取得并持有同一数据库 writer lease；再取 StoreConstructionLock、钉住原 DB inode/父目录，禁止普通 Runtime、Memory 或 ChannelManager 构造。controller lock 一直保证本代唯一效果拥有者。
4. 通过专用无业务连接做有界真实格式/schema/主身份读取；核 marker 与 prepared 原始引用一致。读取器完成并退出自己的快照后才开始 `synchronous=FULL` 的 `BEGIN IMMEDIATE`。事务内再精确 CAS 原 marker、核固定身份，并在已有 objects 中写一个有限迁移 receipt（migration ID、generation、source/target 格式摘要与主身份），与**完整目标格式 marker 同一 SQLite 提交**。不改业务对象、ingress 行/state/seq、channel_inbound 回执、outbox attempt/unknown、记忆/附件或历史正文。已有 `validate_existing_schema` 只证明有界 schema 合同与主身份，不能写成已验证全库 payload；保留旧对象由格式 2 reader 在真实恢复/逐项读取时沿原规则校验，不另写全表扫描或 DDL。
5. COMMIT 返回后重新有界读真实证据及 receipt，确认仍是原 inode/身份、实际格式 2。然后耐久推进 journal 至已确认格式 2，并派生 base=fallback2、observed_format=2。在这一步之前任何新槽 boot role 都为空；不要把 DB 与文件 journal 说成同一原子事务。
6. 按现有 config→selector→unit 前缀安装 candidate2，写 start intent 后才启动。候选 `ManagedBootPermit/Store.open_existing` 再核 journal 该角色、真实完整格式 2、nonce/安装/配置/selector/unit/主身份；在 paused 状态完成核心恢复，不启动外部生产者或重排 unknown。
7. 健康成功后按既有提交→激活→精确投影释放。失败时在同一格式 2 数据库上停止 candidate2、沿冻结逆前缀装并启动 fallback2，使用独立回退 nonce/boot/健康证明；不启动 source1，不把 marker 降回 1，不恢复旧库。fallback2 也失败则 recovery_required，持久 pending/unknown 不动。

这里的 fallback2 是实际可运行的恢复材料，不是“manifest 写 2”、一份旧 DB 备份或新建空数据目录。必须有对应真实临时进程、同库恢复和故障注入证据后，才可将其纳入发行材料。

## COMMIT 不明与恢复矩阵

先重新取得 controller lock、真实空 cgroup/writer lease、构造锁及同 inode/主身份，再按当前 record 的固定 intent 读取实际格式和迁移 receipt。期限不延长，不以异常文字推测提交结果。

| 实际证据 | 允许的处理 |
| --- | --- |
| 完整 1，且没有本代 receipt | 尚未提交；只有原 record/marker CAS 仍成立且原期限允许时可执行原转换，否则恢复 source1 的格式 1 生命周期或保持明确暂停。不能启动候选 2。 |
| 完整 2，且本代精确 receipt 匹配 | 数据库提交已生效；确认耐久后推进同代 journal 的格式确认边界，再走 candidate2/fallback2。即使先前 COMMIT 抛错也不能反向降级。 |
| 2 与旧 journal 仍写“待转换” | 这是允许的两介质崩溃前缀，只有精确 receipt 能证明已生效；先确认并推进该代，不重新执行 SQL 转换，也不按旧 observed=1 启 source1。 |
| 1 却 journal 已确认 2，或 2 没有本代 receipt、receipt 错/重复/未来、未知格式、身份/inode 改变 | recovery_required；不补 receipt、不接纳另一代、不覆盖 marker、不猜哪个副本新。 |
| selector/config/unit 是本代已冻结安装前缀 | 耐久确认后只接续当前 intent；foreign bytes 拒绝覆盖。未知 start/activation 只重观察。 |

journal 记录的 receipt 只是定位引用，必须从实际 DB 有界重读；传入 DTO、Store 缓存格式或 helper exit=0 均不是提交证明。SQLite rollback 只回滚尚未提交的该次事务；绝不把已提交数据库还原到升级前快照。

## 最新入站与 A/B 的验收边界

输入保留靠同库，而不是先清空队列。暂停期间真实签名 owner 输入仍按原入口落 `channel_inbound`/ingress，保留原指纹、route、mode、seq 与回执；在关闭屏障前已返回成功的输入必须可由 candidate2 或 fallback2 恢复。工具尾部、pending 消息与 unknown 出站沿原恢复规则，不能因换槽重新发副作用。

不能承诺远端平台在停机期间必定重投尚未本地接受的数据。验收必须区分持久已接受、handler 正在结算、关闭后明确拒绝三种时点；不把 403/成功 ACK 用作维护暂停，不让 poll cursor 越过未持久接纳的消息。需要根按实际 ChannelManager 生命周期复核这些屏障，不能只查 ingress 总数。

格式 2 提交后，任一旧槽在真实格式入口/托管 boot 必须在业务恢复前拒绝；正在运行的旧进程不会因 marker 变化自动停写，所以停止、cgroup 空与独占 lease 是不可替代条件。之后 A/B 只在两个均读写 2 的真实角色间进行，失败仍用最新同一数据库，不回滚最新手机输入。

## 必要实现与最小验证顺序

目前本树 host/segment 是 WIP，格式门拒 1；本树没有可发布的格式迁移或控制权换代实现，不能标为已发布或已恢复。独立桥接树 d4b648c 的冻结合同与分片证据已同步至 [控制权换代合同](project-controller-transfer-contract.md)，仅复制文档；其 strict reader 与临时拒绝保护不等于本树已有 transfer 能力，也未交付完整换代或格式 2 迁移。

当前首片证据仍限于格式 1 拒写及不需要格式 2 的回归。根先实收 24 passed、6 deselected（pytest 0.53 秒；守卫 1.170 秒、85.6 MiB、swap 0、exit 0），ordinary→project 先独立归档普通事项的原红已转绿，C 独立源码后审未见剩余 P1/P2。Ruff 首检 exit 1 的两处闭包绑定与一处 import 排序修正后，根格式化九个相关文件（八个改变格式、一个保持），Ruff 九文件通过；七测试文件实收 **110 passed、6 deselected**（pytest 1.63 秒；守卫 2.306 秒、127.7 MiB、swap 0、exit 0）。110 已包含上述 24 核心和 86 普通回归，不与 24 重复累计。全 148 生产源 strict/no-incremental 实际 exit 0（守卫 8.365 秒、436.1 MiB、swap 0）。六项项目正向仍实际受格式 1 前置阻断，没有证明以下迁移或手机屏障通过；签名 WIP 检查点不开放格式 2。

1. 先独立实现 bridge 的严格 action 分派、origin-transfer 认证和新 anchor 接纳；普通 V1/V2 reader、same-format assess、不可变 anchor 不放宽。测试新 anchor 孤立前缀、record 已发/selector 未切、精确 recover 实际选新 controller、foreign anchor/材料拒绝、期限/nonce/单 lease 与丢返回。
2. 实现已知 1→2 的纯验证与 receipt/CAS 事务；真实新库 2、已认证 1 迁移、legacy/未知拒绝，保留实际旧对象/输入而非手工重造 payload。COMMIT 前/后异常、journal publish 前/后、fsync 不明和外来 marker/inode 必须逐点注入。
3. 用临时真实进程接完整链：实际 managed1→bridge1→新 controller→candidate2；分别让 candidate2 启动/健康失败而 fallback2 同库恢复。systemd 观察替身与真实 PID/exec/file/fsync/SQLite/lease 的证据分开写。
4. 在 drain 与关闭屏障插真实签名手机输入，前后对照回执、原 ingress seq/内容/route/mode/幂等，重投不加输入/epoch；候选失败回退后仍可处理该输入。已有 unknown outbox attempts 与 HTTP 不增加。
5. 以实际格式 2 前置运行项目真实 fs_read/无工具两处 owner 屏障、本地中断、独立下一 run 与正常 close/reopen；另跑普通无工具/工具批→同 run 项目段→签名 owner 独立下一 run，核三处实际 claim 的 marker 透传及项目权限。再验旧槽在接触项目 pending/归档状态前拒绝。不能只证明新 reader 拒坏 marker，也不能用 fresh2 测试代替 managed1 升级。

根先审本方案并分配格式/维护实现所有权；未通过上述前置之前，当前候选保持未发行，项目正向测试的真实格式门失败继续保留。
