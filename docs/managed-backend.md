# Linux 托管事务的效果后端

`ManagedLinuxBackend` 是 protocol 2 controller 的 Linux 效果适配器，已接入 CLI、helper 与启动宿主。完整入口的整合状态见[本地托管切换](managed-operations.md)。下述模块回归没有操作用户真实 systemd 服务；protocol 1 仍使用原后端。

后端消费同一 journal 的冻结 `RoleBinding`、`RoleSnapshots`、deployment anchor 和 slot metadata。它使用 `ManagedSystemdProbe` 分别观察稳定 unit 的 launcher argv、实际服务进程的角色解释器 argv，以及 boot、PID/start ticks、cgroup 和 loaded unit 属性。配置相同仍比较 selector 与真实 slot；不把配置摘要当成完整安装身份。

任何效果前重读当前 revision、anchor 和 controller 文件。涉及健康、HTTP 激活、安装或启动时，还核当前角色的实际解释器、pyvenv、MuLi 包与兼容声明。另一角色只读冻结 metadata；损坏的 target 包不会阻止恢复仍完整的 base。仅 `stop_base` / `stop_target` 窗口可凭两次一致的精确进程、boot、argv、real executable、cgroup 和三文件观察停止已登记的损坏进程。该例外不产生健康证明，也不放松 foreign PID 或 cgroup 拒绝。

每次同步 `_check` 都从真实私有文件重新认证 anchor、launcher 和各个完整安装摘要对应的 slot。同一安装只读取一次；caller 中冻结的对象只用于比较，不能代替实际字节、SHA 和严格 JSON 解析。controller 与选中角色的完整包认证均位于本批末门之前；随后再次核对所有已读叶的 device、inode、size、mtime、ctime、mode、uid，以及根目录私有权限和完整祖先链。证据只存在于本次函数栈，不跨独立 guard 或等待复用，也不减少效果前检查。损坏的 target 包可沿原流程恢复 base，损坏的 target 元数据仍拒绝。

运行态观察在每次真实 probe 前重新认证，返回后将相邻的 controller 与选中角色核验合为一次完整检查。只在这段不含等待的内部代码中读取当前磁盘、验证进程归属或比较前后观察；局部观察失败时仍先检查 controller，保留原错误优先级。origin 进程继续校验完整 base 安装，stop-only 继续仅认证 controller；readiness 等待后重新读取所有包，不跨等待保存认证证明。停止态确认、第三次 probe 及所有效果前安全门保持原流程。

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

这些模块证据不能证明真实 systemd 用户 manager 的加载或真实服务 exec/激活成功；共享已安装目录被外部程序修改也不是沙箱隔离。生产调用链已接线，另有[真实进程验收](managed-process-recovery.md)；该验收仍使用合成 manager 边界，不代表操作过用户真实服务。

单次同步 `_check` 中，若当前角色与 controller 的完整 `SlotIdentity` 相等，复用本次同步认证已完成的文件核验，不再重复扫描同一安装。不同 slot 仍分别核验解释器路径、pyvenv、兼容声明与完整 package；共享已验证的解释器时，只复用相同实际文件指纹的 Python SHA。包内逐叶证明按完整 canonical slot 的摘要分区，因此 package root、limits 及角色安装证据不同便不混用；分区内再按完整叶路径保存实际读取取得的文件指纹，不把持久化的整体 package 摘要当成逐叶内容证明。远端分项证据与性能验证边界见[恢复核验计时](managed-recovery-timing-design.md)。

解释器链接链到达非链接终点时，直接用当前已安全打开的父目录 FD 调用原 `_fingerprint_at`，不再嵌套重开同一绝对父目录。叶文件仍独立以 nofollow/nonblock 打开，保留完整字节摘要、读取前后 FD 与末次相对路径身份、文件预算，以及外层父目录末次重走和所有链接末次复核。该复用仅限当前同步调用，不新增缓存；下一次独立调用仍重新认证。

新增七项真实临时文件回归，首红实收两失败、五通过：普通文件和三段链接的完整字节、SHA、叶打开及 FD/path 身份断言均先通过，唯一红为终点父目录仍走四次，目标为两次。负控覆盖末父路径替换、先前链接改指向、同 inode 改写并还原 mtime、独立调用重新哈希，以及旧指纹不绕过更小文件预算。修后十九文件共 322 项通过，独立后审未发现新增 P1/P2；新候选原生性能仍待验收，不从遍历次数推算整体耗时。

独立调用一次 `_confirm` 是没有 `await` 的同步批次。其首个 guard 从空 Python 和 package 逐叶证明开始，每个 guard 另建空暂存区；只有完整 revision、anchor、slot、deadline、writer lease、空 cgroup 与三文件核验均成功，才提升本次局部证明。后续 guard 继续重开解释器、核路径与链接链，并完整遍历 package：每个目录和叶文件仍执行原有无链接打开、前后 FD/path 身份核验，逐轮重新累计 entries、深度、文件字节与包总字节、重新构造整体摘要。只有完整叶路径及七字段 `FileIdentity` 都精确相同，才复用此前成功批次 guard 的内容 SHA；七字段为 device、inode、size、mtime、ctime、mode、uid，现格式没有 nlink，本文不增加格式字段。身份变化必须真实重读：Python 仍比较冻结文件指纹，普通包叶则按原整体内容摘要判断；同字节 inode 或权限变化不能被该优化擅自改成一律拒绝。遍历直接复用已打开的父目录 FD 和原 `fingerprint` 安全 stream 主体，不为每叶重新遍历绝对父路径。独立的 launcher、pyvenv 和兼容声明核验保持原样。

局部 Python 和 package 逐叶证明不保存在 backend 实例上。独立 `_confirm` 返回或抛错后该批次失效，相邻两个独立 `_confirm` 即使中间没有 `await` 也重新哈希。安装仅有一个明确例外：第二次 probe 返回后，同步 `_install_files` 从空证明开始，覆盖三组件循环和紧随其后的首次 `_confirm`；每次完整 guard 和当前合法前缀核验通过后才提升证明，首次确认仍要求精确三文件核验。证明只在该同步函数内显式传递，函数返回或抛错即失效，不跨随后的 probe 等待；daemon-reload 两侧的 guard 和末次 `_confirm` 仍使用全新认证。已有 selector 的该连续段保留十六次真实 guard，完整 switch 夹具保留三十一次 checks；遍历、打开、身份与预算核验不减。所有效果前和 await 后的 guard、文件耐久确认及原期限保持原样。

独立 `_confirm` 的三个文件确认仍执行七次真实 guard；不变文件在 package walk 内的完整内容读取由七次降至一次。这不能将整个 backend 成本按七比一折算。四个新节点先实收每份 256 字节 payload 被读 1792 字节的首红，原 guard/open/stat/scandir 断言通过；修后补齐身份漂移、换 inode/mode、增删叶、symlink、controller 损坏、失败首 guard 与四种缓存预算负控。

同步安装扩展新增三项回归，先得到预期的一红两绿：完整 switch 的 controller / target 包叶分别仍读取十九 / 十三份，所有 guard、打开、身份和遍历断言已先通过；修后分别为十 / 四份。另两项验证随后真实 probe 等待之后重新读取，以及跨组件内容变化即使还原 mtime 仍被拒绝。合入 v2.72 后十七文件 339 项通过（74.87 秒、峰值 150.2 MiB、swap 0），489 文件 Ruff/格式及 160 源码严格类型检查通过，独立后审未发现 P1/P2。未运行本机原生 harness，本地读取次数不能换算为远端总耗时，仍需新的原生三事务验收。

package walk 的类型分类现在直接使用一次 `O_NOFOLLOW | O_NONBLOCK` 打开取得的 FD 身份。普通叶将该首次完整 FD 身份作为读取前身份，复用同一安全 stream 主体；读取后 FD 与父目录 FD 下的末次 nofollow path 身份仍须精确一致。不再额外获取一个只用于判断目录类型、却未参与普通叶身份比较的 path stat，因此每叶每次 walk 从两次 path stat 改为一次，真实 open 仍一次、FD 前后检查仍至少两次。目录以首次实际 FD 身份进入递归，保留递归末 FD 身份、父目录下末次 path 身份、父目录完整 walk 前后身份及外层绝对 root 路径链检查；枚举得到的名称本身不是文件类型证明。

每个新 FD 都由紧贴 `open` 的调用方 `try/finally` 唯一拥有和关闭。内部读取主体只创建 `closefd=False` 的缓冲视图，视图关闭不接管真实 FD；正常返回、取消、fdopen/读取/身份/递归/预算异常都由原调用方收尾，不为分类后再打开同一叶文件。该变化不减少任何完整 guard、目录遍历、内容摘要、逐轮预算、writer lease、CAS、fsync 或等待两侧认证，也不延长局部证明寿命。

FD 分类首红实收一失败三通过（0.79 秒、65.1 MiB、swap 0）：完整摘要、真实 open、FD 检查、内容字节与目录 scandir 断言均先通过，唯一失败是普通叶仍执行两次 path stat。修后 31 项针对性验证通过，涵盖异常收尾、取消、末路径替换、枚举后类型变化和四预算；随后十八文件 315 项整合回归通过（74.28 秒、峰值 180.9 MiB、swap 0）。所有本机检查经过原资源守卫，独立后审未发现 P1/P2。尚未取得新候选的远端原生结果，不能把少一次分类查询换算为三事务总耗时。
