# Windows Skills 句柄读取

状态：独立候选，设计与实现已通过独立静态审查；本地 Linux 回归已完成，Windows 托管 CI 验收未执行。版本由整合方决定，Windows 验收通过前不作为平台完成发布。

首片只支持普通绝对盘符路径上的本地 NTFS。UNC、设备命名空间、盘符相对路径、ADS、SUBST 子目录、重解析点和其它文件系统明确不可用，不回退到普通全路径读取。空目录、目录不存在或关闭 Skills 不影响核心聊天。Linux 原有 `dir_fd` / `O_NOFOLLOW` 链保持原样。

`CreateFileW` 不共享 `DELETE` 能限制重命名，却不足以独立证明祖先稳定：属性访问不受共享模式约束，设置重解析点也可使用 `FILE_WRITE_ATTRIBUTES`。因此选择单一、按需加载的 `ctypes` 适配层，不建立通用文件系统或 FFI 框架。[共享模式](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)、[重解析点权限](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-fsa/4aeefef8-92c3-4abc-af7a-a610caf8a165)。

## 读取合同

1. 不先 `resolve()` 配置目录。只用 `CreateFileW` 打开 `X:\` 锚点，指定 `OPEN_EXISTING`、`BACKUP_SEMANTICS`、`OPEN_REPARSE_POINT`；从句柄核对 disk、目录、非 reparse、本地 NTFS，并要求 `GetFinalPathNameByHandleW(VOLUME_NAME_GUID)` **精确为卷根**，不能只匹配前缀。官方说明网络共享没有 Volume GUID，句柄卷信息也不支持 SMB；两项均在同一已打开句柄上成功才准入，不声称证明物理介质类型。SUBST 子目录和网络映射不进入后续读取。盘符只是首次观察入口；持句柄后不再解析盘符别名。[卷根名称](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getfinalpathnamebyhandlew)、[卷信息](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getvolumeinformationbyhandlew)。
2. 配置目录各段、Skill 目录、`SKILL.md`、资源及最后复核，全部使用 `NtCreateFile(RootDirectory=parent, ObjectName=单组件)`。仅打开已有对象，设置 `FILE_OPEN_REPARSE_POINT` 与 `OBJ_DONT_REPARSE`；每次从已打开句柄验证类型和非 reparse，不再从完整路径重走祖先。逐段拒绝分隔符、冒号、`.`、`..`、尾点/空格、控制字符和 Win32 保留名称。目录允许正常读写共享但不共享删除；正确性依据是相对句柄与不跟随重解析点，而非禁止所有并发编辑。[NtCreateFile](https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntcreatefile)、[OBJECT_ATTRIBUTES](https://learn.microsoft.com/en-us/windows/win32/api/ntdef/ns-ntdef-_object_attributes)。
3. 扫描从根句柄分批枚举，固定 32 KiB 缓冲，沿既有 `max_entries` / `max_skills` 限额。每个枚举记录的偏移、长度、对齐和 UTF-16 都先有界核验；原始记录及页数也计费，只额外容纳系统 `.` / `..` 两项，忽略文件或重复点条目不能绕过预算。名称之后再相对打开核对，不信任枚举缓存身份。源和资源只从已经核验的普通文件句柄分块读取，至多现有字节预算加一哨兵，前后与末次相对重开身份必须相等。[句柄目录枚举结构](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_id_both_dir_info)。
4. 扫描与读取统一使用原生 volume/file ID、属性、大小、LastWriteTime、ChangeTime。目录身份只包含稳定对象身份与类型；不用 Python `path.stat()` / `fstat()` 的 `ctime` 跨 API 等值，避免当前 CPython Windows 差异。FFI 显式声明参数、返回值和自然对齐，`UNICODE_STRING.Length` 按 UTF-16 **字节**计；所有句柄由同步操作 `finally` 关闭，不继承、不启用特权。[身份](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_id_info)、[时间](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_basic_info)、[ctypes](https://docs.python.org/3/library/ctypes.html)。

复用现有 `file_io` 的取消等待、Skills 代次复核、失败保旧、YAML/目录/正文/模型总预算、团队会话注入和完整 JSON。原生 API 不可用或不能证明类型时拒绝此来源，不让坏源关闭核心助手。不增加设置、依赖、线程池、执行脚本或权限提升。它不提供恶意同用户程序、硬链接来源或任意过滤驱动的沙箱，也不承诺文件内容的事务快照。

## 必需验收

Windows 3.12/3.13 托管 CI 在真实临时 NTFS 目录执行：普通 Skills 准备/激活/资源分页与更新；预置 junction；打开下一组件前把该组件替成 junction；持父句柄后尝试原位设置重解析点；终文件更新/替换；限额、损坏枚举和失败释放。外部目标读取必须为零；内核拒绝置换时也必须核对确实拒绝且旧内容保持。junction 由临时目录句柄和 `DeviceIoControl` 构造，不要求管理员或符号链接特权，不调用 shell、服务或用户目录。

Linux 仅运行纯名称/枚举解析/错误映射与原有 POSIX 回归，不能把模拟 `ctypes` 或跳过 Windows 用例称为平台完成。Windows API 缺失不能把目标验收静默 skip。现有真实 POSIX 父目录替换用例保留；Windows 使用符合其句柄共享规则的独立门控。

Windows reviewer 已核对卷根形状和相对打开方案无设计阻断，要求扫描、末次复核同样不回到全路径；这不是对尚未执行的 Windows 行为的验收。

## 本地验证

全部检查经统一 `check_local.py --seconds 30` 串行执行，单次 1 GiB、共享 2 GiB、单 CPU、swap 0。`test_windows_skill_files.py` 的跨平台布局、名称、枚举损坏/预算、原生调用参数与错误清理，连同既有 `test_skills.py` 共 **88 项通过、6 项 Windows 真实句柄用例跳过**（0.52 秒，峰 79.2 MiB）。既有 `test_skills_live.py`、`test_extensions.py`、`test_extensions_watcher.py` 另 **78 项通过**（2.54 秒，峰 127.7 MiB），与前组不重叠。两份产品源码 mypy 通过，成功检查峰 143.1 MiB，末次增量 61.4 MiB；首轮类型检查发现 Linux ctypes stub 的平台属性声明和分支变量重名，已修正，该轮峰 170.3 MiB，不涉及放宽路径或读取合同。四份 Python 的 Ruff 与格式检查通过。

现有 POSIX 父目录替换门控保持；两个依赖 POSIX `dir_fd` / symlink 特权的测试仅在 Windows 交由新增真实 junction 门控覆盖。Windows 正常 Skills 宿主用例会随原生 API 可用实际运行，不把它们继续全部跳过。上述 Linux 结果不证明 Win32 ABI、NTFS 行为或 Windows 3.12/3.13 已通过；新增 Windows 专属 fixture 在该平台要求原生 API 和 junction 构造真实成功，不以缺能力为由 skip。首轮远端 CI 若失败，须按精确日志修复后再记录平台结论。

Windows reviewer 和整合方均已只读审查原生读取链与真实门控用例，未发现剩余 P1/P2；没有将静态结论替代 Windows 运行结果。
