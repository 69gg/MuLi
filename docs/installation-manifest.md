# Linux 安装 manifest

状态：同版本恢复的独立文件身份模块；尚未接通 CLI／管理提交、bootstrap、真实控制者与独立 helper。模块和回归不等于已交付可恢复服务，也不改变现有 `self_restart` 行为。

`muli.platforms.installation.InstallationManifest` 复用现有 `UserService`、journal 私有目录、严格 JSON 解析和耐久写入。manifest 不含配置正文、数据库正文、环境变量、口令或 nonce；它只能供受信任的宿主内部构造，不能由模型工具参数选择包目录或解释器。

## 文件与身份

`capture(service, database, *, helper, journal, package_limits)` 记录规范绝对 config/data/database/unit 路径，以及当前主机实际解释器和当前已导入的 MuLi 包。可选预算省略时采用严格模型默认值。

解释器保留 `executable` 词法入口作为 service/helper 启动入口，另存 `real_executable` 与真实普通可执行文件的 dev/inode/size/mtime/ctime/mode/uid 身份。最终解释器软链按有限链逐段核验；普通路径的父目录不跟随链接。另绑定当前 `sys.prefix`、`sys.base_prefix` 和 `pyvenv.cfg` 的有界内容摘要。另一个虚拟环境即使共享同一基础 Python，也不能作为当前环境被 capture。真实二进制这里只承诺文件身份，未逐字节散列整个 Python 安装。

包根从当前 `muli.__file__` 的规范位置取得。`verify()` 重新计算这个位置，拒绝 manifest 指向任意替代包根；按相对文件名、文件大小与 SHA-256 内容摘要计算 MuLi 包指纹。目录项、总字节、单文件和深度分别受 `PackageLimits` 限制；先限目录项再排序。拒绝链接、FIFO 和其他非普通载荷，并核对读取前后文件身份及目录路径。`__pycache__` 不进入遍历，`.pyc`／`.pyo` 不计入代码指纹；它们被扫描到的入口仍占遍历预算。不会扫描虚拟环境其余目录，也不声称核验了完整依赖环境。

config/database 只核路径、普通文件与当前用户归属，不读取正文；数据库 inode 和内容不属于安装身份，Store instance 由后续握手另证。`verify(require_database=False)` 与 capture 同名参数只允许最终数据库文件缺失，不允许缺失父目录、软链或特殊文件。默认仍为 `True`。宿主只能在 journal 从未存在事务的首次 bootstrap 显式使用 `False`；Store 建立后、prepare 与所有事务重入必须使用默认严格路径。该启动阶段参数不进入摘要。

`digest` 为整个 manifest 的 canonical JSON 摘要。`service_digest` 单独绑定标准 unit 文本与 argv，并持久保存；读取摘要不重新 resolve 可能已变化的路径。`user_service()` 以 manifest 的路径和词法解释器重建原 `UserService`。`verify()` 会重读实际标准 unit 并复算 service 摘要；它不访问 systemd 或启动任何组件。运行中的实际 argv、InvocationID、cgroup 与 readiness 仍由只读 probe 和控制者核验。

## 有界预算

`HelperLimits` 是本代持久预算的唯一模型；后续配置直接引用，不再定义同义字段。

| 字段 | 默认 | 有效范围／语义 |
| --- | --- | --- |
| `seconds` | 180 秒 | 大于 0，至多 900；只用于准备本代 deadline |
| `rollback_reserve` | 60 秒 | 大于 0，小于 `seconds` |
| `operation_timeout` | 20 秒 | 大于 0，至多 30；完整 controller action 总预算 |
| `poll_interval` | 0.25 秒 | 大于 0，至多 2 |
| `memory_bytes` | 128 MiB | 64–256 MiB；不包含 swap 配额 |
| `cpu_percent` | 25 | 1–100 |
| `tasks` | 32 | 8–64 |
| `stop_seconds` | 2 秒 | 大于 0，至多 5 |
| `restart_burst` | 3 | 1–3；初次启动也计数 |
| `restart_delay` | 0.5 秒 | 大于 0，至多 5 |

浮点预算须有限，所有字段严格拒绝字符串、布尔替代值和额外键。probe／HTTP 子预算使用 action 总预算的四分之一，沿用现有 `ProbeLimits` 字节上限；这些执行接线由控制者片完成。本模块仅保存预算，不执行资源限制。helper 重入不能续期；实际执行总期限永远取持久 `record.deadline`，更早期限优先。StartLimitInterval 使用本代 `seconds`。

`PackageLimits` 默认最多 512 个入口、8 MiB 总正文、4 MiB 单文件、16 层；各上限分别为 2048、32 MiB、8 MiB、32 层。manifest 嵌套现有 `JournalLimits`，自身还受其 `max_record_bytes` 限制。加载入口有 128 KiB 绝对读取上限，不先信任文件内预算。

## 发布与崩溃重入

`load(root)` 只打开已有 0700 私有目录与 0600 普通 `installation.json`，拒绝链接、非普通文件、重复 JSON 键、NaN、未知字段和非法路径；不创建任何目录。load 是结构读取，不构成真实安装验证或耐久确认。

`manifest.publish(control, projected=...)` 要求调用方持同一个 `JournalControl` 锁，数据库路径和 journal 预算一致，并重新 `verify()`。实际写入复用 control 的 poisoned 原子写入口：同目录私有临时文件、文件 fsync、原子 replace、父目录 fsync。只有成功返回才能调用 `control.prepare()`；文件或父目录同步失败会 poison 此 control，不能继续准备。相同内容重入也重新完成文件和目录耐久确认。

旧 record 未解决或缺少精确投影回执时，不得改换 manifest。相同 manifest 在未解决旧代下重入，也必须与旧 record 的 installation/service 身份一致。旧代已解决且宿主提供真实 Store 投影所得的精确 generation/revision/store receipt 后，可以发布下一安装身份。若新 manifest 已耐久、下一代 prepared 尚未落盘就退出，重入会看到新 manifest 与旧 record 身份不同；同一 resolved 状态和精确回执允许重新耐久发布，不能误判成永久 foreign。回执由宿主查证，本模块不信任模型声称已投影。

安装、依赖和解释器在同版本事务期间不得并发变更。本模块使用 fd 锚定和前后核验，但不承诺排斥恶意同 uid 程序或管理员的并发操作；跨版本安装切换仍属后续能力。配置替换后的耐久采用由 `ConfigurationFile.confirm` 负责，不能拿 manifest fsync 代替 stopped、空 cgroup、独占 writer lease 下的原配置文件与父目录同步屏障。

接口通过 Context7 核对 [Python venv](https://docs.python.org/3/library/venv.html)、[sys 路径初始化](https://docs.python.org/3/library/sys_path_init.html) 与 [os 文件描述符接口](https://docs.python.org/3/library/os.html)。验证仅使用临时目录、假解释器文件与故障注入，不执行这些解释器、不操作真实用户服务。

受限验证已通过新增 54 项 manifest 回归，以及既有 journal、service、配置文件入口回归，合计 116 项，0.32 秒，峰值 59.4 MiB、swap 0；三份 Python 的 Ruff／格式和两份源码 mypy 通过，静态检查峰值 118.2 MiB。回归包括同基础 Python 的另一虚拟环境、原始配置／DB 内容不入身份、字节缓存排除、四类包预算、缺库首次 bootstrap 的显式边界、链接／FIFO、读取期间父目录置换、strict JSON、未解决／未投影拒换、successor 落盘后 prepare 前退出重入，以及文件／父目录 fsync 失败对同内容采用和首次发布均 poison。未验证真实用户服务、发行包或完整恢复闭环。
