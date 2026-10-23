# 真实 systemd 托管切换 CI 设计

状态：CI job、协调脚本和 worker 已实现，含专用 manager 环境、进程/等待观察与定向 journald。现已记录第十四轮 native 失败：2.5秒回执窗口确实保住同次到期摘要，后备正向观察到 runtime-dir 已完成、user manager 仍 activating/start；仍为0个真实事务，没有 readiness 或正常清理证明。最近合并后整合纯验证为430项通过，新增失败后环境/socket 投影已实现并完成独立复审，详见下文；它不代替原生验收。前三轮均未观察到 worker 结果；第四轮实际进入 drive，完成真实导入、两个 slot 与 kernel 限额核验，然后在标准 unit 默认路径检查失败。第五轮起的 manager 启动原因仍未确定。真实环境修补、三事务和正常服务收尾仍未验收。初始基线为签名候选 `f542d9e78d564a749cb5c5e8568f671b634dcd77`；各轮精确证据与纯验证边界见下文。没有在开发机创建用户、挂载文件系统或操作真实服务。

## 目标与范围

增加一个仅在 GitHub 托管 Ubuntu 24.04、Python 3.13 上显式运行的串行案例：真实标准用户服务从保留安装 A 启动，经公开管理 API 迁入 A、切换到 B，再让下一次切换到 A 的合法目标实例拒绝健康检查，实际回退 B。systemd、D-Bus、loaded unit、`/proc`、cgroup、writer lease、bootstrap、nonce HTTP、activation 和投影均使用产品实现。

A、B 复制同一个当前 MuLi 包，保留原 `compatibility.json`；依赖来自 CI 已有安装。它证明同版本的两个真实保留环境可以切换、回退，并保持最新数据库，**不证明跨版本发行升级或依赖安装**。现有[两个真实进程回归](managed-process-recovery.md)继续保留原期限和假 manager 边界，不改成这个新案例，也不提高其预算。

实现只改 CI，并新增一个 stdlib 协调脚本、一个测试辅助脚本和对应纯 fixture 测试：`.github/workflows/ci.yml`、`scripts/check_managed_native_ci.py`、`tests/helpers/managed_native_worker.py`、`tests/test_managed_native_ci.py`，以及本设计文档。不修改生产协议、controller、launcher、probe、backend、服务模板或恢复算法，不新增产品配置和恢复账本。

## 专用 UID 与已有解释器

CI 协调脚本为本次运行创建唯一非 root 用户和组，记录实际 UID/GID、精确 HOME 和私有根。镜像文件、setup receipt 与导出结果仍在 `RUNNER_TEMP` 内由 runner 拥有；唯一 ext4 挂载入口位于现有 `/tmp` 下的本次独占目录。其 HOME、配置、数据、用户 unit、两个 retained prefix、辅助脚本均在这一个 0700 私有卷中。创建冲突直接失败，不复用已有用户；不修改 runner 用户、祖先目录的 mode/ACL/补充组、全局 `user.slice` 或其它 UID 的 manager。

setup receipt 单独放在 root/runner 拥有、临时 UID 不可写的位置，不放在该 UID 的可写卷中。它逐阶段记录本次已确认创建的用户/组、实际 UID/GID/HOME、镜像文件身份和精确 mount 身份；先记准备意图不能冒充已经创建成功。每次 setup 失败和 `always()` 重入清理都重新核 username↔UID/GID/HOME 及挂载身份仍一致，只处理本次已证实创建的资源。旧 UID 已被复用、映射改变或效果不能确认时保留证据并失败，不能依旧 receipt 删除后来账户；首次 setup 未成功创建的资源不归本次清理。若 `useradd` 已产生账户但响应丢失，未确认 receipt 不授权删除；清理发现同名账户或组会明确失败，不能报告已清理。

客户端的 `env -i` 不会自动成为 `systemd-run` service 模式中服务的环境。专用 `user@UID.service` 启动前，协调者只为本次已确认的新 UID 排他创建一个 runtime instance drop-in，在同一文件写入原四项资源限制，并固定其 `HOME`、`XDG_CONFIG_HOME`、`XDG_RUNTIME_DIR` 与用户 bus 地址，随后 daemon-reload，再启动该 manager。不调用 `set-property`，也不创建或领养 `system.control` 配置。标准 `muli.service` 与恢复 helper 从这一 manager 继承环境；不覆盖产品 `unit_dir`、`ExecStart` 或服务模板。四个固定系统 unit 根下若已有该 UID 的 instance drop-in 或主 unit override，直接拒绝，不领养、覆盖或通过 revert 删除。它不修改 runner 或系统全局环境，也不是对同权限 root 的沙箱。

receipt 分别确认这个 root 拥有的 0755 目录与 0644 文件的 dev/inode；文件仅含原四项资源限制与上述四个无凭据的派生环境赋值。启动前及 cleanup 的首个 stop/revert 前，复核原目录、owner/mode、精确文件及内容，最多观察两个目录项，额外项直接拒绝；其它三个系统 unit 根若出现本 instance 配置，也一律拒绝，不能把已确认的文件扩展为整个 `system.control` 目录的删除权限。创建已生效但返回丢失时保留未确认配置并失败，不凭文件名删除；已确认且仍为空的部分创建目录可精确回收。正常停止专用 manager 后复用既有 instance revert 生命周期，确认该目录消失再清掉相应 receipt 身份。所有临时账户、卷和未知效果的失败语义保持。

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

环境诊断只导出实际 UID、HOME、配置目录、runtime 目录与 bus 是否匹配本次私有上下文的布尔值，以及配置目录来源为 `explicit` 或 `default`；不导出环境值或完整环境。drive 在启动标准服务前核验，现有测试 hook 在真实 app 构造后核验服务进程，cleanup worker 也核验。配置目录仍由实际 `UserService` 默认规则计算，不能靠传入 `unit_dir` 绕过。环境不匹配固定失败；这些比较不是 ACL、任意外部环境或跨用户可达性的证明。

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

## 第四轮远端结果与专用 manager 环境

路径修复的精确签名候选 `e1bb47b19a01671b70e457cedfa119f3566ad6d5`，CI [39572518905](https://github.com/69gg/MuLi/actions/runs/39572518905) 最终六项成功、三项失败；另两个失败是 Ubuntu 3.13 的既有合成 rollback 20 秒总期限例与 macOS 3.12 的既有数据库结构读取预算，分别交由原路径审查，没有提高预算或混入本环境片。

原生 [job 119129953174](https://github.com/69gg/MuLi/actions/runs/39572518905/job/119129953174) 的 654 字节[有限 evidence](https://github.com/69gg/MuLi/actions/runs/39572518905/artifacts/12332114203)，zip SHA256 为 `d0d92f1d333665d02aab3f3ac3edabd678915376c5fd0969eb2a5b2504a3eec2`。真实 harness 已进入 drive：`imports_verified=496ms`、`slots_verified=774ms`。实际 harness kernel 限额为 256 MiB、swap 0、单 CPU、32 tasks；其专用 manager 为 1 GiB、swap 0、单 CPU、128 tasks，均核验通过。这证明前一轮执行路径修复已让实际临时 UID 完成导入、真实文件系统与两 slot 核验。

结果为 `completed=false/stage=slots_verified/code=native_unit_path`，尚未到 `manager_start`，即没有启动本次标准 MuLi 服务、仍为 **0 个事务**。这个固定失败只证明默认 `UserService.path` 不等于私有 HOME 下的标准 unit 路径；本轮尚无实际 HOME/XDG 布尔诊断，不能断言究竟哪个变量漂移。`systemd-run` service 不继承客户端全部环境，因而客户端 `env -i` 本身不足以约束该默认路径；本片按上述专用 manager Environment drop-in 闭合这个接缝，真实结果仍待远端。

本轮 cleanup 仍为失败，不能用上一轮的可执行文件查找错误解释，也不把功能启动进展当成正常收尾通过。receipt 记录该次专用账户 `muli-ci-5ab591afe1a7`、UID 999/GID 987 已删除、已记录 mounts 为空；仍只证明这些资源回收。

本次新增 23 项纯 fixture，覆盖专用环境在 manager 启动前准备、fresh coordinator 精确回收、四类根路径既有 instance 配置/主 unit override 冲突、mkdir/install 已生效但丢返回、已确认空目录的部分失败，以及环境被替换/改写/加项时拒绝清理。worker 的 explicit/default 配置来源通过真实 `UserService` 默认路径验证；字段类型或额外环境值不能混入导出。

独立审查在初轮 22 项新增例、完整 111 项通过后，发现 `set-property` 生成的 `system.control` 目录被整体接受，会使 `revert` 删除后来新增的外来文件。将 fake revert 改为忠实删除四个 unit 根的 instance 配置后，新反例在旧实现中稳定失败；修复改为同一自有文件承载资源与环境，删除 `set-property`，其它 instance 配置一律在首个 stop/revert 前拒绝。该反例证明 foreign 文件保留且没有清理效果，不是本机真实服务实验。

最终完整 **112 个唯一 case**（原 89 加新 23）通过 0.49 秒、服务 1.016 秒、峰值 77.4 MiB/swap 0；先前 111 项、新组与两项关键尾例均包含在最终总数内，不重复计数。三份 Python 文件 Ruff/format 和两脚本 strict mypy 通过；本片 mypy 最高 158 MiB，最终尾轮 68 MiB，swap 0。没有提高任何期限或资源预算；所有命令仍经 root 绝对路径守卫、逐条 30 秒串行执行。没有本机原生 harness、账户/挂载/服务效果或构件下载；专用环境、三事务和正常服务收尾仍待下一精确远端候选验收。

## 第五轮远端结果与 manager 准备诊断

签名环境修复 `70425ce78a6a203d35f83160a4d7083d32e7ab95` 无冲突合入精确 2.33 候选 `1cf078dbd31c4f71054328de9cf02a46f2805d8f`，形成签名候选 `92c2b7360801af8d8726a21f71f055378deec5ce`。CI [39577510437](https://github.com/69gg/MuLi/actions/runs/39577510437) 最终八项成功，仅原生 [job 119146605869](https://github.com/69gg/MuLi/actions/runs/39577510437/job/119146605869) 失败，在 `09:30:52Z` 报 `native_command_timeout`，随后清理报 `native_cleanup_deadline`。620 字节[有限 evidence](https://github.com/69gg/MuLi/actions/runs/39577510437/artifacts/12335500167) 的 zip SHA256 为 `6ddfefb5ec00851df26cef81c0920e46b66832bf06a8283f54e9a9c60519f1d4`。

本轮只有 receipt，没有 worker result：专用环境目录及文件已经确认，但 `manager_group` 仍为空；本次 UID/GID 999/987 的账户/组未标记删除，三个挂载仍记录在 receipt。它不能证明已启动 harness，更不能证明三事务或正常清理。环境文件的持久化早于 `daemon-reload`，manager cgroup 赋值晚于 `start` 和 `show`；现有证据只能界定这三条命令之一未完成，不能据此猜测 HOME、PAM、总线或冷启动耗时。

下一诊断只增加以下事实，不改变准备与清理效果：

- receipt 保存严格固定的 `setup_phase`：初始值，以及 `daemon_reload`、`manager_start`、`manager_show` 各自的 `before`/`after`。`manager_start` 仍是一条命令启动原来的 runtime-dir 与 manager 两个 unit，不拆命令增加等待额度。阶段写入先于命令，只有命令成功并返回才写 `after`。
- 准备失败后，runner 自己已有的解释器仅用系统 manager 的显式 `--system` 通路，复用 typed D-Bus 解析读取本次已核 UID 的两个精确 unit；不依赖 retained Python 或用户 bus。只保留有限 load/active/sub/result、PID、退出码、job ID 和 cgroup 是否存在，不读 journal、环境或原始 stderr。精确 typed 列表空集才是缺席，GetUnit/属性错误仍保留观察阶段。
- 整次观察仍共享既有 5 秒绝对诊断期限与 32 KiB 字节上限，不重试启动、不续功能期限，也不借诊断推进 cleanup。观察失败不能覆盖原准备超时；清理没有实际完成时保留原 receipt 和失败事实。真实阶段原因仍待下一精确远端候选。

新增 **24 项纯 fixture** 通过 0.24 秒；完整 **136 个唯一 case**（原 112 加新 24）通过 0.55 秒、服务 1.090 秒、峰值 80.6 MiB/swap 0。它们覆盖三条原命令前后阶段、原异常优先、诊断导出失败/超时、严格系统 manager 查询、缺席与 GetUnit 错误区分、部分观察和累计字节预算。首轮 Ruff 指出 `zip` 需显式声明允许部分观察结果，strict mypy 指出自定义校验函数不能完成可选文本的类型窄化；两处均按原合同窄修，受影响的 8 项查询例再次通过且不另计唯一数。三份 Python 文件 Ruff/format 与两脚本 strict mypy 最终通过；本片最高 163.5 MiB 出现在首轮 mypy，修后 mypy 峰值 157.5 MiB，swap 均为 0。所有检查逐条经 root 绝对路径守卫、每条 30 秒串行执行，没有本机原生 harness、CLI、账户/挂载或真实 manager 效果；诊断片不构成第五轮根因修复或三事务/正常清理通过的证明。

## 第六轮结果与进程身份诊断草案

精确签名 `7349b47a7debe367530d81f4bb0778c92651f68a` 的 [CI 39580845313](https://github.com/69gg/MuLi/actions/runs/39580845313) 最终八项成功，仅 [native 119157776174](https://github.com/69gg/MuLi/actions/runs/39580845313/job/119157776174) 失败。1077 字节 [evidence](https://github.com/69gg/MuLi/actions/runs/39580845313/artifacts/12334889838)，zip SHA256 `f5b258316542c4f17e00af2858144b4bc0a03e0d58571ecb3fb1c20da0b89c44`，只有 1153 字节 receipt 和 769 字节 result。

receipt 现在将原命令超时定位在 `manager_start_before`。独立系统 manager 观察成功：`user-runtime-dir@999.service` 为 loaded/active/exited、退出成功、PID/job 均为 0；`user@999.service` 为 loaded/activating/start，MainPID 2342、job 1891、cgroup 存在。后者的 Result 字段为 success 不代表启动完成；本轮仍在 manager 准备阶段、没有 harness 结果或事务。清理报 `native_cleanup_deadline`，账户/组未标记删除、三个挂载仍在 receipt，不宣称资源回收成功。

固定 v255 源码不支持将该结果直接归为设置自身 D-Bus 地址后同步等待自己：`manager_dbus_is_running` 先检查 dbus.socket 与 dbus.service，核心 API bus 初始化被其门控；用户地址的默认值原本就是 `unix:path=$XDG_RUNTIME_DIR/bus`，Hello 和名称申请为异步。用户 manager 在 basic.target 活跃时发送 READY；进入主循环后，启动 jobs 全部结束也会走 READY 路径。另一方面，notify service 在 PAM 和最终 exec 前便登记初始 MainPID，故目前也不能从 PID 推断已在运行 manager。以上只排除一个直接等待机制，不排除外部 PAM/generator 或其它准备步骤，不能作为移除环境、改 no-block 或提高期限的依据。

对应固定源码：[API bus 门控](https://github.com/systemd/systemd/blob/v255/src/core/manager.c#L1814)、[用户地址选择](https://github.com/systemd/systemd/blob/v255/src/libsystemd/sd-bus/sd-bus.c#L1336)、[异步 Hello](https://github.com/systemd/systemd/blob/v255/src/libsystemd/sd-bus/sd-bus.c#L585)、[basic.target 就绪通知](https://github.com/systemd/systemd/blob/v255/src/core/manager.c#L3743)、[notify 主 PID 登记](https://github.com/systemd/systemd/blob/v255/src/core/service.c#L2198)、[PAM 先于最终 exec](https://github.com/systemd/systemd/blob/v255/src/core/exec-invoke.c#L4173)。

下一诊断已按以下边界实现并完成纯例/静态检查，尚未重跑远端：

- 复用同一个 5 秒绝对结束时刻与累计 32 KiB，先完成原两个 unit 的 typed 观察；仅当 user@ 的 MainPID 为合法非零整数、ControlGroup 为已核绝对路径时，观察这个精确 PID。无 PID 不读取 proc，也不将其解释为 manager 已退出。
- 在一个只读身份窗口中读取有限 `/proc/PID/stat`、`status`、`cgroup` 与 exe 链接；前后 stat 的 PID/startticks 必须相同，前后 typed MainPID/ControlGroup 必须一致，两次 proc cgroup 都必须属于该 unit 的同一路径或其子组。允许 manager 合法进入 init.scope，但不接受其它组或变更 PID。所有原始字节计入同一预算，每一步复用原结束时刻，没有重试或新的诊断窗。
- exe 跨 UID 观察如需特权，只复用有限的 `sudo --non-interactive -- readlink -- /proc/PID/exe` 命令，PID 只来自上述 typed 结果；不提升整个 worker，不接受外部路径。仅将实际存在、root 拥有且不可由组或其它用户写入的固定 `/usr/lib/systemd` 或 `/lib/systemd` 下 `systemd`、`systemd-executor` 文件的解析路径用于 `manager`/`executor` 分类；其它路径为 `other`，不以 basename 猜测，不导出原路径。executor 类别只提供尚未观察到 manager 镜像的阶段线索，不擅自确定卡在 PAM 的哪一步。
- exe 和 status 中四个 UID 各观察两次且必须一致；PID/startticks 不变仍可能发生 exec，因此这些是有限窗口中的观测，不是全字段原子快照。只增加严格有限的观察码、exe 类别、UID 是否匹配与最后单字符 proc 状态。权限失败明确 unavailable；PID/startticks/cgroup、exe/UID 或 typed 绑定变化时不发布所读 exe/UID/状态。UID 不匹配本身可以是执行前阶段事实，不作为其它进程的授权。原 argv、env、comm、路径、日志与任意错误正文均不导出。
- proc 内部能捕获并返回的失败或预算错误保留此前两个 unit 的事实，不覆盖原准备超时，也不改变任何 start/stop、环境、资源值或清理效果。整个 worker 被外层 5 秒结束，或 coordinator 在同一 deadline 拒收整包时，不承诺仍可回传部分事实，不增加接收宽限。纯例覆盖 manager/executor 两类、无 PID、权限拒绝、PID/startticks 或 cgroup 变化、累计字节/原 deadline 和恶意 exe 路径不进入导出；真实检查仍只由后续托管 CI 执行。

本片新增 **29 个唯一纯 case**：原 8 项系统查询例扩展 17 项进程窗口反例，另有 7 项输出白名单和 5 项实际固定路径/文件归属分类例。定向 37 项（含原 8 项）通过 0.23 秒；完整 **165 个唯一 case**（原 136 加新 29）通过 0.58 秒、服务 1.128 秒、峰值 80.9 MiB/swap 0。补强有限 proc 读取参数必须使用剩余预算、且逐次严格递减的断言后，受影响 25 项再次通过 0.22 秒，不另计唯一数。权限拒绝、读失败、超预算和内部捕获的期限错误均验证原两个 unit 事实仍保留；整包晚到的既有拒收门保持，未模拟真实 PAM 或 manager 启动。

三份 Python 文件 Ruff/format 及两脚本 strict mypy 通过。首次类型命令未指定 worktree 源路径，使依赖被当作无类型安装包，报 6 处 `no-any-return`（含既有位置），峰值 224.2 MiB；显式 `MYPYPATH=src` 后通过，峰值 189.3 MiB，没有改类型规则或既有业务逻辑。所有命令逐条经 root 绝对路径守卫、每条 30 秒串行，swap 均为 0，没有本机原生 harness、CLI、manager、账户或挂载效果。第六轮根因仍未证明，三个真实事务及正常清理继续待后续精确远端候选验收。

## 第七轮结果与等待观察（纯验完成，远端待验）

精确签名 `7a0ece9b3274c229a687c0514478b9dea55e379f` 的 [CI 39585202872](https://github.com/69gg/MuLi/actions/runs/39585202872) 已九项终态：八项成功，仅 [native 119172272430](https://github.com/69gg/MuLi/actions/runs/39585202872/job/119172272430) 失败。1103 字节 [evidence](https://github.com/69gg/MuLi/actions/runs/39585202872/artifacts/12338678601) 含 receipt 1140 字节及 result 846 字节，zip SHA256 为 `df2994ec8d7fa800f764b7fced52e2ef96c18e069c0a490b31edca33c34ebb13`。阶段仍为 `manager_start_before`，user@ 为 activating/start、PID 2527/job 1824，cgroup 存在。进程窗口为 `observed/manager/uid_matches=true/state=S`，证明观察时已经是专用 UID 的 manager 镜像；它不再是当时仍在 executor/PAM 最终 exec 前的窗口，但 S 不能说明具体等待原因。仍为 **0 个事务**，没有 harness 结果；清理期限失败，账户/组及三个挂载没有回收成功证明。

固定 v255 的实际边界是：`manager_startup` 先同步运行 environment generators 和 generators，再枚举、加载及准备 manager 的 sockets；之后 `main` 才排入默认 target job 并进入 `manager_loop`。generator 的 `execute_directories → execute_strv` 会 fork 中间进程并等待它退出，因此 manager 镜像已经运行时，仍可能在主循环前同步等待孩子。进入循环后，`manager_check_finished` 在 basic.target 活跃或启动 jobs 清空时发送 READY。以上列出可发生的阶段，不证明本次停在 generator 或某个具体 job。[固定启动顺序与 READY](https://github.com/systemd/systemd/blob/v255/src/core/manager.c)、[默认 target 到主循环](https://github.com/systemd/systemd/blob/v255/src/core/main.c)、[同步 generator 执行与等待](https://github.com/systemd/systemd/blob/v255/src/shared/exec-util.c)。

候选观察的取舍如下；首片选择第一项，不增加第二套查询或进程树框架：

| 候选 | 能取得的最小收益 | 本轮取舍 |
| --- | --- | --- |
| 在原 MainPID 身份窗口内读有限 `wchan` | 区分命中已核内核符号的 child wait、epoll wait 与未知等待 | 首选；沿现有 derived proc 路径与剩余预算，新增一个固定类别 |
| 用户 manager 的 typed Manager/Jobs | 成功时可直接看到 manager/job 状态 | 暂不选；`busctl --address` 仍设 bus-client，不能据此当 peer；`--auto-start=no` 只约束消息，不能证明连接用户 bus 不触发 socket activation；现有 systemctl 用户连接又有 fallback，不能把失败当 manager 未进入主循环的证明 |
| 有限 direct children | 仅能提供某个已绑定直接孩子当时存在的正向事实 | 暂不选；不冻结孩子时 children 列表可能遗漏，空列表不能证明无孩子；中间 generator wrapper 本身也可能仍是 systemd 镜像，额外身份读取不能直接确定具体 generator |

用户 bus 的取舍依据为固定 v255 [busctl acquire_bus](https://github.com/systemd/systemd/blob/v255/src/busctl/busctl.c)、[用户 manager 连接及 fallback](https://github.com/systemd/systemd/blob/v255/src/shared/bus-util.c) 与 [private listener](https://github.com/systemd/systemd/blob/v255/src/core/dbus.c)；本方案不另写 D-Bus/ctypes 客户端。

本片实现沿用已批准的最小 proc 合同：

- 原两个 typed unit 与现有 PID/startticks、typed MainPID/ControlGroup、proc cgroup、exe/UID 前后门保持。先完成并保存原进程观察，再对这次已绑定 PID 在同一绝对期限内读两次 `wchan`；只有两次 token 相同且尾部 UID/exe/cgroup/startticks/typed 绑定仍与原观察一致才发布类别。原窗口本身变化仍拒绝原进程事实，附加尾门变化仅拒绝新增类别；此前已经验证的有限窗口不被回溯当作原子快照。不轮询、不 sleep、不暂停进程。跨 UID 必要时仅沿既有有限 sudo 命令读取精确 `/proc/<typed PID>/wchan`，不提升整个 worker。
- 只导出 `child_wait`、`epoll_wait`、`other` 或 `unavailable`。固定已核符号 `do_wait` 归为前者，`ep_poll`/`do_epoll_wait` 归为后者；不做子串或函数名模糊匹配。`0`、空值、权限拒绝、格式坏、两次值变化或绑定变化不能声称没有等待。未知内核符号归 other，不新增符号搜集/内核版本下载。原始符号、stack、argv、env、comm 与私有路径不导出。
- `wchan` 是内核睡眠位置而不是用户态调用栈。`child_wait` 只支持观察时在对应 child-wait 内核路径，不能指定哪个孩子、generator、PAM 或等待多久；`epoll_wait` 只支持当时的内核 epoll 等候，不能证明已进入 `manager_loop`、basic.target 已活跃或 READY 已被接收。两次读取及身份门仍不构成全字段原子快照。[proc wchan 合同](https://man7.org/linux/man-pages/man5/proc_pid_wchan.5.html)、[固定 Linux v6.8 child wait](https://github.com/torvalds/linux/blob/v6.8/kernel/exit.c)、[固定 Linux v6.8 epoll wait](https://github.com/torvalds/linux/blob/v6.8/fs/eventpoll.c)。这些内核源码仅用于已知符号的语义，不声称已经核定 runner 的精确内核版本或保证符号稳定。
- 新字节、命令 stdout/stderr 与原观察共用已有 **5 秒绝对期限、32 KiB 累计上限**，没有独立额度。wchan 自身读取失败只让新增类别 unavailable，不抹掉已经验证的 unit/进程事实；身份改变不能发布等待类别。内部可捕获错误与外层杀掉 worker/迟到整包拒收的区别继续保持。功能、清理、环境、Type、start 阻塞语义与资源上限均不变。
- 未来若上述结果仍不足，再单独审最多 4 个 direct child 的正向观察：只读该主线程的 children 候选，不扫 `/proc` 或递归；每个孩子须前后 PPid 等于原父 PID、子 PID/startticks 稳定、同专用 cgroup，并且父身份门仍成立。无论空集、退出、遗漏或超限都不能推出无子进程或授予 stop/kill 权限。本轮不实现该分支。[children 非冻结读取限制](https://man7.org/linux/man-pages/man5/proc_tid_children.5.html)。

本片新增 **26 个唯一纯 case**：22 项等待分类/读取/尾部门控与 4 项新增字段导出拒绝，覆盖固定类别、未知/零值/畸形 token、两次变化、身份/权限/剩余字节/原 deadline，以及内部失败仍保留旧事实；取消继续传播。它们通过原 `diagnose_setup` 和注入的命令读取，所有真实 subprocess 入口仍由模块 fixture 硬拒绝。22 项定向组通过 0.20 秒、峰值 71 MiB/swap 0；完整 **191 个唯一 case**（原 165 加新 26）通过 0.60 秒、服务 1.135 秒、峰值 81.6 MiB/swap 0，定向重叠不另计。首次定向组的空响应例失败于夹具要求：两次响应均为零字节时，剩余字节预算可以相同；仅为该例放宽为允许相等后通过，非空响应仍要求递减，没有修改产品预算。

三份 Python 文件 Ruff/format 与两脚本 strict mypy 均通过；格式检查发现的两处排版已修正。本轮最高峰值为类型检查的 **159.1 MiB/swap 0**。所有命令逐条经 root 绝对路径守卫、每条 30 秒串行，没有本机原生 harness、CLI、manager、账户或挂载效果。根与独立审查均未发现确定 P1/P2；本片只增加诊断，所有真实 MainPID、wchan 与 manager 等待阶段仍只能由下一精确托管候选验收，三个真实事务与正常清理尚未通过。

## 第八轮结果与下一观察边界

合并根冻结基线后的精确签名 `b516cf230be027cc87fcf1f132e928bd27026d67`，其 [CI 39590177536](https://github.com/69gg/MuLi/actions/runs/39590177536) attempt 1 已九项终态：八项成功，仅 [native 119188882495](https://github.com/69gg/MuLi/actions/runs/39590177536/job/119188882495) 失败。1114 字节 [evidence](https://github.com/69gg/MuLi/actions/runs/39590177536/artifacts/12342530101) 只有 receipt 1139 字节与 result 866 字节，zip SHA256 为 `c470f81c6580194feed7f958d99352b6a573c72e0622960a644d379ed705c6b7`。固定失败仍为 `native_command_timeout`，随后为 `native_cleanup_deadline`。

阶段仍是 `manager_start_before`。`user-runtime-dir@999.service` 为 active/exited、Result=success、Job=0；`user@999.service` 为 activating/start、Result=success、PID=2504、Job=1448，cgroup 存在。已绑定进程窗口给出 `observed/manager/uid_matches=true/state=S/wait=epoll_wait`。新增等待类别仅证明该有限窗口命中已核 epoll 内核等待符号，不能推出已经进入 `manager_loop`、basic.target 已活跃、READY 已发送或被 PID1 接收，也不能据此确定启动阻塞原因。

仍为 **0 个事务**，没有 harness 结果；receipt 的账户和组删除标记均未成立，仍记录三个挂载，因此没有账户、组或挂载回收成功证明。下一步仅反查 fixture 的启动假设并独审定向 journald 正向证据提案：在原 5 秒/32 KiB 内按精确 unit、当前 InvocationID 与消息来源取有限结构化字段，用已知启动完成或 job 结果证伪明确假设。该提案尚未实现；空结果、缺字段或查询失败不作为阶段缺席证明，不采集原始 MESSAGE，不改变环境、Type、阻塞启动语义或任何期限。未来 CI 分片调整不倒改本轮九项及此前各轮的历史计数。

## 定向 journald 诊断（纯验完成，远端待验）

根与独立审查通过后，本片仅接入上述只读观察，不改变失败的 setup 或 cleanup 效果。明确待证伪假设为“本次 manager 始终没有离开主循环前的同步启动阶段”：只有可信来源绑定到同一专用 manager PID、unit 与当前 InvocationID 的 `USER_STARTUP_FINISHED` 是强正向反证。`UNIT_STARTING`、`UNIT_STARTED`、`UNIT_FAILED` 只说明该 manager 处理过相应 start job；包括条件未满足而跳过的 job 也可能得到 done，不能据此声称 basic.target 当前 active 或 PID1 已接收 READY。无已知事件只是 unknown，不等于上述假设成立。[v255 完成事件与主循环门](https://github.com/systemd/systemd/blob/v255/src/core/manager.c#L3361-L3524)、[job 事件与条件跳过](https://github.com/systemd/systemd/blob/v255/src/core/job.c#L658-L750)、[固定消息 ID 及 job 语义注释](https://github.com/systemd/systemd/blob/v255/src/systemd/sd-messages.h#L61-L141)。

- 仅在此前进程观察已确认 manager 镜像及专用 UID 后追加查询；两侧复核 typed Id/InvocationID/MainPID/ControlGroup、proc startticks 与 cgroup，变化则不发布新事件。它仍是有限身份窗口，不是原子快照；此前已完成的 unit/proc 事实继续保留。
- 一次固定 `sudo --non-interactive -- journalctl --boot=0 --no-pager --quiet --output=json --lines=33 --output-fields=...`，以 `+` 合并两个 AND 分支：PID1 的 `_PID=1/_UID=0/UNIT=精确user@/INVOCATION_ID=本次ID`，以及 manager 的 `_PID=精确MainPID/_UID=专用UID/_SYSTEMD_UNIT=精确user@/_SYSTEMD_INVOCATION_ID=本次ID`。各分支只匹配四个固定 MESSAGE_ID。系统 manager 的 UNIT/INVOCATION_ID 与用户 manager 的 USER_UNIT/USER_INVOCATION_ID 不混用；不连接用户 bus，不用宽泛 `-u`、grep、follow 或 catalog。[v255 字段选择](https://github.com/systemd/systemd/blob/v255/src/core/manager.c#L832-L844)、[日志字段来源](https://github.com/systemd/systemd/blob/v255/man/systemd.journal-fields.xml)。
- 请求字段只有上述来源/归属字段及 USER_UNIT、MESSAGE_ID、JOB_TYPE、JOB_RESULT。JSON 自动附带的四项元数据允许并核定类型后丢弃；重复键、数组、缺字段、坏类型或额外字段的记录不作为正向证据。原始 MESSAGE、argv、env、exe、nonce、配置、私有路径、cursor 与时间戳均不导出。只导固定 source/event/unit/job_result 类别；内部 unit 仅 basic/default/dbus/other。USER_INVOCATION_ID 属于被谈论的内部 unit，本片不查询它，也不把它当外层 ID。[v255 JSON 字段与条数合同](https://github.com/systemd/systemd/blob/v255/man/journalctl.xml#L547-L566)。
- 最多处理最近 32 条；有第 33 条时只将最旧一条作为 incomplete 哨兵，不推断更早记录。查询 stdout/stderr、所有自动元数据、前后 typed 与 proc 核验共用原 **5 秒绝对期限及 32 KiB 累计上限**，没有独立时间窗或第二次查询。权限、格式、身份、期限或字节错误仅使新增 journal 观察不可用，取消继续传播；外层结束 worker 或拒收迟到整包时仍不承诺回传部分事实。

本片新增 **54 个唯一纯 case**：42 项经完整 `diagnose_setup` 的来源、事件、身份、重复键/数组/额外元数据、33 条哨兵、字节/期限与取消反例，以及 12 项 coordinator 输出白名单拒绝例。54 项定向通过 0.28 秒、峰值 74.2 MiB/swap 0；完整 **245 个唯一 case**（原 191 加新 54）通过 0.70 秒、服务 1.256 秒、峰值 82.4 MiB/swap 0，定向重叠不另计。所有真实 subprocess 入口仍由模块 fixture 硬拒绝。

三份 Python 文件 Ruff/format 与两脚本 strict mypy 最终均通过。首轮 strict mypy 发现两处 `Any | None` 未收窄：可缺省的 USER_UNIT 改为等价空串默认，输出文本改为显式 `isinstance` 拒绝，不改变固定错误或事件判断；受影响 54 项随后再次通过 0.15 秒、峰值 57.6 MiB/swap 0，不另计唯一数。类型首轮峰值 **164.5 MiB**，修后 **159.2 MiB**，swap 均为 0。全部命令逐条经 root 绝对路径守卫、每条 30 秒串行，没有本机原生 harness、CLI、manager、账户或挂载效果。下一精确远端候选之前，不声称已观察到真实 journald 事件、找到启动根因、完成三个事务或证明正常清理。

本片不验收跨版本声明、真实 wheel/依赖安装、在线下载、用户已有服务、账号/模型、断电或 macOS/Windows 切换。64 MiB sparse 卷与只读 bind 也不是恶意代码沙箱或宿主总磁盘 quota。

## 第九轮结果

精确签名 `8d5b1814466846dc4376d52caab4b51aa15bed4c` 的 [CI 39597034714](https://github.com/69gg/MuLi/actions/runs/39597034714) attempt 1 已十一项终态：十项成功，仅 [native 119211788494](https://github.com/69gg/MuLi/actions/runs/39597034714/job/119211788494) 失败。1146 字节 [evidence](https://github.com/69gg/MuLi/actions/runs/39597034714/artifacts/12343594805) 只有 receipt 1139 字节与 result 933 字节，zip SHA256 为 `118b753546f4a99d397166e49e2d261c39a64e252b62a92d66d5f1020d2dfd0a`。固定错误依次为 `native_command_timeout` 与 `native_cleanup_deadline`。

阶段仍为 `manager_start_before`：runtime-dir 为 active/exited、Job=0；user@ 为 activating/start、MainPID=2501、Job=1572、cgroup 存在。进程窗口为 `observed/manager/uid_matches=true/state=S/wait=epoll_wait`。新增 journal 查询成功返回，但严格解析后为 `no_known_event/events=[]/incomplete=false`；这不证明原始查询没有记录，也不能证明 manager 始终未进入主循环。仍为 **0 个事务**，没有 harness 结果。receipt 未确认账户或组删除，仍记录三个挂载；正常清理没有通过。后续只研究启动和客户端等待合同，不据这组事实修改 UID、PAM、环境、Type 或 target。

## 单次 job 准入与统一期限（已实现，纯验通过，远端待验）

现有 `NativeCI.call()` 默认给每条命令 5 秒，`prepare()` 没有覆盖整段的绝对期限；`execute()` 在准备完成后才为 drive 客户端设 `now + HARNESS_SECONDS + CLEANUP_SECONDS`，即 95 秒。因此历史失败证明的是这次阻塞 `systemctl start` 客户端越过 5 秒，尚未证明 user@ 已触及 systemd 自身启动超时，更不是已观察了 95 秒 manager 启动。v255 的 `--no-block` 只验证并排入 job，返回成功不能充当就绪；user@ 的 `Type=notify-reload` 仍须由 PID1 接受 READY 才离开 start。[固定客户端语义](https://github.com/systemd/systemd/blob/v255/man/systemctl.xml#L2044-L2051)、[固定 user@ 模板](https://github.com/systemd/systemd/blob/v255/units/user%40.service.in)、[READY 处理](https://github.com/systemd/systemd/blob/v255/src/core/service.c#L4001-L4007)。

本片只调整这个 fixture 的一次准入与观察，不增加第二轮账户或启动效果：

1. **一个 execute 期限。** `execute()` 入口即取 `time.monotonic()`，固定 `end = start + HARNESS_SECONDS + CLEANUP_SECONDS`；prepare、manager readiness、drive 与原启动诊断均消耗这同一 95 秒，准备所用时间必须扣除。协调者和 worker 只在本次同 host、同 kernel boot、同时间命名空间的 `time.monotonic()` 时基中比较该绝对值；继续由 `remaining(end)` 转成 `asyncio.timeout()` 的相对余量，不与 wall clock、journal 时间或未经确认的 `loop.time()` epoch 直接混比。普通命令和每次 typed 查询最多 `min(5 秒, end - now)`，调用前后都复核；同步文件/身份工作也在阶段边界复核。不能在 prepare 后重新计 95 秒，也不能为轮询、迟到结果或诊断续期。该限制是本片新增的总约束，不回写历史轮次。独立 workflow cleanup 仍沿其原 5 秒效果/证明及既有最末命令 reap 合同。
2. **一次准入。** 在精确新 UID、账户/配置归属与两个 unit 的初始状态核定后，仅执行一次 `systemctl --system --no-block start user-runtime-dir@UID.service user@UID.service`。原两 unit 同属一次命令，既有资源与环境 drop-in 不变。客户端成功只记 admission；失败、超时或丢返回均不重发 start，只能在剩余期限内只读观察并保留不确定状态，不进入 harness。初始已有活动实例、job 或无法排除冲突时也不启动。
3. **精确 readiness。** 沿已有 runner 解释器和系统 bus 的 `SystemdProbe` typed 解析读取精确两 unit，不连接用户 bus。首次观察到的非零 InvocationID 固定为本次实例；后续不得用新 InvocationID 或重启后的 PID 代替。manager 须 loaded、active/running、原 notify-reload 类型、Job=0、MainPID 非零；runtime-dir 须 loaded、active/exited、退出成功、Job=0。成功前复用原 proc 身份窗口，核稳定 PID/startticks、专用 UID、manager 镜像、typed/proc cgroup 归属及前后同 InvocationID；再核原四项实际 kernel 限额。客户端零退出、Result=success、epoll 或 Job 单独消失均不授予下一阶段。系统 manager 属性只表明 PID1 接受该 unit 的就绪，不额外宣称用户 bus 或 MuLi 服务已可用。
4. **有界观察。** readiness 使用已有 `POLL_SECONDS` 起步、逐次倍增且封顶于原单命令 5 秒的退避；睡眠也裁到剩余期限。每轮只读上述必要属性，完整 proc/kernel 校验只在拟接受 ready 时进行，不在循环中重复 journal/wchan。查询实际 stdout/stderr、必要 proc 字节和最后一次失败诊断累计不超过原 32 KiB；超限固定失败，不通过每轮重置额度换取无限观察。留出原 5 秒收尾诊断额度，诊断终点最多为 `end`，没有另加的 5 秒；额度不足时保留已确认的有限状态，不宣称完整诊断。
5. **期限传播到实际子任务。** 启动 drive 至少需要原 `CASE_SECONDS + CLEANUP_SECONDS`，即严格多于 65 秒剩余量；不满足则不启动新 harness 或 MuLi 服务。harness `RuntimeMaxSec` 裁为 `min(HARNESS_SECONDS, remaining - CLEANUP_SECONDS)`，不是只缩外层客户端却仍留下原 90 秒运行额度；同一绝对 `end` 和预留后的 drive 结束时刻传给 worker。worker 入场及启动标准 MuLi 服务前复核余量，`Case.deadline`、HTTP/probe、等待和结果接收均取各自原上限与共享结束时刻的较小值；不能迟到后重新给 60 秒。入场耗时导致不再具备完整 case 余量时拒绝业务启动。`RuntimeMaxSec` 是服务启动后的相对期限，排队迟到的业务不启动保证仍须 worker 的绝对门承担，不能只据该属性声称未发生迟到启动。产品已有事务期限、精确单位归属与独立清理不变；客户端退出不证明已接受的 job 或子树为空，失败仍交原所有权复核及 always cleanup，不能写成已回收。

验收结论分开表达：若**同一 InvocationID 在原请求开始 5 秒之后仍成功观察为 activating，随后在共享期限内通过完整 ready/kernel 门**，才支持“原客户端等待边界过短”；仅首次采到 ready 的时间晚于 5 秒不够，可能只是采样晚。如果最后成功观察仍 activating、整体额度耗尽，仍未解释根因；只有固定属性明确进入 failed/非成功终态才报告该终态，不能将 fixture 期限冒称 systemd 的 startup timeout。身份改变、观察失败、材料/资源不符或余量不足分别失败，不自动恢复或再建账号。

纯例合同覆盖准备耗时计入总限、各调用与睡眠不续期；一次 start 返回零但仍 activating；跨 5 秒的同代 ready 正例；active 但 Job/PID/InvocationID/cgroup/kernel 错误拒绝；start 丢返回不二次效果；95 秒末仍 activating 不运行 harness；65 秒余量门与 worker 迟到不启动业务；实际 RuntimeMaxSec、worker deadline、结果接收和最终诊断均被裁限；独立 cleanup 不改成功证据或功能 timeline。初始观察先用 `ListUnitsByPatterns` 列出两个精确名字，只对已列出的 unit 做 `GetUnit`，不让只读观察加载不存在的 unit；已加载但 inactive/dead、PID=0、Job=0、ControlGroup 为空的旧实例允许作为初态，并冻结其旧 manager InvocationID，ready 必须使用新 ID。

`late_ready` 只在**整轮 typed 观察开始时已超过原 start 请求 5 秒，且该轮仍观察同一新 InvocationID 为 activating**后置真。5 秒前读得 activating、后续 service/proc 查询到 5 秒后才完成，不能构成跨 5 秒的正向证据；独立审查发现并收紧了首稿的这个采样边界，新增跨边界反例。最终 ready 仍须同代 typed/proc/kernel 门全过，这一字段不能证明用户 bus 可用或解释启动根因。

初态、readiness 与最终 setup 诊断共用原 32 KiB 累计预算。命令丢返回时无法获得实际字节数，已授予该 worker 的额度不再重用；不能为了取失败诊断重新给予 32 KiB。独立 cleanup 仅从 runner 自有、权限私有、非符号链接的 regular 旧结果文件，最多读 32 KiB 加一个超限哨兵，并通过完整结果白名单验证后保留固定 `elapsed_ms/late_ready` 字段；无效元数据不授予清理成功，原账户、挂载、cgroup 与末期限证明保持。

本片相对原 245 个纯 case 新增 **56 个唯一 case**。首批 45 项通过；收紧 late-ready 边界后 18 项 manager-ready 组通过。全文件首跑 **4 failed/287 passed**：两项旧 cleanup 手工结果缺少新增有限元数据，一项 manager-ready 丢返回后的旧断言仍要求诊断成功，以及旧 CLI 替身未接受新预算参数。逐项核对原业务证明并同步夹具后，相关 16 项通过；最终完整 **301 项通过**，pytest 0.90 秒、guard 服务 1.468 秒、峰值 92.4 MiB/swap 0。定向组有重叠，不另计唯一数。

三份 Python 文件 Ruff 检查通过，首稿 format 检查仅排版失败后已格式化。两脚本 strict mypy 最终通过；首次发现可扩展命令 tuple 被推断成固定长度，改为显式 `tuple[str, ...]`。guard 不转发外层 `MYPYPATH`，因此在 guard 内为目标 tests/src 设置模块搜索路径，并使用 `--explicit-package-bases`；没有改变项目规则。类型检查峰值 168.2 MiB/swap 0。所有执行均经 root 绝对路径 guard、每条 30 秒串行；全部 subprocess、系统文件、进程和时钟边界由纯 fake 注入，真实效果入口硬拒绝。本片没有本机原生 CLI、systemd、账户、挂载、网络、模型或服务效果，也没有新的远端重跑。九轮仍仅支持前述 **0 个事务、正常清理未通过**的历史结论；不据纯例宣称真实 manager 已 ready、三个事务完成或清理成功。

最终独立只读审查确认：轮前 `observed_since` 已闭合 late-ready 的采样边界问题；独立 cleanup 的旧结果读取只继承通过归属、权限、文件类型、字节上限与白名单验证的 readiness 元数据，不授予 completed 或 cleanup 成功。后续未发现确定 P1/P2。保留原 prepare 的 kernel 二次复核作为既有资源事实，新 readiness 核限读取已计入观察预算。审查未运行检查；301 项之后仅有格式、tuple 类型注解与本段文档变更，未改变行为。

## 第十轮 native 失败取证（最终十绿一原生红）

精确签名合并候选 `3f0abb3400860ed3ab560b5c34e315bde40a710f` 的 [run 39968348139](https://github.com/69gg/MuLi/actions/runs/39968348139) attempt 1 中，[native job 120464058434](https://github.com/69gg/MuLi/actions/runs/39968348139/job/120464058434) 已于 2026-10-23 22:18:53（Asia/Shanghai）终态失败。一次取证查询得到 run 仍为 `in_progress/conclusion=null`；其余矩阵尚未终态，不将 native job 的结束写成整轮结束，也没有继续轮询。

有限 job 日志保存于 `/tmp/muli-native-39968348139-120464058434.log`。事务步骤从 22:17:32 左右进入，22:18:45 输出 `native_manager_observation`；独立 always cleanup 随后执行，并于 22:18:50 输出 `native_cleanup_deadline`，两个步骤均非零退出。前述约 72.9 秒包含准备与观察，不能当作 manager 自身等待时长，更不能声称已耗尽完整 95 秒或触及 systemd startup timeout。

只下载元数据确认大小为 **997 字节**的 [managed-native-evidence 12507977390](https://github.com/69gg/MuLi/actions/runs/39968348139/artifacts/12507977390)，未下载同时列出的其他包。zip SHA256 为 `b0ae8e1f5fd09b59b2616dbfaab6df3d7ae816173ffb6338a14a9c2ece5c22c4`，与上传日志一致。本地一次 guard 只读先核两个 entry 的名字、非目录/非符号链接类型与大小，再读有限 JSON，没有解压写盘或执行内容；只有 `receipt.json` **1154 字节**和 `result.json` **533 字节**。该次 guard 服务 75 毫秒、峰值 15.7 MiB/swap 0，完成即归还；没有运行测试或本机原生 fixture。

- receipt：UID=999、GID=987，`user_created=true`，`setup_phase=manager_show_before`，专用环境文件已确认；`manager_group` 为空，仍记录三个挂载，`user_removed=false/group_removed=false`。
- result：`completed=false/stage=manager_setup/code=setup_failed`，`timeline=[]/generations=[]`。仅有 setup resource；`client_code=command_failed/diagnostic_code=command_failed/diagnostic_status=null`，观察 stage、failure、query、process 与 journal 均为 `not_started`，unit 为空、units 与 events 为空。
- **0 个事务**，没有 harness 业务结果，也没有 `manager_readiness`；不能据缺失的观察推出 unit 当前状态、同代 activating 是否跨过 5 秒、用户 bus 可用性或启动根因。cleanup 的实际命令已失败，receipt 没有证明账户、组或三个挂载已回收，正常清理仍未通过。

本次取证当时仅确认 native job 终态；根随后只读查实整轮最终为十项成功、仅原生项失败。前九轮历史计数及精确证据保持，仍为 0 个事务、正常清理未通过，不以本片纯测试改写远端事实。

## 同次 manager 失败摘要（纯验完成，远端待验）

第十轮的 generic 错误不足以确定 bus、typed reply、期限或字节根因。静态核对发现：manager worker 已取得的 query 状态未随失败回包返回；协调器只抛固定 code，没有保存这次观察。无返回时保守耗尽剩余额度，后备 `observe_setup` 又因余额不足跳过，而其已知 `native_diagnostic_output_budget` 被归为 `command_failed`。本片只补这个信息保存接缝，不新增系统查询、启动、重试或 typed 状态快照，不放宽任何 ready、身份、kernel 或 cleanup 条件。

失败回包严格只有 `code/stage/query_status/query_code/used`：失败 code 与 stage 为固定有限枚举，query code 沿已有命令分类；status 仅 null 或有符号 32 位非 bool 整数，used 为非 bool 非负整数。stage 在原查询或本地验证前同步设置；查询异常会清除上一状态并记录固定分类，取消仍传播。`query_status=0/query_code=ok` 只说明命令返回，不能证明 typed 解析或 manager ready。没有返回或未知异常体仍不能还原根因，不导出原始 stderr、异常文本、unit/proc 私有字段或输出正文。

worker 的已消费/保守预留额度 `used` 与回包字节共同计入原累计 32 KiB。协调器额外核 `used <= 本次授予额度`，回包不得超过原 1024 字节预留；全枚举与最大整数序列化纯例证明仍可容纳。worker 超额读取只有在明确 `native_diagnostic_output_budget` 失败下饱和记整个额度，不表示完整核验，也不产生可重用额度。无返回、非法 schema、超额 used 或过大回包仍保守耗尽，不为保存摘要另给额度。

协调器先验证并持久写入独立 `resources.manager_failure`，再抛本次原失败，随后才可尝试既有后备诊断。后备没预算时明确记 `output_budget` 并保留摘要；后备失败或证据写失败不覆盖原错误。独立 cleanup 仅复用既有的归属、私有权限、regular/O_NOFOLLOW、32 KiB 哨兵与完整结果白名单读取，继承这个有限 resource；它不授予 completed、ready 或 cleanup 成功。95 秒总期限、原5秒窗口、1024回包预留与全部成功条件均保持。

先补的纯 fake runner 反例在旧实现稳定失败（`manager_failure` 缺失），再改实现。首个定向组 13 过、2 失败分别是旧预算分类断言及旧失败包形状，按已批准协议更新；新增 query 例的首轮两处预期分类写错，依据已有分类函数修正，没有改生产校验。完整文件随后 **318 项通过**（pytest 1.16秒、guard 1.778秒、峰值104.2 MiB/swap 0）；再增4项非法回包/写失败反例，单独 **4项通过**。扩展既有9项 fresh-cleanup归属/白名单用例后该组再过；重叠不另计数。

静态收尾发现外层 timeout 中断尚未返回的 query 时，必须同时将 query code 记 timeout、used 保守记整个授予额度；独审指出缺少后者会让后备诊断复用未知消耗。已修正，真正外部 CancelledError 仍透传。首个新增例错误地在固定全局 monotonic 的夹具里用真实1毫秒asyncio计时器，计时器无法推进；guard 在 **30.033秒**硬终止，峰值 **81.1 MiB/swap 0**，没有提高期限或重跑同一冻结时基例。该次pytest返回session后误启动Ruff/mypy，存在短时重叠，仍处于共享slice限制内，但不符合串行要求；已明确报告。收齐全部终态后，改用纯fake asynccontext模拟外层取消转TimeoutError，并断言未返回的query占满额度；最后failure/cancel组 **26项通过**（0.33秒、峰值81.6 MiB），共 **323个唯一纯case**。后续检查严格逐项等最终exit_code，三文件Ruff/format、两脚本strict mypy均通过，最终类型峰值162.8 MiB/swap 0，完成即归还guard。

已知原run_process输出预算异常是无结构化类别的普通ValueError；本片不解析其正文、不改生产异常API。该无返回路径保守耗尽额度，但query code仍可能为command_failed，不能把它冒称已确定的预算根因。明确的NativeError/FixtureError输出预算类别会保留output_budget。所有执行均经绝对路径共享guard、每次30秒，真实subprocess入口仍硬拒绝；没有本机native、账户、挂载、CLI、服务或网络效果，也没有远端重跑。

最后统一在实际 `_SetupIO.command` 的未知返回边界捕获 Exception 或 CancelledError，保守用尽额度后原样raise，覆盖query及proc命令；外部取消同对象透传，不转换为普通失败。已返回stdout/stderr先合并计账再检查期限，避免首条charge触期限时漏记另一条已返回pipe。新增2项纯反例验证同对象取消/额度耗尽及双pipe完整计账；最后定向 **28项通过**（0.32秒、峰值81.8 MiB/swap 0），共 **325个唯一纯case**。最后Ruff/format与strict两脚本mypy均通过，类型峰值162.8 MiB/swap 0；逐项实终态后才启动下一项，完成后归还guard。本轮完整文件通过数仍是前述318，后续新增例及受影响组单独通过，不冒称最终325全文件重跑。

独立只读终审复核了pending-query期限、统一query/proc未知返回边界及已返回双pipe计账；此前P2已闭合，最终限定范围内未发现确定P1/P2。取消同对象、耗尽额度和双pipe纯例与合同一致。审查未运行检查或修改文件，完成即停止。

根整合作者签名 `fd962c4e0c27648c5a68a95ba3739e4a4f345222` 与已正式发布的 2.38 精确源码及发行证据，签名合并为 `498a5cf`；自动合并保留 MCP 附件 extras 用例和原生 job。根在整合树独立重跑最终完整文件，**325项全部通过**（pytest 0.99秒、guard 1.652秒、峰值94.1 MiB/swap 0），全部为纯替身，没有本机真实native/CLI/服务。这个结果只补齐最终整合文件证据，仍不改写前十轮0事务、正常清理未通过的事实；后续远端候选单独记录。

## 第十一轮 native 有限失败取证（最终十绿一原生红）

精确候选 `be918e519dee9b2fe884629af5ba7cbb7b3c42fb` 的 [run 39976924194](https://github.com/69gg/MuLi/actions/runs/39976924194)、[native job 120493828204](https://github.com/69gg/MuLi/actions/runs/39976924194/job/120493828204) 已失败，job于2026-10-23 23:17:02开始、23:18:30结束（Asia/Shanghai）。本次只查该job及一次artifact列表，不查询或等待其余矩阵终态。有限日志 `/tmp/muli-native-39976924194-120493828204.log` 先于23:18:21报 `native_manager_observation`，always cleanup于23:18:26报 `native_cleanup_deadline`，两步均非零；约73秒仍包括准备与观察，不能当manager自身等待或systemd startup timeout。

仅下载确认1057字节的 [managed-native-evidence 12511048578](https://github.com/69gg/MuLi/actions/runs/39976924194/artifacts/12511048578)，没有下载4885003字节的package。zip SHA256 `2a4197703296e5a97e60054e2bb5f3daf250fac0d86e01b8749a440bd4d2f37a` 与上传日志一致；guard先核两个entry数量、名字、类型及大小后，只读receipt1152字节/result680字节JSON，无解压写盘或执行，88毫秒/15.9 MiB/swap 0，完成归还。

- receipt：UID999/GID987，user_created=true、manager_show_before，环境文件已确认、manager_group为空；三个挂载仍记录，user_removed=false/group_removed=false。
- 新manager_failure：`code=native_manager_observation/stage=service_properties/query_status=null/query_code=command_failed/used=31665`。它定位到Service属性读取路径，但没有可用退出状态；used含未知返回时保守占满的额度，不证明实际输出31665字节。后备setup为diagnostic_status=null、diagnostic_code=output_budget，其余观察not_started，原失败摘要已保留。
- result仍completed=false/manager_setup/setup_failed，timeline/generations为空，**0事务**，无manager_readiness；正常清理仍未通过，不能宣称账户、组或挂载已回收。原前十轮历史保持；新摘要改善定位，不足以确定输出上限、其他ValueError或OSError中的具体根因。

根随后一次查实该精确run已全终态，十项success、managed-native为唯一failure。本片不再查询或等待全run，不盲目重跑第十一轮。

## 显式进程输出上限异常（仅失败分类）

沿用 `_OutputBudget.charge` 原有计数、边界与错误消息，仅把输出超过限制时的异常窄化为 `ProcessOutputLimitError(ValueError)`。已有捕获ValueError的调用方保持兼容，run_process的启动、双pipe共享预算、取消、reap与返回字段均不改。native `_SetupIO.query` 只按该类型记录output_budget；同文本的普通ValueError及OSError仍为command_failed，不解析异常正文。未知返回仍耗尽原授予额度并原样raise，不新增查询、配置、字段或预算，不修改95秒/5秒期限及ready/cleanup门。

这只是让未来同路径失败可区分输出上限类别，不能据第十一轮旧generic摘要倒推它已触及输出预算，更不声称修复服务、预算根因或正常清理。

新增7项纯case：直接预算单块/累计超过原边界、精确边界通过、旧消息与ValueError捕获兼容；fake run_process分别抛新类型、同消息普通ValueError及OSError，核原异常同对象传播、单次调用、unknown额度全部耗尽及仅新类型标output_budget。一次完整相关纯组 **332项通过**（完整native文件328项加直接预算4项），pytest1.19秒、guard1.812秒、峰值106.5 MiB/swap 0。process与worker及两测试文件Ruff/format通过，process与worker strict mypy通过、峰值169.8 MiB/swap 0。全部经绝对路径共享guard、每条30秒严格串行至真实exit_code；没有未收session或真实子进程/native/CLI/账户/挂载/服务，未运行process测试文件内既有真实子进程用例。检查完成即归还guard，不再追加实验。

独立只读后审已核对ValueError继承兼容、原文本、类型分类、取消及未知额度合同和第十一轮证据边界，未发现确定P1/P2。审查没有运行检查或修改文件，完成即停止。

## 第十二轮 native 有限失败取证（最终九绿两红）

精确签名候选 `691ea5c2c5a18bf7e0fb2670f538f4ef9eaa7385` 已非强制推送原PR分支，触发 [run 39979235619](https://github.com/69gg/MuLi/actions/runs/39979235619)。一次jobs快照为6项success、[managed-native 120501822975](https://github.com/69gg/MuLi/actions/runs/39979235619/job/120501822975) failure、4项in_progress；本次不等待或轮询其余矩阵，不把该job终态当成整轮终态。native于2026-10-23 23:33:07开始、23:34:35结束（Asia/Shanghai）。

有限日志 `/tmp/muli-native-39979235619-120501822975.log` 显示事务步骤于23:33:14进入，23:34:27报native_manager_observation；always cleanup于23:34:32报native_cleanup_deadline，两步均非零。前述约73.5秒仍包含准备和观察，不能等同manager自身等待或systemd startup timeout。

仅取确认1048字节的 [managed-native-evidence 12512103825](https://github.com/69gg/MuLi/actions/runs/39979235619/artifacts/12512103825)，未下载4886620字节package。zip SHA256 `f3b2109611e49fad8f1a6e90dcad3048f213a21f473c1831b5547d07b0756120` 与日志上传摘要一致。一次30秒guard先核两个entry数量、名字、非目录/非符号链接类型、每个最多32 KiB，再只读receipt1138字节/result679字节JSON，不解压写盘或执行；实终态exit0，83毫秒/16.3 MiB/swap 0，完成归还guard，没有运行本地测试。

- manager_failure：`code=native_manager_observation/stage=service_properties/query_status=null/query_code=output_budget/used=31665`。本轮明确获得输出预算失败类别，且发生在Service属性读取路径；generic顶层code及空status仍不提供命令退出结果。used是已消费/未知返回时保守占满的额度，不是实际输出字节证明。
- setup后备仍diagnostic_status=null、diagnostic_code=output_budget，其余观察not_started；原manager失败摘要完整保留。新类型分类改善本轮信息，不能倒推第十一轮旧generic结果已是同类异常，也不提供原始输出、各轮字节明细或服务启动根因。
- receipt仍UID999/GID987、user_created=true、manager_show_before、环境文件确认、manager_group为空，三个挂载未消除，user_removed=false/group_removed=false。result为completed=false/manager_setup/setup_failed、timeline/generations为空，**0事务**、无manager_readiness。实际cleanup命令仍超时失败，账户、组、挂载回收及正常清理均未证明。

本次只记录精确失败证据，不修实现、不增加预算、不重跑CI、不推送文档。前十一轮历史保持；后续如研究预算路径，先静态核算现有typed查询的累计字节开销并以纯替身复现，不能据新分类直接放宽限额或宣称服务已修复。

根随后查实该精确run最终九项success、两项failure：native 120501822975与Windows 3.13 120501823249。Windows为四个真实UIA用例timeout，3528 passed/1757 skipped，属于另一条调查，不混入本片native归因。前述六绿一红四运行只保留为取证时快照。

## 同轮 pending 观察拆分合同（实现前批准）

第十二轮已确定Service读取触及当次剩余输出上限；没有各轮实际字节、stdout/stderr分项，不能确定是累计小回复、单条大回复还是stderr，也不承诺manager就绪根因已定。静态因果依据是 `_manager_units` 在每个pending轮执行一次List、两次GetUnit及四次六属性get-property，共7次busctl/27次D-Bus调用；`_SetupIO` 把每条命令已返回的双pipe字节累计到同一32 KiB额度，未返回则保守耗尽，没有重复扣账或remaining错传。v255多PROPERTY是合法逐项Properties.Get，不是GetAll。

仅在native helper和纯替身测试内减少pending重复开销：每轮重新List精确两个unit，严格验证十列typed行的字段、唯一名称、unit path及Job id/path，保留本轮load/active/sub/job状态。列表尚非两unit active/Job0时，只补读manager的Id/InvocationID与MainPID/ControlGroup，共3次busctl/5次D-Bus调用，继续逐轮核对同代InvocationID/PID/startticks/CG。不得跨轮沿用路径、状态或身份；失败/缺失/换代/进程变化仍拒绝。

列表已显示两unit active/Job0时，在同一轮接续原完整两unit GetUnit、Unit六属性、Service六属性；核对path与列表对应，保留原全部typed属性校验、ready断言、进程/UID/可执行文件、原四kernel limits、最终完整两unit重读和前后proc核验。idle始终完整核验。pending记录不能直接作为ready证据，最终完整核验不降为轻量读取，也不缓存跨轮事实。

沿用32 KiB总诊断、1024字节回执预留、单execute 95秒、5秒清理与原业务入场余量；不增加系统查询/服务启动/重试，不改成功schema、失败摘要或清理合同。纯例将验证每个pending轮只读三次且身份重新取得、候选ready仍走原全量前后核验，以及严格列表/身份/最终变化反例。本片只证明重复读取可减少，实际三事务和正常清理仍未验；测试写完冻结再申请共享guard，不运行真实native或服务。

实现沿用上述拆分，完整路径额外拒绝List path与GetUnit不一致，ready候选也核对本轮List状态与完整属性状态。Context7已查询systemd v255；该条检索仅返回状态稳定性说明，因此十列顺序依据[官方v255 D-Bus文档](https://github.com/systemd/systemd/blob/v255/man/org.freedesktop.systemd1.xml#L1273-L1299)，多属性逐项Get依据[官方busctl实现](https://github.com/systemd/systemd/blob/v255/src/busctl/busctl.c#L1983-L2041)，没有下载或运行systemd包。

新增39项纯case，完整native纯文件 **367项通过**（pytest1.23秒、guard1.845秒、峰值107 MiB/swap 0）。其中同轮替身对比完整观察7次命令与pending观察3次，实际已返回字节后者更少；三轮pending后ready仍执行两unit全部原属性、进程与原四kernel限制、最终完整重读，核对了每轮新的Id/InvocationID/MainPID/CG，非法typed行、重复/共享path、GetUnit不一致和最终属性变化均拒绝。未声称这些纯例证明真实runner会ready或三事务已通过。

初次strict mypy有一项新增require表达式可能返回正则匹配对象的类型错误；两处改为显式 `is not None`，接受/拒绝结果不变，没有重复已绿367全量。最终strict worker mypy通过（496毫秒、峰值162.7 MiB/swap 0），两文件Ruff及format-check通过。全部执行经主仓绝对路径guard、每条30秒串行，pytest返回session已先收真实exit0再开始下一项，所有检查实终态后明确归还共享槽；没有真实子进程/native/CLI/账户/挂载/服务、本地网络集成、push或远端重跑。

独立Sol只读后审未发现确定P1/P2，已核对pending每轮同轮新读取与binding、candidate完整7命令、proc/kernel/final+stat以及原预算；根也复核核心diff与manager_state。审查没有运行检查，真实三事务未验的边界保持。此后仅按授权签名提交并推送一次精确候选，远端结果单独记录，不把纯例或独审当作实际native验收。

## 第十三轮 native 有限失败取证（最终九绿两红）

精确签名候选 `28e0452e75047afa3acff6c2e2b63718900e953a` 的 [run 39982801787](https://github.com/69gg/MuLi/actions/runs/39982801787)、[native job 120514184291](https://github.com/69gg/MuLi/actions/runs/39982801787/job/120514184291) 已失败，job于2026-10-23 23:58:16开始、23:59:59结束（Asia/Shanghai）。本片只查该job及一次artifact列表，不查询、等待或推定其余矩阵终态。

有限日志 `/tmp/muli-native-39982801787-120514184291.log` 显示事务步骤23:58:24进入、23:59:54报 `native_command_timeout`，约90.07秒包括准备和观察，不能等同manager自身等待。always cleanup于23:59:54进入、约1.38秒后报 `native_service_cleanup_failed`；本轮清理错误不是此前的 `native_cleanup_deadline`，两步骤仍均非零。

仅取确认698字节的 [managed-native-evidence 12513768416](https://github.com/69gg/MuLi/actions/runs/39982801787/artifacts/12513768416)，未下载4891370字节package。zip SHA256 `a93fce61b438f997b2ab749ba9e86294633cadeb78c531e98092a1bc4723a57f` 与上传日志一致；一次30秒guard先核两个entry的固定名称、类型、数量、无加密及各不超过32 KiB，再只读receipt316字节/result525字节JSON，无解压写盘或执行。实终态exit0，81毫秒/16.5 MiB/swap 0，完成即归还共享槽，没有运行测试。

- result为completed=false/manager_setup/setup_failed，timeline与generations均空，**0事务**。resources仅有setup，**没有manager_failure或manager_readiness**，所以本轮没有新的worker stage/query_status/query_code/used摘要，不能据缺失摘要判断最后一轮查询或就绪状态。
- setup为UID999、phase=manager_show_before、client_code=timeout、diagnostic_status=null、diagnostic_code=output_budget；stage/query_code/failure均not_started，query_status=null。后备未开始不能当作本次manager查询已确定触及输出上限；第十二轮的typed预算证据也不能倒推成本轮根因。
- 收尾后的receipt记录user_created=true、user_removed=true、group_removed=true，UID999/GID987，mounts为空，环境文件与manager_group归属记录为空，setup_phase仍manager_show_before。账户/组移除及挂载记录清空与此前轮次不同，但整体服务清理命令仍失败，**不能称正常清理或全部服务回收证明通过**。

本次仅记录新事实并签名文档，不修改实现、增加预算、自动重跑或推送；不从约90秒耗尽、后备预算不足或摘要缺失倒推服务启动根因。第十三轮原生验收仍未通过，前十二轮历史保持。

根随后查实该精确run已全终态，九项success，native与Windows 3.12两项failure。Windows有限取证另行负责，本片不推测其原因，也不混入native修复；前述不查询其余矩阵描述保留为本次取证边界。

## 观察到期与失败回执窗口（实现前批准）

只读因果审查确认原协议把同一个绝对E同时传给manager worker和parent命令：execute入口95秒，parent E为其截止减原5秒；worker到E才取消、完成已有进程收尾并构造失败摘要，asyncio.run返回后才打印JSON，而parent到E就可超时并SIGKILL。因此到期失败摘要存在确定的竞争窗口。第十三轮约90秒native_command_timeout与无摘要符合这条路径，但不能证明只有此原因，也不解释manager为何未ready。现有单独worker取消纯例不覆盖parent传输边界。

在原parent E内固定分配回执窗口，长度由现有 `CLEANUP_SECONDS / 2` 推导（2.5秒），不加新宽松上限。worker观察截止为E减该窗口，parent命令硬截止仍E；无execute_deadline的既有单次5秒观察同样在内部拆分，不延长为另一轮预算。若剩余时间已不足窗口，观察直接拒绝，不能重设期限或启动查询。asyncio仍只接相对remaining。

worker仍以传入的较早绝对截止约束全部原typed/proc/kernel/前后核验与取消路径。parent在较早截止前完成原严格校验才允许idle/ready成功；窗口内即使收到ready，也只能失败，不授予manager_readiness或启动事务。窗口仅允许接收、严格验证并保存同次失败摘要，原错误继续抛出；到parent E或更晚的任何回包拒绝。无返回、不可验证的used/回包大小/状态、无效失败摘要或超限仍按原合同保守耗尽额度，真正外部CancelledError不转换成普通failure。

沿用32 KiB累计输出与1024字节回执预留、execute95秒、独立cleanup5秒、单次manager start及全部ready证明；不增加查询/重试、生产进程API、输出字段或服务操作。有限窗口不承诺任意阻塞收尾必能回包，parent原硬终止仍是兜底。先写fake Popen/selector/host clock反例，模拟worker到期后才发布失败摘要：旧同截止会丢回执，新拆分应在原E前保留严格摘要。补窗口内成功拒绝、E边界、无剩余时间与无返回保守记账纯例，再申请guard验证；不使用冻结全局时基配真实asyncio timer。

实现仅改协调器截止分配及原验证后的时间门；worker/helper与生产process没有变化。先写反例再改实现，获guard后首次执行已包含修复，没有声称实际切回旧源码红跑。新增20个纯case用真实parent command逻辑配fake Popen/pipe/selector/monotonic；同截止对照会在失败摘要发布前硬终止并耗尽未知额度，新分配在同一个原parent E内保存严格摘要。两种模式、两种deadline来源、成功收到或校验跨较早截止、失败校验跨parent E、无观察余量、无效失败schema和到期无返回均覆盖，全部没有真实timer或系统进程。

新回执组 **20项通过**（pytest0.40秒、guard931毫秒、89.4 MiB/swap 0）；随后完整native纯文件 **387项通过**（pytest1.14秒、guard1.789秒、100.2 MiB/swap 0）。Ruff格式化84毫秒/28.1 MiB，Ruff check74毫秒/24.7 MiB，协调器与worker strict mypy347毫秒/71.6 MiB，均exit0、swap 0。每条经绝对主仓check_local与解释器、30秒原守卫串行；完整pytest session先收实exit0才开始下一项，所有终态后归还共享槽。之后仅补本文证据，代码冻结待独立后审；没有真实native/CLI/服务/账户/挂载、额外下载、push或远端重跑。纯例证明到期回执竞争可消除，不证明真实manager已ready或三事务与正常清理通过。

第二位Sol只读独审未发现确定P1/P2或阻断，根也已复核协调器与20项fake反例diff，原时间/输出预算边界保持。审查没有运行检查，之后仅按授权签名提交；真实manager就绪、三事务及正常清理仍未验。

回执窗口修复签名为 `fad62fe23c53813aab6da6de80d8c2860a702ee3`，随后按授权以GPG no-ff合并根已发布2.39基线与发行证据 `158ce90e254670854109fbf1b61c92998f177da3`，整合提交 `e398d61781e51ffe6c266d5bc64a980696f3de19`，零冲突；该基线没有修改native/process/workflow，未自动合并独立Windows观测片。

新基线只做一次相关整合纯验证：完整native387项与两个直接_OutputBudget节点的4项，共 **391项通过**（pytest1.06秒、guard1.629秒、峰值100.6 MiB/swap 0），没有选择process测试文件内真实Popen用例。受影响两Python文件Ruff69毫秒/21.5 MiB、format-check75毫秒/21.7 MiB；协调器与worker strict mypy551毫秒/157.1 MiB，全部exit0/swap 0。均经同一绝对路径30秒guard串行，pytest session收实exit0后才下一项，完成即归还共享槽。之后仅补签名文档记录，不push、触发第十四轮或提高任何预算；第十三轮失败及真实事务/清理未验边界不变。

## 失败后 setup 环境与 socket 投影候选

在第十四轮事实与 `75bc95a` 的 private-bus 否决之后，本候选只为 setup 失败后的已有后备观察增加一次 `context`；不加入 ready 轮询，不改变启动命令、workflow、严格 ready 门或真实验收条件。实现与纯 fixture 已通过下述本地检查，没有新的 CI、真实服务或事务结果。

只有已观察到专用 UID 的 manager 镜像才尝试投影。外层在前后检查同一 system unit 的 Id/InvocationID、MainPID/ControlGroup 与 proc startticks；既有 worker 的窄提权子模式在自己的观察窗口内再检查 startticks、四个 UID、manager exe 和 cgroup。任一漂移使整包 context 失效；这仍是有限窗口，不是原子快照。原始 environ 只留在投影子进程内，不通过 stdout、stderr、异常正文或 artifact 输出。

固定字段为 `code`、`notify_env`、`runtime_env`、`bus_env`、`listen_env`、`parent_notify`、`user_notify`、`private_socket`。仅解析 NOTIFY_SOCKET、XDG_RUNTIME_DIR、DBUS_SESSION_BUS_ADDRESS、LISTEN_PID/FDS/FDNAMES；其它值丢弃。notify 只输出 parent/user/missing/other，runtime/bus 只输出 expected/missing/other，LISTEN 仅 present/absent，socket 仅 socket_owner_matches/absent/unexpected/unavailable；失败字段统一 unavailable。重复目标键、截断、非法格式、未知输出键不能作为有效观察。

`NOTIFY_SOCKET=/run/systemd/notify` 是父 PID 1 的上行入口；`/run/user/UID/systemd/notify` 是 user manager 接收子服务通知的入口。前者缺失或指向后者，会将下一步收窄到 exec 初始环境来源；仍不能独立确认历史超时根因。初始值正确且父 socket 类型/owner 匹配，只排除这两项简单错误，不证明 READY 曾发送或被接受。用户 notify/private socket 只做不跟随符号链接的路径元数据观察；存在不证明创建者、监听状态、已完成初始化或已进入主循环；缺失/不可读也不证明对应阶段未执行。没有 socket connect、bus fallback、activation、通知发送或新协议 wrapper。[v255 notify 入口](https://github.com/systemd/systemd/blob/v255/src/core/manager.c#L970)、[v255 private 入口](https://github.com/systemd/systemd/blob/v255/src/core/dbus.c#L850)。Context7 已核对固定 v255，具体入口以这两处官方代码为准。

继续使用原约88秒观察截止、2.5秒失败回执、5秒 cleanup 与32 KiB累计余额、1024字节外层回复预留及原资源限制。投影的512字节内部 wire 上限从现有余额内扣除；原始 proc 读取字节、内部 stdout/stderr、最终外层 JSON 都计费。失败或未知字节读取保守占用额度，不重新分配时间或额度；没有可用余额/期限就不启动。context 失败不覆盖 manager_failure，也不授权进入 harness 或清理资源。

纯 fixture 已覆盖父/用户 notify 区分、缺失/异常/重复/截断环境、内外层身份漂移、socket 非法类型/owner/父目录 symlink、秘密值与异常不外泄，以及取消/不足余额/到期拒绝观察或不复用额度。取得共享槽后，全部逐条经主仓绝对路径30秒 guard 串行并收真实终态：native 文件 **426项通过**（pytest1.39秒、服务1.987秒、峰值115.5 MiB）；最终 Ruff77毫秒/24.4 MiB、format-check83毫秒/29.9 MiB、两脚本 strict mypy652毫秒/164.6 MiB均exit0，swap均0，完成即归还槽。首次 Ruff 找到新 fixture 的 zip 未显式 strict 参数，已补 strict=False；首次 mypy 未设置 worktree 源路径，将依赖视为无类型安装包并报8处 no-any-return（含既有位置），在 guard 内设置 `MYPYPATH=src:tests` 并使用 `--explicit-package-bases` 后通过，没有放宽类型规则。未运行本机服务、账户、挂载或远端 CI；第十四轮0事务、未就绪与 cleanup 超时边界不变。

根与 C 对这四个改动文件分别完成只读独立复审，未发现必须修复的 P1/P2；复核范围包括秘密不透传、父/用户 NOTIFY 方向、内外身份绑定、原截止和累计字节。随后四文件以有效 GPG 签名提交 `4b34f9f00178ab0d7f6a36e05af785b6e6d745b5`，再通过有效签名 merge `39b8df56ff83d34d77d36457cd73d5af9a708259` 无冲突合入已发布 main `ca5615213cdcc9cea99239d26835c5864fb56bed`（v2.40）；两者均为 merge 的祖先，四个 native 文件在合并中无变化。相对 main 的候选仍包含原 native 分支历史；本次投影提交没有修改 workflow。

在 merge `39b8df5` 上重新取得独占槽，逐条经主仓绝对路径30秒 guard 完成最小整合验证并实收exit0：native426项加两个直接 OutputBudget 节点4项，共 **430项通过**（pytest1.37秒、服务1.970秒、峰值117 MiB）；三Python文件 Ruff66毫秒/21.6 MiB、format-check71毫秒/21.4 MiB，两脚本 strict mypy1.085秒/171.7 MiB均通过。mypy 在 guard 内显式使用 `MYPYPATH=src:tests`、`--explicit-package-bases`，没有降级规则；全部swap0，完成即归还槽。本轮没有失败重试、本机真实账户/服务/挂载效果或远端 CI。该合并候选提供新的有限观察假说，不表示 manager 根因已确认、0事务边界已解除或正常 cleanup 已验收。

## 第十四轮 native 有限失败取证与下一验证门

精确候选 `362330cd3a89001e70ce25e5843ec5e99686a7fc` 的 [CI 39987046967](https://github.com/69gg/MuLi/actions/runs/39987046967) 已全终态，**9项成功、2项失败**（native 与 Windows）；[native job 120528818889](https://github.com/69gg/MuLi/actions/runs/39987046967/job/120528818889) 的有限日志在 `2026-10-23T16:30:00Z` 报 `native_deadline_exceeded`。本次已下载的 evidence 为 **1182 字节**，zip SHA256 `5b676f2b4272f8c576cbd5b60d1fa3cc343ab3aa6cd22dc4fd2ca81a650da7c8`。严格确认只有 `receipt.json`（1139 字节）与 `result.json`（1070 字节）两个普通、非加密有界 entry 后，只读取白名单标量；没有再次下载。结果投影没有导出原始路径、命令、环境、stderr。

- `completed=false/stage=manager_setup/code=setup_failed`；timeline 与 generations 均为空，仍为 **0 个真实事务**。
- 本轮首次保留同次 manager 到期摘要：`code=native_deadline_exceeded/stage=manager_validation/query_status=0/query_code=ok/used=17275`。这证明2.5秒失败回执窗口在本次确实保住摘要；不能把传输问题改善写成 manager 就绪问题已修复。本次失败不是该摘要已报告的输出上限或查询退出错误。
- 后备 setup 为 `phase=manager_show_before/client_code=command_failed/diagnostic_status=0/diagnostic_code=ok/failure=none`，最后字段标签 `stage=service_properties/query_status=0/query_code=ok`。这里 `client_code=command_failed` 是协调器对 `native_deadline_exceeded` 的既有泛化映射，不是第二次系统命令退出失败；成功遍历结束也不会把 `stage` 改成新的完成阶段。

| 固定 system unit | load / active / sub | result / ExecMainCode / ExecMainStatus | MainPID / job | cgroup 存在 |
| --- | --- | --- | --- | --- |
| `user-runtime-dir@999.service` | loaded / active / exited | success / 1 / 0 | 0 / 0 | 否 |
| `user@999.service` | loaded / activating / start | success / 0 / 0 | 2580 / 1573 | 是 |

上述是后备观察窗口的有限正向事实：runtime-dir 已成功完成，而 user manager 当时仍有启动 job、未达到既有严格 ready 条件。`Result=success` 与 `ExecMainStatus=0` 在仍 activating 的服务上不能当作已经完成启动；也不能由 PID 非零直接推断 PAM 已结束或 manager 已进入主循环。manager 摘要的 `manager_validation` 是最后保留的阶段；轮后 sleep 到期同样保留该值，不能据此断言某一条 validation 表达式失败。

对同一 zip 的后续有限投影复用现有 `process_facts` 与 `journal_facts` 校验，确认 `process=observed/exe=manager/uid_matches=true/state=S/wait=epoll_wait`；journal 为 `code=no_known_event/incomplete=false/events=[]`。这证明有限身份窗口内已是专用 UID 的 manager 镜像，不能再把该窗口写成仍在 executor/PAM 最终 exec 之前；epoll 仍不证明已进入 `manager_loop`。journal 没有接受到固定四类正向事件，不能推导没有日志、没有启动 basic/default/dbus 或 READY 发送失败。

receipt 为 UID/GID 999/987、`setup_phase=manager_show_before`；环境文件身份仍已确认，`manager_group` 未赋值，账户/组未标记删除，仍记录3个挂载。随后的 cleanup 在 `2026-10-23T16:30:05Z` 报 `native_command_timeout`、exit1，仍在原5秒边界失败，因此没有 readiness、harness 启动、三事务或正常资源回收证明。本轮不与 Windows 或另一条 main/Python 3.13 rollback 超时混为同一根因。

固定 v255 只读交叉核对重申已有 READY 边界：user manager 在 basic.target 活跃时或 jobs 清空时可发送 READY；这两条路径只支持区分“尚未到触发条件”与“通知未成功交付/接受”等待验假说，本次没有新证据判定其中一项。现有 journal 的四类 MESSAGE_ID 与 start/job_result 约束能对应官方 job 日志，但本次没有确认过滤缺陷；`systemctl --user` 的 v255私有连接失败会回退用户 bus，故不能直接追加该命令并宣称完全没有 socket activation 风险。[v255 READY 触发](https://github.com/systemd/systemd/blob/v255/src/core/manager.c)、[v255 job 结构字段](https://github.com/systemd/systemd/blob/v255/src/core/job.c)、[v255 私有连接及回退](https://github.com/systemd/systemd/blob/v255/src/shared/bus-util.c)。Context7 已查询固定 v255；上述具体判断以官方源码为准。

因此本次先冻结失败事实，不启动第十五轮。下一有效观察需要能在同一身份、原期限和32 KiB预算内区分 READY 触发条件与通知路径，或得到新的已核正向事件；先提出可由纯 fixture 证伪的最小方案，再单独审查。当前证据不支持重复同一 CI、提高期限、修改 Type 或把 direct children 当作 epoll 根因证明。本轮尚未形成新的已证实启动根因或代码修复方案。

两次有限投影均经主仓绝对路径 **30秒 `check_local.py` guard** 并实收 exit0/guard success：首次 unit 投影服务85毫秒、峰值16.6 MiB；随后扩充 process/journal 投影98毫秒、峰值22.1 MiB；swap 均0，每次完成即归还共享检查槽。这里只新增失败事实与下一验证门，没有产品或 CI 代码改动、真实服务操作、新下载或新一轮远端运行。

随后独立只读评估否决了直接给现有 `busctl` 添加 user manager private `--address` 的方案。仓库实际只有现有 `_SetupIO`/busctl 路径，没有可复用的 libsystemd/ctypes wrapper；此前关于 wrapper 的推测不作实现依据。固定 v255 的 `acquire_bus()` 即使指定地址也启用 bus-client 模式，`sd_bus_start()` 会先发送 broker Hello；private listener 是直接 server，CLI 没有关闭该握手模式的选项。`--auto-start=no` 仅约束后续 call/emit，不能消除 Hello，因此这条捷径无法提供所需的正向 Get/List 观察。明确地址本身没有 session-bus fallback，不能反过来称其必然激活服务。握手失败只能记观察不可用，不能当作 manager 未进主循环或 READY 通知失败的证据。本次没有新造协议客户端、运行实验或重开 CI。依据：[v255 busctl](https://github.com/systemd/systemd/blob/v255/src/busctl/busctl.c#L68)、[v255 Hello](https://github.com/systemd/systemd/blob/v255/src/libsystemd/sd-bus/sd-bus.c#L540)、[v255 auto-start](https://github.com/systemd/systemd/blob/v255/man/busctl.xml#L326)。

依据：[GitHub 托管 runner 的管理权限](https://docs.github.com/en/actions/reference/runners/github-hosted-runners#administrative-privileges)、[user@.service 与 runtime-dir](https://github.com/systemd/systemd/blob/main/man/user%40.service.xml)、[资源控制](https://github.com/systemd/systemd/blob/main/man/systemd.resource-control.xml)、[cgroup 委派](https://github.com/systemd/systemd/blob/main/docs/CGROUP_DELEGATION.md)、[transient 属性](https://github.com/systemd/systemd/blob/main/docs/TRANSIENT-SETTINGS.md)、[v255 systemd-run service/scope 环境差别](https://github.com/systemd/systemd/blob/v255/man/systemd-run.xml)、[v255 Environment 与进程环境](https://github.com/systemd/systemd/blob/v255/man/systemd.exec.xml)、[v255 revert 的 config/control 目录删除范围](https://github.com/systemd/systemd/blob/v255/src/shared/install.c)、[systemctl link/运行时管理](https://github.com/systemd/systemd/blob/main/man/systemctl.xml)、[Python SQLite URI 与锁等待](https://docs.python.org/3.13/library/sqlite3.html#sqlite3.connect)。这些说明支持设计可行性，不代替实际托管 runner 的权限、导入与 kernel 核验。
