# Linux 托管事务的效果后端

`ManagedLinuxBackend` 是 protocol 2 controller 的 Linux 效果适配器，已接入 CLI、helper 与启动宿主。完整入口的整合状态见[本地托管切换](managed-operations.md)。下述模块回归没有操作用户真实 systemd 服务；protocol 1 仍使用原后端。

后端消费同一 journal 的冻结 `RoleBinding`、`RoleSnapshots`、deployment anchor 和 slot metadata。它使用 `ManagedSystemdProbe` 分别观察稳定 unit 的 launcher argv、实际服务进程的角色解释器 argv，以及 boot、PID/start ticks、cgroup 和 loaded unit 属性。配置相同仍比较 selector 与真实 slot；不把配置摘要当成完整安装身份。

任何效果前重读当前 revision、anchor 和 controller 文件。涉及健康、HTTP 激活、安装或启动时，还核当前角色的实际解释器、pyvenv、MuLi 包与兼容声明。另一角色只读冻结 metadata；损坏的 target 包不会阻止恢复仍完整的 base。仅 `stop_base` / `stop_target` 窗口可凭两次一致的精确进程、boot、argv、real executable、cgroup 和三文件观察停止已登记的损坏进程。该例外不产生健康证明，也不放松 foreign PID 或 cgroup 拒绝。

所有文件修改要求服务已经停止、cgroup 为空，并真实持有现有 writer lease。正向只按配置→selector→unit，回退只按 unit→selector→配置，接受范围是该代冻结正向或反向前缀。不会把三个文件各自的允许值任意组合。已有 desired 内容也调用文件及父目录耐久确认；不存在的 legacy selector 同样确认父目录。selector 只支持本产品 canonical JSON，非 canonical 快照在 prepare 时拒绝，后端再核，不悄悄重新序列化旧证据。

unit 变化后，后端必要时执行有界 `daemon-reload`，再从 fresh manager 属性证明当前 unit 已加载。命令返回零不算确认。只有同 revision、完整三文件耐久确认、空 cgroup、真实 lease 和 fresh loaded unit 证据才能产生 `BindingConfirmation`；start 在效果前独立重做这些核验。旧 boot、错误 nonce、错误格式/安装/配置证明均不能提交。原 deadline 和回退保留时间不续期；未知 start 不重复发送；激活请求超时或取消后只重观察，不由该后端盲目重发。

旧 `LinuxControllerBackend` 仅将原 loopback URL 与有限 nonce HTTP 收发提取为共享函数。protocol 1 的角色判断、proof 核验、效果门和响应语义保持。HTTP 不继承代理、不跟随重定向、不解压，仍限制原始响应 bytes，503 readiness 和传输超时保持原观察语义。

验证使用临时 slot 元数据、真实文件 CAS/fsync 和 flock；systemd、进程观察和 HTTP 都是有界 fake。覆盖同配置 switch/adopt、三文件顺序、写入错误和取消后的前缀重入、loaded unit 未生效、坏 target 停止与 base 恢复、foreign 进程拒绝、原 deadline/revision/安装证据重验，以及未知 start/激活超时。

2026-10-21 本地实测全部经统一守卫串行执行，每次硬期限 30 秒，资源上限未提高。新文件 31 项按不重叠主题分三批全部通过：11 项 18.48 秒、9 项 15.13 秒、11 项 12.10 秒；两份旧 V1 controller/backend 文件全 83 项通过，0.53 秒。3 份源码 mypy 与 4 份 Python Ruff/格式通过。最终测试峰值 62.2 MiB，整轮最高为 mypy 的 127.8 MiB，swap 0。

首轮整文件到 30 秒被守卫清理，未算通过；当时顺序夹具只记 `confirm`，漏记已经完成 file+parent fsync 的 `replace`，导致一条断言失败。仅修正该夹具观测后按主题分批验证，没有提升时间/内存预算或删除回退顺序断言。

独立只读复审已通过：核对冻结前缀、正逆序、两次进程观察、writer/cgroup 门、损坏 target 的 stop-only 例外，以及 V1 HTTP 提取的原语义。复审未把上述 fake 回归视为真实服务验收。

这些模块证据不能证明真实 systemd 用户 manager 的加载或真实服务 exec/激活成功；共享已安装目录被外部程序修改也不是沙箱隔离。生产调用链已接线，另有[真实进程验收](managed-process-recovery.md)；该验收仍使用合成 manager 边界，不代表操作过用户真实服务。

单次同步 `_check` 中，若当前角色与 controller 的完整 `SlotIdentity` 相等，复用本次 `verify_anchor` 已完成的文件核验，不再重复扫描同一安装。不同 slot 即使共享完全相同的解释器仍分别核验；结果不跨 `_check` 保存，下一次 guard 会重新读取和核验文件。所有效果前和 await 后的 guard、文件耐久确认及原期限保持原样。远端分项证据与性能验证边界见[恢复核验计时](managed-recovery-timing-design.md)。
