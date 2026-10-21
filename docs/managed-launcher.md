# Linux 本地候选的稳定入口与选择器

本模块提供已安装目录的身份、私有选择器、真实 `exec` 入口和只读修复诊断。
管理宿主已连接同一账本上的 V2 控制者、被动启动、格式准入、健康证明、激活和
最终投影；公开 `self_adopt` / `self_switch` 的整合状态见[本地托管切换](managed-operations.md)。
下文保留本模块的验证记录，单项通过不能代替完整服务切换证据。

首片只接受显式指定、最终保留的 Linux venv。没有 uv 下载、目录复制安装、
环境变量/凭据发现或依赖解析；普通安装器仍不因此获得修改 marked 库的许可。
没有 `detach` 命令，修复也不隐式退出托管。

## 身份与单一权威

`muli.platforms.managed_deployment` 导出以下接口：

| 接口 | 作用与边界 |
| --- | --- |
| `register_local_slot(prefix, python, package_root, *, limits, python_bytes)` | 有界读取明确路径，不执行候选。 |
| `SlotIdentity` | 绑定词法 Python、真实二进制指纹、预期 prefix、`pyvenv.cfg`、MuLi 包与实际声明。 |
| `verify_slot_files(slot)` | 重新核实静态文件；不证明当前运行的解释器或导入。 |
| `verify_current_slot(slot)` | 在静态复核外核当前 `sys.executable`、`sys.prefix`、venv 与实际导入包。 |
| `DeploymentAnchor` | 冻结 config/data/database/unit/journal 的路径、保留的 controller slot、launcher、unit 原始字节引用和预算。 |
| `DeploymentStore(root)` | 在已存在私有根内发布/读取按摘要寻址的 slot 与 anchor；不另建锁或账本。 |
| `SelectorStore(root)` | 固定 `selector.json` 的读取、精确 CAS 和耐久确认。 |
| `resolve_entry(root, *, purpose, arguments)` | 路由证据；不是 writer lease、BootPermit 或执行授权。 |

slot 内的 `compatibility.json` 必须实际随包交付，并包含 protocol 2、无需迁移的声明。
缺声明、外置声明、包不在预期 prefix 内或支持格式不明时拒绝登记，不能从当前数据库
的格式 ID 自动制造候选支持声明。两侧能否共享现有七个格式域仍由宿主获得真实
`FormatState` 后使用 `assess_compatibility` 判定。

slot 摘要不含 unit、配置、selector 或当前 boot。`declaration.digest` 是原始声明文件摘要；
`slot.compatibility_digest` 复用 `muli.managed_transaction.compatibility_digest`，是规范化
声明的语义摘要，两者不可混用。原文件空白变化会改变 slot 身份，而不改变兼容语义。

解释器哈希按 64 KiB 分块读取，`python_bytes` 独立于包预算，默认 32 MiB、最大 64 MiB；
不能因为正常 Python 可执行文件大于包的单文件/总字节限制而放宽包预算。
包仍复用 `PackageLimits` 的目录项、深度、单文件和总字节约束，不读 `.pyc` 或
`__pycache__`。这些指纹不认证所有依赖，也不是执行恶意代码的沙箱。

私有目录须为当前用户的 `0700` 目录；元数据文件为 `0600` 普通文件。
读取逐段禁止目录软链接，最终文件使用 `O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC`，
并在读取前后复核身份。模块在非 Linux 平台可导入，实际文件操作明确 unsupported。

文件布局：

```text
<同一个 journal root>/
  state.json                       # V1/V2 的唯一事务权威，由宿主严格分派
  slot-<slot digest>.json           # 不可变静态安装身份
  anchor-<deployment digest>.json   # 不可变固定入口身份
  launcher.py                      # 随包交付代码的独立 stdlib 副本
  selector.json                    # 下一次启动必须采用的明确安装
```

未被 `state.json` 引用的 slot/anchor 只是准备材料，不表示已经迁入托管。
`DeploymentStore` 默认沿用 `JournalLimits.max_entries`，未知文件也占目录项预算；
它不自行删除旧证据。当前实现不把 selector 缺失、坏 anchor 或状态缺失当成 legacy。

selector 的唯一格式是：

```json
{"schema_version":1,"deployment":"<64hex>","installation":"<64hex>"}
```

它不另存 confirmed、boot 或 mode。确认来自同代 journal 的 released 终态。
selector 的精确 JSON 引用保存在 V2 role binding，unit 则使用原始字节
`RawSnapshotReference`；systemd unit 不是 JSON。

## CAS 与崩溃重入

`SelectorStore.read()` 返回包含实际字节和文件身份的 `SelectorSnapshot | None`。
`replace(expected, desired, guard=...)` 不覆盖与 expected 不符的文件。
`None` 仅供迁入时明确的 legacy 一侧；普通 A/B 切换两侧都必须有 selector。

guard 由宿主同步核验：同一 controller 锁、实际 writer lease、stopped unit、空 cgroup
和当前持久 deadline。它不是一个可由调用者自报的许可。检查后不能插入异步工作。
初建在临时文件 fsync 后再次核 guard，已有文件替换复用 `ConfigurationFile`，
删除也在最后一次 guard 后才执行。文件 fsync、rename/unlink 和父目录 fsync 的阶段
分别可失败，不声称多个文件原子切换。

rename 或 unlink 已可见而父目录 fsync 失败时，当前 handle 被标记为未确认，后续不能
继续使用它。新 handle 读到期望字节也不代表已耐久：必须先成功
`confirm(expected, guard=...)`，包括明确缺席时对父目录的确认；持续 I/O 错误不能启动服务。
静态 `read()` 始终只是观察，不是耐久收据。

现有控制者按冻结的精确快照执行 config → selector → unit，unit 改变后
daemon-reload 并重新核 loaded 属性，全部 confirm 后才能 start；回退按反序。
这些效果由宿主的 managed backend 执行，本模块不调用 systemctl。即使 A/B 的配置字节相同，
仍必须核对并确认 selector，不能跳过安装切换。

## 固定入口与恢复

unit 的固定 argv 合同为：

```text
<controller lexical python> -I <journal root>/launcher.py --root <journal root> service
```

launcher 只用标准库，不导入任一 MuLi 安装；它核固定 anchor/controller 与允许 role，
然后 `os.execve` 到：

```text
<role lexical python> -I -m muli --config <config> --data-dir <data> --managed-root <root> serve
```

`exec` 保留 PID。V2 probe 分别核 unit 的 launcher argv 与同 MainPID/start_ticks/
InvocationID/cgroup 下的实际 slot argv；两个 venv 共享 real executable 并不表示同安装。
现有 `SystemdProbe.read` 的 V1 约束保持不变。子进程的 bootstrap 在 ConfigFile/Store 前
重读当前代、获得 writer lease，并调用当前 slot、格式及角色准入核验。

`cli` 仅在 V2 已 resolved 时按终态 role 委托；任何未解决代均返回
`transaction_in_progress`，不能在 committed 但尚未激活时把普通 CLI 委托给 base。
CLI 参数不能覆盖 anchor 的 config/data/root；保留长选项及其 argparse 有效缩写
（含 `--option=value` 形式）均拒绝，不能借后置参数覆盖固定路径。

独立 `recover` 不读取 selector 来选控制程序，也不接受普通参数：

```text
<controller lexical python> -I -m muli.managed_host --root <journal root> recover
```

它先核固定 anchor 和保留 controller 的所有静态指纹，再转入宿主控制入口。
该宿主入口已接入固定 controller；同名 helper、预算、持久期限与一次回退规则不由 launcher 重置。

`--root <root> repair` 在选定包的核验/导入之前提供有界只读诊断。
`muli.platforms.managed_repair.inspect_repair` 导出同一观察 API。它不读取配置正文、
数据库或候选代码，不修 selector、不重新捕获安装、不制造新事务。
anchor 变化明确报告 `anchor_unavailable`；保留的 Python 自身无法运行时不承诺软件自愈。

## 本片验证

已通过受限 `tests/test_managed_launcher.py` 的 37 项回归，含两个临时 venv
共用同一个真实二进制时的词法入口区分、实际同 PID exec、坏 selector 的固定 controller
恢复、坏候选不导入的 repair、fsync 失败后不能当作成功收据，以及初建临时文件
fsync 消耗最后期限后不发布 selector，以及真实 argparse 接受路径缩写覆盖时 launcher
仍明确拒绝。最终用例 6.01 秒，三源码严格 mypy 与四文件 Ruff/format 通过，
尾轮最高 70.5 MiB、swap 0，未提高开发资源限制。
没有运行真实 systemd、外部模型、uv 安装或用户数据库。

原有设计参见 [可恢复管理](recoverable-management-design.md)、
[无迁移启动](no-migration-startup-design.md) 与 [兼容声明](upgrade-compatibility.md)。
Python 的 [exec 语义](https://docs.python.org/3/library/os.html#os.execve) 和
[`-I` 隔离模式](https://docs.python.org/3/using/cmdline.html#cmdoption-I) 只约束进程/导入路径，
不能替代上述宿主身份与数据库准入。
