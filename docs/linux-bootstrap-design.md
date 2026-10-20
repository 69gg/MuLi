# Linux 启动前 writer 保护

状态：启动保护模块已接入开发分支的 `serve`、Runtime 和 lifespan，并完成独立入口回归。模块自身不启动 helper、不准备或发布事务；整体整合与真实服务验证边界见[恢复入口](linux-recovery-operations.md)。

`open_boot_session(service, database, *, probe=None)` 在 ConfigFile 和 Store 构造之前运行。宿主从实际 CLI 路径构造 `UserService` 并传入实际数据库路径；模块不读取 Settings 来猜路径。journal 固定为 `service.data_dir / management / transaction-v1`，数据库别名沿用 `TransactionJournal` 的规范化结果，所有 Linux serve 模式使用同一数据库 writer lease。

返回的 `BootSession` 拥有 journal 目录描述符、真实 writer lease、启动时的 config 词法绝对路径，以及可选的 `permit`、`manifest`、固定 `unsupported_reason`。`check_paths(config_path, database)` 在 Runtime 构造前复核租约仍被持有、config 词法路径相同及实际数据库路径相同，不创建 Store。probe 等待期间传入 service 的路径或解释器发生变化会拒绝。宿主必须在全部数据库写入者结束后显式调用幂等 `close_after_writers()`；本模块不提供容易提前释放租约的上下文管理入口。启动失败或外部取消会清理本次取得的描述符和租约。`BootPermit.acquire(..., lease=...)` 接管已经获取的真实租约，核对固定 `lease` 名称、writer 目录身份与实际锁 inode，沿用原有 phase、配置摘要与 Store/main 身份检查，失败也释放所接管租约；不会释放后重取或复制一套 phase 判断。

## 获得事务能力的条件

标准路径只在 `SystemdProbe.read(service)` 证明 unit 与当前进程相符时成立：MainPID、实际进程 PID 均等于 `os.getpid()`，InvocationID 是非零的 32 位十六进制值，unit 正在运行或启动，进程身份、argv、标准 unit 文件等证据仍由既有 probe 核验。该 InvocationID 是本次服务 boot 身份，不能用环境变量、随机数或内核 boot_id 代替。probe 可注入用于离线验证，默认读取仍遵守有界总超时；取消不降级为 unsupported。

随后只对 config parent、数据库 parent、journal 目录、writer 目录调用既有 `verify_local_filesystems`。它不扫描包或数据库，不为普通 foreground 运行附加本地持久文件系统要求。通过后才捕获或验证实际当前解释器词法入口、真实可执行文件、pyvenv、当前导入的 MuLi 包及标准 unit；不接受 manifest 任意指定的包目录，也不把整个依赖环境宣称为已验证。

现存 `installation.json` 在构造 journal 前严格加载，以其已发布的 `JournalLimits` 读取 record，并沿用该代 helper/package 预算。损坏或不可读的现存清单是明确启动错误，不按清单不存在处理。bootstrap 没有预算修改入口；新的预算由后续 prepare 在锁内捕获、耐久发布，再更新当前 permit。

## record 分支

| journal 状态 | 标准当前服务 | foreground、非标准 unit 或能力不支持 |
| --- | --- | --- |
| 没有 record | 捕获真实当前 manifest；只有此分支允许数据库尚未存在，返回普通 permit | 返回无 permit/manifest 的 session，保持 writer lease，原有 serve 可以继续 |
| 未决 record | 必须存在已发布 manifest，严格验证其安装与 config/data/database/unit 路径，再由现有 BootPermit 规则判定 target/base；禁止捕获替代身份 | 在读取配置或打开 Store 前拒绝启动；不能让 unsupported 降级绕过未决事务 |
| resolved record | 可捕获真实当前 manifest 候选，返回普通 permit；不覆盖旧 manifest/record，不声称已投影 | 可保持 writer lease 正常 serve，不伪造安装身份，保留旧证据 |

只要 journal 有任何 record，数据库缺失就拒绝，包含 resolved record；不能让下一步 Store 自动新建空库替换旧身份。首次数据库缺失的例外不放宽链接、特殊文件、父路径错误或已存在数据库错误。

resolved 的旧安装与当前安装不同可以正常 serve，但后续终态投影仍须执行自己的身份与 exact receipt 校验。没有旧代 exact receipt 时不得覆盖旧 journal/manifest 或 prepare 新代；已有 exact receipt 时允许“新 manifest 已耐久、prepared 尚未写”的合法重入。bootstrap 不打开 Store 来判断投影状态，也不抹掉或补造管理收据。

## 宿主后续接线

宿主在所有 `serve` 路径最先取得 session，再读取配置、打开数据库；非 Linux 平台保留既有入口，不调用本模块。Store 连接的 WAL/FULL 耐久策略、工具回执与 plan generation 持久化、`project_final`、bus 通知、helper 创建与恢复 readiness 都属于后续接线。宿主不能因为没有 permit 就跳过 session 的租约持有与最终关闭，也不能在写入者尚存时借调用 `permit.close_after_writers()` 提前解锁。

验证只使用临时目录、真实文件租约、注入 probe 和文件系统 verifier，不调用真实账号、服务控制或用户数据库。新增回归覆盖无配置读取的持锁顺序、不同 root/数据库别名仍共享同库租约、不同库可独立运行、取消和失败清理、foreground 与 unsupported 的未决拒绝、初次数据库缺失、已发布预算与路径约束、target/base phase 复用、probe 等待期间的输入或 record 变化，以及 resolved 旧证据保留。伪造已发布预算无法替换 record 绑定的安装摘要，未持有、错误名称或其它数据库的租约也不能被 BootPermit 接管。

受统一 `check_local.py` 保护，bootstrap、既有 maintenance 与 management projection 三组共 106 项通过（0.55 秒，service 1.029 秒、峰值 75 MiB）；三份 Python 文件 Ruff/格式通过，两份源码 mypy 通过（峰值 118.7 MiB），swap 0。独立只读主链审查无阻断。未运行真实 systemd 服务或恢复接管，不用这些离线结果替代宿主接线与后续整合验证。
