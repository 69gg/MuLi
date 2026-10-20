# Linux 同版本控制者与 helper 接口稿

状态：2026-10-21，当前切片实现 backend 并通过离线替身回归；helper 与管理／CLI 发起接线仍是下述设计。前两块的 journal、控制者状态机、维护门与 nonce 路由已提交，Linux 只读 probe 与配置 CAS 已复用。本稿不证明真实服务重启、硬退出接管或升级能力。

## 文件归属与依赖

| 归属 | 文件与责任 |
| --- | --- |
| 根任务 | `platforms/linux_transaction.py`：结构化 systemd/proc 观察；安装 manifest、原配置字节 CAS、bootstrap 身份模块及测试。保持其实际摘要、文件耐久性与服务入口核验的权威 |
| 当前切片 | `platforms/linux_controller.py`：实现现有 `ControllerBackend`，复用 probe、配置 CAS 及同步安装核验回调，封装固定 nonce HTTP；不另造状态机 |
| 当前切片 | `tests/test_linux_controller.py` 与控制者的必要窄回归：进程、HTTP、时钟、cgroup 均为替身；锁与配置文件只落临时目录，不运行真实服务 |
| 后续 helper 切片 | `management_helper.py` 及其测试：仅接收 journal root 的轻量进程入口，取得同一 flock、验证实际资源、按原 deadline 驱动已有 `TransactionController`；transient ensure 也留在该片 |
| 后续协调接线 | `__main__.py`、`management.py`、`server.py`：在根任务提供 bootstrap 合同后再接，不在本接口稿阶段修改 |

backend 接收 `UserService`、`TransactionJournal`、持有同一 journal flock 的 `JournalControl` 和 `verify_installation: Callable[[], None]`，可注入 `SystemdProbe`、command runner、HTTP transport 与共享 clock。安装模块就绪后用 manifest 的同步 `verify` 提供该回调。backend 不构造 `ConfigFile`、`Runtime` 或 `Store`。真实能力核验不依赖用户当前网页、模型输出或某个 PID 数字。

manifest 提供的最小能力为：有界读取与重新核验；按 canonical 字段重建同一 `UserService`；读取精确配置字节摘要并执行带预期摘要的原子替换；获得核验后的服务 `InvocationID`。helper 自身的 typed unit 属性／硬限观察应从 probe 的窄公开入口复用，不从第二个模块调用私有解析器或复制 D-Bus 解析逻辑。

## 安装与持久化边界

`installation.json` 仅包含 canonical config/data/database/unit/executable/package root、实际 package 摘要和本次固定 helper 预算。安装摘要是该 manifest 的规范化摘要；服务摘要绑定生成的标准 unit 与实际 argv。配置内容、nonce、环境原文和任意命令不写入 manifest。

同一 journal 锁下先检查旧代已经 resolved 且完整 revision 已正确投影，再准备新 manifest／记录。manifest 文件及目录同步成功后，才能发布引用其摘要的 prepared。存在未解决／未投影旧代时不得重写 manifest；同 request ID 重放使用原 manifest、原目标和原预算，不能按新设置重算本代 deadline 或资源。

应用 boot 使用实际核验后的 systemd InvocationID；`ClockReading.boot_id` 仍是内核 boot ID，两者不能互相代替。核验 executable 时分别保留实际入口 argv 与其解析后的 executable 身份，不能因解释器符号链接而把另一条入口当成相同标准服务。

## backend 的观察与效果

`observe(record)` 重新核验 manifest／package、标准 unit、proc 身份和配置摘要，观察 writer lease，并在需要时请求当前角色的 readiness。HTTP 前后仍须是同一 InvocationID；只接受该代 challenge、正确配置／库／服务摘要与当前 boot 的严格 `HealthProof`。原实例的 paused／released 只在 prepared／draining 解释为 drained／resumed；两个停止 intent 只用原实例 proof 重新核身份，不把它当作新目标健康。

标准 unit 的 Type=simple 只表示已执行服务入口。仅在 `activating/start_target` 或 `rolling_back/start_base` 中，systemd active/MainPID 存在而 CLI 尚未取得 writer lease 或本地 core 尚未就绪时，backend 报告 starting，不能提前使 verifying 立即失败。prepared/draining 的原实例仍须 running；已经 verifying 的同 boot 仍运行但暂缺 HTTP 证明时保持 running、health=None，继续原期限观察。明确 blocked 不被 starting 掩盖。

| 方法 | 最小检查与结果 |
| --- | --- |
| drain / resume | 只请求固定内部路由。命令返回不保存成权威状态；下一步 observe 重新 GET 原实例 proof |
| stop | 重新核验本代安装、服务、当前 boot／角色与已有 job 后才请求停止同一 unit；只视为 accepted，后续观察确认退出 |
| install | fresh unit 已停止、cgroup 空，再取得独占 writer lease；借根任务的 raw CAS 替换配置。摘要已是 desired 时仍须确认耐久性，不回滚数据库 |
| start | 已持久 start intent；复核配置、unit、空 cgroup 与可取得 lease。启动前释放 lease，使用 start，不使用会重启已有进程的 restart；不在重入时盲发第二次启动 |
| activate | 只向已持久 committed 的 target 或 rolled_back 的 base 发固定 nonce 请求；下一步仍用 GET 确认实际 released。blocked 的同 boot 不自动重试 |

lease 可取得只证明合作 writer 不在，不代替 cgroup 空；systemd MainPID=0、stop accepted 或配置文件已变化均不能单独证明旧实例退出。已有 stop/start job 时先继续观察，不覆盖其它 job。unit 身份在检查与 manager 调用之间不具备应用自造的原子 CAS；不得将前后核验描述成内核保证的条件停止。后续如使用 invocation 专属 D-Bus 对象路径，应先核对受支持版本与实际语义，不能无证据增加最低版本要求。

空态只接受持有独占 writer lease 的前后 fresh unit 均为 inactive/failed、MainPID=0、无 process 且 Job=(0, '/')；其间读取 manager 给定的 ControlGroup，不自行按 unit 名推路径。非空路径必须通过逐段不跟随链接的有界 `cgroup.events` 取得 populated=0，它覆盖后代成员，不能以直接成员 `cgroup.procs` 为空替代。ControlGroup 空或整个指定路径已被移除也只能与上述停止及 lease 证据合用，不将空字符串拼成 cgroup 根目录。组目录存在但 events 缺失、权限／I/O 错误、FIFO／链接均拒绝。此合作协议不覆盖进程主动逃逸 cgroup 或外部管理员绕过锁的操作。

HTTP endpoint 从受限快照中的 server 配置计算，只访问本机地址；不允许调用者传 URL。使用 `httpx.AsyncClient(trust_env=False, follow_redirects=False)`，单连接、固定 header 和路由；外层总期限覆盖连接与流式读取。只接受 identity 编码的有界原始响应并严格校验 DTO，避免先完整 `.json()` 再检查大小。不把原始响应、nonce 或异常正文写进日志／管理对象。

连接尚未就绪或未取得完整证明可继续观察；已验证的 blocked／明确当前实例硬失败优先于矛盾的 released 结果。HTTP 或 systemd accepted 都不直接产生 completed。

共享 action 总预算默认 20 秒、最多 30 秒；`ProbeLimits.seconds` 与 HTTP 子预算均不得超过其四分之一。helper 将同一 `backend.operation_timeout` 交给 `TransactionController`，不能沿用其独立默认 5 秒再叠加多个 5 秒子操作。持久剩余期限始终先到先停，activating/verifying 继续保留原回退预留；最终 CAS guard、confirm 后的 start、manager／POST 前还同步重核共享 `clock_reading()`，避免同步 fsync 消耗期限后仍启动效果。应用 InvocationID 不参与 deadline 的 kernel boot ID 计算。

POST activate 可能已被宿主强拥有任务接受而响应超时。backend 在发送前记录唯一的 `(generation, boot, role)` 尝试，超时／丢响应后同一对象不再 POST，只通过后续 GET 观察；取消仍向外传播。helper 重建后的同代重发仍依赖宿主强拥有任务幂等，并非恰好一次效果保证。committed/rolled_back 的暂缺 proof 或短 action 截止在原有效 deadline 尚有剩余时只等待、不激活、不换代、不完成；错误 proof、明确拒绝／blocked 或其它后端错误仍失败，总期限耗尽或时钟失效仍阻断。该窄规则不改变决定前的回退语义。

## 独立 helper 寿命

固定绝对解释器以隔离模式执行 `-m muli.management_helper`，业务参数仅 journal root；建议 `WorkingDirectory=/`，不依赖调用者 cwd／PYTHONPATH。helper 名绑定完整安装摘要和 generation。ensure 先读取现有同名 unit 并核验 argv、身份与限制，不能以另起名字绕过已存在控制者或重启限制。

transient 使用 service、Type=exec、KillMode=control-group、明确 UMask、MemoryMax、MemorySwapMax、CPUQuota、TasksMax、RuntimeMaxSec／TimeoutStopSec，并有有限 Restart/StartLimit；不使用 scope、后台 shell、PartOf 或 BindsTo 原应用的关系。不要求安装新 slice。manager 接受／exec 成功不是事务完成；helper 驱动任何效果前必须核验实际 cgroup 硬限。缺少能力明确 unsupported，保留原服务，不退回无保护进程。

runner 顺序为：有界读取 manifest → 重构 journal → 取得固定 flock 并 adoption barrier → 核验自身 unit/资源与安装 → 在原持久 deadline 内逐步调用既有状态机。只在没有进展时做有限等待，等待预算来自 manifest；不创建第二套阶段或额外成功状态。普通控制者取消保留最后持久 intent，收尾 HTTP／进程句柄后退出；异常重入仍先观察实际状态，不能重新给满额 deadline。

即使接管时记录已 resolved，也尝试最后一次同代 readiness GET 作为宿主提示。该请求失败不撤销持久 released、不制造隐式未完成状态；宿主现有配置／唤醒入口仍会按 journal 自修提示。配置、Store 与用户效果均不由 helper 做补偿或投影。blocked／recovery_required 不靠自动循环解除；未提供明确恢复能力时如实返回该状态。

transient 在 user manager／机器整体重启后可能不存在。只由受支持服务的 bootstrap 或显式 recover 再 ensure 同代 helper，不新增常驻轮询守护进程，不启用 linger；未自动启动原服务且无人调用恢复入口时不承诺自动续接。

## 离线验收与来源

本 backend 的最小反证包含：启动窗口未持 lease 不过早 verifying、HTTP 前后 InvocationID 变化、原 boot 被拒作新健康、超限／重定向／压缩响应、已有 stop job、MainPID=0 但 cgroup 非空、lease 被占、CAS 冲突／desired 可见但 fsync 持续失败不得 start、同步确认后期限耗尽、激活响应丢失／取消后不重复 POST、真实控制者配合假服务完成 commit 及单次 rollback。helper 同名异 argv／资源不足、重复 ensure 与 resolved 接管后的最后提示仍待后续 helper 切片，当前未验收。测试仅替身，全部 guard 串行。

本片共 83 项定向用例有通过证据（45 个 backend 与 38 个控制者）。首组 82 项 0.56 秒、峰值 66.1 MiB；新增 backend 效果前时钟失效单例另验 0.09 秒，确认持久 recovery_required 且不替换配置。四文件 Ruff／格式、两源码严格 mypy 通过，静态检查峰值 123.7 MiB，swap 0。首轮 mypy 的两个局部 Optional 句柄提示改为区分可取得句柄与跨观察持有的 lease 后消除，相关 44 个 backend 也复验通过；没有放宽类型或资源限制。

已通过 Context7 查验 [systemd transient 可用属性](https://github.com/systemd/systemd/blob/main/docs/TRANSIENT-SETTINGS.md)、[D-Bus 接口](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.systemd1.html)、[systemd-run 行为](https://github.com/systemd/systemd/blob/main/src/run/run.c) 和 [HTTPX 流式响应及客户端选项](https://github.com/encode/httpx/blob/0.28.1/httpx/_client.py)。这些上游资料不能代替已安装版本的 capability 核验；本稿未运行命令探测或真实产品服务。
