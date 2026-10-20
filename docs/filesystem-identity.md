# Linux 恢复事务的文件系统支持面

`platforms/filesystem_identity.py` 提供只读的
`verify_local_filesystems(directories, *, limits=None, proc_root=Path('/proc/self'))`。
调用方明确传入已存在的配置父目录、数据库父目录、journal 和 writer 目录；本模块不会创建、同步、修改文件或挂载，也不扫描数据库、安装包或目录内容。
这是恢复事务的能力检查；普通前台运行不调用它，已有正常服务不因未知文件系统而被停止。

默认只接受当前挂载命名空间内可写的 ext4、Btrfs、XFS 和 F2FS。
tmpfs、overlay、NFS、CIFS、FUSE 及其他未列明类型均拒绝跨进程恢复；mount options 和 super options 任一只读也拒绝。
明确关闭写屏障、日志恢复的模式（如 `nobarrier`、`barrier=0`、`fsync_mode=nobarrier`、`noload`、`norecovery`、`disable_roll_forward`、`nologreplay`）和 F2FS `checkpoint=disable[:…]` 不受支持。
不支持时返回固定 `FilesystemError.code`，不改挂载选项、不执行外部程序、不退回未经验证的事务。

检查以已打开目录的真实挂载为准：复用现有逐段 `O_DIRECTORY|O_NOFOLLOW` 的打开方法，从该 fd 的 `fdinfo.mnt_id` 精确定位 `mountinfo`，并核验挂载点包含目标路径。
不能仅选最长路径或按相同挂载点的行序决定覆盖层；bind/覆盖挂载可以具有相同路径和设备号。
mountinfo 的设备号字段仍严格解析，但不与 `fstat.st_dev` 强行相等：Btrfs 对子卷返回单独的匿名设备号。
目录的 dev/inode、fd 的 mount ID、重新打开的同路径目录，以及前后的完整有界 mountinfo 必须一致。
任何不一致、来源缺失、类型异常或 I/O 失败都拒绝；不会自动重试到偶然通过。

默认最多 8 个目录，mountinfo 最多 512 KiB／4096 条挂载，单份 fdinfo 最多 4 KiB，路径最多 4096 字节；`FilesystemLimits` 只允许有限的小范围调整。
按内核格式识别 ` - ` 分隔符及四种路径八进制转义，忽略不认识的 optional tags；重复 mount ID、缺失或重复 `mnt_id`、不完整记录均拒绝。
所有 fd 在成功和失败时关闭，普通文件来源使用 `O_NONBLOCK|O_NOFOLLOW` 并在读取前核验类型及上限，不会阻塞读取 FIFO。

prepare、标准服务 bootstrap 和 helper 的安装核验应重新调用此入口；本片只提供函数及离线回归，不修改这些既有调用点。
这是一段有界观察，不会锁住挂载拓扑，也不能中断任意内核文件系统调用。
正式事务仍依赖实际 writer/journal 锁、文件与目录 fsync、SQLite WAL FULL、原始配置 CAS 和独立进程硬期限；管理员绕过合作协议、更改挂载、设备谎报持久写入或不可靠存储不在额外保证内。

依据经 Context7 核对的 [Linux proc 格式](https://github.com/torvalds/linux/blob/master/Documentation/filesystems/proc.rst)、
[fdinfo 的 mount ID](https://man7.org/linux/man-pages/man5/proc_pid_fdinfo.5.html)、
[内核 mountinfo 输出与转义](https://github.com/torvalds/linux/blob/master/fs/proc_namespace.c)、
[Btrfs getattr 的设备号](https://github.com/torvalds/linux/blob/master/fs/btrfs/inode.c)、
[ext4 写屏障](https://docs.kernel.org/admin-guide/ext4.html)和
[F2FS fsync／checkpoint 选项](https://docs.kernel.org/filesystems/f2fs.html)。
定向验证经 `scripts/check_local.py` 保护入口完成：75 项回归通过（0.09 秒，峰值 53.1 MiB、swap 0），覆盖重叠挂载、四种路径转义、目录与 mount ID 变化、有限读取、错误选项、异常关闭 fd 和不支持平台。
两份 Python 文件的 Ruff 格式／检查及单源码严格 mypy 通过。
测试只使用临时目录和假 proc 内容，不创建真实挂载、不操作用户服务；本片不声明真实 ext4、Btrfs、XFS、F2FS 故障恢复验证。
