# 托管后端认证与验证归档

原始位置：`docs/managed-backend.md`。本页在 2.124.0 本地候选准备时迁入 `devarchive/managed-backend-authentication-design.md`，完整保留技术合同、历史验证与失败记录。当前用户操作见[Linux 托管切换与恢复](../docs/managed-backend.md)。

本页用于开发与历史核对。正文中的版本、测试数量、资源数据及阶段结论按原记录保存，不因归档而成为本次或后续候选的验收结果。当前候选的真实 systemd 原生整链验收尚未完成；受控进程或 fake 边界不能证明用户真实服务已经验证。

本地组合来源为 M `fcc864482e88080e03d4370794e79be413bd5130`、M 上未提交的 K tree `3917e6ff09b551491ef6b838f5f2a6f5b0394911`（冻结 ready SHA-256 `555bd1805509f4fbe0d17f753d95dfc97364b9c8e7295e488e00253ac9347bf5`）与 U 签名候选 `dff5772bec99e8127d1b926c147332bfd29fdc8c`。本地准备不代表 U 已发布；下文仅调整四个 docs 相对链接，技术正文与历史记录保持原文。
---

# Linux 托管事务的效果后端

`ManagedLinuxBackend` 是 protocol 2 controller 的 Linux 效果适配器，已接入 CLI、helper 与启动宿主。完整入口的整合状态见[本地托管切换](../docs/managed-operations.md)。下述模块回归没有操作用户真实 systemd 服务；protocol 1 仍使用原后端。

后端消费同一 journal 的冻结 `RoleBinding`、`RoleSnapshots`、deployment anchor 和 slot metadata。它使用 `ManagedSystemdProbe` 分别观察稳定 unit 的 launcher argv、实际服务进程的角色解释器 argv，以及 boot、PID/start ticks、cgroup 和 loaded unit 属性。配置相同仍比较 selector 与真实 slot；不把配置摘要当成完整安装身份。

任何效果前重读当前 revision、anchor 和 controller 文件。涉及健康、HTTP 激活、安装或启动时，还核当前角色的实际解释器、pyvenv、MuLi 包与兼容声明。另一角色只读冻结 metadata；损坏的 target 包不会阻止恢复仍完整的 base。仅 `stop_base` / `stop_target` 窗口可凭两次一致的精确进程、boot、argv、real executable、cgroup 和三文件观察停止已登记的损坏进程。该例外不产生健康证明，也不放松 foreign PID 或 cgroup 拒绝。

每次同步 `_check` 都从真实私有文件重新认证 anchor、launcher 和各个完整安装摘要对应的 slot。同一安装只读取一次；caller 中冻结的对象只用于比较，不能代替实际字节、SHA 和严格 JSON 解析。controller 与选中角色的完整包认证均位于本批末门之前；随后再次核对所有已读叶的 device、inode、size、mtime、ctime、mode、uid，以及根目录私有权限和完整祖先链。元数据证据只在本次作用域内有效；内容摘要仅在下述明确的相邻同步批次内复用，不跨等待，也不减少效果前检查。损坏的 target 包可沿原流程恢复 base，损坏的 target 元数据仍拒绝。

底层私有元数据作用域也供[固定 controller 宿主](../docs/managed-host.md)使用，入口分别保留认证次序和异常策略。backend 仍先读 controller 元数据并与冻结预期比较，失败为 `managed_slot_unavailable` 或原 `_AuthenticationMismatch`；后者只由 backend 映射为 `ObservationError`。随后 launcher/controller 内容认证的小区段才包装为 `anchor_unavailable`。host 则在该小区段内先核 launcher，再读 controller 元数据和完整内容，不能把两入口的初始错误顺序统一。作用域复用命中仍即时核对原七字段身份，正常退出才执行全体叶/root 围栏；body 已发生的 journal fsync、快照或 selected 认证错误不会被退出检查覆盖。现有独立 guard、选中角色内容认证及 Python/package 证明的传递范围保持原样。

运行态观察在每次真实 probe 前重新认证，返回后将相邻的 controller 与选中角色核验合为一次完整检查。只在这段不含等待的内部代码中读取当前磁盘、验证进程归属或比较前后观察；局部观察失败时仍先检查 controller，保留原错误优先级。origin 进程继续校验完整 base 安装，stop-only 继续仅认证 controller；readiness 等待后重新读取所有包，不跨等待保存认证证明。停止态确认、第三次 probe 及所有效果前安全门保持原流程。

停止流程的第二次 probe 前由效果门完成完整认证与期限检查，复用为该次 probe 的前置门；返回后的独立认证和停止命令前的效果门保留。两次 probe 各自在等待后完整认证成功，才将新取得的 controller 内容证明交给紧邻的同步效果门；首份在第二次真实观察等待前清空，末份在停止命令等待前丢弃。drain、resume 与两种 activate 也仅在首个 probe 返回后与紧邻角色效果门之间复用 controller 内容；不同 selected 包仍完整读取，HTTP 等待后重新认证。每次正常 stop 与宿主命令均保留五次 `_check`、独立打开、完整遍历和身份、元数据、角色、期限核验；不缓存跨等待的授权，不以字节读取减少推算原生三事务耗时。

启动的两次 probe 各自在等待后的独立完整认证及元数据、根目录末门全部成功后，才产生新的局部 Python 与完整 slot 逐叶证明，交给紧邻的 writer-held guard。该 guard 仍独立打开、完整遍历并核对身份、元数据、writer lease、cgroup 和原期限；选中角色没有精确证明时仍完整读取。每份返回证明借用后立即丢弃，确认和最终效果检查只使用各自 guard 的新结果；第一批证明不跨第二次等待，第二次仍重新完整认证。不把本地读取次数的减少当作原生三事务已通过。

所有文件修改要求服务已经停止、cgroup 为空，并真实持有现有 writer lease。正向只按配置→selector→unit，回退只按 unit→selector→配置，接受范围是该代冻结正向或反向前缀。不会把三个文件各自的允许值任意组合。已有 desired 内容也调用文件及父目录耐久确认；不存在的 legacy selector 同样确认父目录。selector 只支持本产品 canonical JSON，非 canonical 快照在 prepare 时拒绝，后端再核，不悄悄重新序列化旧证据。

unit 变化后，后端必要时执行有界 `daemon-reload`，再从 fresh manager 属性证明当前 unit 已加载。命令返回零不算确认。只有同 revision、完整三文件耐久确认、空 cgroup、真实 lease 和 fresh loaded unit 证据才能产生 `BindingConfirmation`；start 在效果前独立重做这些核验。旧 boot、错误 nonce、错误格式/安装/配置证明均不能提交。原 deadline 和回退保留时间不续期；未知 start 不重复发送；激活请求超时或取消后只重观察，不由该后端盲目重发。

protocol 2 可直接消费本次激活请求返回的完整 released 证明。回执先经过原有请求后真实 probe、完整角色包认证与进程、boot、三文件一致性门，再同步确认 writer 仍被占用；意外取得 writer lease 时释放新句柄并回落普通观察，不触碰服务原有锁。控制器复用完整终态判定，重新核原绝对期限，以原 revision 做 CAS；中间不再等待。缺失、失返或 paused 回执仍沿原路径，blocked 与 foreign 仍拒绝。回执不缓存，最终观察、精确投影及 helper 退出屏障保留；protocol 1 继续原流程。

protocol 2 在启动目标或恢复 base 后，也可直接消费本次完整观察中的 paused 健康证明。控制器仍先以真实 CAS 持久化 `verifying` 或 `verify_base`，再核原时钟、绝对期限与回退保留时间，通过原全部健康门后，以新 revision 完成第二次 CAS。两次决策之间没有等待或服务效果；缺失或无效证明只保留原中间态，CAS 失败不会返回虚构成功。此时仍是等待激活的持久决策，激活、最终观察、精确投影及 helper 退出屏障保持独立；protocol 1 不合并步骤。新候选的原生三事务仍待验收。

旧 `LinuxControllerBackend` 仅将原 loopback URL 与有限 nonce HTTP 收发提取为共享函数。protocol 1 的角色判断、proof 核验、效果门和响应语义保持。HTTP 不继承代理、不跟随重定向、不解压，仍限制原始响应 bytes，503 readiness 和传输超时保持原观察语义。

恢复宿主对既有 helper 的只读认证仍执行前后两轮 fresh manager、proc 与 kernel 观察；仅将每轮同一接口的身份与限额属性合为一次 `get-property`。一次 `GetUnit` 加两轮 Unit/Service 请求由九条降至五条，三十八个属性仍各读两次，继续使用原 typed 解析和全部资源/生命周期依赖校验。普通服务 `_snapshot` 的请求保持原样，不在 probe 实例上缓存证明，不减少 `_record` 文件认证，也不改变效果门、取消、输出预算或绝对期限。

新五项回归复用真实 `SystemdProbe`、临时 proc/cgroup 文件与 fake bus。完整快照、三十八字段双读及 proc/kernel 双观察先于计数断言，旧实现实收一失败、四通过（0.09 秒，守卫 612 毫秒、58.3 MiB），唯一红为九不等于五。第二轮资源声明、InvocationID、进程参数或仍在限额内的 kernel 值变化继续拒绝；旧漂移例改在真实 runner 的第二轮字段门注入，仍经过原 parser。修后两文件 62 项通过（0.23 秒、峰值 62.3 MiB），六份调用方文件另有 264 项通过（21.05 秒、89.8 MiB）；独立后审未发现新增 P1/P2。原生三事务仍待验，尚不能推算其总耗时。

验证使用临时 slot 元数据、真实文件 CAS/fsync 和 flock；systemd、进程观察和 HTTP 都是有界 fake。覆盖同配置 switch/adopt、三文件顺序、写入错误和取消后的前缀重入、loaded unit 未生效、坏 target 停止与 base 恢复、foreign 进程拒绝、原 deadline/revision/安装证据重验，以及未知 start/激活超时。

2026-10-21 本地实测全部经统一守卫串行执行，每次硬期限 30 秒，资源上限未提高。新文件 31 项按不重叠主题分三批全部通过：11 项 18.48 秒、9 项 15.13 秒、11 项 12.10 秒；两份旧 V1 controller/backend 文件全 83 项通过，0.53 秒。3 份源码 mypy 与 4 份 Python Ruff/格式通过。最终测试峰值 62.2 MiB，整轮最高为 mypy 的 127.8 MiB，swap 0。

首轮整文件到 30 秒被守卫清理，未算通过；当时顺序夹具只记 `confirm`，漏记已经完成 file+parent fsync 的 `replace`，导致一条断言失败。仅修正该夹具观测后按主题分批验证，没有提升时间/内存预算或删除回退顺序断言。

独立只读复审已通过：核对冻结前缀、正逆序、两次进程观察、writer/cgroup 门、损坏 target 的 stop-only 例外，以及 V1 HTTP 提取的原语义。复审未把上述 fake 回归视为真实服务验收。

这些模块证据不能证明真实 systemd 用户 manager 的加载或真实服务 exec/激活成功；共享已安装目录被外部程序修改也不是沙箱隔离。生产调用链已接线，另有[真实进程验收](../docs/managed-process-recovery.md)；该验收仍使用合成 manager 边界，不代表操作过用户真实服务。

单次同步 `_check` 中，若当前角色与 controller 的完整 `SlotIdentity` 相等，复用本次同步认证已完成的文件核验，不再重复扫描同一安装。不同 slot 仍分别核验解释器路径、pyvenv、兼容声明与完整 package；共享已验证的解释器时，只复用相同实际文件指纹的 Python SHA。包内逐叶证明按完整 canonical slot 的摘要分区，因此 package root、limits 及角色安装证据不同便不混用；分区内再按完整叶路径保存实际读取取得的文件指纹，不把持久化的整体 package 摘要当成逐叶内容证明。远端分项证据与性能验证边界见[恢复核验计时](../docs/managed-recovery-timing-design.md)。

解释器链接链到达非链接终点时，直接用当前已安全打开的父目录 FD 调用原 `_fingerprint_at`，不再嵌套重开同一绝对父目录。叶文件仍独立以 nofollow/nonblock 打开，保留完整字节摘要、读取前后 FD 与末次相对路径身份、文件预算，以及外层父目录末次重走和所有链接末次复核。该复用仅限当前同步调用，不新增缓存；下一次独立调用仍重新认证。

新增七项真实临时文件回归，首红实收两失败、五通过：普通文件和三段链接的完整字节、SHA、叶打开及 FD/path 身份断言均先通过，唯一红为终点父目录仍走四次，目标为两次。负控覆盖末父路径替换、先前链接改指向、同 inode 改写并还原 mtime、独立调用重新哈希，以及旧指纹不绕过更小文件预算。修后十九文件共 322 项通过，独立后审未发现新增 P1/P2；新候选原生性能仍待验收，不从遍历次数推算整体耗时。

独立调用一次 `_confirm` 是没有 `await` 的同步批次。其首个 guard 从空 Python 和 package 逐叶证明开始，每个 guard 另建空暂存区；只有完整 revision、anchor、slot、deadline、writer lease、空 cgroup 与三文件核验均成功，才提升本次局部证明。后续 guard 继续重开解释器、核路径与链接链，并完整遍历 package：每个目录和叶文件仍执行原有无链接打开、前后 FD/path 身份核验，逐轮重新累计 entries、深度、文件字节与包总字节、重新构造整体摘要。只有完整叶路径及七字段 `FileIdentity` 都精确相同，才复用此前成功批次 guard 的内容 SHA；七字段为 device、inode、size、mtime、ctime、mode、uid，现格式没有 nlink，本文不增加格式字段。身份变化必须真实重读：Python 仍比较冻结文件指纹，普通包叶则按原整体内容摘要判断；同字节 inode 或权限变化不能被该优化擅自改成一律拒绝。遍历直接复用已打开的父目录 FD 和原 `fingerprint` 安全 stream 主体，不为每叶重新遍历绝对父路径。独立的 launcher、pyvenv 和兼容声明核验保持原样。

局部 Python 和 package 逐叶证明不保存在 backend 实例上。独立 `_confirm` 返回或抛错后该批次失效，相邻两个独立 `_confirm` 即使中间没有 `await` 也重新哈希。同步 `_install_files` 仍覆盖三组件循环和紧随其后的首次 `_confirm`；每次完整 guard 和当前合法前缀核验通过后才提升证明，首次确认仍要求精确三文件核验。调用方现在只显式接通四处相邻交接：停止态 `observe` 第二次 probe 的 post-check 到 `_confirm`；安装首次 probe 的 post-check 到 writer-held probe 的前置 guard；安装 writer-held probe 的 post-check 到 `_install_files`；安装最终 probe（含 reload 后新 probe）的 post-check 到最终 `_confirm`。每次 producer 的真实 post-await 完整认证与元数据/root 末门全部成功后才发布证明；这些 controller 内容只能供随后同步 guard 使用，未认证的选中 role 仍须完整读取。调用方在下一真实 await 或异常退出前清空该批次；reload 命令前先丢弃命令前证明，返回后重新独立认证。已有 selector 的安装连续段保留十六次真实 guard；本轮普通安装、reload 安装和停止态 observe 夹具分别保留二十九、三十二、十三次 checks，不重算历史完整 switch 夹具的计数。遍历、打开、身份、预算、效果前和 await 后 guard、文件耐久确认及原期限保持原样；不缓存整次检查的授权结果。

安装首份证明只取自首次 probe 等待后的独立 controller 认证，不借用等待前 selected 的内容。停止态检查和同步 writer lease 获取保留，前置 guard 仍依次执行 fresh revision、完整 metadata、controller/selected 文件路径与身份、原 deadline、lease、空 cgroup 和合法三文件前缀检查。该完整 guard 的 `finally` 在第二次真实 probe 等待前清空输入；安装外层 `finally` 另覆盖服务未停止、lease busy、producer 异常与取消。第二 probe 的输出使用另一对象，后续 reload guard 不能重新消费首份证明。不同 selected package root 或 Python 实际身份不借用 controller 内容；同字节新 package inode 仍真实重读并按原整体摘要判断，Python 继续比较冻结指纹。

此首 probe 交接先在未修改生产实现时得到 target/base 两项真实首红（守卫退出 1，3.496 秒、70 MiB）：完整安装、原二十九次检查、打开、FD/path 围栏、遍历、事务与效果断言均先通过，只在窗口内容字节断言失败。共享 Python 多读一份 65,796 字节，controller 两个被观察夹具叶分别多读 256、175 字节；该观察器只覆盖这两个夹具叶，不能称为全 package 总字节。新负控把实际挂起点设为第二 probe，并分别覆盖取得 lease 前的真实 busy、取得 lease 后的 inode 替换、首份证明提前退出清理，以及同摘要不同实际 Python 的 selected 完整读取。Python observer 汇总两个 inode 的字节，逐文件归属还依赖未改 reader 的精确 path/identity 命中边界，不能把合计独称逐文件实测。

修后原两项正向回归通过（1.90 秒、64.9 MiB），26 项新负控通过（14.60 秒、86 MiB）；两文件整合 A 共 99 项通过（66.10 秒、97.9 MiB，包含上述用例，不能累加成独立数量）。四文件整合 C 的部署认证与 Python/package 证明回归共 35 项通过，守卫 16.420 秒、73.7 MiB。以上均由原资源守卫收取 success。

整合 B 首轮为 27 通过、2 失败（32.00 秒、74.1 MiB）：失败只在旧 install-batch target/base 两例的最终精确字节预期，原三次 probe、29 次检查、25 次 selected/4 次 controller-only 调用、所有打开/FD/path/scandir、三文件与完整 confirmation 断言均先通过。controller 两个被观察叶分别实读 1,280/875 字节，旧预期为 1,536/1,050，恰好各少一份；不同 target 根仍读四份，base 恢复仍不读取未选 target。对应旧预期仅将 controller 从六份更新为五份，五次 fresh 内容读仍分别来自首次 preflight、三次 post-check、最终 probe preflight，其余断言保留。修订后原 B 三文件实际 29 项通过（30.98 秒），守卫 success、32.267 秒、88.9 MiB；195 份源码的原 strict 类型检查也通过，守卫 1.160 秒、209.3 MiB。新候选原生 CI/三事务仍待验收，不能由上述读取差异推断原生 60 秒期限已通过。

独立 `_confirm` 的三个文件确认仍执行七次真实 guard；不变文件在 package walk 内的完整内容读取由七次降至一次。这不能将整个 backend 成本按七比一折算。四个新节点先实收每份 256 字节 payload 被读 1792 字节的首红，原 guard/open/stat/scandir 断言通过；修后补齐身份漂移、换 inode/mode、增删叶、symlink、controller 损坏、失败首 guard 与四种缓存预算负控。

同步安装扩展新增三项回归，先得到预期的一红两绿：完整 switch 的 controller / target 包叶分别仍读取十九 / 十三份，所有 guard、打开、身份和遍历断言已先通过；修后分别为十 / 四份。另两项验证随后真实 probe 等待之后重新读取，以及跨组件内容变化即使还原 mtime 仍被拒绝。合入 v2.72 后十七文件 339 项通过（74.87 秒、峰值 150.2 MiB、swap 0），489 文件 Ruff/格式及 160 源码严格类型检查通过，独立后审未发现 P1/P2。未运行本机原生 harness，本地读取次数不能换算为远端总耗时，仍需新的原生三事务验收。

package walk 的类型分类现在直接使用一次 `O_NOFOLLOW | O_NONBLOCK` 打开取得的 FD 身份。普通叶将该首次完整 FD 身份作为读取前身份，复用同一安全 stream 主体；读取后 FD 与父目录 FD 下的末次 nofollow path 身份仍须精确一致。不再额外获取一个只用于判断目录类型、却未参与普通叶身份比较的 path stat，因此每叶每次 walk 从两次 path stat 改为一次，真实 open 仍一次、FD 前后检查仍至少两次。目录以首次实际 FD 身份进入递归，保留递归末 FD 身份、父目录下末次 path 身份、父目录完整 walk 前后身份及外层绝对 root 路径链检查；枚举得到的名称本身不是文件类型证明。

每个新 FD 都由紧贴 `open` 的调用方 `try/finally` 唯一拥有和关闭。内部读取主体只创建 `closefd=False` 的缓冲视图，视图关闭不接管真实 FD；正常返回、取消、fdopen/读取/身份/递归/预算异常都由原调用方收尾，不为分类后再打开同一叶文件。该变化不减少任何完整 guard、目录遍历、内容摘要、逐轮预算、writer lease、CAS、fsync 或等待两侧认证，也不延长局部证明寿命。

FD 分类首红实收一失败三通过（0.79 秒、65.1 MiB、swap 0）：完整摘要、真实 open、FD 检查、内容字节与目录 scandir 断言均先通过，唯一失败是普通叶仍执行两次 path stat。修后 31 项针对性验证通过，涵盖异常收尾、取消、末路径替换、枚举后类型变化和四预算；随后十八文件 315 项整合回归通过（74.28 秒、峰值 180.9 MiB、swap 0）。所有本机检查经过原资源守卫，独立后审未发现 P1/P2。尚未取得新候选的远端原生结果，不能把少一次分类查询换算为三事务总耗时。

后续固定成本测量仅扩展原生 CI worker 的窄 `helper_guard_costs`，不修改产品实现或任何认证、等待与期限。旧 version 1 快照可附带独立 version 1 的 `breakdown`：只在 `host_record`、`checks` 两个同步 owner 内统计 `_MetadataScope._read_record` 的 private read/hash/叶身份检查及成功返回字节，`_ManagedRecord.canonical`，以及 Python/package 文件核验的包含式 wall 与 helper-process CPU。后两项包含内部文件工作，不能再与其他包含式总量相加。严格解码、目录/祖先链、其他 guard 剩余成本与 hook 前启动区间仍未单独测量；本候选也不新增 `read_helper`/`read_managed` 外层 CPU hook。

逐叶 `_fingerprint_fd` 不读取时钟、不新增 open/stat/read：按原函数相同的纯输入条件区分成功 full-read 与 proof-hit，按 Python/package/other 三个固定同步角色累计成功次数与原完整身份核验支持的 size 字节。零字节文件仍算一次完整读取；异常只计 raised，异常前实际读取字节以 `raised_bytes: null` 明确保持未知，不能填零。角色不依据路径猜测，不保存文件名、内容、指纹或跨等待证明。

RPC 仅包装原 `SystemdProbe._command`，固定分类为 GetUnit、Unit、Service、other，保留原 runner 身份、参数和 TaskGroup 并发。记录 entered/returned/raised 与包含调度等待的 wall；不记录可被并发重复覆盖的单 RPC helper CPU。entered 表示方法进入，不证明已经创建 busctl 子进程；在途调用使该次明细暂报 `detail_reason: in_flight`，不提供不守恒的完成合计。GetUnit wall 只是原串行 GetUnit 可移除时的乐观上界，不是实际净收益；spawn、busctl CPU、D-Bus 和调度分项仍未知，wall−CPU 也不是已测 throttle。

明细自身统计失败只令 `breakdown` unavailable 并给出固定 `detail_reason`，原 guard 结果、返回对象、异常/取消身份和旧 `reason` 不变。原 32 KiB、64 行、两个 prefix 与一个 final 上限及发布机会保持；单快照和聚合超界均先丢明细，再沿旧诊断裁剪顺序处理，原 failure/timeline/cleanup 优先。新 reader 接受不带明细的旧快照；不宣称旧 reader 接受新增字段，也不从测量本身推断优化收益或原生三事务验收通过。

后续启动定位仍只扩展同一窄 helper 诊断，不调整产品实现、service 导入、文件认证或证明寿命。可选 `startup` 子对象使用独立 version 1，固定字段为 `version/clock/state/reason/points`；`clock` 为 `monotonic_ns/process_time_ns`，`state` 只允许 complete、partial、unavailable，reason 使用有限机器码，points 恰为以下八项：hook_entered、worker_import_returned、unit_birth_returned、host_import_dispatch、host_import_returned、main_entered、entry_dispatch、entry_returned。每项仅为严格非负整数 wall_ns/cpu_ns 或 null；拒绝 bool、倒序、额外点和任意私密文字，缺失或失败不填零。新 decoder 接受完全不含 startup 的旧快照，不能据此声称旧 reader 支持新字段。

最早三点来自 prepare 实际生成的 fixture hook：第一次加载 time 后、导入 worker 前，worker 导入返回后，以及原 unit_birth 函数返回后。unit_birth 自己吞掉观察失败，因此第三点不代表 unit 所有权观察成功。host 导入前后两点使用独立内存时钟；现有唯一 profile 仅等待精确的实际 `-m` main，核其 ModuleSpec、文件、globals 和 canonical main/_record code 后立即撤回。只有同次核准实际 namespace 的 `_entry` 是同 globals/filename/canonical code 的原函数，才在绑定时记 main_entered，并在该 namespace 放一层透传 wrapper 记录首次 `_entry` 调用前及正常返回后；原调用每次恰一次，原返回对象、异常及取消身份不变。再次调用仍正常执行业务，但不添加或覆盖八个点。startup 的打点、绑定或统计故障不覆盖原 guard reason。

早期读数必须沿原 fixture、UID/PID/INVOCATION_ID、真实 main 和 helper source 门才能导出；未绑定的 startup 只给 unavailable/null。原 first_record、rows 与 source 条件和发布机会不变：过早异常、绑定失败或 kill 可能完全没有启动 payload，不为补齐数据增加文件、发布、轮询或重试。能发布的 partial 也不证明覆盖全部启动。startup 搭原两个 prefix 和一个 final；单快照与聚合都先裁 startup，再裁 breakdown，仍不够才沿旧整体诊断退让，保留原 failure/timeline/cleanup。

这些点不读取 `/proc` 或文件叶，不持有认证证明，不增加 profile 或通用模块跟踪。time 首次导入、进入 hook 之前的 Python/系统调度仍未覆盖；main_entered 是通过身份门后的绑定点。wall 是同一单调时基下的有限区间，CPU 仅代表 helper 本进程；不同进程的 wall/CPU、包含式 guard 成本及 prepared→draining 间隔不能直接相加或当作可删除时间。离线新回归须实际 await helper coroutine 到真实 `_entry` 保留文件认证，再在受控 controller step 停下；只关闭 coroutine 不能证明入口两点。新候选尚未取得 hosted 数据，已验证性能收益仍为零，原生失败和原限额继续保留。
