# Linux 同版本恢复：接线合同

状态：第三片设计，尚未交付实际服务交接。前两片及只读 systemd 探测已完成独立审查；根分支整合后 99 项维护、管理、服务器及记忆路由用例通过，峰值 198.9 MiB、swap 0。

## 路径与安装身份

独立 helper 的 argv 只带私有 journal 目录。该目录内持久 `installation.json`，不放配置正文、口令或 nonce。它记录规范绝对 config/data/database/unit/executable/package 路径、MuLi 包指纹和固定于准备时的控制者预算。以 canonical JSON 摘要作为 installation identity；service identity 另绑定标准 unit 文本与 argv。路径须从根逐段不跟随符号链接地打开，不能从默认目录反推自定义配置路径。

manifest 在 journal 锁内发布；旧代未解决或终态未投影时不得替换。相同内容可重入。控制者和 boot guard 必须重新计算实际安装及 unit 身份，不将 manifest 自报内容当作验证。保留 unit 实际调用的解释器词法入口和解析后的可执行文件身份两项，helper 使用前者以保留虚拟环境；另核对其 pyvenv 身份。包指纹有文件数和总字节预算，排除 Python 字节缓存；不扫描整个虚拟环境，也不代表完整依赖环境已被核验。解释器、依赖环境和包安装在本次同版本事务期间不可并发变更；跨版本环境切换属于后续 A/B 安装切片。

实际服务 boot 使用已核对进程的 systemd InvocationID。原进程 readiness 只证明同代原 boot；新的 target/base 仍要求各自 challenge、配置、Store/main 身份和新 boot。任何 URL 重定向、代理、未知 bind 地址或身份不符均不能接受。

## 配置文件比较与替换

抽取现有 `maintenance.configuration_digest` 为一个可复用的 Linux 文件入口，保留该函数的错误语义。只读与替换均从 `/` 逐段打开父目录，拒绝链接、FIFO、非本用户普通文件和读取中身份变化。配置父目录不强求 0700，不自动 chmod 或创建目录；journal 自身仍要求 0700。

替换要求调用方已持 journal 锁、空 cgroup 和独占 writer lease，并通过同步回调在最终 replace 前再核验。读取原文件的精确字节和身份，写入同目录的 0600 独占临时文件并 fsync，重读原文件核 raw digest 与 inode/mtime/ctime，核父目录路径仍指向原 fd，才原子 replace、父目录 fsync。任何失败不得记录安装成功；replace 后 fsync 失败也必须让上层继续按未确认状态处理。不会覆盖不属于本事务的人工配置。

这是合作进程的 CAS 与崩溃恢复协议，不承诺排斥忽略锁的管理员或恶意同 uid 程序。检查与 rename 之间没有内核级内容 CAS；文档和测试不得把它描述成这样的隔离。

控制者重入时，即使读取到目标摘要，也要在旧服务已停止、cgroup 空与独占 lease 下调用 `ConfigurationFile.confirm`，完成配置文件及父目录 fsync 和前后身份重核，才向状态机提供可以启动的事实。plain read、上一次进程内的成功标记、journal 自身的目录同步均不能代替该屏障。持续 fsync 失败必须阻止下一次 start。

## 任务分工与验证

安装身份作者负责独立 [manifest 模块](installation-manifest.md)及其回归；根任务负责配置文件入口、bootstrap/管理投影接线；服务作者负责 `ControllerBackend`、nonce HTTP、独立 helper 与有界 manager 操作；独立审查者审合同、失败窗口与最终差异。所有新路径先使用注入 runner/HTTP transport、临时目录和假 cgroup；不启动用户真实服务。

初始支持面限标准同用户 systemd unit、可核验 cgroup v2 与本地持久文件系统。unsupported 保留原服务。机器重启后恢复依赖已启用的服务或显式恢复入口；不会自动启用 linger。最终只有包含 bootstrap、管理入口、独立 helper、真实 writer lease 和终态投影的闭环全部通过，才宣传同版本可恢复重启。

配置文件入口已实现，50 项定向用例通过（其中 19 项新增），0.94 秒、峰值 112 MiB、swap 0；三个文件 Ruff/格式、两份源码 mypy 通过。首次测试的 11 个失败全部来自测试将全局 fixture 预建的 `muli-home` 误判为残留；测试改用专有目录后通过，未放宽产品检查。独立复审指出重开 handle 不能继承内存中的未确认状态，随后增加明确 confirm 屏障及持续目录同步失败的反证。真实 backend 必须调用此入口，单模块通过仍不表示服务恢复闭环已交付。
