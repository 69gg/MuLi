# 回桥后的逻辑 V1 permit 与真实 writer owner

失败迁入恢复 legacy raw unit 后，`prepare_legacy_permit` 复用既有
`prepare_legacy_after_managed`，创建新 V1 generation，同时返回 `LegacyBootPermit`。
调用宿主仍须先认证当前服务 PID/InvocationID，并持有配置锁和 journal 控制锁。

适配对象继承 V1 `BootPermit`，但只提供逻辑身份。其 installation/service 来自桥函数
实际核验的完整 `InstallationManifest`，不是 V2 slot 摘要，也不向旧 managed 对象
增添 `.service`。boot、journal 与 writer lease 仍是同一对象；不调用
`BootPermit.acquire`，不重新取得 lease，也不打开第二个业务 Store。

返回值强引用原 `ManagedBootPermit`。路径核验和 `close_after_writers()` 都委托原 owner，
保留它的实际 DB inode fd 和 sticky cleanup failure。宿主必须先完成所有业务 writer
的关闭，再请求 owner 关闭；Store 关闭本身不释放 inode anchor 或 lease。关闭失败不能
用适配对象直接释放 raw lock。journal 描述符仍由外层 `BootSession` 持有。

适配对象在持久桥写入前构造，但只有 bridge 成功后才赋予新 V1 record 并返回。
成功返回后没有新的 I/O、校验、锁转移或 await。宿主应在下一次 await 前同步更新
Maintenance 和 BootSession 的 permit、发布已经耐久的 manifest；Runtime 的 verifier
也应在调用时取得当前逻辑 permit。普通失败或取消不会修改、关闭原 owner。

宿主以不可变 `PendingLegacy` 保留本次已经核验的 manifest、上一代 V2 record、
request/action、本次 base/target 字节、deadline 和预分配 generation，不增加第二份持久权威。
若 state 已发布而返回丢失，必须退出旧的、可能已 poison 的 journal 控制锁，再在
新锁下调用 `confirm_legacy_permit`。原 poison 不会清除。确认先执行已有的目录同步
屏障，再复核同一次授权、同一实际 Store 的旧精确回执、历史快照、安装清单和当前
base/unit/selector 文件；持续 fsync 错误或任一证据改变仍拒绝绑定。
即使同 generation、target 的 base 快照、state 摘要和实际配置被一致改写，仍须与
宿主原来冻结的本次 base 相同；不能借确认过程把新的配置解释为旧授权。

确认只接受原实例仍持 lease 的 prepared/draining/aborted，以及精确的
activating/stop_base、rolling_back/stop_target 身份观察窗口。它不把 committed、
rolled_back 或新实例健康证明认成旧 owner，不重新 prepare、不写 state、不换代。
确认成功后宿主同步绑定 V1 视图，仍传播原始异常，让管理计划保留 applying 及本代
generation；之后显式 recover 可以继续本代。实际 Store、DB fd 和 lease 始终由同一
managed owner 清理。这一模块不直接启动 helper、服务或控制者。

离线回归使用实际 managed permit、临时 marked Store、DB inode anchor 与 flock，
覆盖 aborted/rolled_back 后 configure/restart 的 V1 origin drain/readiness、完整
manifest 身份投影与原 routing、已投影后热配置继续事务、桥失败/取消、路径替换和
sticky close 拒绝。新 9 项与 bridge、managed boot、维护门合计 106 项通过
（7.38 秒、峰值 116.7 MiB），两份源码 mypy、四份 Python 文件 Ruff/格式检查通过；
最高峰值 124.8 MiB，swap 为 0，均经过统一资源守卫串行运行。
真实 `FrozenRouting` 序列化字段由投影单例额外复验通过。
没有操作真实 systemd 或用户数据库。

丢失返回确认片的 adapter/bridge 定向组为 53 项通过（13.49 秒、峰值 97.9 MiB），
覆盖原 poison 控制锁不可复用、可见 state 持续目录 fsync EIO 时拒绝、fresh 屏障
成功后同代续接、合法原实例阶段推进、不可变授权变化、文件/旧投影变化，以及
restart 配置未变也不能把 committed target 绑定成旧 owner。两份源码 mypy、四份
Python 文件 Ruff 通过，静态最高峰值 124.8 MiB、swap 0；均使用同一受限入口。
随后冻结本次 base 的尾差单独复验 adapter 29 项通过（10.40 秒、峰值 82.2 MiB），
再补准备授权后配置变化的 1 项通过（0.45 秒、峰值 63.8 MiB）；两份源码 mypy
和相关 Ruff 通过，峰值 124.6 MiB、swap 0。这两轮没有重复统计为一次完整组合。
上述数字不包含完整 RecoveryHost 集成测试。

宿主整合另以根工作树源码执行 5 项回归，通过显式 pytest 配置、rootdir 与源码路径
断言确认实际测试目标。实际 `ManagedBootPermit` 回退到 legacy 后构造完整 Runtime，
经公开 stage、真实 manifest/bridge/Store 与 fake helper 覆盖 configure/restart、
helper 观察失败/取消，以及真实 state 写入成功后丢失返回的同进程确认；保留同代
applying、原 routing 和原 writer owner，Store 关闭前不会释放 DB fd 或 lease。
临时目录的文件系统拓扑观察及服务进程观察由替身提供，文件身份、快照、准入、
排他锁与清理仍使用真实实现；没有真实 systemd 操作。
这 5 项为 2.10 秒、峰值 79.2 MiB、swap 0，单文件 Ruff/格式检查通过，均经资源
守卫串行运行，未与前述独立回归轮次合计为一次整合测试。
