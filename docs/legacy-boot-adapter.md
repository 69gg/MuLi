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

若桥写入失败但留下可见文件，仍由原账本的同步屏障和恢复流程判断持久状态，不能
仅凭可见 V1 JSON 就宣称桥接成功。这一模块不直接启动 helper、服务或控制者，也不
单独宣称管理入口已经回桥。

离线回归使用实际 managed permit、临时 marked Store、DB inode anchor 与 flock，
覆盖 aborted/rolled_back 后 configure/restart 的 V1 origin drain/readiness、完整
manifest 身份投影与原 routing、已投影后热配置继续事务、桥失败/取消、路径替换和
sticky close 拒绝。新 9 项与 bridge、managed boot、维护门合计 106 项通过
（7.38 秒、峰值 116.7 MiB），两份源码 mypy、四份 Python 文件 Ruff/格式检查通过；
最高峰值 124.8 MiB，swap 为 0，均经过统一资源守卫串行运行。
真实 `FrozenRouting` 序列化字段由投影单例额外复验通过。
没有操作真实 systemd 或用户数据库。
