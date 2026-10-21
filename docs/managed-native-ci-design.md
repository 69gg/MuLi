# 真实 systemd 托管切换 CI 设计

状态：CI job、协调脚本和 worker 已实现，含私有卷入口修复的 89 项纯 fixture 与限定静态检查已通过；前三轮真实托管 runner 均未观察到 worker 结果，记录为 0 个事务；仅第三轮的精确 typed listing 证实该 harness 单元缺席。第三轮已取得客户端可执行文件查找权限失败与用户总线成功的独立事实，本轮调整私有卷入口，真实启动与三事务待下一精确远端候选验收。基线为签名候选 `f542d9e78d564a749cb5c5e8568f671b634dcd77`。没有在开发机创建用户、挂载文件系统或操作真实服务。

## 目标与范围

增加一个仅在 GitHub 托管 Ubuntu 24.04、Python 3.13 上显式运行的串行案例：真实标准用户服务从保留安装 A 启动，经公开管理 API 迁入 A、切换到 B，再让下一次切换到 A 的合法目标实例拒绝健康检查，实际回退 B。systemd、D-Bus、loaded unit、`/proc`、cgroup、writer lease、bootstrap、nonce HTTP、activation 和投影均使用产品实现。

A、B 复制同一个当前 MuLi 包，保留原 `compatibility.json`；依赖来自 CI 已有安装。它证明同版本的两个真实保留环境可以切换、回退，并保持最新数据库，**不证明跨版本发行升级或依赖安装**。现有[两个真实进程回归](managed-process-recovery.md)继续保留原期限和假 manager 边界，不改成这个新案例，也不提高其预算。

实现只改 CI，并新增一个 stdlib 协调脚本、一个测试辅助脚本和对应纯 fixture 测试：`.github/workflows/ci.yml`、`scripts/check_managed_native_ci.py`、`tests/helpers/managed_native_worker.py`、`tests/test_managed_native_ci.py`，以及本设计文档。不修改生产协议、controller、launcher、probe、backend、服务模板或恢复算法，不新增产品配置和恢复账本。

## 专用 UID 与已有解释器

CI 协调脚本为本次运行创建唯一非 root 用户和组，记录实际 UID/GID、精确 HOME 和私有根。镜像文件、setup receipt 与导出结果仍在 `RUNNER_TEMP` 内由 runner 拥有；唯一 ext4 挂载入口位于现有 `/tmp` 下的本次独占目录。其 HOME、配置、数据、用户 unit、两个 retained prefix、辅助脚本均在这一个 0700 私有卷中。创建冲突直接失败，不复用已有用户；不修改 runner 用户、祖先目录的 mode/ACL/补充组、全局 `user.slice` 或其它 UID 的 manager。

setup receipt 单独放在 root/runner 拥有、临时 UID 不可写的位置，不放在该 UID 的可写卷中。它逐阶段记录本次已确认创建的用户/组、实际 UID/GID/HOME、镜像文件身份和精确 mount 身份；先记准备意图不能冒充已经创建成功。每次 setup 失败和 `always()` 重入清理都重新核 username↔UID/GID/HOME 及挂载身份仍一致，只处理本次已证实创建的资源。旧 UID 已被复用、映射改变或效果不能确认时保留证据并失败，不能依旧 receipt 删除后来账户；首次 setup 未成功创建的资源不归本次清理。若 `useradd` 已产生账户但响应丢失，未确认 receipt 不授权删除；清理发现同名账户或组会明确失败，不能报告已清理。

`/tmp` 必须经 `lstat` 确认为非符号链接、root 拥有的 01777 目录；记录并重核其 dev/inode。卷入口名只由固定前缀和 receipt 中已严格验证的本次用户名派生，不接受命令行另给任意挂载目标。排他 `mkdir` 后，先确认原入口的 dev/inode、runner owner 和 0700 并持久保存，才建立镜像和挂载。跨进程 cleanup 从同一 receipt 重建精确路径，复核 `/tmp` 身份、已有 mount 身份，以及卸载后原入口的 dev/inode/owner/mode；碰撞、未确认创建、目录替换或失证均失败且不删除它。保留卸载后的空私有入口与镜像，不递归删除输出。这样无需穿越权限为 0750 的 runner 祖先，也不引入新的 bind 或放宽私有数据权限。

临时数据使用现有 Linux fixture 相同配方的独立 64 MiB sparse ext4 镜像，挂载选项为 `loop,nodev,nosuid,noatime`。新 job 不与 matrix 的卷共用可写目录。先报告实际挂载、可用字节和 inode，再运行真实 `verify_local_filesystems`；不支持就使该显式 job 失败，不跳过或修改白名单。该卷只承载小型合成事实，不模拟断电耐久性。

不复制整份 venv，不安装或下载依赖，不放宽 runner HOME 权限。只建立两个固定用途的只读 bind：

1. 当前解释器实际 `sys.base_prefix` 到私有 `input/python`；
2. 当前 venv 的实际 site-packages 到私有 `input/deps`。

先规范化并验证两个源，拒绝它们位于本次输出目录或互相形成递归挂载。bind 后逐个 remount 为只读，并通过 `findmnt` 的结构化结果检查目标、挂载身份与只读状态；固定目标的 dev/inode/mode 由受限 `sudo stat` 数值输出核实，不能依赖 runner 穿越移交后的 0700 卷。新 ext4 根先给予仅 runner 的准备权限，完成复制后才整体移交临时 UID；HOME、配置和数据保持私有权限。失败不继续。只读 bind 提供可遍历的新入口，不使源文件内部原有不可读权限失效，也不复制依赖资料。

每个 retained prefix 复用现有 `slot()` 的轻量布局：`bin/python` 指向只读 Python bind 内经过核实的实际基础可执行文件，`pyvenv.cfg` 指向同一 runtime；自有 site-packages 复制有界的当前 MuLi 包，自有 `.pth` 先设置 `sys.dont_write_bytecode`，再只把只读 deps 加入搜索路径并显式导入唯一测试钩子。`-I` 不依赖 `PYTHON*` 环境来禁写 pyc。MuLi 自有包优先，不通过 deps 中 editable 路径借用 checkout 的 MuLi；不递归处理 deps 内 `.pth`。辅助脚本复制到私有可读位置，不依赖 runner HOME 下的 tests 路径。

开始服务前，必须以**实际临时 UID**、retained Python 和 `-I` 有限导入 `muli`、`fastapi`、`httpx`、`uvicorn`、`pydantic`、`sqlite3`，核对 `sys.prefix`、解释器身份和 `muli.__file__`；这才是读取权限可用的证据。私有根的全部祖先也必须对该 UID 可遍历；若 runner 布局不满足就固定失败，不修改 runner HOME 或父目录权限。基础 Python 的相对文件位置必须落在被绑定的真实 runtime 内，不能留下指回不可遍历 runner HOME 的入口。禁止写 pyc；源代码及随包兼容声明保持原 bytes。

64 MiB 卷不会容纳基础解释器和依赖副本，两份 MuLi 复制受现有 `PackageLimits` 限制。复制前计算实际包大小、两份包和固定合成资料的空间需求，核实际剩余空间并留收尾余量；达到阈值就失败，不自动扩卷。SQLite、journal、快照和合成对象均有既有大小限制，日志不写成无界卷文件。

## 同一 manager 的聚合资源保护

由 PID 1 启动仅该 UID 的 `user-runtime-dir@UID.service` 和 `user@UID.service`，使用其真实 `/run/user/UID` 与 D-Bus socket，不启用 linger。实际管理命令在该 UID 下运行；`muli.service` 因新 HOME 自然使用标准用户 unit 路径，不改生产 unit 名称。

仅对本次创建的 **`user@UID.service`** 施加 runtime 聚合上限：MemoryMax 1 GiB、MemorySwapMax 0、CPUQuota 100%、TasksMax 128。harness 也由该 user manager 的独立 transient unit 运行，另限 256 MiB、32 tasks、RuntimeMaxSec 90；它与标准服务、恢复 helper 都必须实际位于该聚合 cgroup 下。runner 进程和 setup/cleanup 协调者不在这个 UID 中，不会被改动上限。

启动任何 MuLi unit 前读取实际 `ControlGroup`，核 cgroup v2 的 `memory.max`、`memory.swap.max`、`cpu.max`、`pids.max` 和有效 controllers；启动后核 harness、服务和 helper 的真实 membership。上述实际 UID 的解释器导入预检在这个 bounded harness unit 内完成，再启动 MuLi 服务，不在未受限的 sudo 子进程中执行导入。标准服务仍使用 `UserService.render()` 的原资源属性，helper 仍由 `helper_launch_arguments()` 与真实 `read_helper()` 验证。不能添加会被产品 probe 拒绝的 drop-in，也不能用 `systemctl` 返回零或父测试进程的 cgroup 冒充子树保护。缺少 controller、delegate 或权限时该 job 失败，无裸进程 fallback。

## 三个真实事务

整个案例只有一条顺序链、同一个数据库和 journal，不并行运行多个服务。配置使用动态预选的 loopback 地址，真实 Uvicorn 绑定；若端口被占用，失败并清理，不重试管理业务。关闭模型、启动更新检查、渠道、MCP、Skills、目录监听、插件、自动记忆、注意队列和通知，不接外部账号。

1. **真实 legacy A → adopt A。** 由真实标准 `muli.service` 启动 A，bootstrap 获得当前 PID/InvocationID 对应的受支持 BootSession，Runtime 新建数据库及完整格式标记。真实 owner 登录后，调用 `POST /api/management/transactions`，`action=adopt`、candidate 指向当前 A 的三个实际路径。走原 idle-only drain、stop、持久发布、真实 helper 和稳定 launcher，核同 DB/主身份、最终 released 和唯一终态投影。
2. **managed A → switch B。** 使用相同 API 与原管理权限，候选是已核本地 B。必须实际观察不同 retained Python/prefix、fresh boot、loaded unit 和 cgroup；不能只凭配置摘要相同判成功。fixed controller 仍是 A。
3. **managed B → switch A → rollback B。** A 的测试钩子仅在本次合法 target 运行时写晚于 prepare 的合成资料，再注册明确不支持能力。真实健康路径拒绝后，由原 helper 停止目标、还原三文件、启动 fresh B 并投影失败。核晚写资料、pending ingress、unknown outbox、原发令 routing 与主身份仍在，不能用旧 DB 副本覆盖。

这些请求由真实 Web owner 发出，保存并比较的只是该 Web 管理来源；本例不覆盖真实外部渠道的原路投递。legacy origin 就绪后先冻结只读获取的 Store instance、main session 和数据库 dev/inode，三代 record、实际数据库及停服收尾均必须等于这份初始事实，不能只比较每代 record 与当时的库。

每次启动后重新走 owner 登录，不能沿用上个进程的内存会话。使用精简的 `GET /api/management/transactions/current` 观察：`RecoveryHost.current()` 只有 available/reason，transaction 为 `TransactionRecordV2.public()` 的十个有界标量，不取完整 `/api/management` 的 plans/插件/其它证据。POST 返回 plan，不能套用旧 nonce 用例的 4 KiB 限制；本案例所有 HTTP 响应（含错误）统一沿当前 `JournalLimits.max_record_bytes` 做实际流式计数，超限失败，不打印正文来诊断。该上限与案例私有路径共同核验，不为通过测试临时调大。

单次 HTTP 共享整段请求/响应/关闭上限 `min(2 秒, remaining)`，全链功能 deadline 为 60 秒，不为每个事务重开；harness 的 90 秒是外层最后限制，不是续期。读取状态是有间隔、有总期限的确定性验收，不由模型轮询，不加产品后台循环。产品自己的持久 deadline、回退 reserve 和 helper 限额保持不变。

每代完成屏障同时要求 exact receipt 和**该代精确 helper 已退出、无 job、cgroup 为空或已收集**，之后才提交下一代。`run_managed_helper()` 在 controller lock 内观察最终健康，`drive_loop()` 看见 resolved 后仍会 await `observe_final()`；不能把先暴露的 resolved/投影当成 helper 已释放锁。等待仍消耗同一个 60 秒，不重试已失败的 prepare，也不重开业务请求或延长期限。

## 失败钩子的准确边界

仅 retained 环境自有 `.pth` 导入唯一辅助模块；它只为真实 `serve` 入口包装 `server.create_app`，helper、launcher 和其它命令不安装该包装。原 `create_app` 返回后才能取得 `app.state.runtime`；此时实际 CLI/bootstrap、`ManagedBootPermit.open_store()`、Runtime 构造、nonce route 安装和 maintenance seal 已完成，lifespan activation 尚未执行。

包装器先验证：permit 确为 `ManagedBootPermit`、`role=target`、当前实际 slot 为 A、record.action 为 switch 且 base 是 B；同 journal 新鲜 record 仍属于该次合法目标，实际 lease 检查成功，Store/main 与 record identity 相同，Store 已无迁移准入、WAL/FULL，admission 仍暂停。只有全部成立才写固定数量的小型 fixture 对象、pending ingress 和 unknown outbox，并登记 `fixture_target_rejected` 的 `Maintenance.unsupported()` 回调。写入携带当前 request/generation 与 prepare 后时序证据，不能在拿到合法 boot/DB writer 前写业务事实。

下一次真实 `GET /api/internal/management/readiness` 先经过原 nonce、boot、generation、binding、Store/core 校验，再由原 `require_support()` 返回拒绝；测试不构造错误 HealthProof、不改 record、不制造 controller decision。失败是合成的显式不支持能力，并非模拟崩溃或真实第三方组件故障。

钩子只写合成资料和注册现有能力检查，不替换 probe、systemd 命令、socket、activation/projection 或管理 API，不 patch 固定 controller、稳定 launcher、协议对象或 MuLi 源文件。A/B 中 `muli` 包及兼容声明的指纹仍来自原真实字节；测试辅助模块位于包外。正常 adopt A、switch B 不触发失败条件。

## 结果、收尾与证据

每次事务验证 plan/request/generation、真实 fresh InvocationID、MainPID 的 UID/argv/exe/start ticks/cgroup、Store/main 身份和最终 exact receipt。终态只产生一次 `management.completed`；重复紧凑查询不重投。运行中的独立核验只用已存在数据库的只读连接，显式 `timeout=0`，遇锁立即失败，不调用会初始化或恢复业务的 Store 构造。服务停止、实际 writer lease 可重取后，再核同一库中的最新 fixture、pending ingress、unknown outbox 和原 routing；不要把 cleanup 成功或 selector 文件变化当作事务成功。

功能结束后的清理效果与证明共享一个 5 秒绝对期限，协调者将原期限传给 cleanup worker，命令前后及最终发布 clean 结论前均重核，不给每条命令新开 5 秒，不借它继续健康检查或推进事务。若命令失败，最末已拥有的客户端进程组仍走既有 `SIGKILL` 加最多 5 秒 reap 窗口；这只回收客户端，不能继续任何服务效果、业务观察、挂载或删除，也不能发布 `cleanup=verified`。因此最坏 wall time 包含这段额外回收窗口，不能声称整体仅 5 秒。setup 协调者和 workflow `always()` 均使用本次固定路径及已创建 UID 的精确记录，不能依赖 setup 最后才导出的变量。顺序为：

1. 从本次私有 journal 的实际部署/代推导精确 helper 名，并在协调者拥有的有限 result 中保留本次三个事务实际观察到的名称，与 setup receipt 一起保存；先停止这些 helper，防旧 helper 尚未退出时重新启动服务。不匹配就保留证据并失败，不接受任意用户提供的 unit 名或通配停止。
2. 对固定 `muli.service` 单独进行精确结构化 listing，区分缺席与 manager 观察失败，不借用产品只允许 helper 名的 `helper_exists()`。核实际 FragmentPath 属于本次私有 unit，再停止它；真实确认没有 pending job、MainPID 为零、cgroup 已空或可证明已删除。
3. 收取 harness；用实际 writer lease 重取和 SQLite 关闭结果确认不再有写入者。常规收尾只有全部证明成功才称 clean。
4. 停止仅本次创建的 `user@UID.service` 与 runtime-dir；如果进程异常导致不能完成正常收尾，仍可定向关闭这个无其它业务的专用 UID manager，但不得把它记为成功事务或正常服务退出。
5. 保存有界、无凭据的合成结果；先卸两个精确 bind，再卸本次 ext4。任何子挂载/进程仍在、卸载或用户清理失败都使步骤失败，不 lazy/force 卸载，不递归删除仍挂载的树，不影响 runner manager 或其它 UID。仅在确认归属后删除本次创建的用户；再实际确认同名私有组是否仍在，只有同 GID 且为空的本次组才删除并复核。保留协调者 receipt、result 与独立镜像，不递归删除私有输出树。

有限证据包括实际资源属性与 kernel 限额、单元状态/InvocationID、固定错误码、各请求的最终状态、receipt 次数及只含合成资料的 DB 断言。协调者只导出严格白名单结构与有限标量；自定义 `NativeError` 仅保存格式受限的 `native_*` 固定码，其他异常只记类型，不输出原异常正文。worker 无法导入时也保留协调者预写的 `harness_start/worker_not_observed` 证据。清理不重写功能 timeline 或清空三代历史；仅全部清理证明完成且仍在期限内才单独标记 `resources.cleanup=verified`。默认不输出配置、nonce、Cookie、密码、完整 journal 或任意业务正文。取消或外层硬杀后走独立 always 清理；VM 最终销毁不是已经成功清理的证据。

若启动客户端返回失败且 worker 尚未产生结果，协调者使用 **runner 自己已有的解释器与源 worker** 做一次只读诊断，不再依赖出错的 retained Python。只有其中的 `busctl` 经 `sudo` 切到本次 receipt 已确认、并重新通过 passwd 核对 UID/HOME 的专用用户；环境明确指定 `/run/user/UID/bus`，不读取 runner 的 user manager。诊断只对本次生成的精确 `muli-native-32hex.service`，复用 `SystemdProbe` 的 typed D-Bus 读取，不依赖启动成功才会存在的完整 process identity。真实 worker 已留下的结果始终优先，不被此诊断替换。

`resources.startup` 分别保留启动客户端与诊断客户端的退出码、固定命令报告分类，以及观察器的 `identity/ancestors/import/manager/listing/get_unit/unit_properties/service_properties` 阶段和最后一条 `busctl` 的退出码/分类。未知退出码保持空值，已尝试但超时不能记为从未启动。分类只对白名单短语作匹配；它是命令所报告的线索，不证明实际根因、权限或进程授权，原 stderr、路径和异常正文均不导出。先成功读取 typed Manager，再对精确 unit 执行 `ListUnitsByPatterns`；只有该 typed 列表成功且为空才能记 `unit_absent`。总线不可达、GetUnit 失败、属性查询失败或坏响应均保留各自阶段，不能归为单元缺席。成功观察才附加 `Id/LoadState/ActiveState/SubState/Result/ExecMainCode/ExecMainStatus/MainPID` 的有限事实。

同一次诊断仅对从 `/` 到本次私有卷父目录的固定祖先链做至多 16 次 `lstat`，按顺序索引保留 mode（含文件种类）、uid/gid，不扫描目录或导出祖先路径。本轮父目录是 `/tmp`，不再从 HOME 层级推作 `RUNNER_TEMP`。它们只是 DAC 线索，不检查 ACL、补充组或有效可遍历权限，也不修改任何父目录权限。`user@UID.service` 已启动和聚合 cgroup 已核验都不等于普通用户 D-Bus 已可达；[v255 的 user@ unit](https://github.com/systemd/systemd/blob/v255/units/user%40.service.in) 依赖 runtime-dir，不能据此替代实际总线查询。[v255 的 systemd-run](https://github.com/systemd/systemd/blob/v255/src/run/run.c) 在本例 `--wait --pipe` 下强制使用普通 user bus，且在连接前先查找可执行文件；不带这些选项时可能使用的 `systemd/private` 通路不能混作本例的可达证据。

这次诊断至多消耗现有 5 秒命令预算，并且必须落在本次原有 `90+5` 秒启动观察总期限的剩余时间内；启动观察已耗尽就不发诊断命令，不增加功能 60 秒、harness 90 秒或清理 5 秒的上限。Ubuntu 24.04 对应的 [systemd v255 参数实现](https://github.com/systemd/systemd/blob/v255/src/run/run.c)支持当前 `--wait --pipe --service-type=exec --expand-environment=no`；[同版本手册](https://github.com/systemd/systemd/blob/v255/man/systemd-run.xml)说明关闭环境展开的选项从 v254 起存在。参数支持不等于本次实际 exec 成功；retained `bin/python` 与 `pyvenv.cfg.home` 的源 runtime 相对关系也由纯 fixture 明确核验，实际权限仍由远端运行证明。

## 实施与验收顺序

设计已由主 agent 与独立 reviewer 复审后实现上述 CI job、两个脚本及纯 fixture 回归。第一轮本机只允许受守卫的纯 fixture 单位测试及静态检查，不运行真实 harness，不创建本机用户、挂载或服务。真实效果只在明确的托管 CI job 内执行，失败读取精确有限日志，不能通过扩大期限、跳过或放宽生产 gate 处理。

必须覆盖协调脚本的拒绝路径：已有用户名/路径冲突、两个 bind 之一失败、实际 UID 导入失败、聚合 kernel 限额不符、错误 FragmentPath/归属、功能期限耗尽、清理失败不删除仍使用的目录。它们不计为真实 systemd 验收；只有远端三事务完整成功并完成实际收尾，才报告这一新增覆盖。

本机验收：`tests/test_managed_native_ci.py` 共 50 个唯一 case 通过，所有原生 subprocess 入口在该模块由 autouse fixture 硬拒绝。先运行的 44 例为 0.34 秒，再新增末次 receipt 持久化耗尽期限 1 例为 0.06 秒，以及只读 SQLite 锁/固定诊断码 5 例为 0.12 秒；强化已有清理期限例后单跑的 1 例属于前 44 例，不重复计数。测试最高 71.3 MiB、swap 0。三份 Python 文件 Ruff/format 与两脚本 strict mypy 通过；首轮类型检查的局部注解/分支收窄问题已修，类型检查最高 223 MiB，最终尾轮 156.4 MiB，swap 0。每条检查均经 root 的 `check_local.py`、30 秒单次守卫串行执行。

纯 fixture 同时覆盖：handoff 后 runner 不能直接 stat 私有卷、useradd 已创建但响应失败时不越权删除、只读 SQLite 遇真实临时库排他锁零等待失败、固定服务 listing 与严格 helper 名区分、清理最后一步超时不覆盖功能成功证据，以及任意异常正文/非白名单字段不进入导出。静态复审发现并修复的接口、权限与清理缺口没有当成真实托管环境已经运行；实际 runner 的 UID 导入、60 秒三事务和服务正常收尾仍待验收。首轮实际账户、组与已记录挂载的回收事实见下一段，不能等同于该完整验收。

## 首轮远端结果与诊断补片

PR [#37](https://github.com/69gg/MuLi/pull/37) 的首轮精确候选为签名提交 `54a91d6dec6ce6a348280d3cb159f7a1f5a01865`，CI [39561110110](https://github.com/69gg/MuLi/actions/runs/39561110110) 九项中七项成功、两项失败。macOS 3.13 的独立 AX 期限夹具失败另行处理；原生 [job 119092322076](https://github.com/69gg/MuLi/actions/runs/39561110110/job/119092322076) 在 `07:19:06Z` 报 `native_command_failed`，随后 always 清理仍报 `native_service_cleanup_failed`，没有将任一步记为成功。

该轮 490 字节[有限 evidence](https://github.com/69gg/MuLi/actions/runs/39561110110/artifacts/12326704803)中的 `result.json` 是 `completed=false/stage=harness_start/code=worker_not_observed`，timeline 与 generations 均为空，即 **0 个事务**；没有 retained 导入或业务阶段的成功证据。`receipt.json` 记录该次独有用户 `muli-ci-f9911e829db9`、UID 999/GID 987 已分别 `user_removed=true/group_removed=true`，`mounts=[]`。这证明对应账户、组和已记录挂载已回收，不能把它当作服务正常收尾或三事务已验收。原始失败没有单元退出状态，不能直接归因于 DAC、解释器导入或 systemd 参数；上述只读启动诊断补片用于下一精确候选取得这项缺失事实，不重试旧 head。

诊断补片新增 11 项纯 fixture，覆盖 typed 退出状态、精确 UID/bus、错误 unit/账号/期限前拒绝、原 envelope 不续期、原 stderr 和额外字段不导出、真实 worker 结果优先、CLI 只读分派与 retained 路径/启动参数关系。整文件 61 个唯一 case（原 50 加新 11）通过，0.34 秒、服务 0.879 秒、峰值 72.4 MiB/swap 0。首轮 typed 回归发现 `ProbeLimits` 默认 command 输出上限为 64 KiB，与此 fixture 32 KiB 合同不符，已显式使用原 `MAX_BYTES` 缩回，再通过相关 13 例与完整 61 例；13 例与 61 例重叠不重复计数。三份 Python 文件 Ruff/format 和两脚本 strict mypy 通过，mypy 峰值 157.5 MiB/swap 0。所有命令仍经 root 绝对路径守卫逐条 30 秒串行执行，无本机真实 harness。这是诊断链的纯回归证据，不计为远端启动已经修复。

## 第二轮远端结果与分阶段诊断

第二轮精确签名候选 `f9a1ca920d462830f630e650985f1a28bfcc0131` 已包含 2.32.1 候选 `1cf156b7b498c394b4aa719e3c3391ddafaba003`。CI [39564407572](https://github.com/69gg/MuLi/actions/runs/39564407572) 最终八项成功，仅原生 [job 119103115810](https://github.com/69gg/MuLi/actions/runs/39564407572/job/119103115810) 失败；不重试该旧 head。`07:47:07Z` 仍为 `native_command_failed`，always 清理仍为 `native_service_cleanup_failed`。

617 字节[有限 evidence](https://github.com/69gg/MuLi/actions/runs/39564407572/artifacts/12329236961) 中，功能结果依然是 `harness_start/worker_not_observed`、timeline/generations 为空，即 **0 个事务**。本轮已取得实际聚合 kernel 值：memory.max 1073741824、memory.swap.max 0、cpu.max `100000 100000`、pids.max 128；启动客户端退出码为 1，旧观察器只返回 `unavailable`。这仍不足以区分可执行文件查找、导入或用户总线故障。该次 receipt 的专用用户 `muli-ci-0af59a4095a9`、UID 999/GID 987 分别已删除，已记录 mounts 为空；服务正常收尾仍未通过，不能据此称三事务成功。

分阶段诊断补充 19 项纯 fixture，覆盖两个客户端独立失败、未知退出码的超时、产品导入失败、五个 D-Bus 观察阶段、typed 空集与坏列表区别，以及有限祖先元数据不改权限、不泄路径。最终整文件 **80 个唯一 case**（原 61 加新 19）通过，0.44 秒、服务 0.982 秒、峰值 74.5 MiB/swap 0；先运行的 28 项及过渡 79 项均为重叠检查，不重复计数。三份 Python 文件 Ruff/format、两脚本 strict mypy 通过；本轮 mypy 最高 162.5 MiB，最后缓存尾轮 65.7 MiB，swap 0。每条仍通过 root 绝对路径 30 秒守卫串行执行，没有本机真实 harness、用户/挂载/服务操作。这一补片只提高下一精确候选失败时的辨识度，不宣称已修复启动根因或已经完成原生验收。

## 第三轮远端结果与私有卷入口修复

精确签名候选 `02ab6da94fb0c3de8badfdafbb9361177932860d` 的 CI [39569796860](https://github.com/69gg/MuLi/actions/runs/39569796860) 最终八项成功，仅原生 [job 119120822395](https://github.com/69gg/MuLi/actions/runs/39569796860/job/119120822395) 失败。739 字节[有限 evidence](https://github.com/69gg/MuLi/actions/runs/39569796860/artifacts/12331815464)的 zip SHA256 为 `bc8cc9bdaa24612fc91545bab5ebe4f0ba265706b19bfb01c4983e082a3bb8d8`。功能结果仍为 `harness_start/worker_not_observed`，timeline/generations 为空，即 **0 个事务**。

启动客户端退出 1，固定分类为 `executable_permission`；诊断客户端退出 0，typed Manager 与精确 listing 成功，得到 `stage=listing/code=unit_absent/query_status=0`。因此本轮不是用户总线不可达，而是在客户端查找可执行文件时报权限错误，尚未创建该精确 harness。旧入口祖先元数据的索引 2 为 040750、UID/GID 1001，其余为 040755；本次专用 UID 999/GID 987。这些 mode 仍不作为 ACL 或实际有效访问权限证明；路径调整只移开这一必须经过的 runner 私有祖先，不扩大其权限。

同一轮 `native_service_cleanup_failed` 不能解释为合法缺席被误拒：cleanup transient 也使用同一 retained 可执行文件路径，无法启动就保留失败；其后的专用 manager 停止、用户/组删除与已记录挂载回收属于兜底。receipt 中 `user_removed=true/group_removed=true/mounts=[]` 证明这些资源已回收，不能称正常服务收尾。修复不把 `unit_absent` 当清理 worker 成功；标准服务原有 typed 缺席分支保持，仍须真正执行 cleanup worker 并完成其余证明。

本轮新增 9 项纯 fixture，覆盖不可遍历 runner 祖先下的协调者仍可使用独立卷执行 drive 与 fresh-coordinator cleanup、`/tmp` 类型/owner/mode、入口目录/悬空符号链接碰撞、父目录替换和原入口身份替换拒绝。新 9 项通过 0.13 秒、峰值 63.3 MiB；最终整文件 **89 个唯一 case**（原 80 加新 9）通过，0.41 秒、服务 0.973 秒、峰值 74.1 MiB/swap 0。新 9 项和权限 fake 单例均包含在 89 项内，不重复计数。首次整文件运行发现旧权限 fake 在卸载后仍永久禁止原入口本身的 `lstat`；仅允许该入口的 nofollow 元数据读取，私有内容与普通 `stat` 仍拒绝，再通过该单例和完整 89 项。三份 Python 文件 Ruff/format、两脚本 strict mypy 通过，mypy 峰值 157.8 MiB/swap 0，所有检查均由 root 绝对路径守卫逐条 30 秒串行执行。

所有真实 subprocess 继续在该测试模块被硬拒绝；没有本机原生 harness、用户/挂载/服务操作。纯 fixture 不证明真实临时 UID 可执行，实际查找、导入与三事务仍待下一精确远端候选验收。95 秒启动 envelope、60 秒功能期限、5 秒清理效果/证明及已有故障命令 kill/reap 上限均不变。

本片不验收跨版本声明、真实 wheel/依赖安装、在线下载、用户已有服务、账号/模型、断电或 macOS/Windows 切换。64 MiB sparse 卷与只读 bind 也不是恶意代码沙箱或宿主总磁盘 quota。

依据：[GitHub 托管 runner 的管理权限](https://docs.github.com/en/actions/reference/runners/github-hosted-runners#administrative-privileges)、[user@.service 与 runtime-dir](https://github.com/systemd/systemd/blob/main/man/user%40.service.xml)、[资源控制](https://github.com/systemd/systemd/blob/main/man/systemd.resource-control.xml)、[cgroup 委派](https://github.com/systemd/systemd/blob/main/docs/CGROUP_DELEGATION.md)、[transient 属性](https://github.com/systemd/systemd/blob/main/docs/TRANSIENT-SETTINGS.md)、[systemctl link/运行时管理](https://github.com/systemd/systemd/blob/main/man/systemctl.xml)、[Python SQLite URI 与锁等待](https://docs.python.org/3.13/library/sqlite3.html#sqlite3.connect)。这些说明支持设计可行性，不代替实际托管 runner 的权限、导入与 kernel 核验。
