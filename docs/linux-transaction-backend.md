# Linux 事务后端：只读观察入口

状态：只读观察切片已实现、完成独立静态复审和 35 项离线回归，尚未接入服务管理。此片只建立受支持服务的有界观察，不能据此声称可恢复重启、A/B 更新或进程交接已经完成。

后端沿用 `UserService` 的 unit 名、路径、argv 和生成文本，以及 `run_process` 的取消、子进程回收与输出预算。先读取结构化 systemd 属性，拒绝解析 `systemctl status/show` 中展示给人的 ExecStart 文本；不引入 D-Bus Python 库。

## 观察合同

- 使用 `busctl --user --json=short --auto-start=no --allow-interactive-authorization=no`，限定固定的只读 `GetUnit` 和属性名单。`GetUnit` 的 data 是单参数数组；`get-property` 按请求顺序逐行返回有类型的 JSON。严格校验类型、数量、长度，不读取服务 Environment 或原始异常文本。
- `ControlGroup` 来自 `org.freedesktop.systemd1.Service`；`InvocationID`、`FragmentPath`、`DropInPaths` 与 `NeedDaemonReload` 来自 Unit。通过服务返回的路径定位 cgroup，不从 unit 名猜测 cgroup 层级。
- 只接受 `UserService.render()` 的当前标准 unit、相同的实际 ExecStart 参数、无 drop-in、无待重新加载配置；其它布局返回固定的 unsupported 原因，保留原服务。
- 连续两次结构化快照之间读取受限 `/proc/<MainPID>` 身份与已生成 unit 文件；确认 PID 的启动时间、实际 executable、argv、uid 和 cgroup 相符，前后快照必须一致。停止或切换中的 unit 返回可观察的状态，不将单次 MainPID=0 当作所有 writer 已退出。
- 同一探测有总期限、每条命令输出上限与本地文件读取上限。发生版本不支持、格式变化、取消、路径替换或进程消失时明确失败，不执行修复、重载或重启。

这仅是瞬时观察；真正的 stop/install/start/activate 仍须在每次效果前重新核验、持有 journal 锁，并另行检查安装摘要、内核 cgroup populated、数据库 writer lease、配置精确字节 CAS 与 nonce HTTP 健康。普通 PID 或标准 unit 内容都不能替代这些事实。

## 来源与验证

依据 [systemd D-Bus 接口](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.systemd1.html)、[busctl](https://www.freedesktop.org/software/systemd/man/latest/busctl.html) 和 [cgroup 接口](https://systemd.io/CONTROL_GROUP_INTERFACE/)。Context7 已核对上游源码与调用签名；本机只读探针只访问本次资源 guard 自身的临时 unit，未查询或变更用户 MuLi 服务。首探误将 ControlGroup 放在 Unit 接口，修正为 Service 后成功；正确探针 service 83ms、峰值 16.5 MiB、swap 0。

测试使用注入进程结果、临时 proc/cgroup 文件和受控异步窗口，覆盖类型混淆、输出预算、同 PID 不同启动时间、相同内容 unit 换 inode、配置/命令/入口变化、FIFO/符号链接拒绝、取消与状态切换。首轮 34 项通过、一项指出 `TimeoutError` 被共同 `OSError` 分支归类；修复为保留总期限错误，并在同步文件观察后再次核验截止期限。最终 35 项通过，0.09 秒；两文件 Ruff/格式、单产品源码严格 mypy 通过，service 728ms、峰值 67.6 MiB、swap 0。独立只读复审无阻断，没有执行真实服务控制。

文件字节预算与返回前期限核验不等于能中断任意阻塞文件系统调用；正式控制者还须核验受支持的本地文件系统并有独立进程硬期限。真实 systemd 停服、启动和崩溃接管尚未验收。
