# 候选进程的 Linux 资源边界

`platforms.candidate_process.run_candidate_process(ProcessSpec)` 是一次候选准备命令的执行后端，不选择版本、wheel、索引或凭据。它复用 `candidate_policy.py` 的唯一预算；本片不接 `self_update`，不安装真实 MuLi，不改变当前服务或固定 controller。

调用者交付已拥有的 0700 attempt 目录、其中的工作目录、绝对可执行文件、完整环境、同一个绝对 monotonic deadline 和有限同步 `observe()`。执行前、放行前和运行中的周期核查复用该回调；它负责宿主已有的目录身份、材料与磁盘阈值观察。后端不改变 HOME、不为调用者选安装源，也不删除整个 attempt。

仅 Linux systemd user manager 与 cgroup v2 支持此后端。`disk_mode=threshold` 可执行，周期观察存在超调，不能称为内核磁盘配额。当前 `strict` 固定返回 `candidate_disk_quota_unsupported`，没有裸进程 fallback。此边界限制误用与资源消耗，不隔离同用户恶意进程，也不声称整个应用的多个候选共享一个总预算；宿主仍须 singleflight。

## 放行顺序

1. 用当前可信 Python 的 `-I -S` 执行包内独立 stdlib 脚本 `_candidate_worker.py`。不先加载 MuLi、插件、候选包或 `.pth`。固定 worker argv 仅包含私有控制文件路径；命令、环境、nonce 留在有界 0600 控制文件中。
2. `systemd-run --user --pipe --wait` 创建唯一 transient `Type=exec`、`Restart=no` 单元。`RuntimeMaxSec` 取此次调用剩余期限，不续期；关闭 core dump，使用 control-group kill、SIGKILL、OOMPolicy=kill、MemoryMax、swap 0、CPUQuota、TasksMax、0077 和 NoNewPrivileges。
3. worker 只连接 Linux abstract AF_UNIX gate 等待。abstract 名称不会因 retained_root 较长而超出文件系统 socket 地址上限；随机名称不是授权。worker 用 `SO_PEERCRED` 核父 PID/uid；父也用实际 peer PID/uid 核同一 worker，并验证单次 nonce。错误 peer 被关闭，不消费许可；总期限与至多八次 accept 限制等待。断连、错误消息或超时都不能执行命令。
4. 放行前两次读取固定类型的 systemd 属性、实际 proc PID/start time/uid/executable/argv/cgroup，以及内核 `memory.max`、`memory.swap.max`、`cpu.max`、`pids.max`、`memory.oom.group`。声明与实限同时匹配，单元身份和观察不变，才在最后一次 `observe()`、目录与期限核查后发一个许可字节。
5. worker 再核绝对期限，以同 PID `execve` 候选命令和完整环境。未获许可之前不执行 argv；放行之后不自动重试命令。

该单元独立于 MuLi 服务生命周期，不使用控制 helper 的 `Restart=on-failure` 策略。只在明确成功的结构化 D-Bus 列表为空时认定 unit 不存在；stderr、命令失败或未知 manager 状态都不是不存在证明。

## 输出、取消与收尾

`run_process` 限定两条输出管道的总字节数、拥有启动任务并收取 `systemd-run` 客户端。客户端退出并不代表服务树退出：后端另外核 systemd invocation、静态 worker ExecStart 和实际 cgroup 是否为空。必要时 StopUnit，再对同 invocation 的所有进程 SIGKILL；停止观察总上界为两倍 `stop_seconds`，此为到期后的有限清理时间，不延长命令执行许可。客户端自身 reaping 沿用现有 `run_process` 的有限管道排空与强拥有实现。

重复取消观察者时仍等待同一次清理。`ProcessOutput` 只在整树已核空且客户端收尾已返回后产生；stdout/stderr 私有，不作为模型输入。取消且清理成功则传播 `CancelledError`；无法证明清理成功时优先抛 `CandidateProcessError(cleanup_confirmed=False, unit_name=...)`，错误正文只有固定代码。宿主必须保留 attempt，不能清除可能仍被进程使用的文件。

开始请求丢返回且从未观察到 unit 时，一次列表不存在也不能排除迟到的 StartTransientUnit；此时保守报告未确认。gate 已关闭，迟到 worker 不会获准执行，有限 RuntimeMaxSec 仍生效。没有凭一次 not-found 结果伪造可删除证明。后端不新增持久执行账本、不承诺外部效果恰好一次。

## 验证边界

修正后的回归只操作小型临时目录、fake manager/proc/cgroup 和已知 stdlib worker。重点验证真实 gate 放行前零执行、断连/错误父身份/期限失败、内核与声明独立校验、观察变化拒绝、客户端已退出但 cgroup 仍活跃、强制 kill、重复取消与未知清理保留。本轮修正后检查没有创建真实 transient 服务、运行 uv、下载、安装候选或访问用户目录；真实 manager 与候选准备整链仍待后续单独验收。

全部检查通过主仓库 `scripts/check_local.py`，单次 30 秒以内、单次 1 GiB / 共享 2 GiB、swap 0。fake 后端 48 项通过（1.33 秒，58.6 MiB），其中遗漏 runner 与误传默认 runner 的两例都由模块级 spawn 边界直接拒绝；独立文件中的普通 stdlib worker 7 项通过（0.26 秒，62.2 MiB）；既有 helper/probe 103 项通过（0.26 秒，57.9 MiB），共 158 项。三个源码文件严格 mypy、五个 Python 文件 Ruff 与格式检查通过；成功类型检查峰值 122.9 MiB。运行器协议允许一般 Awaitable，启动任务用内部协程承接并强持有，保留原收尾语义。最终只读 manager 列表仍只有下表原五个 failed 单元，没有新增候选单元。这些结果不代替真实 manager 与完整候选安装的单独验收。

首轮夹具有一次真实边界越界，不能计作 fake-only 验收：`SystemdProbe.__init__` 的 `runner` 实例属性遮蔽了 fake 同名方法，五次调用意外运行了真实 `systemd-run`。全部 worker 未获 gate 许可，以 125 退出；只读复核五个精确单元均 failed、PID/ControlPID 为 0、cgroup 已移除、原 PID 不存在。实际寿命 2.006509–2.022519 秒、每个峰值 5,500,928–5,701,632 字节、swap 0。它们属于 `app.slice`，没有继承开发守卫的共享 slice；不能将其描述为共享守卫内的执行。未运行 uv 或候选 argv，失败单元证据保留。修正后的 fake 后端独立测试文件在每次入口核 bound method，并在模块级 fixture 禁止 `asyncio.create_subprocess_exec`；真实 gate 子进程另文件执行，避免再以等待事件超时掩盖错误启动。

| 精确单元名（均以 `muli-candidate-` 开头、`.service` 结尾） | 原 PID | 实际秒数 | MemoryPeak 字节 |
| --- | ---: | ---: | ---: |
| `muli-candidate-42c3a525026307fee222ea91b564adfc.service` | 412230 | 2.008247 | 5,550,080 |
| `muli-candidate-f0e559441ac7f244807ac13d4f0ef57b.service` | 412236 | 2.021493 | 5,701,632 |
| `muli-candidate-c6ec04c49f6ec37a71b60a8467d07ce2.service` | 412249 | 2.020729 | 5,550,080 |
| `muli-candidate-e6954c7646cf9a7e9e2e5842a5164fb0.service` | 412271 | 2.006509 | 5,500,928 |
| `muli-candidate-2f711cdc1320ffac5fb6ef15b8f818f8.service` | 412581 | 2.022519 | 5,500,928 |

五条内核路径均为 `/user.slice/user-1000.slice/user@1000.service/app.slice/<完整单元名>`，核验时均 ENOENT。manager 的 `Result=exit-code`、`ExecMainCode=1`、`ExecMainStatus=125`、`ActiveState=failed`、`ControlGroup=`；没有凭客户端返回值假称树空。故意遗漏 runner 和误传默认 runner 的两条回归必须被 fake 模块的 spawn 边界直接拒绝，不能再次发起服务。

参考官方接口：[Python 非阻塞 socket 协程](https://docs.python.org/3.12/library/asyncio-eventloop.html#working-with-socket-objects-directly)、[systemd-run](https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html)、[systemd.resource-control](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html)。
