# 真实 systemd 托管切换 CI 设计

状态：CI job、协调脚本和 worker 已实现，50 项纯 fixture 与限定静态检查已通过；真实托管 runner 尚未执行。基线为签名候选 `f542d9e78d564a749cb5c5e8568f671b634dcd77`。没有在开发机创建用户、挂载文件系统或操作真实服务。

## 目标与范围

增加一个仅在 GitHub 托管 Ubuntu 24.04、Python 3.13 上显式运行的串行案例：真实标准用户服务从保留安装 A 启动，经公开管理 API 迁入 A、切换到 B，再让下一次切换到 A 的合法目标实例拒绝健康检查，实际回退 B。systemd、D-Bus、loaded unit、`/proc`、cgroup、writer lease、bootstrap、nonce HTTP、activation 和投影均使用产品实现。

A、B 复制同一个当前 MuLi 包，保留原 `compatibility.json`；依赖来自 CI 已有安装。它证明同版本的两个真实保留环境可以切换、回退，并保持最新数据库，**不证明跨版本发行升级或依赖安装**。现有[两个真实进程回归](managed-process-recovery.md)继续保留原期限和假 manager 边界，不改成这个新案例，也不提高其预算。

实现只改 CI，并新增一个 stdlib 协调脚本、一个测试辅助脚本和对应纯 fixture 测试：`.github/workflows/ci.yml`、`scripts/check_managed_native_ci.py`、`tests/helpers/managed_native_worker.py`、`tests/test_managed_native_ci.py`，以及本设计文档。不修改生产协议、controller、launcher、probe、backend、服务模板或恢复算法，不新增产品配置和恢复账本。

## 专用 UID 与已有解释器

CI 协调脚本为本次运行创建唯一非 root 用户和组，记录实际 UID/GID、精确 HOME 和私有根。其 HOME、配置、数据、用户 unit、两个 retained prefix、辅助脚本均在 `RUNNER_TEMP` 内独占的 0700 子根。创建冲突直接失败，不复用已有用户；不修改 runner 用户、全局 `user.slice` 或其它 UID 的 manager。

setup receipt 单独放在 root/runner 拥有、临时 UID 不可写的位置，不放在该 UID 的可写卷中。它逐阶段记录本次已确认创建的用户/组、实际 UID/GID/HOME、镜像文件身份和精确 mount 身份；先记准备意图不能冒充已经创建成功。每次 setup 失败和 `always()` 重入清理都重新核 username↔UID/GID/HOME 及挂载身份仍一致，只处理本次已证实创建的资源。旧 UID 已被复用、映射改变或效果不能确认时保留证据并失败，不能依旧 receipt 删除后来账户；首次 setup 未成功创建的资源不归本次清理。若 `useradd` 已产生账户但响应丢失，未确认 receipt 不授权删除；清理发现同名账户或组会明确失败，不能报告已清理。

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

## 实施与验收顺序

设计已由主 agent 与独立 reviewer 复审后实现上述 CI job、两个脚本及纯 fixture 回归。第一轮本机只允许受守卫的纯 fixture 单位测试及静态检查，不运行真实 harness，不创建本机用户、挂载或服务。真实效果只在明确的托管 CI job 内执行，失败读取精确有限日志，不能通过扩大期限、跳过或放宽生产 gate 处理。

必须覆盖协调脚本的拒绝路径：已有用户名/路径冲突、两个 bind 之一失败、实际 UID 导入失败、聚合 kernel 限额不符、错误 FragmentPath/归属、功能期限耗尽、清理失败不删除仍使用的目录。它们不计为真实 systemd 验收；只有远端三事务完整成功并完成实际收尾，才报告这一新增覆盖。

本机验收：`tests/test_managed_native_ci.py` 共 50 个唯一 case 通过，所有原生 subprocess 入口在该模块由 autouse fixture 硬拒绝。先运行的 44 例为 0.34 秒，再新增末次 receipt 持久化耗尽期限 1 例为 0.06 秒，以及只读 SQLite 锁/固定诊断码 5 例为 0.12 秒；强化已有清理期限例后单跑的 1 例属于前 44 例，不重复计数。测试最高 71.3 MiB、swap 0。三份 Python 文件 Ruff/format 与两脚本 strict mypy 通过；首轮类型检查的局部注解/分支收窄问题已修，类型检查最高 223 MiB，最终尾轮 156.4 MiB，swap 0。每条检查均经 root 的 `check_local.py`、30 秒单次守卫串行执行。

纯 fixture 同时覆盖：handoff 后 runner 不能直接 stat 私有卷、useradd 已创建但响应失败时不越权删除、只读 SQLite 遇真实临时库排他锁零等待失败、固定服务 listing 与严格 helper 名区分、清理最后一步超时不覆盖功能成功证据，以及任意异常正文/非白名单字段不进入导出。静态复审发现并修复的接口、权限与清理缺口没有当成真实托管环境已经运行；实际 runner 的 UID 导入权限、controller 委派、容量、60 秒三事务和实际卸载仍待新增 job 首次验收。

本片不验收跨版本声明、真实 wheel/依赖安装、在线下载、用户已有服务、账号/模型、断电或 macOS/Windows 切换。64 MiB sparse 卷与只读 bind 也不是恶意代码沙箱或宿主总磁盘 quota。

依据：[GitHub 托管 runner 的管理权限](https://docs.github.com/en/actions/reference/runners/github-hosted-runners#administrative-privileges)、[user@.service 与 runtime-dir](https://github.com/systemd/systemd/blob/main/man/user%40.service.xml)、[资源控制](https://github.com/systemd/systemd/blob/main/man/systemd.resource-control.xml)、[cgroup 委派](https://github.com/systemd/systemd/blob/main/docs/CGROUP_DELEGATION.md)、[transient 属性](https://github.com/systemd/systemd/blob/main/docs/TRANSIENT-SETTINGS.md)、[systemctl link/运行时管理](https://github.com/systemd/systemd/blob/main/man/systemctl.xml)、[Python SQLite URI 与锁等待](https://docs.python.org/3.13/library/sqlite3.html#sqlite3.connect)。这些说明支持设计可行性，不代替实际托管 runner 的权限、导入与 kernel 核验。
