# Managed 服务启动前的真实身份与 writer 交接

`open_boot_session` 在同一个 `state.json` 上先用 `read_current()` 严格区分 V1/V2。
V1 保留原标准服务及 foreground 规则；V2 始终进入 `admit_managed_boot`，不能退回
无 permit 的普通启动。分派发生在加载旧 `installation.json` 之前，损坏或过时的 V1
manifest 不成为 V2 的启动依据，也不阻塞已经选定的 legacy 回退。

入口新增两个关键字参数：`managed_root: Path | None` 和可注入的
`managed_probe: ManagedServiceProbe | None`。显式 root 必须已存在且有严格 V2 record；
缺失、V1 或坏记录均拒绝，不创建替代根或回退到普通服务。没有该参数时仍读取原
`data_dir/management/transaction-v1`，因此恢复后的 legacy raw unit 即便没有
`--managed-root`，也能从同一账本识别 V2。没有新增 mode 文件、数据库或锁。

默认入口缺失 `state.json` 时，只有 `selector.json` 目录项也不存在才允许普通
启动。共享 `require_unselected_without_record(root)` 使用 `lstat`，因此坏 JSON、
dangling symlink、FIFO 或目录均按残留 selector 拒绝；不会打开特殊文件或跟随链接。
仅预发布 anchor/slot、尚无 selector 的准备崩溃前缀仍可走普通入口。此门不重建
记录、重签安装或猜测上一代选择；宿主 CLI 的缺记录分支也应调用此检查。

## 准入顺序

1. 原 bootstrap 持有实际数据库 writer lease，才读取并分派 record。V2 从不可变
   deployment anchor 取得固定路径、controller 和预算，复读当前 record。旧清单
   不参与身份验证。调用者 config、data、database 和 unit 路径必须与 anchor 相同。
2. `resolve_entry(..., purpose="service")` 仅提供这一代的路由建议；它不能代替
   子进程的准入。读取两份冻结 slot 身份，但不重新验证未选 role 的当前包文件，
   以免坏候选阻塞 base 回退；固定 controller 和实际选中安装仍由既有入口与 permit
   验证。
3. 默认 `ManagedSystemdProbe` 分开验证稳定 unit 的 launcher argv 和 exec 后进程
   的 slot argv。bootstrap 进一步要求 MainPID 和真实 PID 都是 `os.getpid()`、
   有效非零 InvocationID、实际 running/starting 状态、已确认加载的 unit，以及
   process_roles 包含本次允许 role。legacy 回退验证其真实 raw unit/argv，不能
   把为新入口生成的路由 argv 当成实际进程证据。超时或观察不支持均明确拒绝；
   外部取消保持取消，不产生 foreground 降级。
4. 只有上述检查通过才把同一 lease 移交 `ManagedBootPermit.acquire`。permit 在
   journal 同步屏障后重读权威角色，再核实际解释器、配置/selector/unit、无迁移
   格式证据及 Store/main 身份；允许合法同代 revision 推进，拒绝不可变事实变化。
   返回的 permit 提供已核 `ConfigFile.from_bytes`；普通业务 Store 仍由宿主随后通过
   `permit.open_store()` 显式取得。bootstrap 本身不构造 Runtime 或业务 Store。

普通启动、target probation、base 回退及 resolved 角色都使用同一个入口。默认
probe 的子预算沿用 anchor 的 `operation_timeout / 4`，不改变持久总期限。

## 关闭所有权

`BootSession.permit` 可为 V1 或 V2 permit。`close_after_writers()` 先委托 permit，
成功后才关闭 journal 并标记 session 已关闭；没有 permit 时才直接释放原 lease。
宿主仍须先确认所有业务 writer 已关闭。

V2 准入若遇 SQLite 关闭失败，其异常强拥有的 permit 会转交给 session。外层失败
清理必须再次经过该 owner 的拒绝门，不能在 `finally` 中无条件关闭 raw lease 或
journal。Sticky cleanup failure 保留连接、DB inode anchor、writer lease 和 journal
描述符至进程退出；不把“请求关闭”当成“关闭成功”，不自动重试业务或修库。

## 验证边界

新增 18 项与原 Linux bootstrap、managed boot 共 107 项通过（pytest 1.53 秒，
service 2.044 秒，峰值 105.8 MiB）；两源码严格 mypy、三份 Python 文件 Ruff 与
格式检查通过，静态检查最高 128.8 MiB，swap 0。全部经统一守卫、每次 30 秒硬限。
首轮 106 过、1 个 fixture 失败：模拟只推进 revision 时误重写了 unit，实际 probe
正确拒绝身份变化。fixture 改为只在 journal 锁内推进 record 后，同组全部通过。

缺记录尾片另增 7 项，连同 Linux/bootstrap/launcher 相关组共 104 项通过
（pytest 6.88 秒，service 7.475 秒，峰值 107.5 MiB）；两源码 mypy、三份 Python
文件 Ruff/格式检查通过，静态检查最高 126.5 MiB，swap 0，每次仍为 30 秒硬限。
旧入口的单例反证确实越过 bootstrap 到达 `ConfigFile` 构造哨兵；补门后普通
`serve` 对四种残留 selector 均在配置、Runtime 和 SQLite 入口前拒绝，并释放
尚未移交的 writer lease。首轮相关组 103 过、1 个既有显式 root 错误码断言失败，
新门移到显式 root 原拒绝分支之后，保留既有接口后同组全部通过。

回归使用临时 marked 数据库、真实 flock、实际 ManagedSystemdProbe 配合合成
D-Bus/procfs，以及临时安装声明。覆盖同一账本的
默认/显式入口、坏 V1 manifest 不干扰 V2、真实当前 role 与 PID 核验、legacy raw
unit 回退、合法 revision 推进、配置/路径变化、取消与失败所有权。

这些是宿主启动原语的离线证据；没有运行真实 systemd、真实候选解释器或用户
数据库。Runtime/Maintenance、CLI、helper 及状态投影的最终组合由宿主接线验收，
不能据此宣称完整 A/B 切换已经恢复成功。参见[managed 准入](managed-boot.md)、
[服务观察](managed-probe.md)和[原 Linux bootstrap](linux-bootstrap-design.md)。
