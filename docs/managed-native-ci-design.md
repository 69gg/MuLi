# 真实 systemd 托管切换 CI：设计与诊断

这项 CI 用真实用户服务验证同一数据库上的 adopt、切换与故障回退。本文说明夹具的运行边界、证据含义和维护约束；逐次修改、红绿日志及历史构件留在 PR 和 CI 中。

验收结果以对应候选的 `managed-native` 作业为准，须同时核对三次事务和独立清理的完整结果。`cleanup=verified` 只证明清理，不能代替事务完成。

## 覆盖范围与入口

显式的 `managed-native` job 仅在 GitHub 托管 Ubuntu 24.04、Python 3.13 上串行运行。systemd、D-Bus、loaded unit、进程与 cgroup、writer lease、bootstrap、nonce HTTP、activation 和最终投影均走产品实现。

A、B 是当前同一 MuLi 包的两份完整 retained 安装，保留原 `compatibility.json`，依赖复用 CI 已有安装。案例覆盖同版本保留环境之间的切换和回退，不验收跨版本升级、wheel 或依赖安装、在线下载、已有用户服务、真实外部渠道、断电恢复或 macOS/Windows 托管切换。现有[真实进程恢复回归](managed-process-recovery.md)保留自己的期限与假 manager 边界。

| 入口 | 职责 |
| --- | --- |
| [CI workflow](../.github/workflows/ci.yml) | 启动受限协调器、独立 always cleanup，上传有限 evidence |
| [协调器](../scripts/check_managed_native_ci.py) | 专用账户与卷、receipt、manager 准备、harness 和最终结果校验 |
| [worker](../tests/helpers/managed_native_worker.py) | 三事务、只读诊断、受限 root 子模式与服务收尾 |
| [unit 出生凭据](../tests/helpers/managed_native_unit.py) | 仅跟踪本夹具创建的标准 `muli.service` 文件及真实成功替换 |
| [产品 backend](../src/muli/platforms/managed_backend.py) | 文件认证、同步内容证明、效果 guard 与事务操作 |

这些脚本不能在工作站直接执行。开发检查统一经过 `scripts/check_local.py`，串行收取真实退出码与守卫结果；纯 fixture 回归不创建本机账户、挂载或服务。实际跨平台矩阵和原生效果由托管 CI 验收。

## 专用环境与资源归属

协调器排他创建本次唯一的非 root 用户和私有组，记录实际 UID/GID/HOME。创建冲突直接失败，不复用已有账户，不修改 runner 用户、其祖先目录权限、全局 `user.slice` 或其它 UID 的 manager，也不启用 linger。

runner 拥有的 receipt 和结果位于 `RUNNER_TEMP`，临时 UID 不可写 receipt。receipt 逐阶段记录已确认的账户、镜像、挂载、入口目录和配置文件身份；准备意图不等于效果已确认。跨进程 cleanup 必须重核用户名与 UID/GID/HOME 及资源身份。创建已生效但丢返回、UID 被复用或归属失证时失败并保留证据，不从同名路径重新领养或删除资源。

合成配置、数据、HOME、用户 unit、两个 retained prefix 和辅助脚本位于 `/tmp` 下本次独占的 0700 卷。`/tmp` 必须是非符号链接、root 拥有的 01777 目录，入口路径仅由已验证的本次用户名派生；父目录和原入口的 dev/inode、owner/mode 在创建、挂载与卸载后重新核对。保留卸载后的空入口及镜像，不递归删除输出树。

卷使用独立 64 MiB sparse ext4 镜像和 `loop,nodev,nosuid,noatime`。挂载身份、可用空间和 inode 经实际观察后，再运行产品 `verify_local_filesystems`；不支持即失败，不扩大白名单或自动扩卷。复制前核两份完整包及固定资料的空间，并保留收尾余量。

解释器和依赖通过两个固定的只读 bind 提供：实际 `sys.base_prefix` 到 `input/python`，当前 venv 的 site-packages 到 `input/deps`。源先规范化，拒绝输出目录与递归关系；逐个 remount 只读并核实际挂载身份。不复制整份 venv，不下载依赖，不放宽源权限。每份 slot 自有完整 MuLi 包优先于 deps，`bin/python` 和 `pyvenv.cfg` 指向同一已核 runtime，`.pth` 禁写 pyc、加入固定依赖及测试 hook，不递归执行 deps 内的 `.pth`。

启动 MuLi 前，实际临时 UID 在受限 harness 中以 retained Python 和 `-I` 导入 `muli`、`fastapi`、`httpx`、`uvicorn`、`pydantic`、`sqlite3`，核 prefix、解释器与 MuLi 实际来源。路径存在或 root 可读不能代替这项权限证明。

### 专用 user manager 的启动

本次 UID 的 runtime instance drop-in 同时承载四项资源限制、HOME/XDG/bus 环境和经核定的 `ExecStart`。模板仅在 inactive/dead、无 job/MainPID 时通过 typed `ExecStartEx` 前后观察，要求唯一已安装 root 镜像、`systemd --user`、空 flags、合法整数及身份一致；镜像路径由实际系统发现。drop-in 重置该实例 ExecStart，加入本次自有 `--unit`，保留 Type、NotifyAccess、PAM 和 UID 语义。

私有 bootstrap target 显式 `Requires/After=basic.target dbus.socket`、`DefaultDependencies=no`；同树还创建固定 `gpg-agent-ssh.socket` mask 和 `dbus.socket.d/muli-runtime.conf`，后者只固定本次 runtime 目录。这些文件和目录均排他创建、保存身份并核固定正文。root 文件位于专用 UID 可写父目录中，因此仍需前后 no-follow/dir-fd 身份验证，不能当作原子沙箱。该配置未封闭 runner 的全部依赖、generator、PAM 或系统配置。

配置安装、复读、daemon-reload 和 bootstrap create 完整核验后，先核两精确系统 unit 的初态，只发一次 `--no-block start`。成功返回只证明 job 准入；失败、超时或丢返回均不重发 start。初态允许已加载但 inactive/dead、PID=0、Job=0、cgroup 空的实例，同时冻结旧 InvocationID。

readiness 经系统 bus 观察本次两个 unit。pending 轮轻量读取仍逐轮重取路径与身份；拟接受 ready 时执行完整 typed/proc/kernel 前后核验：runtime-dir 为 loaded/active/exited、退出成功、Job=0；manager 为 loaded/active/running、原 notify-reload 类型、Job=0、非零 MainPID 和新的稳定 InvocationID，进程镜像、四 UID、start ticks、cgroup 及四项 kernel 限额均匹配。轮询退避和睡眠消耗原期限，不重复 journal/wchan 诊断。

ready 后再次核 bootstrap 文件，并仅为该 manager 归一化 `XDG_CONFIG_HOME`、`XDG_RUNTIME_DIR` 两键，再启动任何 MuLi 子 unit。标准服务仍由实际 `UserService` 默认路径生成；不传 `unit_dir` 绕过默认路径检查。真实用户 bus、服务启动和三事务仍须随后分别成功。

## 时间、字节与资源预算

| 边界 | 固定合同 |
| --- | --- |
| 整轮 worker Case | 600 秒，共同覆盖 drive 内导入、初始服务、三事务及每笔完整投影，不为每笔重开 |
| harness | 最多 720 秒；启动时裁到共享剩余量减 cleanup 预留，worker 仍核绝对截止 |
| 协调器 execute | 入口起统一 840 秒，prepare、manager 观察、drive 和原启动诊断共同消耗 |
| 业务入场 | 协调器必须严格多于 720 秒剩余量；worker 入场及启动标准服务前须多于整轮 600 秒，并再次核原 Case 截止，不允许迟到后重开预算 |
| 普通命令与 setup 诊断 | 默认每次最多 5 秒，且不越共享绝对截止；manager 观察在原 parent 截止内留 60 秒失败回执窗 |
| 单次 HTTP | 请求、响应与关闭合计 `min(30 秒, remaining)`；客户端使用独立同值期限，响应流式计入原 `JournalLimits.max_record_bytes` |
| Case 探针与失败收取 | 单次仍最多 2 秒，失败收取与 cleanup 的探针再裁到原绝对截止；不借用 HTTP 期限 |
| cleanup | 效果与证明共用入口起 120 秒绝对期限，不能用于继续事务或健康工作 |
| 最末客户端回收 | 失败时原进程组 SIGKILL 后最多再用 120 秒 reap；只回收客户端，不能执行其它效果或发布 verified |
| setup 观察字节 | 累计 32 KiB；双 pipe、proc、内部回包及最终 JSON 按各现有协议计费，未知返回保守占满授额 |
| 最终结果 | `result.json` 总量 32 KiB；可选诊断让位于原失败、timeline、来源与清理事实 |

1024 字节是既有最小回包预留；manager 失败回执、实例投影子回包，以及实例 `setup` 与 `setup_accounting` 合包另有各自的 1024 字节校验，不能将它当成所有 setup JSON 的统一上限。内部 context wire 为 512 字节，仍从原余额扣除。原始输入计费、内部 wire 与父级最终结算有不同责任边界，不能声称整个案例的全部 I/O 或 D-Bus wire 都统一受一份 32 KiB 账本约束。无回包、非法 schema 或无法确认的已用量不退款。

| 进程主体 | 内存 / swap / CPU / tasks | 核验方式 |
| --- | --- | --- |
| 专用 `user@UID.service` 聚合 | 1 GiB / 0 / 100% / 128 | 实际 ControlGroup、cgroup v2 限额与 controllers；harness、服务、helper 均须属于该子树 |
| harness 子 unit | 256 MiB / 0 / 100% / 32 | 实际 membership、kernel 限额及 `memory.oom.group=1` |
| 产品 helper | 原 anchor/`HelperLimits`；默认 128 MiB / 0 / 25% / 32 | 原 `helper_launch_arguments` 与 `read_helper`，不借用 manager 的 100% 配额 |
| 系统协调器 run / cleanup service | 各 1 GiB / 0 / 100% / 128 | 本次 run/attempt/mode、runner UID/GID、实际 `/system.slice/{unit}` 和 kernel 限额 |

workflow 的协调器 service 使用 `systemd-run --service-type=exec --wait --pipe --expand-environment=no`、整组终止且不自动重启。外层 `RuntimeMaxSec` 分别为 960 秒和 240 秒，含客户端回收窗口；job 最长三十分钟。整轮验收和清理预算为适配托管 runner 调整，内存、swap、CPU、tasks 与字节门不变。协调器、普通 worker 和三个固定 root 子模式在效果或诊断分派前核资源包络；sudo 仅窄保留包络三键，环境声明不能替代实际 membership。脱组、缺 controller、限额不符或无法核验即固定失败，无裸进程 fallback。

系统协调器包络与专用 UID 聚合是两条独立边界，不是整项 job 的共同内存总额。声明资源属性、systemctl 返回零或父进程受限均不证明实际子进程受限；开发守卫也不等于恶意代码沙箱。

### run 与 always cleanup 的互斥

`systemd-run --wait` 客户端退出或被取消，不证明已接受的服务请求和子树已经结束。两入口用 fixture 外、同 run/attempt 的固定私有 inode 取得非阻塞 kernel flock；锁忙立即失败，不等待、不停止另一个入口。锁文件只接受空正文或不可逆的 `closed\n`，拒绝 symlink、hardlink、未知或部分正文，不 unlink、替换或重建已知 inode。

run 在完整持锁期间执行、诊断并收取自有客户端。cleanup 从入口资源/锁核验前计 120 秒，获锁后先在同 inode 写入并 fsync closed，再核固定 run cgroup 的 `cgroup.events`：仅 populated0 或封门后固定 group 缺失才可继续。populated1、坏数据或身份不确定均失败并保留 closed 与 receipt。未来迟到 run 即使创建 cgroup，也会在效果前被 closed 拒绝；父锁释放本身不能代替 population 门。fixture root 不存在也先封门。

## 三个真实事务与成功条件

整个案例使用同一数据库和 journal，只有一条顺序链。真实 Uvicorn 绑定动态预选 loopback 端口；端口被占用即失败并清理，不重试管理业务。配置关闭模型外部使用、更新检查、渠道、MCP、Skills、插件、自动记忆、目录监听、注意队列和通知，不接外部账号。

1. **legacy A → adopt A。** 真实标准 `muli.service` 启动 A，bootstrap 建立受支持 BootSession，Runtime 新建数据库及完整格式标记。真实 owner 登录后，经 `POST /api/management/transactions` 排入 adopt，走原 drain、stop、持久发布、真实 helper 和稳定 launcher，验证 fresh boot、released 与唯一终态投影。
2. **managed A → switch B。** 相同公开 API 和管理权限排入 B，核实际不同 retained Python/prefix、fresh InvocationID、loaded unit 与 cgroup；fixed controller 仍为 A，不能只比较配置摘要。
3. **managed B → switch A → rollback B。** 合法 target A 在 prepare 后写入合成最新资料、pending ingress 和 unknown outbox，再登记现有 `Maintenance.unsupported()`。原健康路径拒绝后，真实 helper 停目标、还原三文件、启动 fresh B，最终投影 rolled_back/released。最新资料和原 routing 必须保留，不能用旧 DB 覆盖。

失败 hook 只在真实 serve 的 `create_app` 返回、实际 Runtime/BootPermit/Store 已构造，且 lifespan activation 尚未执行时生效。必须先核 target A、switch/base B、同代新鲜 record、真实 lease、Store/main、WAL/FULL、admission 暂停和迁移准入关闭，再写合成资料与登记能力拒绝。readiness 仍通过原 nonce、boot、generation、binding 和 Store/core 校验；不伪造 HealthProof、不改 record、不替 controller 决策。辅助模块位于包外，不改变 A/B 原包字节与兼容声明；正常 adopt 和 switch B 不触发。

每次进程更替重新登录 owner，不沿用内存会话。紧凑 current API 只返回 available/reason 和 `TransactionRecordV2.public()` 有界标量，不取完整 management plans。POST 返回 plan，不能套旧 nonce 用例的 4 KiB 限制；所有 HTTP 响应包含错误响应都按原流式字节门处理，不打印正文。

legacy origin ready 后冻结 Store instance、main session 和数据库 dev/inode。三代 record、实际数据库与停服后收尾都必须等于初始身份。运行中的独立读取只用既有数据库只读连接、`timeout=0` 和 query-only，锁忙立即失败，不构造会初始化/恢复业务的 Store。

每代最终屏障要求精确 record/current receipt、rolled_back 或 committed 且 released、真实 fresh 运行角色、同库投影及恰一次 `management.completed`，并核原请求 routing。非终态保留完整 helper 观察；resolved 后只用原精确 listing 等待 helper 消失，再核 controller lock 可取及完整投影，之后才提交下一代。先出现 resolved 或 selector 变化不证明 helper 已释放锁；失败、blocked、recovery_required、坏 listing 与未知状态不降级为成功。

## 认证与相邻同步内容证明

完整认证仍包括全部包字节与 SHA、解释器、pyvenv 和兼容声明、三文件绑定、私有 metadata、目录枚举、独立 open、FD/path-stat 与最终身份围栏。controller 先于 selected，恢复 base 仍允许相反 target 的可执行包损坏；其原 metadata 检查保留。角色、revision、deadline、writer lease 和 cgroup 授权不能缓存，效果前 guard 与原错误先后不能省略。

`_FileProofs` 仅携带已完整认证且精确文件身份仍匹配的内容证明。每次验证仍重开文件、遍历、核 metadata 和身份；身份或内容变化时按原完整 hash 规则重读。已有同步批次包括 `_check` 内认证、三组件安装/确认、post-probe 到相邻强 guard，以及启动前 guard 到紧邻确认；只在原无等待段借内容，不把整个检查或授权结果复用为成功。

确认到相邻 probe 的三处交接如下：

| 路径 | 可以借用的内容 | 生命周期 |
| --- | --- | --- |
| 停止态 `observe` | writer-held、cgroup 空、磁盘匹配且 `_confirm` 完整成功后的 batch | 只供下一次原 `role=None` preflight；局部 finally 清空后才进入 `read_managed` await |
| `install` | `_install_files` 最终确认成功后的 batch | 只供第三 probe 原 `role=None` preflight；同样在真实 await 前清空 |
| `start` | writer-held `_confirm` 完整成功后的 batch | 只供第二 probe 原 `role=None` preflight；finally 在真实 await 前清空，await 后仍独立认证，再通过原效果 guard |

await 后独立完整读取与 hash，失败和取消释放局部证明及原 lease。正常已加载路径保留 observe 的 13 次、install 的 29 次原角色检查，各三次 probe；start 保留 14 次原角色检查和两次 probe，第二 probe 后仍在 writer 内执行原第 13 次强 guard，释放 writer 后执行原第 14 次效果 guard。reload、未命中确认分支、unit 未加载时保留原检查与返回行为。

原 `role=None` preflight 只承担 controller/metadata/revision 认证，不增加 selected、lease、deadline 或 cgroup 效果门。observe 最后确认后没有新的角色效果门；start 则保留后续第 13/14 次效果门，两者不能混淆。不能声称内容借用新增了拒绝保证，也不能把有限观察当成原子状态。独立回归分别覆盖确认前与确认后窗口、内容/身份/权限变化、错误优先级、真实 await 内变化与取消。

离线读取计量使用小型 fixture，只证明相邻同步段少读一份内容，不代表线上完整包成本、整轮墙钟、CPU 收益或原生验收已满足。

托管 helper 仅在自建 probe 且 `runner is run_process` 时启用 `join_interface_reads`。同轮 Unit/Service 白名单属性并发读取，两组收齐后才组装与校验；TaskGroup 拥有任务并保留 Unit 普通错误优先，外部取消继续传播。通用 probe 默认串行，注入 probe/runner、manager 与 setup 不自动启用。GetUnit、前后观察、typed 解析、双 pipe 字节门、期限和完整认证不变，不使用 GetAll 或请求 Environment。这只重叠等待，不减少 RPC、扫描或总 CPU 工作。

## 结果与诊断怎么读

`result.json` 顶层严格为 `completed/stage/code/timeline/generations/resources`，timeline 最多 64 行、generations 最多三代。协调器通过公共验证器接受有限白名单、固定枚举和有界整数，拒绝额外字段、非法版本、布尔冒充整数及来源/时间不一致。任意异常只保留类型或固定码；不导出 nonce、Cookie、密码、配置正文、完整 journal、原 stderr、环境值、路径或业务正文。

| resources 字段 | 可用事实与限制 |
| --- | --- |
| `manager`、`harness`、`manager_peak` | 已观察的 kernel 限额及峰值，不代表其它进程链或整个 job 的共同额度 |
| `initial`、`environment` | 初始 Store/main/DB 身份；UID、HOME、config、runtime、bus 匹配布尔值及 explicit/default 来源 |
| `manager_readiness` | 完整 ready 门后的 `elapsed_ms/late_ready`，不替代用户 bus、MuLi 服务或三事务 |
| `manager_failure` | 同次失败的固定 code/stage/query 状态/used，以及可选最后完整轮 |
| `setup`、`setup_accounting`、`startup` | setup 实例/后备观察、父级结算和精确 harness 启动诊断；均不授予业务成功 |
| `helper_source`、`helper_source_observation`、`controller_failure` | 独立认证 helper 来源、失败收取时间窗口及固定 controller 失败事实 |
| `helper_costs`、`helper_cost_history`、`helper_cost_samples`、`helper_start_samples`、`helper_entry_checkpoint` | 兼容的有界成本和正向边界；缺失不等于未执行，当前生成 hook 不安装旧完整成本观察器 |
| `helper_guard_costs` | 当前窄 helper guard 部分成本，含可选 breakdown/startup |
| `binding_inputs`、`binding_costs` | 实际输入与已观察应用进程的部分绑定成本，不包含未取得来源的进程 |
| `worker_await` | 真实逃出 drive 的取消等待位置，不是 helper 内部执行栈 |
| `cleanup` | 独立收尾证明；verified 不代表功能或回滚成功 |

字段缺失、`not_observed`、`unavailable` 和 unknown 都保留缺证含义，不能补零、借上一代事实或根据最后标签推断根因。诊断失败不得覆盖原失败，新增可选字段不得挤掉原事实。

失败日志仅转发唯一、通过公共验证器的 `native_failure_diagnostic:` 安全位置。重复、损坏、超限或无法认证的帧不转发，也不回显原命令输出、异常正文或路径；安全 raise 位置仍不能单独解释整轮累计根因。

### manager 与启动诊断

`manager_failure` 的固定字段为 `code/stage/query_status/query_code/used`；`query_status` 可为 null，零退出只说明命令返回。stage 是最后设置的阶段，后续 sleep 到期也可能保留 `manager_validation`，不能据它定位具体表达式。`used` 包含无返回时保守占用的授额，不一定是实际输出字节。明确的输出上限异常类型或固定码才记 output_budget，同文本普通 ValueError/OSError 不能据此倒推预算根因。

可选 `last_completed_round` version1 只在原 pending 轮结构与身份门全部通过后冻结：包含轮开始的相对 `elapsed_ms`、runtime/manager 的 active/sub/job，以及 manager 的 PID/start_ticks/InvocationID。它是历史完整轮，不是完成时刻、实时年龄、到期原子快照或 READY 证明；`not_observed` 不伪造默认状态。

后备 `setup` 支持原 units/process/journal/context/notification 形状，也支持绑定上述 checkpoint 的 version1 实例投影。后者固定 `uid/pid/start_ticks/invocation/exe/uids/internal/used`，可附 `pending_job`；只有同代身份匹配才接受。internal direct-peer 标量或固定 dbus.socket 控制进程观察均为有限窗口，不把 STATUS 中的 job identifier 提示升级为已认证 unit。读端保留合法旧形状的兼容，未知形状仍拒绝。

`setup_accounting` 固定 `parent_before/raw_grant/child_charged/reply_bytes/parent_after/settlement`，区分 not_started、validated 和 unvalidated。父级核 worker used 与实际回包共同计费；无回包或坏回包保守占满。worker 返回零但最终整包超额时，后备默认 `not_started` 表示未接受事实，不能说 worker 没有观察。先保存的 manager_failure 不被后备失败抹掉。

原 setup/startup 观察区分两个客户端状态与 query 状态；harness 阶段固定为 identity、ancestors、import、manager、listing、get_unit、unit_properties、service_properties。只有成功解析精确 typed 空 listing 才是 unit_absent；总线不可达、坏响应或 GetUnit 失败不能冒充缺席。祖先 mode/UID/GID 只提供有限 DAC 线索，不证明 ACL 或有效可遍历权限。

旧 process/context/notification/journal 字段也只提供各自正向事实：manager 镜像与 UID 不证明进入事件循环；epoll_wait 不证明 basic 完成；socket owner 匹配不证明监听、创建者或 READY 交付；STATUS 非空/固定文本不等于 READY 位收据；无已知 journal 事件不证明无日志或未执行对应阶段。前后身份绑定是有限窗口，不把不同 getter 结果拼成原子快照。

观察截止早于 parent 硬截止的 60 秒窗只接收并验证同次失败回执；窗内收到 ready 也不能成功。`late_ready` 仅在整轮 typed 观察开始已超过原 start 请求 5 秒、仍观察同一新 InvocationID 为 activating，随后完整 ready 门通过时成立；采样晚或 getter 跨界不够。

### helper 来源、进度与成本

helper 来源五元组为 generation/helper/UID/PID/InvocationID，必须由真实 journal/deployment 与完整 unit/proc/kernel 观察认证。历史 source 不代表当前健康，listing、成本文件和 checkpoint 不能创建、恢复或重绑来源。来源漂移撤销当代样本，不抹掉已接受的其它代完整历史。

`stage=helper_exit/state=collected` 是历史兼容标签。可选 observation 明确区分 `never_observed`、`present`、`absent_after_present`：精确空 listing 只能说明本次缺席；即使此前 present，也不证明成功启动、健康或正常退出。journal 的 start intent 先于 backend 调用，不能当作 start 命令已分派。

原失败保存后，可在原 `drive_deadline` 收取窗内至多一次 fresh 观察最后登记且仍 pending 的 helper。它保留原双侧 unit/proc/kernel、专用 manager membership 与前后同代 pending 门，不推进 transaction/timeline，不重试，不借 cleanup 做健康工作。成功来源的 `helper_source_observation` 精确字段为 `source/kind/started_ns/observed_ns/case_deadline_ns/elapsed_ms`，kind 固定 failure_collection；时间分别对应 Case 起点、观察完成点与原截止。截止后取得的来源不能倒填为 Case 内进度。该观察确有额外 RPC 和文件成本，不是零新增查询。

兼容的 start 三边界是 `start_entered/start_command_entered/start_command_returned`；entry checkpoint 使用固定 dispatch/returned 边界及经公共验证器接受的有限 failed/cancelled 位置。dispatch 不证明函数体进入，命令正常返回不证明 readiness；缺片不能推出未执行。当前 hook 安装 unit birth、serve 与窄 guard 观察，不调用旧 `install_helper_cost_hook()`；旧完整成本、start/entry 观察器仍由离线正控覆盖，不能假定本次 hosted 一定有这些字段。

`helper_guard_costs` version1 固定为 `partial/observed_helper_sources_only`，最多三份来源。窄 hook 只在精确 `-I -m muli.managed_host --root <本次journal> run` 绑定真实 `__main__` 的 main/_record 身份，随后撤下自身 profiler，不覆盖已有或后来 profiler。原 `_record` 与 backend `_check` 各调用一次，原返回、异常和取消对象传播；最多 64 个 generation/revision/phase/intent 位置，累计 entered/returned/raised、wall_ns/cpu_ns。成功 `_record` 按真实返回 record 分桶，失败进入 unpositioned_raised，不借旧 revision 或再读 journal。

每 helper 最多尝试两个 prefix（first_check_returned、first_install_target_check_returned）及一次正常 atexit final，失败也消耗机会，不重试或轮询。私有 PID/invocation JSON 原子发布，不 fsync；prefix 仅是已返回调用的下界，final 仅说明该 helper 经过正常退出回调。强杀、未绑定、未达边界或写失败允许 not_observed。

可选 breakdown version1 含 host_record/checks 内的 private_record、record_canonical、python_verify、package_verify、fileproof，以及固定 GetUnit/Unit/Service/other RPC 桶。它们是嵌套的已观察子集，不能相加当作互斥成本分区或纯 SHA 成本；fileproof 失败字节为 null，不伪造已读总量。startup version1 仅有八个固定同进程单调 wall/CPU 点，complete 表示八点齐全，不代表启动或事务成功。

收取沿原 helper 观察、正常退出及 failure collection 机会，核 no-follow、owner/mode、普通单链接、前后 FD/path 身份与 32 KiB 读取门。快照 monotonic_ns 与 collected_ns 分开；未来 revision、错误来源和异常时钟拒绝。同一快照保留首次接受的收取时刻，后续坏/缺快照不能覆盖旧有效历史。原 helper_costs version1/2/3 继续严格可读，controller/selected 桶是 checks 子项，不与 checks 或 host_record 再相加。

应用 `binding_inputs` 复用原完整 slot 注册结果，记录实际 A/B 包 files/size/digest、Python 大小/digest 与原预算，不新增 hash。`binding_costs` version1 只覆盖已由 origin_ready/最终 projection 认证的最多四个应用 source，固定 `partial/observed_sources_only`，每进程最多 64 个位置、八次 prefix 和一次正常关闭 final。被拒且未观察到的 target 不在覆盖内，也不能用应用绑定成本补 helper 成本。所有发布的 prior_publications 只计此前已完成发布，不含自身完整写入成本；进程墙钟、CPU、嵌套桶与事务总墙钟不能直接相加或作单一因果归因。

### worker 取消位置

`resources.worker_await` version1 精确字段为 `version/operation/outcome/started_ns/cancelled_ns/deadline_ns`，outcome 仅 cancelled。operation 仅 command、poll、http、helper_exists、helper_unit_path、helper_snapshot、helper_properties、managed_read；时间相对原 Case.start，整数必须满足原范围与先后约束。

同步边界不增加 await、task、probe、文件 I/O 或 helper 发布。真实 task 取消先记最内层位置，只有同一取消实际逃出 drive，或被已到期的 drive timeout 直接转换为以它为 cause 的 TimeoutError，才随未完成失败结果保存。普通异常、未请求的直接 CancelledError、HTTP 自身短超时和正常结束均不生成该位置，不遍历任意异常链。

该字段只定位 worker 当时等待，不能区分 helper 内部认证、业务 await 或异常 close。结果或 collector 合并超原 32 KiB 时先移除它，再判断旧成本/历史/样本候选，不用它挤掉原证据；空间足够则保留。缺失仍为 unknown。

## 清理与证据保留

cleanup 使用原 receipt、固定路径和已登记的至多三代 helper 名，不接受任意 unit 或通配停止。原功能失败先保存；取消或外层硬杀后由独立 always cleanup 重核归属并收尾。

1. 停止本次 journal/deployment 推导且已登记的精确 helper，避免旧 helper 尚存时重启服务。
2. 对固定 `muli.service` 单独做精确结构化 listing，核本次 FragmentPath，再停止并确认无 pending job、PID=0、cgroup 为空或已收集。它不借产品 helper-only 名称接口判断标准服务。
3. cleanup worker 实际重取 journal/controller lock 和 writer lease；功能已 completed 时重核同库最新资料、pending ingress、unknown outbox 和 routing，再在原 guard 与出生凭据匹配后移除自有 unit。无真实 journal/lease 不得删除现有 app 文件。协调器收取 worker 结果，不能用 stop 返回零代替这些证明。
4. 停止仅本次创建的 user manager/runtime-dir，按 receipt 核已登记 manager cgroup 为空，再移除身份与固定正文仍匹配的 bootstrap、mask、socket 配置及空自有目录，沿原 instance revert 生命周期撤销专用 drop-in。四个固定系统 unit 根中的外来 override/drop-in、额外条目、身份替换、丢失确认或非空树均拒绝后续破坏性清理并保留现场。
5. 先卸两个精确 bind，再卸本次 ext4；只在归属仍确认时删除本次账户及同 GID 的空私有组并复核。子挂载/进程残留、卸载或账户组回收失败都非零，不 lazy/force 卸载、不递归删仍挂载树。保留 receipt、result、镜像与空私有入口。

unit 出生凭据从本夹具排他创建开始，只跟随真实成功 CAS 替换更新；配置相同、产品托管身份或文件名不能代替删除权限。未知创建效果、pending 标记、foreign 内容与身份变化均失败关闭。

清理可在服务异常时定向关闭无其它业务的专用 UID manager，但账户/挂载账本清空只证明相应回收。泛化 native_service_cleanup_failed 不定位具体失败命令，VM 最终销毁也不证明正常收尾。清理不重写功能 timeline、三代历史或失败首因；任一命令失败或最终期限耗尽不能发布 verified。

## 维护与验收

修改认证、观察器或预算接缝时，先用离线回归覆盖真实接口、身份漂移、内容变化后恢复 mtime、错误优先级、同对象异常/取消、失败关闭与最坏编码。检查在原 `scripts/check_local.py` 限额内先小范围、再相关整合，严格收取终态后才启动下一项；不通过扩大上限、跳过用例或降低完整包/认证门让检查变绿。

离线 manager-instance 父层计量回归的调用期限跟随 cleanup 常量；失败收取回归使用从 harness 常量派生且持续走动的正时刻模型，明确保持 Case 截止已过、原收取期限尚未耗尽，避免依赖 runner 开机时长。身份认证、字节授额、1024/1025 字节边界与只收取一次的断言仍须完整验证。

本机检查共用 `muli-checks.slice` 的 1 GiB、swap 0、半个逻辑 CPU 等效预算与 96 tasks；单次最多 768 MiB、180 秒。Node 堆 192 MiB、前端文件单并发。无法核验保护时停止检查，不回退无保护运行；这些开发限额与上文 hosted fixture 限额分别生效。

本地回归、类型检查、读取减少、RPC 重叠、正常 cleanup 或前两代投影均不构成完整原生成功。只有同一精确候选在 Case600 / harness720 / cleanup120、既有资源与字节限制内完成全部三事务、全 hash/来源认证、最终数据库与路由投影及实际收尾，才可报告新增原生覆盖已验收。其它平台失败与取消分别保留，不拿另一提交的绿灯补齐。

诊断字段和严格读端以[协调器结果校验](../scripts/check_managed_native_ci.py)与 [worker 验证器](../tests/helpers/managed_native_worker.py)为准。更细的[启动正向边界](managed-native-start-boundaries.md)、[应用绑定成本](managed-native-binding-costs.md)和[认证设计归档](../devarchive/managed-backend-authentication-design.md)用于解释各自局部合同，不替代当前精确 CI 的完整验收结论。
