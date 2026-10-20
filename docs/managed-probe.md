# 稳定服务入口与实际运行进程

`ManagedSystemdProbe` 是 Linux managed 整合使用的只读观察器。它复用现有有界 D-Bus 和 procfs 读取；`ManagedUserService` 复用原用户服务渲染及资源限制，只改变启动参数。

systemd 保存的是固定控制安装的 launcher 参数，exec 后同一个 MainPID 运行的则是当前 role 安装。观察器分别比对两套参数，并检查 UID、cgroup、invocation、解释器真实路径和两次观测的一致性。两个虚拟环境共用同一个基础 Python 文件，不能代替对虚拟环境词法路径的验证。配置与安装文件的准入仍由启动许可和控制后端负责，probe 不发放健康许可。

运行态必须匹配冻结的完整 role 与已加载 unit。只有已停止、无 MainPID、无待处理 job 的安装或恢复阶段，才容许磁盘 unit 已改但 daemon-reload 尚未完成；这种状态不会返回 unit_confirmed。源文件、drop-in、未知启动参数及手工修改均拒绝。回退期间保留对冻结身份进程的观察能力，避免损坏的新安装阻塞停止和旧安装恢复。

12 项定向测试使用临时 unit/procfs、假 D-Bus，覆盖入口与进程参数不同、共享解释器、错误 role、reload 阶段边界、第二次观测进程和文件变化。与旧观察器及管理协议合并共 253 项回归通过，三源码严格 mypy 通过；整组最高 124.7 MiB、swap 0，均经统一资源守卫。没有读取或操纵真实用户服务。这是内部模块证据，尚不代表 A/B 切换已完整可用。

独立复审发现 `TimeoutError` 会被通用 `OSError` 分支误归类；现与原观察器一致，保留超时语义并等待底层读取取消完成。新增超时/主动取消回归后，新旧观察器合计 61 项通过（2.36 秒、峰值 64 MiB、swap 0），Ruff 与格式检查通过。
